from copy import deepcopy
import json

from langchain_core.messages import AIMessage, HumanMessage
import pytest

from text2splat.chat import chat_context
from text2splat.context import size
from text2splat.tools import ToolFailure


def turn(index, message="讨论物件", assistant="方案二：青瓷小鸟，底座刻松针纹。", status="COMPLETED"):
    return dict(id=str(index), status=status, message=message, assistant=assistant)


def excerpts(messages):
    for message in messages:
        if "较早对话的摘录" in str(message.content):
            return json.loads(message.content.split("\n", 1)[1])["recent_earlier_turns"]
    return []


def test_complete_conversation_is_kept_when_it_fits_even_after_six_turns():
    previous = [turn(i, message=f"讨论第 {i} 个细节") for i in range(9)]
    original = deepcopy(previous)
    current = turn("current", message="继续讨论方案二，先不要生成")
    messages, stats = chat_context(current, previous, [], None)
    assert messages[1:-1] == [m for item in previous for m in (
        HumanMessage(item["message"]), AIMessage(item["assistant"]))]
    assert messages[-1].content == current["message"]
    assert stats["droppedTurns"] == 0 and not stats["compressed"]
    assert size(messages) == stats["beforeCharacters"] == stats["afterCharacters"]
    assert previous == original and not excerpts(messages)


def test_large_memory_does_not_displace_complete_chat_history():
    previous = [turn(i) for i in range(9)]
    messages, stats = chat_context(turn("current"), previous,
                                   [{"id": "pref:large", "value": "历史偏好" * 4000}], None)
    assert stats["droppedTurns"] == stats["memoryItems"] == 0
    assert messages[1].content == previous[0]["message"]
    assert len([m for m in messages if isinstance(m, AIMessage)]) == len(previous)
    assert stats["compressed"] and size(messages) <= 12000


def test_old_turn_excerpts_preserve_both_sides_and_mark_truncation():
    previous = [turn("old", "旧设计问题" * 100, "底座刻松针纹。" + "长讨论" * 1200),
                turn("recent", "改用哑光釉", "保留松针纹并使用哑光釉。")]
    messages, stats = chat_context(turn("current", "继续讨论，不要生成"), previous, [], None, budget=1800)
    records = excerpts(messages)
    assert records == [{"turn_id": "old", "status": "COMPLETED",
                        "request": previous[0]["message"][:160],
                        "assistant": previous[0]["assistant"][:240],
                        "request_truncated": True, "assistant_truncated": True}]
    assert [m.content for m in messages[-3:-1]] == [previous[-1]["message"], previous[-1]["assistant"]]
    assert stats["droppedTurns"] == stats["excerptTurns"] == 1
    assert stats["excerptSourceIds"] == ["old"] and size(messages) <= 1800
    assert json.loads(messages[0].content.split("\n", 1)[1])["history_incomplete"] is True


def test_excerpts_cannot_crowd_out_recent_complete_turns_or_current_job():
    previous = [turn(i, "旧问题" * 400, "旧回答" * 1500) for i in range(12)]
    previous.append(turn("latest", "保留此轮用户要求" * 25, "保留此轮完整方案" * 40))
    job = dict(id="a" * 32, status="MULTIVIEW", message="实验性候选视图", prompt="青瓷小鸟")
    current = turn("current", "这次先聊聊")
    messages, stats = chat_context(current, previous, [], job, budget=2300)
    assert [m.content for m in messages[-3:]] == [previous[-1]["message"], previous[-1]["assistant"], current["message"]]
    assert json.loads(messages[0].content.split("\n", 1)[1])["current_generation"] == job
    assert size(messages) <= 2300 and stats["droppedTurns"] == 12
    assert stats["excerptTurns"] < 8
    assert stats["excerptSourceIds"] == [r["turn_id"] for r in excerpts(messages)]


def test_failed_turn_excerpt_does_not_invent_a_completed_answer():
    previous = [turn("failed", "问题" * 800, "", status="FAILED")]
    messages, stats = chat_context(turn("current"), previous, [], None, budget=1200)
    record, = excerpts(messages)
    assert record["status"] == "FAILED" and record["assistant"] == ""
    assert record["request_truncated"] and not record["assistant_truncated"]
    assert stats["droppedTurns"] == 1


def test_current_message_is_never_truncated_to_fit():
    with pytest.raises(ToolFailure, match="当前消息和必要状态"):
        chat_context(turn("current", "当前要求" * 500), [], [], None, budget=1000)
