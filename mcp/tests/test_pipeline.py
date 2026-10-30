import io
import json
from dataclasses import replace
from pathlib import Path

import anyio
import httpx
from PIL import Image, PngImagePlugin
import pytest

from graphics_mcp.creative import Creative
from graphics_mcp.providers import Providers, ProviderError
from graphics_mcp.server import create_server
from graphics_mcp.settings import Settings, local_url
from graphics_mcp.store import Store
from graphics_mcp.webscan import Webscan


def png(index=0):
    buf = io.BytesIO()
    Image.new("RGB", (512, 512), (index * 13 % 256, 100, 170)).save(buf, "PNG")
    return buf.getvalue()


class FakeProviders:
    def __init__(self, consistent=True):
        self.calls = 0
        self.reviews = 0
        self.consistent = consistent
        self.on_image = None

    def plan(self, prompt):
        return {"title": "测试花瓶", "object_prompt": prompt}

    def image(self, prompt, reference=None):
        self.calls += 1
        if self.on_image:
            self.on_image()
        return png(self.calls)

    def review(self, reference, views):
        self.reviews += 1
        assert len(views) == 12
        return {"consistent": self.consistent, "reason": "一致" if self.consistent else "结构不一致", "scope": "appearance_only"}


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path, zhipu_key="test-key", image_provider="cogview", allow_paid=False)
    store = Store(tmp_path)
    fake = FakeProviders()
    return settings, store, fake, Creative(settings, store, fake)


def completed_reference(creative, store):
    job = creative.reference("一个青瓷花瓶", "ref")
    creative.execute(*store.claim())
    assert store.get(job["id"])["status"] == "succeeded"
    return job["id"]


def test_idempotency_conflicts_and_terminal_state(setup):
    _, store, _, creative = setup
    a = creative.reference("一个花瓶", "once")
    assert creative.reference("一个花瓶", "once")["id"] == a["id"]
    with pytest.raises(ValueError, match="不同内容"):
        creative.reference("一只小鸟", "once")
    creative.cancel(a["id"])
    store.update(a["id"], status="succeeded")
    assert store.get(a["id"])["status"] == "cancelled"
    assert store.claim() is None


def test_restart_does_not_reissue_generation(setup):
    _, store, fake, creative = setup
    job = creative.reference("一个花瓶", "restart")
    store.claim()
    store.recover()
    assert store.get(job["id"])["status"] == "failed"
    assert store.claim() is None
    assert fake.calls == 0


def test_full_image_pipeline_and_review_gate(setup):
    settings, store, fake, creative = setup
    ref = completed_reference(creative, store)
    multi = creative.multiview(ref, "multi")
    creative.execute(*store.claim())
    result = store.get(multi["id"])
    assert result["status"] == "succeeded"
    assert len(result["assets"]) == 12 and fake.calls == 13
    assert fake.reviews == 1
    manifest = store.manifest(multi["id"])
    assert manifest["reference_conditioned"] is False
    assert manifest["geometry_verified"] is False
    fake.consistent = False
    rejected = creative.multiview(ref, "reject")
    creative.execute(*store.claim())
    assert store.get(rejected["id"])["status"] == "needs_review"
    with pytest.raises(ValueError, match="一致性"):
        Webscan(settings, store).import_views(rejected["id"])


def test_cancel_during_image_response_never_starts_next_image(setup):
    _, store, fake, creative = setup
    ref = completed_reference(creative, store)
    job = creative.multiview(ref, "cancel")
    fake.on_image = lambda: creative.cancel(job["id"])
    creative.execute(*store.claim())
    assert fake.calls == 2
    assert store.get(job["id"])["status"] == "cancelled"


@pytest.mark.parametrize("duplicate_at", [2, 7, 12])
def test_duplicate_views_stop_generation_immediately(setup, duplicate_at):
    settings, store, fake, creative = setup
    ref = completed_reference(creative, store)
    # Different PNG bytes and metadata, but the same decoded pixels as view 1.
    encoded = io.BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("comment", "same image, different encoding")
    with Image.open(io.BytesIO(png(1))) as image:
        image.save(encoded, "PNG", pnginfo=metadata, compress_level=0)
    repeated = encoded.getvalue()
    assert repeated != png(1)
    calls = []

    def image(prompt, reference):
        calls.append(prompt)
        return repeated if len(calls) == duplicate_at else png(len(calls))

    fake.image = image
    job = creative.multiview(ref, "duplicates")
    creative.execute(*store.claim())
    assert len(calls) == duplicate_at
    assert fake.reviews == 0
    state = store.get(job["id"])
    assert state["status"] == "needs_review"
    assert f"第 {duplicate_at} 张" in state["message"]
    assert "第 1 张" in state["message"]
    assert len(state["assets"]) == duplicate_at
    assert state["assets"][0]["sha256"] == state["assets"][-1]["sha256"]
    assert all((store.directory(job["id"]) / asset["name"]).is_file() for asset in state["assets"])
    assert "asset_set_id" not in state
    assert not (store.directory(job["id"]) / "manifest.json").exists()
    with pytest.raises(ValueError, match="尚未完成"):
        Webscan(settings, store).import_views(job["id"])
    assert creative.multiview(ref, "duplicates")["id"] == job["id"]
    assert store.claim() is None


def test_missing_key_paid_opt_in_and_path_validation(setup):
    settings, store, _, _ = setup
    with pytest.raises(ValueError, match="ZHIPU_API_KEY"):
        replace(settings, zhipu_key="").check_generation()
    with pytest.raises(ValueError, match="ALLOW_PAID"):
        replace(settings, image_provider="seedream", ark_key="test").check_generation()
    with pytest.raises(ValueError):
        store.manifest("../../secret")
    with pytest.raises(ValueError):
        local_url("https://cloud.example.com")


def test_provider_payloads_do_not_invent_cogview_reference_support(setup):
    settings, _, _, _ = setup
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"data": [{"url": "https://images.example.org/one.png"}]})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    providers = Providers(settings, client)
    providers.download = lambda url: png()
    providers.image("描述", png())
    assert requests[-1]["model"] == "cogview-3-flash"
    assert "image" not in requests[-1]
    providers = Providers(replace(settings, image_provider="seedream", allow_paid=True, ark_key="test"), client)
    providers.download = lambda url: png()
    providers.image("描述", png())
    assert requests[-1]["image"].startswith("data:image/png;base64,")
    assert requests[-1]["sequential_image_generation"] == "disabled"


def test_provider_errors_are_sanitized_and_not_retried(setup):
    settings, _, _, _ = setup
    requests = []
    def reject(request):
        requests.append(request)
        return httpx.Response(429, json={"message": "do-not-expose-secret"})
    provider = Providers(settings, httpx.Client(transport=httpx.MockTransport(reject)))
    with pytest.raises(ProviderError) as error:
        provider.image("花瓶")
    assert "secret" not in str(error.value)
    assert len(requests) == 1
    with pytest.raises(ProviderError, match="公网"):
        provider.download("https://127.0.0.1/private")


def test_webscan_http_contract_and_artifact_gate(setup):
    settings, store, _, creative = setup
    ref = completed_reference(creative, store)
    multi = creative.multiview(ref, "multi")
    creative.execute(*store.claim())
    sid, jid = "a" * 32, "b" * 32
    requests, jobs = [], []
    session_status = "capturing"
    has_ply = False

    def respond(request):
        nonlocal session_status
        requests.append(request)
        path = request.url.path
        if path == "/api/sessions" and request.method == "POST":
            return httpx.Response(201, json={"id": sid})
        if path == f"/api/sessions/{sid}":
            return httpx.Response(200, json={"id": sid, "status": session_status, "jobs": jobs})
        if path.endswith("/frames"):
            assert b'"source": "ai-generated"' in request.content
            assert b"extrinsics" not in request.content
            return httpx.Response(200, json={"ok": True})
        if path.endswith("/finalize"):
            session_status = "ready"
            return httpx.Response(200, json={"id": sid})
        if path == "/api/jobs" and request.method == "POST":
            payload = json.loads(request.content)
            assert payload["backend"] == "brush" and payload["steps"] == 3000
            assert payload["evaluation_mode"] == "all"
            jobs.append({"id": jid, "config": {k:v for k,v in payload.items() if k != "session_id"}})
            return httpx.Response(201, json={"id": jid})
        if path == f"/api/jobs/{jid}":
            return httpx.Response(200, json={"id": jid, "session_id": sid, "status": "succeeded"})
        if path.endswith("/artifacts"):
            return httpx.Response(200, json={"files": [{"name": "gaussians.ply", "bytes": 100}] if has_ply else []})
        raise AssertionError(path)

    scan = Webscan(settings, store, httpx.Client(base_url=settings.webscan_url, transport=httpx.MockTransport(respond)))
    imported = scan.import_views(multi["id"])
    assert imported["session_id"] == sid
    assert len([r for r in requests if r.url.path.endswith("/frames")]) == 12
    before = len(requests)
    scan.import_views(multi["id"])
    assert len(requests) == before
    first = scan.start(sid, "train")
    assert scan.start(sid, "train") == first
    # A persisted intent and lost response recover the existing server-side job.
    store.link("train:train", {"session_id": sid})
    assert scan.by_request("train")["job_id"] == jid
    assert scan.start(sid, "train")["job_id"] == jid
    assert len([r for r in requests if r.url.path == "/api/jobs" and r.method == "POST"]) == 1
    with pytest.raises(ValueError, match="PLY"):
        scan.result(jid)
    has_ply = True
    result = scan.result(jid)
    assert result["viewer_url"].endswith(f"?p={sid}&j={jid}")
    assert result["files"][0]["name"] == "gaussians.ply"


def test_real_mcp_tool_schemas_and_calls(setup):
    settings, _, _, _ = setup
    async def check():
        creative, _ = create_server("creative", settings, 0)
        scan, _ = create_server("webscan", settings, 0)
        tools = await creative.list_tools()
        names = {tool.name for tool in tools}
        assert {"creative_get_job", "creative_generate_reference", "creative_get_request"} <= names
        tool = next(t for t in tools if t.name == "creative_generate_reference")
        assert set(tool.inputSchema["required"]) == {"prompt", "request_id"}
        result = await creative.call_tool("creative_health", {})
        # Real MCP SDK serialization, not a hand-written JSON-RPC imitation.
        assert "test-key" not in str(result)
        assert "cogview-3-flash" in str(result)
        assert len(await scan.list_tools()) == 7
    anyio.run(check)


@pytest.mark.parametrize("status,temporary", [(408, True), (429, True), (500, True), (502, True),
                                               (503, True), (504, True), (400, False), (401, False),
                                               (403, False), (404, False), (409, False), (422, False)])
def test_webscan_error_classification_never_replays_requests(setup, status, temporary):
    from graphics_mcp.webscan import TemporaryWebscanError
    settings, store, _, _ = setup
    requests = []
    def reject(request):
        requests.append(request)
        return httpx.Response(status, json={"message": "private-server-error"})
    scan = Webscan(settings, store, httpx.Client(base_url=settings.webscan_url, transport=httpx.MockTransport(reject)))
    # Even a retryable response to a mutation is returned once, not replayed by the adapter.
    with pytest.raises(ValueError) as error:
        scan.request("POST", "/api/jobs", json={})
    assert isinstance(error.value, TemporaryWebscanError) is temporary
    assert "private-server-error" not in str(error.value)
    assert len(requests) == 1


def test_webscan_network_failure_and_bad_json_are_distinct(setup):
    from graphics_mcp.webscan import TemporaryWebscanError
    settings, store, _, _ = setup
    def unavailable(request):
        raise httpx.ConnectError("private connection details", request=request)
    scan = Webscan(settings, store, httpx.Client(base_url=settings.webscan_url, transport=httpx.MockTransport(unavailable)))
    with pytest.raises(TemporaryWebscanError) as error:
        scan.status("a" * 32)
    assert "private" not in str(error.value)
    scan.client = httpx.Client(base_url=settings.webscan_url, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text="private invalid body")))
    with pytest.raises(ValueError) as error:
        scan.status("a" * 32)
    assert not isinstance(error.value, TemporaryWebscanError)
    assert "private" not in str(error.value)


def test_real_mcp_preserves_temporary_error_code(setup, monkeypatch):
    from graphics_mcp.webscan import TemporaryWebscanError
    settings, _, _, _ = setup
    def unavailable(*args):
        raise TemporaryWebscanError("本地重建服务暂不可用")
    monkeypatch.setattr(Webscan, "status", unavailable)
    async def check():
        app, _ = create_server("webscan", settings, 0)
        result = await app.call_tool("webscan_get_status", {"job_id": "a" * 32})
        assert "TEMPORARY_UNAVAILABLE" in str(result)
        assert "本地重建服务暂不可用" in str(result)
    anyio.run(check)
