"""A bounded ReAct model/tool loop, with deterministic guards around MCP actions."""
import asyncio
import json
import logging
import time
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from pydantic import ValidationError

from .react import ACTIONS, ReActModel
from .context import prepare_context
from .memory import MemoryStore
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
    reconstruction_done: bool
    verified_result: dict
    messages: Annotated[list[AnyMessage], add_messages]
    rounds: int
    failure: dict
    finished: bool
    recalled_memories: list[dict]
    context_summary: dict
    context_stats: dict


class Cancelled(Exception):
    pass


class ReviewRequired(ToolFailure):
    pass


class Workflow:
    READS = {"creative_get_job", "webscan_get_status", "webscan_get_result"}
    MAX_ROUNDS = 12

    def __init__(self, settings, store, tools, checkpointer, model=None):
        self.settings, self.store, self.tools = settings, store, tools
        self.model = model if model is not None else ReActModel(settings)
        self.memory = MemoryStore(settings.data_dir)
        self.memory_available = True
        builder = StateGraph(State)
        nodes = {"preflight": self.preflight, "model": self.decide, "tools": self.act, "finish": self.finish}
        for name, fn in nodes.items():
            builder.add_node(name, fn)  # No graph-level RetryPolicy for side-effecting nodes.
        builder.add_edge(START, "preflight")
        builder.add_edge("preflight", "model")
        builder.add_conditional_edges("model", lambda s: "tools" if s["messages"][-1].tool_calls else "finish")
        builder.add_conditional_edges("tools", lambda s: "end" if s.get("finished") else "model",
                                      {"end": END, "model": "model"})
        builder.add_edge("finish", END)
        self.graph = builder.compile(checkpointer=checkpointer, name="text2splat-react")
        self.queue = asyncio.Queue()
        self.worker = None
        self.active = None

    async def start(self):
        # Preserve previous restart semantics. Checkpoints are inspectable but an
        # incomplete side effect is never blindly replayed with graph.ainvoke(None).
        self.memory_call(lambda: self.memory.list(1), [])
        for job in self.store.list():
            self.memory_call(lambda: self.memory.record_job(job), False)
            if job["status"] not in TERMINAL:
                self.queue.put_nowait(("recover", job["id"]))
        self.worker = asyncio.create_task(self._consume(), name="text2splat-graph-worker")

    def memory_call(self, operation, default):
        try:
            result = operation()
            self.memory_available = True
            return result
        except Exception:
            # A memory index failure must not invalidate generation or kill the worker.
            self.memory_available = False
            logging.getLogger(__name__).warning("Local memory unavailable; continuing without this memory operation")
            return default

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
        self.memory_call(lambda: self.memory.list(1), [])
        result = {"engine": "langgraph", "tool_framework": "langchain", "agent_loop": "react",
                  "decision_model": "glm-4.7-flash", "decision_configured": bool(self.settings.zhipu_key),
                  "max_decisions": self.MAX_ROUNDS}
        result["memory"] = {"long_term": "local_sqlite", "available": self.memory_available,
                            "retrieval": "lexical", "context_budget_characters": 12000,
                            "compression": "structured_summary_and_recent_exchanges"}
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
                self.memory_call(lambda: self.memory.record_job(self.store.get(jid)), False)
                self.active = None
                self.queue.task_done()

    async def run(self, jid):
        job = self.store.get(jid)
        self.memory_call(lambda: self.memory.capture_explicit_preference(job["prompt"], jid), None)
        recalled = self.memory_call(lambda: self.memory.recall(job["prompt"], jid), [])
        self.store.update(jid, memorySources=[m["id"] for m in recalled])
        state = State(job_id=jid, prompt=job["prompt"], mode=job["mode"],
                      deadline=time.time() + self.settings.job_timeout, rounds=0, recalled_memories=recalled,
                      messages=[HumanMessage(json.dumps({"description": job["prompt"], "mode": job["mode"]}, ensure_ascii=False))])
        try:
            await self.graph.ainvoke(state, {"configurable": {"thread_id": jid},
                                            "recursion_limit": 2 * self.MAX_ROUNDS + 5}, durability="sync")
        except Cancelled:
            await self.finish_cancel(jid)
        except Exception as exc:
            if self.store.get(jid).get("cancelRequested"):
                await self.finish_cancel(jid)
            else:
                await self.fail(jid, str(exc)[:800] if isinstance(exc, ToolFailure) else
                                "本地任务未完成，请检查服务和磁盘空间")

    async def fail(self, jid, message):
        job = self.store.get(jid)
        failure = job.get("agentFailure")
        clean = job.get("cleanupConfirmed", False) if failure else await self.cleanup(jid)
        status = failure["status"] if failure else "FAILED"
        message = failure["message"] if failure else message
        self.store.update(jid, status=status if clean else "CANCELLING", message=message if clean else
                          message + "；尚未确认下游停止，恢复服务后再次取消。")

    def allowed_actions(self, state):
        allowed = ["inspect_task", "stop"]
        if state.get("failure"):
            return allowed
        if not state.get("reference_id"):
            allowed.append("generate_reference")
        elif state["mode"] == "CONCEPT":
            allowed.append("finish")
        elif not state.get("views_id"):
            allowed.append("generate_multiview")
        elif not state.get("session_id"):
            allowed.append("import_views")
        elif not state.get("reconstruction_done"):
            allowed.append("reconstruct")
        elif not state.get("verified_result"):
            allowed.append("validate_result")
        else:
            allowed.append("finish")
        return allowed

    async def decide(self, state):
        self.check(state)
        rounds = state["rounds"] + 1
        if rounds > self.MAX_ROUNDS:
            raise ToolFailure("Agent 已达到 12 轮决策上限，未继续调用模型或工具")
        snapshot = {**self.observation({**state, "rounds": rounds}),
                    "attempted_actions": self.store.get(state["job_id"]).get("agentActions", []),
                    "generation_retry_allowed": False}
        context = prepare_context(state["messages"], snapshot, state.get("recalled_memories", []))
        self.store.update(state["job_id"], agentLoop="react", agentRounds=rounds,
                          contextStats=context.stats, message="正在根据当前结果决定下一步")
        task = asyncio.create_task(self.model.ainvoke(context.messages))
        until = time.monotonic() + self.settings.model_timeout
        try:
            while not task.done():
                self.check(state)
                if time.monotonic() >= until:
                    raise ToolFailure("Agent 决策超时，未自动重试")
                await asyncio.wait({task}, timeout=.1)
            reply = task.result()
            self.check(state)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if not isinstance(reply, AIMessage) or reply.invalid_tool_calls:
            raise ToolFailure("Agent 返回无效工具调用，未执行工具")
        ids = [c.get("id") for c in reply.tool_calls]
        if any(not isinstance(i, str) or not i or len(i) > 160 for i in ids) or len(set(ids)) != len(ids):
            raise ToolFailure("Agent 工具调用标识无效，未执行工具")
        return {"rounds": rounds, "messages": [AIMessage(content="", tool_calls=reply.tool_calls)],
                "context_summary": context.summary, "context_stats": context.stats}

    def observation(self, state):
        job = self.store.get(state["job_id"])
        return {"mode": state["mode"], "allowed_actions": self.allowed_actions(state),
                "remaining_decisions": self.MAX_ROUNDS - state["rounds"],
                "reference_ready": bool(state.get("reference_id")), "views_reviewed": bool(state.get("views_id")),
                "imported": bool(state.get("session_id")), "reconstruction_succeeded": bool(state.get("reconstruction_done")),
                "artifact_verified": bool(state.get("verified_result")),
                "spec": job.get("spec"), "review": job.get("review"), "failure": state.get("failure")}

    async def act(self, state):
        self.check(state)
        calls = state["messages"][-1].tool_calls
        if len(calls) != 1:
            return {"messages": [ToolMessage(tool_call_id=c["id"], content=json.dumps({
                "ok": False, "error": "每轮只允许一个工具；本轮没有执行任何工具"}, ensure_ascii=False)) for c in calls]}
        call = calls[0]
        name = call["name"]
        update = {}
        error = None
        if name not in self.allowed_actions(state):
            error = "工具未知、前置条件未满足或已执行；未调用下游"
        else:
            try:
                args = ACTIONS[name][1].model_validate(call["args"])
            except ValidationError:
                error = "工具参数无效；不能指定其他任务 ID、路径、供应商或费用"
            else:
                if name == "generate_reference" and args.design_hint:
                    enriched = state["prompt"] + "\n补充渲染建议（以原描述为准）：" + args.design_hint
                    if len(enriched) > 2000:
                        error = "描述和补充建议合计超过 2000 字；请缩短或省略 design_hint"
        if not error:
            # Only valid actions enter the side-effect boundary. Even if a graph
            # node is manually reentered, the job journal forbids a second attempt.
            try:
                if name not in {"inspect_task", "stop", "finish"}:
                    job = self.store.get(state["job_id"])
                    attempts = job.get("agentActions", [])
                    if name in attempts:
                        raise ToolFailure("该操作已提交过，未重复执行")
                    self.store.update(state["job_id"], agentActions=[*attempts, name])
                if name == "generate_reference":
                    update = await self.reference(state, args.design_hint)
                elif name == "generate_multiview":
                    update = await self.multiview(state)
                elif name == "import_views":
                    update = await self.import_views(state)
                elif name == "reconstruct":
                    update = await self.start_reconstruction(state)
                    await self.wait_reconstruction({**state, **update})
                    update["reconstruction_done"] = True
                elif name == "validate_result":
                    update = await self.validate_result(state)
                elif name == "finish":
                    update = await self.finish(state)
                elif name == "stop":
                    await self.fail(state["job_id"], "Agent 决定停止，尚未生成经过验证的结果")
                    update = {"finished": True}
            except ToolFailure as exc:
                failure = {"status": "NEEDS_REVIEW" if isinstance(exc, ReviewRequired) else "FAILED", "message": str(exc)[:800]}
                clean = await self.cleanup(state["job_id"])
                self.store.update(state["job_id"], agentFailure=failure, cleanupConfirmed=clean)
                update = {"failure": failure}
                error = failure["message"]
        self.check(state)
        data = {"ok": error is None, "observation": self.observation({**state, **update})}
        if error:
            data["error"] = error
        return {**update, "messages": [ToolMessage(tool_call_id=call["id"], name=name,
                                                   content=json.dumps(data, ensure_ascii=False))]}

    async def finish(self, state):
        self.check(state)
        if not state.get("failure") and state["mode"] == "CONCEPT" and state.get("reference_id"):
            self.store.update(state["job_id"], status="CONCEPT_READY", stage="complete", message="概念图已完成")
        elif not state.get("failure") and state.get("reconstruction_done") and state.get("verified_result"):
            self.store.update(state["job_id"], result=state["verified_result"], status="READY", stage="complete",
                              message="3D 展品已生成，可旋转查看并下载")
        else:
            await self.fail(state["job_id"], "Agent 提前结束，尚未完成产物验证")
        return {"finished": True}

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

    async def reference(self, state, design_hint=""):
        self.stage(state, "PLANNING", "planning", "正在整理物件设计")
        child = await self.submit_once(state, "referenceRequest", "creative_generate_reference",
                                       {"prompt": state["prompt"] + ("\n补充渲染建议（以原描述为准）：" + design_hint if design_hint else "")},
                                       "creative_get_request", "id")
        self.store.update(state["job_id"], creativeJobId=child)
        result = await self.wait_creative(state, child, False)
        ref = valid_id(result.get("asset_set_id"))
        self.store.update(state["job_id"], referenceSetId=ref, spec=result.get("spec", {}))
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
        if not any(isinstance(f, dict) and f.get("name") == "gaussians.ply" and f.get("url")
                   and isinstance(f.get("bytes"), (int, float)) and f["bytes"] > 0 for f in result.get("files", [])):
            raise ToolFailure("缺少有效的 Gaussian PLY 产物")
        self.check(state)
        return {"verified_result": result}

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
