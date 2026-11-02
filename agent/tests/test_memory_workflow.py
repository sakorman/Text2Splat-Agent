import asyncio
import json

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
import pytest

from text2splat.workflow import Workflow
from text2splat.settings import AgentSettings
from text2splat.store import JobStore
from test_workflow import FakeTools, FakeModel, action, complete, rig

pytestmark = pytest.mark.asyncio


async def test_completed_task_is_recalled_after_service_restart(tmp_path):
    settings, store = AgentSettings(data_dir=tmp_path, poll_interval=.01), JobStore(tmp_path)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "graph.db")) as saver:
        first = Workflow(settings, store, FakeTools(), saver, model=FakeModel())
        await first.start()
        old_job = await complete(first)
        await first.close()
    store.close()
    store = JobStore(tmp_path)
    try:
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "graph.db")) as saver:
            model = FakeModel()
            second = Workflow(settings, store, FakeTools(), saver, model=model)
            await second.start()
            try:
                new_job = await complete(second, key="after-restart")
                assert new_job["status"] == "READY"
                assert new_job["memorySources"] == ["episode:" + old_job["id"]]
                assert "verified_3d" in model.history[0][1].content
                assert new_job["agentRounds"] == 6
            finally:
                await second.close()
    finally:
        store.close()


async def test_explicit_preference_flows_into_new_tasks_but_does_not_replace_current_request(rig):
    workflow, store, tools, _ = rig
    job = workflow.submit("记住我的偏好：哑光青瓷。\n生成一个小鸟", "CONCEPT", None, "pref")
    await asyncio.wait_for(workflow.queue.join(), 3)
    assert store.get(job["id"])["status"] == "CONCEPT_READY"
    workflow.model = FakeModel()
    second = workflow.submit("这次用金属材质，生成一个花瓶", "THREE_D", None, "override")
    await asyncio.wait_for(workflow.queue.join(), 3)
    first_context = workflow.model.history[0]
    assert json.loads(first_context[0].content)["description"] == second["prompt"]
    assert "哑光青瓷" in first_context[1].content
    assert store.get(second["id"])["status"] == "READY"
    submitted = [a["prompt"] for n, a in tools.arguments if n == "creative_generate_reference"]
    assert submitted[-1] == second["prompt"]  # Memory never silently substitutes the actual request.


async def test_memory_cannot_promote_new_task_to_success(rig):
    workflow, _, tools, _ = rig
    old = await complete(workflow)
    workflow.model = FakeModel([action("finish"), action("stop")])
    new = await complete(workflow, key="false-success")
    assert new["memorySources"] == ["episode:" + old["id"]]
    assert new["status"] == "FAILED" and "result" not in new
    assert tools.calls["creative_generate_reference"] == 1


async def test_context_compacts_during_real_react_loop_without_resetting_budget(rig):
    workflow, _, tools, _ = rig
    workflow.model = FakeModel([action("inspect_task", identifier=str(i)) for i in range(10)])
    job = await complete(workflow)
    assert job["status"] == "FAILED" and job["agentRounds"] == 12
    assert job["contextStats"]["compressed"] is True
    assert job["contextStats"]["afterCharacters"] < job["contextStats"]["beforeCharacters"]
    assert tools.calls["creative_generate_reference"] == 1
    checkpoint = await workflow.graph.aget_state({"configurable": {"thread_id": job["id"]}})
    assert checkpoint.values["context_summary"]["current_task"]["remaining_decisions"] == 0
    assert len(checkpoint.values["messages"]) > len(workflow.model.history[-1])


async def test_memory_database_failure_does_not_block_generation(rig, tmp_path):
    workflow, _, _, _ = rig
    workflow.memory.path = tmp_path  # A directory cannot be opened as a SQLite file.
    job = await complete(workflow)
    assert job["status"] == "READY" and job["memorySources"] == []
    assert (await workflow.health())["memory"]["available"] is False


async def test_long_design_context_compresses_and_still_finishes_once(rig):
    workflow, _, _, _ = rig

    class DetailedTools(FakeTools):
        async def call(self, name, args):
            result = await super().call(name, args)
            if name == "creative_get_job":
                result["spec"] = {"title": "花瓶", "object_prompt": "细密卷草纹理" * 400}
            return result

    tools = DetailedTools()
    workflow.tools = tools
    job = await complete(workflow)
    assert job["status"] == "READY" and job["contextStats"]["compressed"] is True
    assert job["contextStats"]["afterCharacters"] <= 12000 < job["contextStats"]["beforeCharacters"]
    assert job["agentRounds"] == 6
    assert tools.calls["creative_generate_reference"] == tools.calls["webscan_start_reconstruction"] == 1
