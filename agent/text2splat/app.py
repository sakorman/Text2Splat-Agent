"""Loopback-only FastAPI host for the LangGraph agent and the existing studio."""
import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit
import uuid

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import BaseModel, Field, field_validator

from .settings import AgentSettings
from .store import JobStore, TERMINAL, valid_id
from .tools import McpTools
from .workflow import Workflow
from .chat import ChatAgent
from .generation_mcp import GenerationMCP

STATIC = Path(__file__).parent / "static" / "graphics"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "frame-src http://127.0.0.1:* http://localhost:*; object-src 'none'; base-uri 'self'; frame-ancestors 'self'")


class LocalOnly:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        host = request.headers.get("host", "")
        allowed = bool(re.fullmatch(r"(localhost|127\.0\.0\.1|\[::1\])(?::[0-9]{1,5})?", host))
        origin = request.headers.get("origin")
        try:
            target = urlsplit(scope["scheme"] + "://" + host)
            if target.port is not None and not 1 <= target.port <= 65535:
                allowed = False
            if origin:
                parsed = urlsplit(origin)
                allowed = allowed and not parsed.username and not parsed.password and not parsed.query and not parsed.fragment
                allowed = allowed and parsed.path in ("", "/") and (
                    parsed.scheme, parsed.hostname, parsed.port or 80) == (target.scheme, target.hostname, target.port or 80)
        except ValueError:
            allowed = False
        if not allowed:
            return await JSONResponse({"message": "仅允许本机同源访问"}, status_code=403)(scope, receive, send)

        async def with_headers(message):
            if message["type"] == "http.response.start":
                message["headers"] += [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                                       (b"content-security-policy", CSP.encode()),
                                       (b"cache-control", b"no-store" if scope["path"].startswith("/api/") else b"no-cache")]
            await send(message)
        await self.app(scope, receive, with_headers)


class CreateRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=2000)
    mode: str = Field(default="CONCEPT", pattern=r"^(CONCEPT|THREE_D)$")
    parentJobId: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @field_validator("prompt", mode="before")
    @classmethod
    def strip_prompt(cls, value):
        return value.strip() if isinstance(value, str) else value


class PreferenceRequest(BaseModel):
    value: str = Field(min_length=2, max_length=200)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=2000)
    conversationId: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")

    @field_validator("message", mode="before")
    @classmethod
    def strip_message(cls, value):
        return value.strip() if isinstance(value, str) else value


def create_app(settings=None, tools=None, model=None, chat_model=None):
    settings = settings or AgentSettings()

    @asynccontextmanager
    async def lifespan(app):
        store = JobStore(settings.data_dir)
        try:
            async with AsyncSqliteSaver.from_conn_string(str(settings.data_dir / "langgraph.sqlite3")) as saver:
                await saver.setup()
                workflow = Workflow(settings, store, tools or McpTools(settings), saver, model=model)
                app.state.store, app.state.workflow = store, workflow
                await workflow.start()
                chat = None
                try:
                    generation = GenerationMCP(workflow)
                    chat = ChatAgent(settings, workflow, generation, saver, model=chat_model)
                    app.state.generation, app.state.chat = generation, chat
                    await chat.start()
                    yield
                finally:
                    if chat:
                        await chat.close()
                    await workflow.close()
        finally:
            store.close()

    app = FastAPI(title="Text2Splat Agent", version="0.2.0", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(LocalOnly)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        if request.url.path.startswith("/api/graphics/chat"):
            return JSONResponse({"message": "请输入 1–2000 字消息和有效的对话标识"}, status_code=400)
        if request.url.path.startswith("/api/graphics/memories"):
            return JSONResponse({"message": "请检查记忆分页参数或提供 2–200 字偏好内容"}, status_code=400)
        return JSONResponse({"message": "请提供 2–2000 字描述和有效的生成模式"}, status_code=400)

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse({"message": str(exc)}, status_code=400)

    @app.exception_handler(KeyError)
    async def missing(request, exc):
        return JSONResponse({"message": str(exc.args[0])}, status_code=404)

    @app.exception_handler(RuntimeError)
    async def unavailable(request, exc):
        return JSONResponse({"message": "本地任务服务暂不可用，请检查服务和磁盘空间"}, status_code=503)

    @app.get("/")
    async def home():
        return RedirectResponse("/graphics/")

    @app.get("/api/graphics/health")
    async def health():
        result = await app.state.workflow.health()
        result["agents"] = ["chat_supervisor", "3d_react_specialist"]
        result["generation_trigger"] = "generate_3d_model"
        result["delegation_transport"] = "mcp_in_memory"
        return result

    @app.post("/api/graphics/chat", status_code=202)
    async def chat(body: ChatRequest, request: Request):
        key = request.headers.get("idempotency-key", str(uuid.uuid4()))
        return app.state.chat.submit(body.message, body.conversationId, key)

    @app.get("/api/graphics/chats/{cid}")
    async def conversation(cid: str):
        result = app.state.chat.store.conversation(cid)
        result["latestJob"] = app.state.chat.latest_job(cid)
        return result

    @app.get("/api/graphics/chat/turns/{tid}")
    async def turn(tid: str):
        return app.state.chat.store.get(tid)

    @app.get("/api/graphics/chat/requests/{key}")
    async def chat_request(key: str):
        return app.state.chat.store.by_request(key)

    def memory_operation(operation):
        try:
            return operation(app.state.workflow.memory)
        except (sqlite3.Error, OSError):
            raise RuntimeError("本地记忆库暂不可用") from None

    @app.get("/api/graphics/memories")
    async def memories(limit: int = 50, offset: int = 0):
        return memory_operation(lambda m: m.list(limit, offset))

    @app.put("/api/graphics/memories/preferences/{key}")
    async def preference(key: str, body: PreferenceRequest):
        return memory_operation(lambda m: m.set_preference(key, body.value))

    @app.delete("/api/graphics/memories/{mid}")
    async def forget_memory(mid: str):
        return {"deleted": memory_operation(lambda m: m.forget(mid))}

    @app.delete("/api/graphics/memories")
    async def forget_memories():
        return {"deleted": memory_operation(lambda m: m.forget())}

    @app.post("/api/graphics/jobs", status_code=202)
    async def create(body: CreateRequest, request: Request):
        key = request.headers.get("idempotency-key", str(uuid.uuid4()))
        if body.mode == "THREE_D":
            return await app.state.generation.generate(body.prompt, key, body.parentJobId, prompt_source="user")
        return app.state.workflow.submit(body.prompt, body.mode, body.parentJobId, key, prompt_source="user")

    @app.get("/api/graphics/jobs")
    async def jobs():
        return app.state.store.list()[:50]

    @app.get("/api/graphics/jobs/{jid}")
    async def job(jid: str):
        return app.state.store.get(jid)

    @app.get("/api/graphics/requests/{key}")
    async def by_request(key: str):
        return app.state.store.by_request(key)

    @app.post("/api/graphics/jobs/{jid}/cancel")
    async def cancel(jid: str):
        return app.state.workflow.cancel(jid)

    @app.get("/api/graphics/jobs/{jid}/events")
    async def events(jid: str, request: Request):
        app.state.store.get(jid)

        async def stream():
            while not await request.is_disconnected():
                value = app.state.store.get(jid)
                yield f"event:progress\nid:{value['updatedAt']}\ndata:{json.dumps(value, ensure_ascii=False)}\n\n"
                if value["status"] in TERMINAL:
                    break
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/graphics/assets/{set_id}/{name}")
    async def asset(set_id: str, name: str):
        try:
            valid_id(set_id)
        except ValueError:
            raise KeyError("图片不存在") from None
        directory = settings.data_dir / "assets" / set_id
        path = directory / name
        if (not re.fullmatch(r"[0-9]{2}\.png", name) or directory.is_symlink()
                or path.is_symlink() or not path.is_file()):
            raise KeyError("图片不存在")
        return FileResponse(path, media_type="image/png")

    app.mount("/graphics", StaticFiles(directory=STATIC, html=True), name="graphics")
    return app


def main():
    load_dotenv()
    import uvicorn
    uvicorn.run(create_app(), host="127.0.0.1", port=int(os.getenv("PORT", "8080")),
                proxy_headers=False, timeout_graceful_shutdown=15)


if __name__ == "__main__":
    main()
