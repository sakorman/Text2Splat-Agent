"""A persisted LangGraph owns the text → images → local reconstruction workflow."""
import asyncio
import time
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from .store import TERMINAL, valid_id
from .tools import TemporaryToolFailure, ToolFailure


class State(TypedDict, total=False):
    job_id: str
    prompt: str
    mode: str
    deadline: float
    reference_id: str
    views_id: str
    session_id: str
    reconstruction_id: str


class Cancelled(Exception):
    pass


class ReviewRequired(ToolFailure):
    pass


class Workflow:
    READS = {"creative_get_job", "webscan_get_status", "webscan_get_result"}

    def __init__(self, settings, store, tools, checkpointer):
        self.settings, self.store, self.tools = settings, store, tools
        builder = StateGraph(State)
        nodes = {"preflight": self.preflight, "reference": self.reference, "multiview": self.multiview,
                 "import_views": self.import_views, "start_reconstruction": self.start_reconstruction,
                 "wait_reconstruction": self.wait_reconstruction, "validate_result": self.validate_result}
        for name, fn in nodes.items():
            builder.add_node(name, fn)  # No graph-level RetryPolicy for side-effecting nodes.
        builder.add_edge(START, "preflight")
        builder.add_edge("preflight", "reference")
        builder.add_conditional_edges("reference", lambda state: "concept" if state["mode"] == "CONCEPT" else "3d",
                                      {"concept": END, "3d": "multiview"})
        for left, right in [("multiview", "import_views"), ("import_views", "start_reconstruction"),
                            ("start_reconstruction", "wait_reconstruction"),
                            ("wait_reconstruction", "validate_result"), ("validate_result", END)]:
            builder.add_edge(left, right)
        self.graph = builder.compile(checkpointer=checkpointer, name="text2splat")
        self.queue = asyncio.Queue()
        self.worker = None
        self.active = None

    async def start(self):
        # Preserve previous restart semantics. Checkpoints are inspectable but an
        # incomplete side effect is never blindly replayed with graph.ainvoke(None).
        for job in self.store.list():
            if job["status"] not in TERMINAL:
                self.queue.put_nowait(("recover", job["id"]))
        self.worker = asyncio.create_task(self._consume(), name="text2splat-graph-worker")

    async def close(self):
        if self.worker:
            self.worker.cancel()
            try:
                await self.worker
            except asyncio.CancelledError:
                pass  # Durable intent remains for the next startup's reconciliation.

    def submit(self, prompt, mode, parent, key):
        job, created = self.store.create(prompt, mode, parent, key)
        if created:
            self.queue.put_nowait(("run", job["id"]))
        return job

    def cancel(self, jid):
        job = self.store.update(jid, cancelRequested=True, status="CANCELLING", message="正在取消生成")
        if job["status"] not in TERMINAL and jid != self.active:
            if not any(job.get(k) for k in ("referenceRequest", "viewsRequest", "trainRequest")):
                job = self.store.update(jid, status="CANCELLED", message="任务已取消")
            else:
                self.queue.put_nowait(("cancel", jid))
        return job

    async def health(self):
        result = {"engine": "langgraph", "tool_framework": "langchain"}
        for name in ("creative", "webscan"):
            try:
                result[name] = await self.tools.call(name + "_health", {})
            except Exception:
                result[name] = {"available": False, "message": "本地 MCP 服务暂不可用"}
        return result

    async def _consume(self):
        while True:
            action, jid = await self.queue.get()
            self.active = jid
            try:
                if self.store.get(jid)["status"] in TERMINAL:
                    continue
                if action == "run":
                    await self.run(jid)
                elif action == "cancel":
                    await self.finish_cancel(jid)
                else:
                    clean = await self.cleanup(jid)
                    cancelled = self.store.get(jid).get("cancelRequested")
                    self.store.update(jid, status=("CANCELLED" if cancelled else "FAILED") if clean else "CANCELLING",
                                      message="服务重启，原任务已中断；未自动重发生图或训练。" if clean else
                                      "服务重启，尚未确认下游停止。恢复 MCP 服务后再次取消。")
            finally:
                self.active = None
                self.queue.task_done()

    async def run(self, jid):
        job = self.store.get(jid)
        state = State(job_id=jid, prompt=job["prompt"], mode=job["mode"],
                      deadline=time.time() + self.settings.job_timeout)
        try:
            await self.graph.ainvoke(state, {"configurable": {"thread_id": jid}}, durability="sync")
        except Cancelled:
            await self.finish_cancel(jid)
        except Exception as exc:
            if self.store.get(jid).get("cancelRequested"):
                await self.finish_cancel(jid)
            else:
                clean = await self.cleanup(jid)
                message = str(exc)[:800] if isinstance(exc, ToolFailure) else "本地任务未完成，请检查服务和磁盘空间"
                self.store.update(jid, status=("NEEDS_REVIEW" if isinstance(exc, ReviewRequired) else "FAILED") if clean else "CANCELLING",
                                  message=message if clean else message + "；尚未确认下游停止，恢复服务后再次取消。")

    def check(self, state):
        if self.store.get(state["job_id"]).get("cancelRequested"):
            raise Cancelled()
        if time.time() >= state["deadline"]:
            raise ToolFailure("任务超过最长等待时间，已请求停止下游任务")

    def stage(self, state, status, stage, message):
        self.check(state)
        self.store.update(state["job_id"], status=status, stage=stage, message=message, engine="langgraph")

    async def pause(self, state, seconds=None):
        end = time.monotonic() + (self.settings.poll_interval if seconds is None else seconds)
        while time.monotonic() < end:
            self.check(state)
            await asyncio.sleep(min(0.1, max(0, end - time.monotonic())))
        self.check(state)

    async def read(self, state, tool, arguments):
        if tool not in self.READS:
            raise ToolFailure("只有任务状态和产物查询可以自动重试")
        for attempt in range(3):
            self.check(state)
            try:
                result = await self.tools.call(tool, arguments)
                self.check(state)
                if attempt:
                    self.store.update(state["job_id"], connectionRetry=0)
                return result
            except TemporaryToolFailure:
                if attempt == 2:
                    raise
                self.store.update(state["job_id"], connectionRetry=attempt + 1,
                                  message="进度连接暂时中断，正在恢复；不会重新生成图片或启动重建")
                await self.pause(state, min(5, self.settings.poll_interval * (2 ** attempt)))

    async def submit_once(self, state, field, tool, arguments, lookup, id_field):
        self.check(state)
        jid = state["job_id"]
        key = jid + {"referenceRequest": ":reference", "viewsRequest": ":views", "trainRequest": ":train"}[field]
        if self.store.get(jid).get(field):
            # Existing intent is evidence of a possible accepted mutation, never permission to repeat it.
            found = await self.tools.call(lookup, {"request_id": key})
            result = found.get("job")
            if not isinstance(result, dict):
                raise ToolFailure("尚未确认之前的提交结果，未重复提交")
        else:
            self.store.update(jid, **{field: key})
            result = await self.tools.call(tool, {**arguments, "request_id": key})
        self.check(state)
        return valid_id(result.get(id_field))

    async def preflight(self, state):
        self.check(state)
        if state["mode"] == "THREE_D":
            health = await self.tools.call("webscan_health", {})
            if not health.get("available") or not health.get("brush_available"):
                raise ToolFailure("请先启动本地 web-scan 并安装 Brush，再尝试 3D 重建")
        provider = await self.tools.call("creative_health", {})
        if not provider.get("configured"):
            raise ToolFailure("请在本地 .env 配置 ZHIPU_API_KEY 并重启 MCP 服务")
        self.check(state)
        self.store.update(state["job_id"], provider=provider)
        return {}

    async def reference(self, state):
        self.stage(state, "PLANNING", "planning", "正在整理物件设计")
        child = await self.submit_once(state, "referenceRequest", "creative_generate_reference",
                                       {"prompt": state["prompt"]}, "creative_get_request", "id")
        self.store.update(state["job_id"], creativeJobId=child)
        result = await self.wait_creative(state, child, False)
        ref = valid_id(result.get("asset_set_id"))
        self.store.update(state["job_id"], referenceSetId=ref, spec=result.get("spec", {}))
        if state["mode"] == "CONCEPT":
            self.stage(state, "CONCEPT_READY", "complete", "概念图已完成")
        return {"reference_id": ref}

    async def wait_creative(self, state, child, views):
        while True:
            result = await self.read(state, "creative_get_job", {"job_id": child})
            phase = result.get("stage", "")
            fields = dict(stage=phase, message=result.get("message", ""),
                          status="MULTIVIEW" if views else "PLANNING" if phase == "planning" else "REFERENCE")
            if "assets" in result:
                fields["viewAssets" if views else "referenceAssets"] = result["assets"]
            if result.get("review") is not None:
                fields["review"] = result["review"]
            self.store.update(state["job_id"], **fields)
            status = result.get("status")
            if status == "succeeded":
                return result
            if status == "failed":
                raise ToolFailure(result.get("message") or "图片生成失败")
            if status == "needs_review":
                raise ReviewRequired(result.get("message") or "视图需要检查")
            if status == "cancelled":
                raise Cancelled()
            if status not in {"queued", "running"}:
                raise ToolFailure("生图工具返回未知任务状态")
            await self.pause(state)

    async def multiview(self, state):
        self.stage(state, "MULTIVIEW", "multiview", "正在生成候选视图")
        child = await self.submit_once(state, "viewsRequest", "creative_generate_multiview",
                                       {"reference_id": state["reference_id"]}, "creative_get_request", "id")
        self.store.update(state["job_id"], creativeJobId=child)
        result = await self.wait_creative(state, child, True)
        views = valid_id(result.get("asset_set_id"))
        self.store.update(state["job_id"], viewsSetId=views)
        return {"views_id": views}

    async def import_views(self, state):
        self.stage(state, "IMPORTING", "importing", "正在导入本地重建服务")
        result = await self.tools.call("webscan_import_generated_views", {"asset_set_id": state["views_id"]})
        sid = valid_id(result.get("session_id"))
        self.store.update(state["job_id"], sessionId=sid)
        self.check(state)
        return {"session_id": sid}

    async def start_reconstruction(self, state):
        self.stage(state, "RECONSTRUCTING", "queued", "等待本地重建")
        jid = await self.submit_once(state, "trainRequest", "webscan_start_reconstruction",
                                     {"session_id": state["session_id"]}, "webscan_get_request", "job_id")
        self.store.update(state["job_id"], reconstructionId=jid)
        return {"reconstruction_id": jid}

    async def wait_reconstruction(self, state):
        while True:
            result = await self.read(state, "webscan_get_status", {"job_id": state["reconstruction_id"]})
            self.store.update(state["job_id"], stage=result.get("stage", ""), message=result.get("message", ""), reconstruction=result)
            status = result.get("status")
            if status == "succeeded":
                return {}
            if status == "failed":
                raise ReviewRequired("本地重建失败：" + (result.get("message") or "视图几何不一致"))
            if status == "cancelled":
                raise Cancelled()
            if status not in {"queued", "running"}:
                raise ToolFailure("重建工具返回未知任务状态")
            await self.pause(state)

    async def validate_result(self, state):
        self.stage(state, "VALIDATING", "export", "正在检查 3D 产物")
        result = await self.read(state, "webscan_get_result", {"job_id": state["reconstruction_id"]})
        if not any(isinstance(f, dict) and f.get("name") == "gaussians.ply" and f.get("url") for f in result.get("files", [])):
            raise ToolFailure("缺少有效的 Gaussian PLY 产物")
        self.check(state)
        self.store.update(state["job_id"], result=result, status="READY", stage="complete", message="3D 展品已生成，可旋转查看并下载")
        return {}

    async def cleanup(self, jid):
        complete = True
        job = self.store.get(jid)
        for field in ("referenceRequest", "viewsRequest", "trainRequest"):
            if not job.get(field):
                continue
            train = field == "trainRequest"
            try:
                found = await self.tools.call("webscan_get_request" if train else "creative_get_request", {"request_id": job[field]})
                if "job" not in found:
                    raise ToolFailure("查询返回格式异常")
                if found["job"] is not None:
                    child = valid_id(found["job"].get("job_id" if train else "id"))
                    await self.tools.call("webscan_cancel_reconstruction" if train else "creative_cancel_job", {"job_id": child})
            except Exception:
                complete = False
        return complete

    async def finish_cancel(self, jid):
        if self.store.get(jid)["status"] in TERMINAL:
            return
        clean = await self.cleanup(jid)
        self.store.update(jid, status="CANCELLED" if clean else "CANCELLING",
                          message="任务已取消；已发送的模型请求可能仍由供应商完成。" if clean else
                          "尚未确认下游任务停止。请恢复 MCP 服务后再次取消。")
