import argparse
import functools

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

from .creative import Creative
from .settings import Settings
from .store import Store
from .webscan import Webscan, TemporaryWebscanError


def create_server(kind, settings, port):
    store = Store(settings.data_dir)
    app = FastMCP(f"graphics-{kind}", host="127.0.0.1", port=port,
                  stateless_http=True, json_response=True)

    def tool(fn):
        @functools.wraps(fn)
        def safe(*args, **kwargs):
            try:
                return {"ok": True, "data": fn(*args, **kwargs)}
            except TemporaryWebscanError as exc:
                return {"ok": False, "error": str(exc)[:600], "error_code": "TEMPORARY_UNAVAILABLE"}
            except ValueError as exc:
                return {"ok": False, "error": str(exc)[:600]}
            except Exception:
                return {"ok": False, "error": "本地 MCP 操作失败，请检查配置和服务状态"}
        app.tool()(safe)
        return safe

    worker = None
    if kind == "creative":
        worker = Creative(settings, store)

        @tool
        def creative_health() -> dict:
            """Read configured generation providers; never exposes credentials."""
            return worker.health()

        @tool
        def creative_generate_reference(prompt: str, request_id: str) -> dict:
            """Queue one object specification and reference image; returns a job ID immediately."""
            return worker.reference(prompt, request_id)

        @tool
        def creative_generate_multiview(reference_id: str, request_id: str) -> dict:
            """Queue 12 candidate views and an appearance check; camera poses are NOT assumed."""
            return worker.multiview(reference_id, request_id)

        @tool
        def creative_get_job(job_id: str) -> dict:
            """Read generation progress, local asset references, and review outcome."""
            return store.get(job_id)

        @tool
        def creative_get_request(request_id: str) -> dict:
            """Recover a generation ID after a lost submit response, without generating again."""
            return {"job": store.by_request(request_id)}

        @tool
        def creative_cancel_job(job_id: str) -> dict:
            """Stop future generation steps; already submitted provider requests cannot be recalled."""
            return worker.cancel(job_id)
    else:
        scan = Webscan(settings, store)

        @tool
        def webscan_health() -> dict:
            """Check the local web-scan service and Brush availability."""
            return scan.health()

        @tool
        def webscan_import_generated_views(asset_set_id: str) -> dict:
            """Import a reviewed local asset set into web-scan and freeze its images."""
            return scan.import_views(asset_set_id)

        @tool
        def webscan_start_reconstruction(session_id: str, request_id: str) -> dict:
            """Start local COLMAP + Brush training with a bounded fast preset; returns a job ID."""
            return scan.start(session_id, request_id)

        @tool
        def webscan_get_status(job_id: str) -> dict:
            """Read actual reconstruction phase, status, progress and report."""
            return scan.status(job_id)

        @tool
        def webscan_get_request(request_id: str) -> dict:
            """Recover the reconstruction ID after a lost submit response."""
            return {"job": scan.by_request(request_id)}

        @tool
        def webscan_get_result(job_id: str) -> dict:
            """Return viewer/artifact links only after real reconstruction succeeds."""
            return scan.result(job_id)

        @tool
        def webscan_cancel_reconstruction(job_id: str) -> dict:
            """Cancel a queued/running local reconstruction."""
            return scan.cancel(job_id)
    return app, worker


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["creative", "webscan"])
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    settings = Settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    # One process per adapter, before recovery can mutate persisted task state.
    import fcntl
    with (settings.data_dir / f"{args.kind}.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"{args.kind} MCP 已在运行") from None
        app, worker = create_server(args.kind, settings, args.port or (8766 if args.kind == "creative" else 8767))
        if worker:
            worker.start()
        try:
            app.run(transport="streamable-http")
        finally:
            if worker:
                worker.stop.set()


if __name__ == "__main__":
    main()
