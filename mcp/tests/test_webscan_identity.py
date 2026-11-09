"""HTTP contract fixtures only: never run reconstruction or serve real artwork."""
import httpx
import pytest

from graphics_mcp.settings import Settings
from graphics_mcp.store import Store
from graphics_mcp.webscan import TemporaryWebscanError, Webscan

JOB, SESSION, FOREIGN = (c * 32 for c in "abc")


@pytest.mark.parametrize("changes", [{"id": FOREIGN}, {"id": None}, {"session_id": None},
                                      {"session_id": "invalid"}])
@pytest.mark.parametrize("operation", ["status", "result"])
def test_status_identity_is_checked_before_listing_artifacts(tmp_path, changes, operation):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"id": JOB, "session_id": SESSION, "status": "succeeded", **changes})

    with httpx.Client(base_url="http://127.0.0.1:8787", transport=httpx.MockTransport(respond)) as client:
        scan = Webscan(Settings(data_dir=tmp_path), Store(tmp_path), client)
        with pytest.raises(ValueError) as error:
            getattr(scan, operation)(JOB)
        assert not isinstance(error.value, TemporaryWebscanError)
    assert calls == [f"/api/jobs/{JOB}"]


@pytest.mark.parametrize("manifest", [{"job_id": FOREIGN}, {}, {"job_id": None}])
def test_foreign_or_missing_manifest_identity_cannot_publish_result(tmp_path, manifest):
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == f"/api/jobs/{JOB}":
            return httpx.Response(200, json={"id": JOB, "session_id": SESSION, "status": "succeeded"})
        return httpx.Response(200, json={"files": [{"name": "gaussians.ply", "bytes": 100}], **manifest})

    with httpx.Client(base_url="http://127.0.0.1:8787", transport=httpx.MockTransport(respond)) as client:
        scan = Webscan(Settings(data_dir=tmp_path), Store(tmp_path), client)
        with pytest.raises(ValueError, match="产物清单") as error:
            scan.result(JOB)
        assert not isinstance(error.value, TemporaryWebscanError)
    assert calls == [f"/api/jobs/{JOB}", f"/api/jobs/{JOB}/artifacts"]
