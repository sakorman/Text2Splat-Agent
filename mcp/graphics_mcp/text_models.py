"""Explicit Zhipu text profiles and volatile thinking/tool-call compatibility."""
import os

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI
from pydantic import PrivateAttr


DEFAULT_TEXT_MODEL = "glm-4.7-flash"
QUOTA_TEXT_MODEL = "glm-5.3-flash"


def text_model_from_env():
    model = os.getenv("ZHIPU_TEXT_MODEL", DEFAULT_TEXT_MODEL)
    if model not in {DEFAULT_TEXT_MODEL, QUOTA_TEXT_MODEL}:
        raise ValueError("ZHIPU_TEXT_MODEL 仅支持 glm-4.7-flash 或 glm-5.3-flash")
    return model


def text_request_options(model, max_tokens):
    if model == QUOTA_TEXT_MODEL:
        # Standard API, explicit thinking, bounded effort. No paid fallback.
        return {"max_tokens": max_tokens, "thinking": {"type": "enabled", "clear_thinking": True},
                "reasoning_effort": "low"}
    if model != DEFAULT_TEXT_MODEL:
        raise ValueError("不支持的文本模型")
    return {"max_tokens": max_tokens, "thinking": {"type": "disabled"}}


class ZhipuChatModel(ChatOpenAI):
    """Keep provider-only reasoning outside LangChain messages and checkpoints.

    langchain-openai 1.7.3 intentionally drops reasoning_content. Zhipu requires
    it back for thinking + tool exchanges, even with clear_thinking=True.
    These private fields live only for one request; the caller owns task scope.
    """
    _input_reasoning: dict = PrivateAttr(default_factory=dict)
    _output_reasoning: str | None = PrivateAttr(default=None)

    def set_reasoning_history(self, history):
        self._input_reasoning = history

    def take_reasoning(self):
        value, self._output_reasoning = self._output_reasoning, None
        return value

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        if self.model_name == QUOTA_TEXT_MODEL:
            messages = self._convert_input(input_).to_messages()
            for message, item in zip(messages, payload["messages"], strict=True):
                if isinstance(message, AIMessage) and message.tool_calls:
                    if message.id not in self._input_reasoning:
                        # Do not invent thinking or silently replay a restored task.
                        raise ValueError("工具轮次的临时上下文已丢失")
                    reasoning = self._input_reasoning[message.id]
                    # The standard API may omit thinking even when enabled.
                    # None records that absence; never fabricate a replacement.
                    if reasoning is not None:
                        item["reasoning_content"] = reasoning
        return payload

    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        if self.model_name == QUOTA_TEXT_MODEL:
            data = response if isinstance(response, dict) else response.model_dump()
            message = data["choices"][0]["message"]
            reasoning = message.get("reasoning_content")
            if reasoning is not None and not isinstance(reasoning, str):
                raise ValueError("模型思考字段格式无效")
            if message.get("tool_calls") and isinstance(reasoning, str):
                self._output_reasoning = reasoning
        return result
