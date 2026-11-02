"""LangChain tools backed by the official MCP SDK, with no mutation retries."""
import asyncio
from datetime import timedelta
import json

import httpx
from langchain_core.tools import StructuredTool
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import StrictStr, create_model

from graphics_mcp.settings import local_url

SCHEMAS = {
    "creative_health": (), "webscan_health": (),
    "creative_generate_reference": ("prompt", "request_id"),
    "creative_generate_multiview": ("reference_id", "request_id"),
    "creative_get_job": ("job_id",), "creative_cancel_job": ("job_id",),
    "creative_get_request": ("request_id",),
    "webscan_import_generated_views": ("asset_set_id",),
    "webscan_start_reconstruction": ("session_id", "request_id"),
    "webscan_get_status": ("job_id",), "webscan_get_result": ("job_id",),
    "webscan_cancel_reconstruction": ("job_id",), "webscan_get_request": ("request_id",),
}


class ToolFailure(ValueError):
    pass


class TemporaryToolFailure(ToolFailure):
    pass


def decode(result):
    if result.isError:
        raise ToolFailure("MCP 工具执行失败")
    data = result.structuredContent
    if data is None:
        try:
            data = json.loads(next(c.text for c in result.content if c.type == "text"))
        except (StopIteration, ValueError):
            raise ToolFailure("MCP 返回内容不是有效 JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise ToolFailure("MCP 返回结构异常")
    if not data["ok"]:
        error = TemporaryToolFailure if data.get("error_code") == "TEMPORARY_UNAVAILABLE" else ToolFailure
        raise error(str(data.get("error") or "本地 MCP 操作失败")[:600])
    if not isinstance(data.get("data"), dict):
        raise ToolFailure("MCP 返回缺少有效数据")
    return data["data"]


def temporary(exc):
    if isinstance(exc, BaseExceptionGroup):
        return bool(exc.exceptions) and all(temporary(child) for child in exc.exceptions)
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {408, 429, 500, 502, 503, 504}
    return isinstance(exc, (httpx.TransportError, TimeoutError, ConnectionError))


class McpTools:
    def __init__(self, settings):
        self.urls = {"creative": local_url(settings.creative_url), "webscan": local_url(settings.webscan_url)}
        self.timeout = settings.mcp_timeout
        self.tools = {name: self._tool(name, fields) for name, fields in SCHEMAS.items()}

    def _tool(self, name, fields):
        async def invoke(**arguments):
            return await self._call(name, arguments)
        schema = create_model(name + "Input", **{field: (StrictStr, ...) for field in fields})
        return StructuredTool.from_function(name=name, description=f"Local graphics MCP tool: {name}",
                                            args_schema=schema, coroutine=invoke)

    async def call(self, name, arguments):
        if name not in self.tools:
            raise ToolFailure("未知图形工具")
        return await self.tools[name].ainvoke(arguments)

    async def _call(self, name, arguments):
        server = "creative" if name.startswith("creative_") else "webscan"
        try:
            # A fresh stateless session per call avoids reusing expired server state.
            # Only the workflow's explicitly allowlisted reads may retry this call.
            async with asyncio.timeout(self.timeout):
                async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=False) as client:
                    async with streamable_http_client(self.urls[server] + "/mcp", http_client=client) as (read, write, _):
                        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=self.timeout)) as session:
                            await session.initialize()
                            result = await session.call_tool(name, arguments)
            return decode(result)
        except ToolFailure:
            raise
        except Exception as exc:
            error = TemporaryToolFailure if temporary(exc) else ToolFailure
            raise error("本地 MCP 连接暂时不可用" if temporary(exc) else "本地 MCP 通信或返回格式异常") from None
