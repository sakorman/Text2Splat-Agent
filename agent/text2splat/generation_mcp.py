"""Built-in MCP boundary delegating to the independently queued 3D agent.

Uses real MCP initialize/tools/call messages over the SDK's in-memory transport.
No new port, HTTP self-call, model endpoint, or transport dependency is needed.
"""
import asyncio
from datetime import timedelta

from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from .tools import ToolFailure, decode


class GenerationMCP:
    def __init__(self, workflow):
        self.workflow = workflow
        self.server = FastMCP("text2splat-generation-agent")

        @self.server.tool()
        async def generate_3d_model(description: str, request_id: str, parent_job_id: str = "") -> dict:
            """Delegate an explicit creation request to the 3D agent. Returns a queued job, not a model."""
            try:
                job = workflow.submit(description, "THREE_D", parent_job_id or None, request_id)
                return {"ok": True, "data": job}
            except (ValueError, KeyError) as exc:
                return {"ok": False, "error": str(exc)[:400]}
            except Exception:
                return {"ok": False, "error": "无法记录生成请求，请检查本地服务与磁盘"}

    async def generate(self, description, request_id, parent_job_id=None):
        try:
            async with asyncio.timeout(15):
                async with create_connected_server_and_client_session(
                    self.server, read_timeout_seconds=timedelta(seconds=15)
                ) as session:
                    result = await session.call_tool("generate_3d_model", {
                        "description": description, "request_id": request_id,
                        "parent_job_id": parent_job_id or "",
                    })
            return decode(result)
        except ToolFailure:
            raise
        except Exception:
            raise ToolFailure("生成请求返回未确认；未重新提交，请查询原任务") from None
