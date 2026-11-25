"""Creative MCP configuration checks against the real graph with an offline decision model."""
import json

from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from graphics_mcp.server import create_server
from graphics_mcp.settings import Settings
from text2splat.tools import decode
from test_workflow import complete, rig


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,allow_paid,ark_key,mode,reason", [
    ("seedream", False, "ark-test-only", "THREE_D", "ALLOW_PAID_IMAGE_API"),
    ("seedream", True, "", "CONCEPT", "ARK_API_KEY"),
    ("typo-test-only", False, "", "THREE_D", "IMAGE_PROVIDER"),
])
async def test_invalid_image_config_fails_before_planning_or_recording_a_submission(
        rig, tmp_path, monkeypatch, provider, allow_paid, ark_key, mode, reason):
    workflow, _, tools, _ = rig
    settings = Settings(data_dir=tmp_path / "creative", zhipu_key="glm-test-only",
                        image_provider=provider, ark_key=ark_key, allow_paid=allow_paid)
    server, worker = create_server("creative", settings, 0)

    def unexpected_provider_call(*args, **kwargs):
        raise AssertionError("unexpected external provider call")

    monkeypatch.setattr(worker.providers, "plan", unexpected_provider_call)
    original_call = tools.call
    creative_calls = []
    worker.start()
    try:
        async with create_connected_server_and_client_session(server) as session:
            async def call(name, arguments):
                if not name.startswith("creative_"):
                    return await original_call(name, arguments)
                creative_calls.append(name)
                return decode(await session.call_tool(name, arguments))

            monkeypatch.setattr(tools, "call", call)
            job = await complete(workflow, mode=mode)
            assert job["status"] == "FAILED" and reason in job["message"]
            assert creative_calls == ["creative_health"] and not workflow.model.history
            assert "referenceRequest" not in job
            with worker.store.transaction() as db:
                assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
            health = decode(await session.call_tool("creative_health", {}))
            assert health["worker_running"] and not health["configured"]
            public = json.dumps([job, health])
            assert all(marker not in public for marker in ("glm-test-only", "ark-test-only", "typo-test-only"))
    finally:
        worker.stop.set()
        worker.thread.join(3)
