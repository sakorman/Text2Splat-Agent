"""Queue isolation with real job/checkpoint stores and explicit offline tool fixtures."""
import asyncio
import json

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
import pytest
import pytest_asyncio

from text2splat.settings import AgentSettings
from text2splat.store import JobStore, TERMINAL
from text2splat.workflow import Workflow
from test_workflow import FakeModel, FakeTools

pytestmark = pytest.mark.asyncio
CHILD = "e" * 32
PRIVATE_ERROR = "test-only-private-storage-path"


@pytest_asyncio.fixture
async def queue_rig(tmp_path):
    store, tools = JobStore(tmp_path), FakeTools()
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
        workflow = Workflow(AgentSettings(data_dir=tmp_path, poll_interval=.01), store, tools,
                            saver, model=FakeModel())
        await workflow.start()
        try:
            yield workflow, store, tools, saver
        finally:
            # Also dispose a dead worker when demonstrating the pre-fix regression.
            workflow.worker.cancel()
            await asyncio.gather(workflow.worker, return_exceptions=True)
    store.close()


@pytest.mark.parametrize("action", ["run", "recover", "cancel"])
async def test_queue_survives_one_task_write_failure(queue_rig, monkeypatch, caplog, action):
    workflow, store, tools, _ = queue_rig
    first, _ = store.create("一个青瓷花瓶", "THREE_D", None, "first")
    jid = first["id"]
    if action != "run":
        request = jid + ":reference"
        store.update(jid, referenceRequest=request, referenceJobId=CHILD,
                     cancelRequested=action == "cancel", status="CANCELLING" if action == "cancel" else "REFERENCE")
        tools.requests[request] = {"id": CHILD}
    workflow.queue.put_nowait((action, jid))
    second = workflow.submit("一个木雕小鸟", "CONCEPT", None, "second")
    original_save = store._save
    failures = 0

    def fail_once(job):
        nonlocal failures
        boundary = "memorySources" in job if action == "run" else job["status"] in TERMINAL
        if job["id"] == jid and boundary and not failures:
            failures += 1
            raise OSError(PRIVATE_ERROR)
        return original_save(job)

    monkeypatch.setattr(store, "_save", fail_once)
    await asyncio.wait_for(workflow.queue.join(), 3)

    assert failures == 1 and not workflow.worker.done() and workflow.active is None
    assert store.get(second["id"])["status"] == "CONCEPT_READY"
    assert store.get(jid)["status"] == ("FAILED" if action == "run" else "CANCELLING")
    assert PRIVATE_ERROR not in caplog.text + json.dumps(store.get(jid))
    assert tools.calls["creative_generate_reference"] == 1
    assert tools.calls["webscan_start_reconstruction"] == tools.calls["creative_generate_multiview"] == 0
    if action != "run":
        # A lost local completion write must not automatically repeat cancellation.
        assert tools.calls["creative_get_request"] == tools.calls["creative_cancel_job"] == 1
        workflow.cancel(jid)
        await asyncio.wait_for(workflow.queue.join(), 3)
        assert store.get(jid)["status"] == "CANCELLED"
        assert [args["job_id"] for name, args in tools.arguments if name == "creative_cancel_job"] == [CHILD, CHILD]
    else:
        assert tools.calls["creative_get_request"] == tools.calls["creative_cancel_job"] == 0


@pytest.mark.parametrize("fault", ["write", "read"])
@pytest.mark.parametrize("recovery", ["cancel", "restart"])
async def test_unrecorded_submit_receipt_survives_storage_failure_without_replay(
        queue_rig, monkeypatch, caplog, fault, recovery):
    workflow, store, tools, saver = queue_rig
    first = workflow.submit("一个青瓷花瓶", "THREE_D", None, "lost-receipt")
    second = workflow.submit("一个木雕小鸟", "CONCEPT", None, "next")
    jid = first["id"]
    original_call, original_get, original_save = tools.call, store.get, store._save
    failing = False
    saved_before_failure = None

    async def submit_then_lose_storage(name, args):
        nonlocal failing, saved_before_failure
        result = await original_call(name, args)
        if name == "creative_generate_reference" and args["request_id"] == jid + ":reference":
            result["id"] = CHILD  # The fixture target accepted a distinct child job.
            saved_before_failure = original_get(jid)
            failing = True
            raise OSError(PRIVATE_ERROR)  # Target accepted; local receipt remains unknown.
        return result

    def unavailable_get(target):
        if failing and target == jid and fault == "read":
            raise OSError(PRIVATE_ERROR)
        return original_get(target)

    def unavailable_save(job):
        if failing and job["id"] == jid and fault == "write":
            raise OSError(PRIVATE_ERROR)
        return original_save(job)

    monkeypatch.setattr(tools, "call", submit_then_lose_storage)
    monkeypatch.setattr(store, "get", unavailable_get)
    monkeypatch.setattr(store, "_save", unavailable_save)
    await asyncio.wait_for(workflow.queue.join(), 3)

    assert not workflow.worker.done() and workflow.active is None
    assert original_get(second["id"])["status"] == "CONCEPT_READY"
    assert saved_before_failure is not None and original_get(jid) == saved_before_failure
    assert original_get(jid)["referenceRequest"] == jid + ":reference"
    assert "referenceJobId" not in original_get(jid) and "result" not in original_get(jid)
    assert PRIVATE_ERROR not in caplog.text + json.dumps(original_get(jid))
    assert tools.calls["creative_generate_reference"] == 2  # Exactly one per submitted task.
    assert tools.calls["creative_cancel_job"] == tools.calls["creative_generate_multiview"] == 0
    assert tools.calls["webscan_start_reconstruction"] == 0

    # Storage returns: same client key still cannot replay generation; explicit
    # cancellation or a fresh workflow uses the saved request to pin the child.
    failing = False
    assert workflow.submit(first["prompt"], "THREE_D", None, "lost-receipt")["id"] == jid
    assert workflow.queue.empty()
    if recovery == "cancel":
        workflow.cancel(jid)
        await asyncio.wait_for(workflow.queue.join(), 3)
    else:
        await workflow.close()
        restarted = Workflow(workflow.settings, store, tools, saver, model=FakeModel())
        try:
            await restarted.start()
            await asyncio.wait_for(restarted.queue.join(), 3)
            assert not restarted.model.history  # Recovery never invokes the graph/model.
        finally:
            await restarted.close()
    assert store.get(jid)["status"] == ("CANCELLED" if recovery == "cancel" else "FAILED")
    assert store.get(jid)["referenceJobId"] == CHILD
    assert tools.calls["creative_generate_reference"] == 2
    assert [args["job_id"] for name, args in tools.arguments if name == "creative_cancel_job"] == [CHILD]


async def test_shutdown_propagates_cancellation_and_leaves_next_job_unstarted(queue_rig):
    workflow, store, tools, _ = queue_rig
    tools.block = True
    first = workflow.submit("一个青瓷花瓶", "THREE_D", None, "interrupted")
    second = workflow.submit("一个木雕小鸟", "CONCEPT", None, "waiting")
    await asyncio.wait_for(tools.entered.wait(), 3)
    saved = store.get(first["id"])
    assert saved.get("referenceRequest") and saved.get("referenceJobId")

    await asyncio.wait_for(workflow.close(), 3)

    assert workflow.worker.cancelled() and workflow.active is None
    assert store.get(first["id"]) == saved
    assert store.get(second["id"])["status"] == "QUEUED" and workflow.queue.qsize() == 1
    assert tools.calls["creative_generate_reference"] == 1
    assert tools.calls["creative_cancel_job"] == tools.calls["creative_generate_multiview"] == 0
    assert len(workflow.model.history) == 1
