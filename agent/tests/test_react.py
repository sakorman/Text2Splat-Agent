import asyncio
import json
from dataclasses import replace

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
import pytest

from text2splat.react import ReActModel
from text2splat.settings import AgentSettings
from text2splat.tools import ToolFailure
from test_workflow import FakeModel, action, complete, rig

pytestmark = pytest.mark.asyncio


async def test_model_chooses_inspection_then_design_and_observes_results(rig):
    workflow, store, tools, _ = rig
    workflow.model = FakeModel([action("inspect_task"), action("generate_reference", {"design_hint": "柔和均匀光照"})])
    job = await complete(workflow)
    assert job["status"] == "READY" and job["agentLoop"] == "react" and job["agentRounds"] == 7
    assert len(workflow.model.history) == 7
    assert json.loads(workflow.model.history[1][-1].content)["observation"]["reference_ready"] is False
    assert json.loads(workflow.model.history[2][-1].content)["observation"]["reference_ready"] is True
    assert json.loads(workflow.model.history[-1][-1].content)["observation"]["artifact_verified"] is True
    assert job["agentActions"] == ["generate_reference", "generate_multiview", "import_views", "reconstruct", "validate_result"]
    submitted = next(args["prompt"] for name, args in tools.arguments if name == "creative_generate_reference")
    assert submitted.startswith(job["prompt"]) and "柔和均匀光照" in submitted
    snapshot = await workflow.graph.aget_state({"configurable": {"thread_id": job["id"]}})
    assert len([m for m in snapshot.values["messages"] if isinstance(m, ToolMessage)]) == 7
    assert snapshot.next == ()


async def test_model_can_stop_before_generation(rig):
    workflow, _, tools, _ = rig
    workflow.model = FakeModel([action("stop")])
    job = await complete(workflow)
    assert job["status"] == "FAILED"
    assert not tools.calls["creative_generate_reference"]
    assert job["agentRounds"] == 1


@pytest.mark.parametrize("bad", [action("finish"), action("reconstruct"), action("run_shell", {"command": "anything"}),
                                      action("generate_reference", {"prompt": "替换原目标", "request_id": "other"})])
async def test_rejected_actions_return_observation_then_model_can_correct(rig, bad):
    workflow, _, tools, _ = rig
    workflow.model = FakeModel([bad])
    job = await complete(workflow)
    assert job["status"] == "READY"
    observed = json.loads(workflow.model.history[1][-1].content)
    assert observed["ok"] is False and observed["observation"]["reference_ready"] is False
    assert tools.calls["creative_generate_reference"] == tools.calls["webscan_start_reconstruction"] == 1


async def test_model_cannot_claim_success_with_text(rig):
    workflow, _, tools, _ = rig
    workflow.model = FakeModel([AIMessage(content="3D 已生成，所有检查都通过")])
    job = await complete(workflow)
    assert job["status"] == "FAILED" and "result" not in job
    assert not tools.calls["creative_generate_reference"]


async def test_repeated_generation_is_refused_without_second_mcp_call(rig):
    workflow, _, tools, _ = rig
    workflow.model = FakeModel([action("generate_reference"), action("generate_reference")])
    job = await complete(workflow)
    assert job["status"] == "READY"
    assert json.loads(workflow.model.history[2][-1].content)["ok"] is False
    assert tools.calls["creative_generate_reference"] == 1


async def test_parallel_call_batch_executes_nothing(rig):
    workflow, _, tools, _ = rig
    workflow.model = FakeModel([
        AIMessage(content="", tool_calls=[{"id": "a", "name": "generate_reference", "args": {}},
                                           {"id": "b", "name": "generate_multiview", "args": {}}]),
        action("stop"),
    ])
    job = await complete(workflow)
    assert job["status"] == "FAILED"
    assert not tools.calls["creative_generate_reference"]
    assert [m.tool_call_id for m in workflow.model.history[1][-2:]] == ["a", "b"]


async def test_failed_quality_is_observed_but_model_cannot_bypass_gate(rig):
    workflow, _, tools, _ = rig
    tools.reject_views = True
    workflow.model = FakeModel([action("generate_reference"), action("generate_multiview"),
                                action("reconstruct"), action("generate_multiview"), action("stop")])
    job = await complete(workflow)
    assert job["status"] == "NEEDS_REVIEW"
    seen = json.loads(workflow.model.history[2][-1].content)
    assert seen["ok"] is False and seen["observation"]["failure"]["status"] == "NEEDS_REVIEW"
    assert tools.calls["creative_generate_multiview"] == 1 and not tools.calls["webscan_import_generated_views"]


async def test_inspection_loop_has_hard_model_budget(rig):
    workflow, _, tools, _ = rig
    workflow.model = FakeModel([action("inspect_task", identifier=str(i)) for i in range(20)])
    job = await complete(workflow)
    assert job["status"] == "FAILED" and "12 轮" in job["message"]
    assert job["agentRounds"] == len(workflow.model.history) == 12
    assert not tools.calls["creative_generate_reference"]


async def test_model_cannot_import_another_jobs_images(rig):
    workflow, _, tools, _ = rig
    workflow.model = FakeModel([action("generate_reference"), action("generate_multiview"),
                                action("import_views", {"asset_set_id": "f" * 32}), action("stop")])
    job = await complete(workflow)
    assert job["status"] == "FAILED" and not tools.calls["webscan_import_generated_views"]
    assert json.loads(workflow.model.history[-1][-1].content)["ok"] is False


async def test_concept_mode_does_not_allow_3d_tools(rig):
    workflow, _, tools, _ = rig
    workflow.model = FakeModel([action("generate_reference"), action("generate_multiview"), action("finish")])
    job = await complete(workflow, "CONCEPT")
    assert job["status"] == "CONCEPT_READY"
    assert not tools.calls["creative_generate_multiview"] and not tools.calls["webscan_start_reconstruction"]


async def test_two_jobs_do_not_share_model_context_or_artifacts(rig):
    workflow, _, _, _ = rig
    first = await complete(workflow, key="first")
    second = await complete(workflow, key="second")
    assert first["id"] != second["id"] and first["status"] == second["status"] == "READY"
    assert len(workflow.model.history[0]) == len(workflow.model.history[6]) == 1
    for job in (first, second):
        state = await workflow.graph.aget_state({"configurable": {"thread_id": job["id"]}})
        assert state.values["job_id"] == job["id"] and state.values["rounds"] == 6


@pytest.mark.parametrize("cancel", [True, False])
async def test_waiting_for_decision_can_cancel_or_timeout_without_executing_late_tools(rig, cancel):
    workflow, store, tools, _ = rig
    entered, stopped = asyncio.Event(), asyncio.Event()

    class SlowModel:
        async def ainvoke(self, messages):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    workflow.model = SlowModel()
    workflow.settings = replace(workflow.settings, model_timeout=5 if cancel else .15)
    job = workflow.submit("一个青瓷花瓶", "THREE_D", None, "waiting")
    await asyncio.wait_for(entered.wait(), 1)
    if cancel:
        workflow.cancel(job["id"])
    await asyncio.wait_for(workflow.queue.join(), 1)
    assert stopped.is_set()
    assert store.get(job["id"])["status"] == ("CANCELLED" if cancel else "FAILED")
    assert not tools.calls["creative_generate_reference"]


def model_response(name="generate_reference", arguments="{}", finish="tool_calls"):
    return {"id": "fixture", "model": "glm-4.7-flash", "choices": [{"index": 0, "finish_reason": finish,
            "message": {"role": "assistant", "content": "discarded explanation", "reasoning_content": "private reasoning",
                        "tool_calls": [{"id": "native-call", "type": "function",
                                        "function": {"name": name, "arguments": arguments}}]}}]}


async def test_native_langchain_function_calling_round_trip_controls_actual_graph(rig):
    workflow, _, tools, _ = rig
    payloads = []

    def respond(request):
        body = json.loads(request.content)
        payloads.append(body)
        assert request.url == "https://open.bigmodel.cn/api/paas/v4/chat/completions"
        assert body["model"] == "glm-4.7-flash" and body["tool_choice"] == "auto"
        assert body["max_tokens"] == 1024 and body["thinking"] == {"type": "disabled"}
        assert not {"response_format", "parallel_tool_calls", "max_completion_tokens"}.intersection(body)
        assert len(body["tools"]) == 8
        assert body["messages"][0]["role"] == "system"
        if body["messages"][-1]["role"] == "tool":
            last = body["messages"][-1]
            assert last["tool_call_id"] == "native-call"
            allowed = json.loads(last["content"])["observation"]["allowed_actions"]
            name = next(n for n in allowed if n not in {"inspect_task", "stop"})
        else:
            name = "generate_reference"
        return httpx.Response(200, json=model_response(name))

    workflow.model = ReActModel(replace(workflow.settings, zhipu_key="test-secret"), httpx.MockTransport(respond))
    job = await complete(workflow)
    assert job["status"] == "READY" and len(payloads) == 6
    assert tools.calls["webscan_start_reconstruction"] == 1
    snapshot = await workflow.graph.aget_state({"configurable": {"thread_id": job["id"]}})
    assert all(m.content == "" for m in snapshot.values["messages"] if isinstance(m, AIMessage))
    assert "test-secret" not in str(snapshot.values) and "private reasoning" not in str(snapshot.values)


@pytest.mark.parametrize("failure", [429, 503, "timeout", "malformed", "truncated"])
async def test_decision_provider_fails_once_and_sanitizes(failure):
    calls = []

    def respond(request):
        calls.append(request)
        if failure == "timeout":
            raise httpx.ReadTimeout("secret-details", request=request)
        if isinstance(failure, int):
            return httpx.Response(failure, json={"error": {"message": "secret-details"}})
        return httpx.Response(200, json=model_response(arguments="not-json" if failure == "malformed" else "{}",
                                                       finish="length" if failure == "truncated" else "tool_calls"))

    model = ReActModel(AgentSettings(zhipu_key="test-key"), httpx.MockTransport(respond))
    with pytest.raises(ToolFailure) as caught:
        await model.ainvoke([HumanMessage("花瓶")])
    assert len(calls) == 1 and "secret-details" not in str(caught.value)
