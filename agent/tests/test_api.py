import time

from fastapi.testclient import TestClient
import pytest

from text2splat.app import create_app
from text2splat.settings import AgentSettings


class UnconfiguredTools:
    async def call(self, name, args):
        assert name in {"creative_health", "webscan_health"}
        return {"configured": False} if name == "creative_health" else {"available": True, "brush_available": True}


@pytest.fixture
def client(tmp_path):
    app = create_app(AgentSettings(data_dir=tmp_path, poll_interval=.01), UnconfiguredTools())
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client


def test_minimal_static_page_health_and_headers(client):
    page = client.get("/graphics/")
    assert page.status_code == 200
    assert "造物方舟" in page.text and 'studio.js' in page.text
    assert page.text.count("<textarea") == 1 and page.text.count('type="submit"') == 1
    assert page.headers["x-content-type-options"] == "nosniff"
    assert page.headers["cache-control"] == "no-cache"
    for name in ("studio.js", "studio.css", "request-journal.mjs", "job-updates.mjs"):
        assert client.get("/graphics/" + name).status_code == 200
    health = client.get("/api/graphics/health")
    assert health.json()["engine"] == "langgraph" and health.json()["tool_framework"] == "langchain"
    assert health.json()["agent_loop"] == "react" and health.json()["max_decisions"] == 12
    assert health.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("body", [{"prompt": " "}, {"prompt": "短"}, {"prompt": "a" * 2001},
                                       {"prompt": "花瓶", "mode": "UNKNOWN"}, {"prompt": "花瓶", "parentJobId": "../x"}])
def test_invalid_input_never_enqueues(client, body):
    assert client.post("/api/graphics/jobs", json=body).status_code == 400
    assert client.get("/api/graphics/jobs").json() == []


@pytest.mark.parametrize("headers", [{"origin": "https://untrusted.example"}, {"host": "evil.example"},
                                          {"origin": "http://127.0.0.1:9999"}, {"origin": "null"},
                                          {"host": "localhost:99999"}])
def test_local_same_origin_only(client, headers):
    assert client.post("/api/graphics/jobs", json={"prompt": "花瓶"}, headers=headers).status_code == 403
    assert client.get("/api/graphics/jobs").json() == []


def test_async_api_idempotency_and_terminal_sse(client):
    args = dict(json={"prompt": "一个青瓷花瓶", "mode": "THREE_D"}, headers={"Idempotency-Key": "api-test"})
    accepted = client.post("/api/graphics/jobs", **args)
    assert accepted.status_code == 202
    jid = accepted.json()["id"]
    assert client.post("/api/graphics/jobs", **args).json()["id"] == jid
    for _ in range(100):
        job = client.get("/api/graphics/jobs/" + jid).json()
        if job["status"] == "FAILED":
            break
        time.sleep(.01)
    assert job["status"] == "FAILED" and "ZHIPU_API_KEY" in job["message"]
    assert client.get("/api/graphics/requests/api-test").json() == job
    events = client.get("/api/graphics/jobs/" + jid + "/events")
    assert events.headers["content-type"].startswith("text/event-stream")
    assert "event:progress" in events.text and '"status": "FAILED"' in events.text
    assert client.post("/api/graphics/jobs/" + jid + "/cancel").json() == job
    conflict = client.post("/api/graphics/jobs", json={"prompt": "另一只小鸟"}, headers=args["headers"])
    assert conflict.status_code == 400


def test_request_recovery_is_read_only(client):
    assert client.get("/api/graphics/requests/unknown").status_code == 404
    assert client.get("/api/graphics/requests/" + "x" * 161).status_code == 400
    assert client.get("/api/graphics/jobs").json() == []
    job, _ = client.app.state.store.create("旧花瓶任务", "THREE_D", None, "lost")
    assert client.get("/api/graphics/requests/lost").json()["id"] == job["id"]
    assert client.post("/api/graphics/jobs/" + job["id"] + "/cancel").json()["status"] == "CANCELLED"


@pytest.mark.parametrize("mode", ["CONCEPT", "THREE_D"])
def test_direct_creation_keeps_explicit_user_preference_capture(client, mode):
    response = client.post("/api/graphics/jobs", json={"prompt": "记住我的偏好：哑光青瓷。\n生成花瓶", "mode": mode})
    assert response.status_code == 202
    jid = response.json()["id"]
    for _ in range(100):
        if client.get(f"/api/graphics/jobs/{jid}").json()["status"] == "FAILED":
            break
        time.sleep(.01)
    records = client.get("/api/graphics/memories").json()
    assert len(records) == 1
    assert records[0]["value"] == "哑光青瓷" and records[0]["source_job_id"] == jid
    assert records[0]["source_turn_id"] is None


def test_assets_reject_absent_invalid_names_and_symlinks(client, tmp_path):
    assets = tmp_path / "assets" / ("a" * 32)
    assets.mkdir(parents=True)
    (assets / "00.png").write_bytes(b"test-fixture")
    (assets / "01.png").symlink_to(assets / "00.png")
    prefix = "/api/graphics/assets/" + "a" * 32 + "/"
    assert client.get(prefix + "00.png").content == b"test-fixture"
    for name in ("01.png", "02.png", "manifest.json", "anything.png"):
        assert client.get(prefix + name).status_code == 404
    assert client.get("/api/graphics/assets/invalid/00.png").status_code == 404


def test_memory_preferences_can_be_inspected_updated_and_forgotten(client):
    assert client.get("/api/graphics/memories").json() == []
    route = "/api/graphics/memories/preferences/material"
    saved = client.put(route, json={"value": "哑光青瓷"})
    assert saved.status_code == 200 and saved.json()["source"] == "user_explicit"
    assert client.put(route, json={"value": "浅色木雕"}).status_code == 200
    records = client.get("/api/graphics/memories")
    assert records.headers["cache-control"] == "no-store"
    assert len(records.json()) == 1 and records.json()[0]["value"] == "浅色木雕"
    assert client.delete("/api/graphics/memories/pref:material").json()["deleted"] == 1
    assert client.get("/api/graphics/memories").json() == []
    assert client.put(route, json={"value": "a" * 201}).status_code == 400
    assert client.get("/api/graphics/memories?limit=1000").status_code == 400
    assert client.delete("/api/graphics/memories/invalid").status_code == 400
    assert client.get("/api/graphics/jobs").json() == []


def test_memory_modification_obeys_local_same_origin_policy(client):
    route = "/api/graphics/memories/preferences/style"
    headers = {"origin": "https://untrusted.example"}
    assert client.put(route, json={"value": "简约"}, headers=headers).status_code == 403
    client.put(route, json={"value": "简约"})
    assert client.delete("/api/graphics/memories", headers=headers).status_code == 403
    assert client.delete("/api/graphics/memories").json()["deleted"] == 1
