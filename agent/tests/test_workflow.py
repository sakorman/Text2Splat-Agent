import asyncio
from collections import Counter
import json
import time

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
import pytest
import pytest_asyncio

from text2splat.settings import AgentSettings
from text2splat.store import JobStore, TERMINAL
from text2splat.tools import TemporaryToolFailure, ToolFailure
from text2splat.workflow import Workflow, State

pytestmark = pytest.mark.asyncio
REF, VIEWS, SESSION, TRAIN = (letter * 32 for letter in "abcd")


def action(name, args=None, identifier="call-test"):
    return AIMessage(content="", tool_calls=[{"id": identifier, "name": name, "args": args or {}}])


class FakeModel:
    """Explicit model fixture; production never falls back to this decision policy."""
    def __init__(self, replies=()):
        self.replies = list(replies)
        self.history = []

    async def ainvoke(self, messages):
        self.history.append(list(messages))
        if self.replies:
            return self.replies.pop(0)
        if isinstance(messages[-1], ToolMessage):
            observation = json.loads(messages[-1].content)["observation"]
            names = observation["allowed_actions"]
            name = next((n for n in names if n not in {"inspect_task", "stop"}), "stop")
        else:
            name = "generate_reference"
        return action(name, identifier=f"call-{len(self.history)}")


class FakeTools:
    """Contract fixtures only; these outputs are not generated artwork."""
    def __init__(self):
        self.calls = Counter()
        self.requests = {}
        self.arguments = []
        self.fail_tool = None
        self.failures = 0
        self.permanent = False
        self.lose_response = False
        self.reject_views = False
        self.fail_training = False
        self.missing_ply = False
        self.block = False
        self.cancel_ack = "valid"
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def call(self, name, args):
        self.calls[name] += 1
        self.arguments.append((name, args))
        if name == self.fail_tool and self.failures:
            self.failures -= 1
            self.entered.set()
            raise (ToolFailure if self.permanent else TemporaryToolFailure)("连接中断")
        if name == "creative_health":
            return {"configured": True}
        if name == "webscan_health":
            return {"available": True, "brush_available": True}
        if name in {"creative_generate_reference", "creative_generate_multiview", "webscan_start_reconstruction"}:
            value = {"job_id": TRAIN} if name.startswith("webscan") else {
                "id": REF if name.endswith("reference") else VIEWS}
            self.requests[args["request_id"]] = value
            if self.lose_response:
                raise TemporaryToolFailure("提交结果未知")
            return value
        if name.endswith("_get_request"):
            return {"job": self.requests.get(args["request_id"])}
        if name == "creative_get_job":
            if self.block:
                self.entered.set()
                await self.release.wait()
            multi = args["job_id"] == VIEWS
            return {"status": "needs_review" if multi and self.reject_views else "succeeded",
                    "stage": "review" if multi else "reference", "asset_set_id": VIEWS if multi else REF,
                    "assets": [{"url": "/api/graphics/assets/" + REF + "/00.png"}], "spec": {"title": "测试花瓶"}}
        if name == "webscan_import_generated_views":
            return {"session_id": SESSION}
        if name == "webscan_get_status":
            return {"status": "failed" if self.fail_training else "succeeded", "stage": "sfm", "message": "测试状态"}
        if name == "webscan_get_result":
            return {"viewer_url": "http://127.0.0.1:8787/?p=" + SESSION + "&j=" + TRAIN,
                    "files": [] if self.missing_ply else [{"name": "gaussians.ply", "bytes": 100, "url": "/test.ply"}]}
        if name in {"creative_cancel_job", "webscan_cancel_reconstruction"}:
            if self.cancel_ack == "wrong_id":
                return {"id": "f" * 32, "status": "cancelled"}
            if self.cancel_ack == "running":
                return {"id": args["job_id"], "status": "running"}
            return {"id": args["job_id"], "status": "cancelled"}
        raise AssertionError(name)


@pytest_asyncio.fixture
async def rig(tmp_path):
    store = JobStore(tmp_path)
    tools = FakeTools()
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
        workflow = Workflow(AgentSettings(data_dir=tmp_path, poll_interval=.01), store, tools, saver, model=FakeModel())
        await workflow.start()
        yield workflow, store, tools, saver
        await workflow.close()
    store.close()


async def complete(workflow, mode="THREE_D", key="test"):
    job = workflow.submit("一个青瓷花瓶", mode, None, key)
    await asyncio.wait_for(workflow.queue.join(), 3)
    return workflow.store.get(job["id"])


async def test_graph_completes_and_sqlite_checkpoint_survives_reopen(rig, tmp_path):
    workflow, store, tools, saver = rig
    job = await complete(workflow)
    assert job["status"] == "READY" and job["engine"] == "langgraph"
    assert job["referenceAssets"] and job["viewAssets"] and job["result"]["files"]
    assert tools.calls["creative_generate_reference"] == tools.calls["creative_generate_multiview"] == tools.calls["webscan_start_reconstruction"] == 1
    config = {"configurable": {"thread_id": job["id"]}}
    snapshot = await workflow.graph.aget_state(config)
    assert snapshot.next == () and snapshot.values["reconstruction_id"] == TRAIN
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as reopened:
        checkpoint = await reopened.aget_tuple(config)
        assert checkpoint.checkpoint["channel_values"]["views_id"] == VIEWS


async def test_concept_does_not_enter_3d_nodes(rig):
    workflow, _, tools, _ = rig
    assert (await complete(workflow, "CONCEPT"))["status"] == "CONCEPT_READY"
    assert not tools.calls["creative_generate_multiview"] and not tools.calls["webscan_health"]


async def test_idempotency_conflict_and_terminal_immutability(rig):
    workflow, store, tools, _ = rig
    job = await complete(workflow)
    assert workflow.submit(job["prompt"], "THREE_D", None, "test")["id"] == job["id"]
    with pytest.raises(ValueError, match="不同内容"):
        workflow.submit("一个木雕小鸟", "THREE_D", None, "test")
    assert store.update(job["id"], status="FAILED") == job
    assert workflow.cancel(job["id"]) == job
    assert tools.calls["creative_generate_reference"] == 1


@pytest.mark.parametrize("flag,status,absent", [
    ("reject_views", "NEEDS_REVIEW", "webscan_start_reconstruction"),
    ("fail_training", "NEEDS_REVIEW", "webscan_get_result"),
    ("missing_ply", "FAILED", None),
])
async def test_quality_and_artifact_gates(rig, flag, status, absent):
    workflow, _, tools, _ = rig
    setattr(tools, flag, True)
    job = await complete(workflow)
    assert job["status"] == status and job["viewAssets"]
    if absent:
        assert tools.calls[absent] == 0
    assert "result" not in job


async def test_cancel_wins_over_late_success(rig):
    workflow, store, tools, _ = rig
    tools.block = True
    job = workflow.submit("青瓷花瓶", "THREE_D", None, "cancel")
    await asyncio.wait_for(tools.entered.wait(), 2)
    assert workflow.cancel(job["id"])["status"] == "CANCELLING"
    tools.release.set()
    await asyncio.wait_for(workflow.queue.join(), 2)
    assert store.get(job["id"])["status"] == "CANCELLED"
    assert tools.calls["creative_cancel_job"] == 1 and not tools.calls["creative_generate_multiview"]


async def test_lost_mutation_response_is_looked_up_and_cancelled_not_replayed(rig):
    workflow, _, tools, _ = rig
    tools.lose_response = True
    assert (await complete(workflow))["status"] == "FAILED"
    assert tools.calls["creative_generate_reference"] == tools.calls["creative_cancel_job"] == 1
    assert tools.calls["creative_get_request"] == 1


@pytest.mark.parametrize("ack", ["wrong_id", "running"])
async def test_invalid_cancel_ack_keeps_job_unconfirmed(rig, ack):
    workflow, store, tools, _ = rig
    tools.cancel_ack = ack
    tools.lose_response = True
    assert (await complete(workflow))["status"] == "CANCELLING"
    assert store.get(workflow.store.by_request("test")["id"])["status"] == "CANCELLING"


async def test_missing_child_is_not_treated_as_confirmed_cleanup(rig):
    workflow, store, tools, _ = rig
    job, _ = store.create("一个花瓶", "THREE_D", None, "missing-child")
    store.update(job["id"], referenceRequest=job["id"] + ":reference")
    assert await workflow.cleanup(job["id"]) is False
    assert tools.calls["creative_cancel_job"] == 0


@pytest.mark.parametrize("read", ["creative_get_job", "webscan_get_status", "webscan_get_result"])
async def test_only_transient_reads_recover(rig, read):
    workflow, _, tools, _ = rig
    tools.fail_tool, tools.failures = read, 2
    job = await complete(workflow)
    assert job["status"] == "READY" and job["connectionRetry"] == 0
    assert tools.calls[read] == (4 if read == "creative_get_job" else 3)
    assert tools.calls["creative_generate_reference"] == tools.calls["webscan_start_reconstruction"] == 1


@pytest.mark.parametrize("permanent,expected", [(False, 3), (True, 1)])
async def test_read_retry_limit_and_permanent_failure(rig, permanent, expected):
    workflow, _, tools, _ = rig
    tools.fail_tool, tools.failures, tools.permanent = "creative_get_job", 10, permanent
    assert (await complete(workflow))["status"] == "FAILED"
    assert tools.calls["creative_get_job"] == expected


@pytest.mark.parametrize("cancel", [True, False])
async def test_backoff_observes_cancellation_and_original_deadline(rig, cancel):
    workflow, store, tools, _ = rig
    from dataclasses import replace
    workflow.settings = replace(workflow.settings, poll_interval=1, job_timeout=5 if cancel else .1)
    tools.fail_tool, tools.failures = "creative_get_job", 10
    job = workflow.submit("一个青瓷花瓶", "THREE_D", None, "backoff")
    await asyncio.wait_for(tools.entered.wait(), 2)
    if cancel:
        workflow.cancel(job["id"])
    await asyncio.wait_for(workflow.queue.join(), 1)
    assert store.get(job["id"])["status"] == ("CANCELLED" if cancel else "FAILED")
    assert tools.calls["creative_get_job"] == 1


async def test_existing_intent_never_replays_mutation_even_if_lookup_is_empty(rig):
    workflow, store, tools, _ = rig
    job, _ = store.create("一个花瓶", "THREE_D", None, "intent")
    store.update(job["id"], referenceRequest=job["id"] + ":reference")
    state = State(job_id=job["id"], deadline=time.time() + 5)
    with pytest.raises(ToolFailure, match="未重复提交"):
        await workflow.submit_once(state, "referenceRequest", "creative_generate_reference", {}, "creative_get_request", "id")
    assert not tools.calls["creative_generate_reference"]
    tools.requests[job["id"] + ":reference"] = {"id": REF}
    assert await workflow.submit_once(state, "referenceRequest", "creative_generate_reference", {}, "creative_get_request", "id") == REF
    assert not tools.calls["creative_generate_reference"]


async def test_restart_reads_legacy_records_without_reentering_graph(tmp_path):
    directory = tmp_path / "workflows"
    directory.mkdir()
    jid = "f" * 32
    legacy = dict(id=jid, prompt="旧任务", mode="THREE_D", status="REFERENCE", createdAt="2026-10-31T00:00:00Z",
                  updatedAt="2026-10-31T00:00:00Z", requestKey="legacy", referenceRequest=jid + ":reference")
    (directory / (jid + ".json")).write_text(json.dumps(legacy))
    store, tools = JobStore(tmp_path), FakeTools()
    tools.requests[jid + ":reference"] = {"id": REF}
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
        workflow = Workflow(AgentSettings(data_dir=tmp_path), store, tools, saver)
        await workflow.start()
        await asyncio.wait_for(workflow.queue.join(), 2)
        assert store.by_request("legacy")["status"] == "FAILED"
        assert tools.calls == {"creative_get_request": 1, "creative_cancel_job": 1}
        assert await saver.aget_tuple({"configurable": {"thread_id": jid}}) is None
        await workflow.close()
    store.close()


async def test_unconfirmed_cleanup_can_be_cancelled_again(rig):
    workflow, store, tools, _ = rig
    tools.lose_response, tools.fail_tool, tools.failures = True, "creative_get_request", 1
    job = await complete(workflow)
    assert job["status"] == "CANCELLING"
    workflow.cancel(job["id"])
    await asyncio.wait_for(workflow.queue.join(), 2)
    assert store.get(job["id"])["status"] == "CANCELLED"
    assert tools.calls["creative_generate_reference"] == 1


async def test_store_exclusive_and_queue_limit(rig, tmp_path):
    workflow, store, _, _ = rig
    with pytest.raises(RuntimeError, match="已有 Agent"):
        JobStore(tmp_path)
    for index in range(16):
        store.create("排队花瓶", "THREE_D", None, str(index))
    with pytest.raises(ValueError, match="队列已满"):
        store.create("另一个花瓶", "THREE_D", None, "full")
