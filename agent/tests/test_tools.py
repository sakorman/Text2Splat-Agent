import asyncio
import json
import socket

import httpx
from langchain_core.tools import StructuredTool
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent
import pytest
import uvicorn

from text2splat.settings import AgentSettings
from text2splat.tools import McpTools, ToolFailure, TemporaryToolFailure, decode, temporary


@pytest.mark.parametrize("payload", [{"ok": False, "error": "质量检查失败"}, {"ok": True, "data": []},
                                        {"ok": "true", "data": {}}, [], "invalid"])
def test_tool_envelopes_fail_closed(payload):
    result = CallToolResult(content=[TextContent(type="text", text=json.dumps(payload))])
    with pytest.raises(ToolFailure) as caught:
        decode(result)
    assert not isinstance(caught.value, TemporaryToolFailure)


def test_structured_content_and_typed_temporary_error():
    result = CallToolResult(content=[], structuredContent={"ok": True, "data": {"available": True}})
    assert decode(result) == {"available": True}
    with pytest.raises(TemporaryToolFailure):
        decode(CallToolResult(content=[], structuredContent={"ok": False, "error_code": "TEMPORARY_UNAVAILABLE"}))
    with pytest.raises(ToolFailure):
        decode(CallToolResult(content=[], isError=True))


@pytest.mark.parametrize("status,expected", [(408, True), (429, True), (503, True), (400, False), (401, False), (404, False)])
def test_transport_classification(status, expected):
    response = httpx.Response(status, request=httpx.Request("GET", "http://127.0.0.1/mcp"))
    exc = httpx.HTTPStatusError("fixture", request=response.request, response=response)
    assert temporary(ExceptionGroup("SDK transport wrapper", [exc])) is expected
    assert temporary(ExceptionGroup("mixed protocol failure", [exc, ValueError()])) is False


@pytest.mark.asyncio
async def test_real_sdk_and_langchain_tool_bridge():
    calls = []
    mcp = FastMCP("fixture", stateless_http=True, json_response=True)

    @mcp.tool()
    async def creative_get_job(job_id: str) -> dict:
        calls.append(job_id)
        return {"ok": True, "data": {"id": job_id, "status": "running"}}

    @mcp.tool()
    async def creative_generate_reference(prompt: str, request_id: str) -> dict:
        calls.append(request_id)
        return {"ok": False, "error_code": "TEMPORARY_UNAVAILABLE", "error": "测试暂时故障"}

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(mcp.streamable_http_app(), log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        async with asyncio.timeout(5):
            while not server.started:
                if task.done():
                    await task
                await asyncio.sleep(.01)
        tools = McpTools(AgentSettings(creative_url=f"http://127.0.0.1:{port}"))
        assert isinstance(tools.tools["creative_get_job"], StructuredTool)
        assert await tools.call("creative_get_job", {"job_id": "a" * 32}) == {"id": "a" * 32, "status": "running"}
        with pytest.raises(TemporaryToolFailure):
            await tools.call("creative_generate_reference", {"prompt": "测试", "request_id": "once"})
        assert calls == ["a" * 32, "once"]  # The bridge itself never retries a mutation.
        with pytest.raises(ToolFailure):
            await tools.call("not_allowlisted", {})
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 5)
        listener.close()
