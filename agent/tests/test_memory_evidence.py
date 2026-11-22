"""Offline archive/reindex checks; fixture receipts are not real 3D artifacts."""
import json

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
import pytest

from text2splat.memory import MemoryStore
from text2splat.settings import AgentSettings
from text2splat.store import JobStore
from text2splat.workflow import Workflow
from test_memory import episode
from test_chat import chat_rig, send
from test_workflow import FakeModel, FakeTools, rig


def verified_job():
    job = episode()
    job.update(reconstructionId="c" * 32, sessionId="d" * 32)
    job["result"].update(job_id=job["reconstructionId"], session_id=job["sessionId"], ply_vertex_count=1)
    return job


def seed_legacy(memory, job):
    """A pre-fix archive row, without any current evidence-policy version."""
    item = dict(id="episode:" + job["id"], kind="episode", source="task_record", source_job_id=job["id"],
                description=job["prompt"], title=job["prompt"], mode="THREE_D", outcome="verified_3d",
                artifact_verified=True, review="", failure="", updated_at=job["updatedAt"])
    raw = json.dumps(item, ensure_ascii=False)
    with memory.connection() as db:
        db.execute("INSERT INTO memories VALUES (?,?,?,?,?)", (item["id"], "episode", raw,
                                                              "original-created-at", item["updated_at"]))
    return item, raw


@pytest.mark.parametrize("location,field,value", [
    ("result", "ply_vertex_count", None), ("result", "ply_vertex_count", True),
    ("result", "ply_vertex_count", 0), ("result", "ply_vertex_count", "1"),
    ("result", "job_id", None), ("result", "job_id", "e" * 32),
    ("result", "session_id", None), ("result", "session_id", "e" * 32),
    ("job", "reconstructionId", None), ("job", "sessionId", None),
    ("job", "mode", "CONCEPT"),
])
def test_ready_record_needs_matching_structural_receipt_before_verified_memory(tmp_path, location, field, value):
    memory, job = MemoryStore(tmp_path), verified_job()
    target = job["result"] if location == "result" else job
    if value is None:
        target.pop(field)
    else:
        target[field] = value
    assert memory.record_job(job) is False
    assert memory.recall("青瓷花瓶") == [] and memory.list() == []


def test_legacy_verified_claim_is_downgraded_on_read_without_editing_archive(tmp_path):
    memory = MemoryStore(tmp_path)
    original, raw = seed_legacy(memory, verified_job())
    reopened = MemoryStore(tmp_path)
    for item in (reopened.list()[0], reopened.recall("青瓷花瓶")[0]):
        assert item["artifact_verified"] is False and item["outcome"] == "unverified_3d"
        assert item["source_job_id"] == original["source_job_id"]
        assert item["description"] == original["description"]
    with reopened.connection() as db:
        assert db.execute("SELECT payload FROM memories").fetchone()[0] == raw


@pytest.mark.parametrize("has_evidence", [True, False])
def test_reindex_only_upgrades_legacy_memory_when_source_has_evidence(tmp_path, has_evidence):
    memory, job = MemoryStore(tmp_path), verified_job()
    seed_legacy(memory, job)
    if not has_evidence:
        job["result"].pop("ply_vertex_count")
    assert memory.record_job(job) is has_evidence
    assert memory.record_job(job) is False  # Current entries are not rewritten on every startup.
    item = MemoryStore(tmp_path).list()[0]
    assert item["artifact_verified"] is has_evidence
    assert item["outcome"] == ("verified_3d" if has_evidence else "unverified_3d")
    with memory.connection() as db:
        assert db.execute("SELECT created_at FROM memories").fetchone()[0] == "original-created-at"


def test_reindex_does_not_restore_forgotten_legacy_memory_or_change_preferences(tmp_path):
    memory, job = MemoryStore(tmp_path), verified_job()
    item, _ = seed_legacy(memory, job)
    preference = memory.set_preference("material", "哑光青瓷")
    assert memory.forget(item["id"]) == 1
    reopened = MemoryStore(tmp_path)
    assert reopened.record_job(job) is False
    assert reopened.list() == [preference] and reopened.recall("青瓷花瓶")[0]["value"] == "哑光青瓷"


@pytest.mark.asyncio
async def test_chat_receives_legacy_memory_as_unverified_without_creating_generation(chat_rig):
    chat, (workflow, store, tools, _) = chat_rig
    seed_legacy(workflow.memory, verified_job())
    turn = await send(chat, "讨论一下青瓷花瓶的设计，先不要生成")
    reference = next(m.content for m in chat.model.history[0] if m.content.startswith("历史参考数据"))
    item = json.loads(reference.split("\n", 1)[1])[0]
    assert item["artifact_verified"] is False and item["outcome"] == "unverified_3d"
    assert turn["status"] == "COMPLETED" and turn["jobId"] is None
    assert store.list() == [] and not tools.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("has_evidence", [True, False])
async def test_startup_reindexes_terminal_archive_without_replaying_generation(tmp_path, has_evidence):
    store = JobStore(tmp_path)
    try:
        job, _ = store.create("青瓷花瓶", "THREE_D", None, "archived")
        saved = verified_job()
        if not has_evidence:
            saved["result"].pop("ply_vertex_count")
        fields = {k: saved[k] for k in ("status", "spec", "referenceSetId", "reconstructionId", "sessionId", "result")}
        job = store.update(job["id"], **fields)
        async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "checkpoints.db")) as saver:
            model, tools = FakeModel(), FakeTools()
            workflow = Workflow(AgentSettings(data_dir=tmp_path), store, tools, saver, model=model)
            seed_legacy(workflow.memory, job)
            try:
                await workflow.start()
                await workflow.queue.join()
                item = workflow.memory.recall("青瓷花瓶")[0]
                assert item["artifact_verified"] is has_evidence
                assert item["outcome"] == ("verified_3d" if has_evidence else "unverified_3d")
                assert store.get(job["id"]) == job
                assert not model.history and not tools.calls
            finally:
                await workflow.close()
    finally:
        store.close()
