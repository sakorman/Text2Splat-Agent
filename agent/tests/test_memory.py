import json

import pytest

from text2splat.memory import MemoryStore


def episode(jid="a" * 32, prompt="青瓷花瓶", status="READY"):
    return dict(id=jid, prompt=prompt, mode="THREE_D", status=status, referenceSetId="b" * 32,
                spec={"title": prompt}, updatedAt="2026-11-02T00:00:00Z",
                result={"viewer_url": "http://127.0.0.1/private", "files": [
                    {"name": "gaussians.ply", "bytes": 100, "url": "http://127.0.0.1/private.ply"}]})


def test_memory_persists_with_sources_and_relevant_chinese_recall(tmp_path):
    store = MemoryStore(tmp_path)
    store.set_preference("material", "我喜欢哑光青瓷")
    store.record_job(episode())
    store.record_job(episode("c" * 32, "火箭金属雕塑"))
    recalled = MemoryStore(tmp_path).recall("做一个青瓷小鸟")
    assert recalled[0]["value"] == "我喜欢哑光青瓷"
    assert [m["source_job_id"] for m in recalled if m["kind"] == "episode"] == ["a" * 32]
    assert "private" not in json.dumps(recalled)
    assert recalled[1]["artifact_verified"] is True
    assert len(store.recall("一个测试模型")) == 1  # preference only, no irrelevant episodes
    assert len(store.recall("青瓷花瓶", exclude_job_id="a" * 32)) == 1


@pytest.mark.parametrize("prompt,expected", [
    ("一个青瓷小鸟", None), ("不要记住我喜欢青瓷", None),
    ("记住我的偏好：哑光青瓷。\n生成一个小鸟", "哑光青瓷"),
    ("请记住我喜欢简约风格。\n生成一个小鸟", "简约风格"),
    ("Remember that I prefer matte ceramic.\nCreate a bird", "matte ceramic"),
    ("记住我的偏好：    。", None),
])
def test_only_explicit_preferences_are_captured(tmp_path, prompt, expected):
    store = MemoryStore(tmp_path)
    item = store.capture_explicit_preference(prompt, "a" * 32)
    if expected:
        assert item["value"] == expected and item["source"] == "user_explicit"
        assert item["source_job_id"] == "a" * 32 and item["evidence"] in prompt
    else:
        assert item is None and store.list() == []


def test_updating_preferences_replaces_same_key_and_preserves_new_user_choice(tmp_path):
    store = MemoryStore(tmp_path)
    store.set_preference("material", "青瓷")
    store.set_preference("material", "木雕")
    assert len(store.list()) == 1 and store.recall("花瓶")[0]["value"] == "木雕"
    store.forget("pref:material")
    store.set_preference("material", "金属")  # Explicit update can restore it.
    assert store.recall("花瓶")[0]["value"] == "金属"


@pytest.mark.parametrize("prompt", [
    "解释下面的示例，不要保存：\n记住我的偏好：亮面塑料。",
    "```text\n记住我喜欢亮面塑料。\n```",
    "What does this command mean?\nRemember that I prefer glossy plastic.",
    "Do not save the following example:\n```text\nRemember that I like glossy plastic.\n```",
    "以下是别人的聊天记录：\n记住我喜欢亮面塑料。",
    "记住我的偏好：亮面塑料。这个句子是什么意思？",
    "记住我的偏好：亮面塑料？",
    "> 记住我喜欢亮面塑料。",
])
def test_discussed_memory_commands_do_not_replace_real_preference(tmp_path, prompt):
    store = MemoryStore(tmp_path)
    original = store.capture_explicit_preference("记住我的偏好：哑光青瓷。", turn_id="a" * 32)
    assert store.capture_explicit_preference(prompt, turn_id="b" * 32) is None
    assert MemoryStore(tmp_path).list() == [original]


def test_discussed_memory_command_does_not_restore_forgotten_preference(tmp_path):
    store = MemoryStore(tmp_path)
    store.capture_explicit_preference("记住我喜欢哑光青瓷。")
    store.forget("pref:explicit-description")
    assert store.capture_explicit_preference("解释以下例句：\n记住我喜欢亮面塑料。") is None
    assert MemoryStore(tmp_path).list() == []


@pytest.mark.parametrize("prompt,value", [
    ("\n  请记住我喜欢哑光青瓷。  \n生成一个小鸟", "哑光青瓷"),
    ("\n Remember that I prefer matte ceramic. \nCreate a bird", "matte ceramic"),
])
def test_leading_memory_request_accepts_whitespace_and_following_task(tmp_path, prompt, value):
    store = MemoryStore(tmp_path)
    assert store.capture_explicit_preference(prompt)["value"] == value


def test_concepts_and_failures_are_not_stored_as_verified_3d(tmp_path):
    store = MemoryStore(tmp_path)
    concept = episode(status="CONCEPT_READY")
    concept["mode"] = "CONCEPT"
    store.record_job(concept)
    failure = episode("d" * 32, status="NEEDS_REVIEW")
    failure["agentFailure"] = {"message": "候选视图结构不一致", "status": "NEEDS_REVIEW"}
    store.record_job(failure)
    records = store.recall("青瓷花瓶")
    assert {r["outcome"] for r in records} == {"concept_only", "needs_review"}
    assert not any(r["artifact_verified"] for r in records)
    assert any(r["failure"] == "候选视图结构不一致" for r in records)
    assert store.record_job(episode("f" * 32, status="CANCELLED")) is False
    missing = episode("e" * 32)
    missing["result"] = {"files": []}
    assert store.record_job(missing) is False
    assert store.record_job({"id": "f" * 32, "status": "FAILED", "message": "missing key"}) is False


def test_forgetting_survives_reindex_and_restart(tmp_path):
    store, job = MemoryStore(tmp_path), episode()
    assert store.record_job(job) is True
    assert store.record_job(job) is False
    assert store.forget("episode:" + job["id"]) == 1
    reopened = MemoryStore(tmp_path)
    assert reopened.record_job(job) is False and reopened.recall("青瓷花瓶") == []
    reopened.set_preference("style", "简约")
    reopened.record_job(episode("c" * 32))
    assert reopened.forget() == 2
    assert reopened.record_job(episode("c" * 32)) is False
    assert reopened.list() == []


def test_memory_has_no_model_summaries_credentials_or_artifact_locations(tmp_path):
    store = MemoryStore(tmp_path)
    job = episode()
    job.update(api_key="test-secret", modelSummary="hallucinated truth", localPath="/secret/path")
    store.record_job(job)
    data = json.dumps(store.list())
    assert all(value not in data for value in ("test-secret", "hallucinated truth", "/secret/path", "private.ply"))


@pytest.mark.parametrize("key,value", [("../x", "花瓶"), ("a", ""), ("a", "x" * 201)])
def test_invalid_preferences_are_rejected(tmp_path, key, value):
    with pytest.raises(ValueError):
        MemoryStore(tmp_path).set_preference(key, value)
