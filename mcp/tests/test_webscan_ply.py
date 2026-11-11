"""Synthetic bytes for the artifact contract, not a reconstructed model."""
import httpx
import pytest

from graphics_mcp.settings import Settings
from graphics_mcp.store import Store
from graphics_mcp.webscan import TemporaryWebscanError, Webscan

JOB, SESSION = "a" * 32, "b" * 32
ARTIFACT = f"/api/jobs/{JOB}/artifacts/gaussians.ply"
GAUSSIAN_FIELDS = ("x y z nx ny nz f_dc_0 f_dc_1 f_dc_2 opacity scale_0 scale_1 scale_2 "
                   "rot_0 rot_1 rot_2 rot_3").split()


def ply_fixture(count=1, fields=GAUSSIAN_FIELDS, newline="\n"):
    lines = ["ply", "format binary_little_endian 1.0", "comment offline fixture", f"element vertex {count}",
             *(f"property float {name}" for name in fields), "end_header", ""]
    return newline.join(lines).encode() + bytes(count * len(fields) * 4)


@pytest.mark.parametrize("case", ["plain_points", "missing_header_end", "incomplete_sh", "duplicate_field",
                                  "zero_vertices", "truncated_file", "size_changed", "short_range", "not_found",
                                  "range_ignored", "redirect", "bad_range", "temporarily_unavailable"])
def test_unverified_gaussian_artifacts_cannot_be_published(tmp_path, case):
    body = {
        "plain_points": ply_fixture(fields=["x", "y", "z"]),
        "missing_header_end": b"ply\n" + b" " * 17_000,
        "incomplete_sh": ply_fixture(fields=[*GAUSSIAN_FIELDS, "f_rest_0"]),
        "duplicate_field": ply_fixture(fields=[*GAUSSIAN_FIELDS, "opacity"]),
        "zero_vertices": ply_fixture(count=0),
        "truncated_file": ply_fixture()[:-4],
    }.get(case, ply_fixture())
    size = len(body) + (1 if case == "size_changed" else 0)
    calls = []

    def respond(request):
        calls.append(request.url.path)
        assert request.method == "GET"
        if request.url.path == f"/api/jobs/{JOB}":
            return httpx.Response(200, json={"id": JOB, "session_id": SESSION, "status": "succeeded"})
        if request.url.path == f"/api/jobs/{JOB}/artifacts":
            return httpx.Response(200, json={"job_id": JOB, "files": [{"name": "gaussians.ply", "bytes": size}]})
        assert request.url.path == ARTIFACT
        assert request.headers["range"] == "bytes=0-16383"
        status = {"not_found": 404, "range_ignored": 200, "redirect": 307,
                  "temporarily_unavailable": 503}.get(case, 206)
        prefix = body[:16384]
        headers = {"Content-Range": f"bytes 0-{len(prefix)-1}/{len(body)}", "Content-Length": str(len(prefix))}
        if case == "bad_range":
            headers["Content-Range"] = f"bytes 1-{len(prefix)-1}/{len(body)}"
        if case == "redirect":
            headers["Location"] = "http://other.example/other.ply"
        if case == "short_range":
            prefix = prefix[:-1]
        return httpx.Response(status, headers=headers, stream=httpx.ByteStream(prefix))

    with httpx.Client(base_url="http://127.0.0.1:8787", transport=httpx.MockTransport(respond),
                      follow_redirects=True) as client:
        scan = Webscan(Settings(data_dir=tmp_path), Store(tmp_path), client)
        with pytest.raises(ValueError, match="PLY") as error:
            scan.result(JOB)
    assert isinstance(error.value, TemporaryWebscanError) == (case == "temporarily_unavailable")
    assert calls == [f"/api/jobs/{JOB}", f"/api/jobs/{JOB}/artifacts", ARTIFACT]


def test_large_brush_ply_is_checked_using_only_a_bounded_prefix(tmp_path):
    count = 400
    fields = [*GAUSSIAN_FIELDS, *(f"f_rest_{i}" for i in range(45))]
    ply = ply_fixture(count, fields, newline="\r\n")

    def respond(request):
        if request.url.path == f"/api/jobs/{JOB}":
            return httpx.Response(200, json={"id": JOB, "session_id": SESSION, "status": "succeeded"})
        if request.url.path == f"/api/jobs/{JOB}/artifacts":
            return httpx.Response(200, json={"job_id": JOB, "files": [{"name": "gaussians.ply", "bytes": len(ply),
                                                                     "url": "http://other.example/other.ply"}]})
        assert request.url.path == ARTIFACT and request.headers["range"] == "bytes=0-16383"
        return httpx.Response(206, headers={"Content-Range": f"bytes 0-16383/{len(ply)}", "Content-Length": "16384"},
                              stream=httpx.ByteStream(ply[:16384]))

    with httpx.Client(base_url="http://127.0.0.1:8787", transport=httpx.MockTransport(respond)) as client:
        result = Webscan(Settings(data_dir=tmp_path), Store(tmp_path), client).result(JOB)
    assert result["ply_vertex_count"] == count
    assert result["files"][0]["url"] == f"http://127.0.0.1:8787{ARTIFACT}?download=true"
