import json

import httpx
import pytest

from graphics_mcp.settings import Settings
from graphics_mcp.store import Store
from graphics_mcp.webscan import TemporaryWebscanError, Webscan


class ReconstructionService:
    """HTTP contract double; never runs training."""
    session_id = "a" * 32
    job_id = "b" * 32

    def __init__(self, tmp_path):
        self.settings = Settings(data_dir=tmp_path)
        self.store = Store(tmp_path)
        self.jobs, self.posts, self.cancelled = [], [], []
        self.response_mode = "accepted_then_timeout"
        self.response_changes = {}
        self.submitted = None
        self.client = httpx.Client(base_url=self.settings.webscan_url, transport=httpx.MockTransport(self.respond))

    def adapter(self):
        # Fresh adapter/store objects exercise durable recovery after a restart.
        return Webscan(self.settings, Store(self.settings.data_dir), self.client)

    def respond(self, request):
        if request.method == "GET" and request.url.path == f"/api/sessions/{self.session_id}":
            return httpx.Response(200, json={"id": self.session_id, "status": "ready", "jobs": self.jobs})
        if request.method == "POST" and request.url.path == "/api/jobs":
            payload = json.loads(request.content)
            self.posts.append(payload)
            self.submitted = {"id": self.job_id, "session_id": self.session_id, "status": "queued",
                              "config": {k: v for k, v in payload.items() if k != "session_id"}}
            if self.response_mode != "timeout_before_commit":
                self.jobs.insert(0, self.submitted)
            if self.response_mode != "success":
                raise httpx.ReadTimeout("private transport detail", request=request)
            return httpx.Response(200, json={**self.submitted, **self.response_changes})
        if request.method == "POST" and request.url.path.endswith("/cancel"):
            jid = request.url.path.split("/")[-2]
            self.cancelled.append(jid)
            return httpx.Response(200, json={"id": jid, "status": "cancelled"})
        raise AssertionError((request.method, request.url.path))


@pytest.fixture
def service(tmp_path):
    service = ReconstructionService(tmp_path)
    yield service
    service.client.close()


def lose_start_response(service):
    with pytest.raises(TemporaryWebscanError):
        service.adapter().start(service.session_id, "request")
    assert len(service.posts) == 1


def test_lost_start_recovers_matching_job_not_newest_job_and_persists_it(service):
    lose_start_response(service)
    unrelated = {**service.submitted, "id": "c" * 32,
                 "config": {**service.submitted["config"], "steps": 6000}}
    service.jobs.insert(0, unrelated)
    adapter = service.adapter()
    recovered = adapter.by_request("request")
    assert recovered["job_id"] == service.job_id
    adapter.cancel(recovered["job_id"])
    assert service.cancelled == [service.job_id]
    # A later matching job must not change a mapping already confirmed and saved.
    service.jobs.insert(0, {**service.submitted, "id": "d" * 32})
    assert service.adapter().by_request("request")["job_id"] == service.job_id
    assert service.adapter().start(service.session_id, "request")["job_id"] == service.job_id
    assert len(service.posts) == 1


def test_unknown_start_never_reposts_even_before_the_job_becomes_visible(service):
    service.response_mode = "timeout_before_commit"
    lose_start_response(service)
    for _ in range(2):
        with pytest.raises(ValueError, match="未知"):
            service.adapter().start(service.session_id, "request")
    assert len(service.posts) == 1
    service.jobs.append(service.submitted)
    assert service.adapter().start(service.session_id, "request")["job_id"] == service.job_id
    assert len(service.posts) == 1


@pytest.mark.parametrize("operation", ["start", "lookup"])
@pytest.mark.parametrize("matches", ["none", "multiple"])
def test_unknown_or_ambiguous_recovery_never_adopts_or_cancels_another_job(service, operation, matches):
    lose_start_response(service)
    if matches == "none":
        service.jobs[:] = [{**service.submitted, "id": "c" * 32,
                            "config": {**service.submitted["config"], "backend": "reference"}}]
    else:
        service.jobs.insert(0, {**service.submitted, "id": "c" * 32})
    adapter = service.adapter()
    with pytest.raises(ValueError, match="未知|多个"):
        if operation == "start":
            adapter.start(service.session_id, "request")
        else:
            adapter.by_request("request")
    assert len(service.posts) == 1
    assert service.cancelled == []
    assert not service.store.link("train:request").get("job_id")


def test_existing_active_job_with_other_parameters_prevents_start(service):
    service.response_mode = "success"
    service.jobs.append({"id": "c" * 32, "session_id": service.session_id,
                         "status": "running", "config": {"backend": "reference"}})
    with pytest.raises(ValueError, match="其他参数"):
        service.adapter().start(service.session_id, "new-request")
    assert service.posts == []
    assert service.store.link("train:new-request") is None


def test_legacy_intent_can_recover_only_one_matching_preset(service):
    service.response_mode = "success"
    original = service.adapter().start(service.session_id, "request")
    service.store.link("train:request", {"session_id": service.session_id})
    assert service.adapter().by_request("request")["job_id"] == original["job_id"]
    assert service.store.link("train:request")["job_id"] == original["job_id"]
    assert len(service.posts) == 1


@pytest.mark.parametrize("changes", [{"session_id": "e" * 32}, {"config": {"backend": "reference"}}])
def test_start_does_not_confirm_a_response_for_another_session_or_config(service, changes):
    service.response_mode = "success"
    service.response_changes = changes
    with pytest.raises(ValueError, match="不匹配"):
        service.adapter().start(service.session_id, "request")
    assert len(service.posts) == 1
    assert not service.store.link("train:request").get("job_id")
    assert service.cancelled == []


def test_recovery_uses_the_persisted_parameters_not_a_new_default(service):
    lose_start_response(service)
    original_config = {**service.submitted["config"], "steps": 6000}
    service.store.link("train:request", {"session_id": service.session_id, "config": original_config})
    service.jobs[:] = [{**service.submitted, "config": original_config}]
    recovered = service.adapter().by_request("request")
    assert recovered["job_id"] == service.job_id
    assert recovered["config"] == original_config
    assert len(service.posts) == 1
