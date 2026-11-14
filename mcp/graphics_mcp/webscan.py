import json
import threading

import httpx

from .gaussian_ply import PLY_PREFIX_BYTES, gaussian_vertex_count
from .settings import Settings
from .store import Store, valid_id

RECONSTRUCTION_CONFIG = {"backend": "brush", "steps": 3000, "resolution": 512, "max_points": 150000,
                         "camera_mode": "auto", "evaluation_mode": "all"}
TEMPORARY_STATUS_CODES = {408, 429, 500, 502, 503, 504}


class TemporaryWebscanError(ValueError):
    """Temporary availability failure; callers must not replay mutations."""


class Webscan:
    def __init__(self, settings: Settings, store: Store, client=None):
        self.url, self.store = settings.webscan_url, store
        self.client = client or httpx.Client(base_url=self.url, timeout=30, follow_redirects=False)
        self.lock = threading.RLock()

    def request(self, method, path, **kwargs):
        try:
            response = self.client.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            error = TemporaryWebscanError if exc.response.status_code in TEMPORARY_STATUS_CODES else ValueError
            raise error(f"web-scan 返回 HTTP {exc.response.status_code}，请检查本地服务及输入照片") from None
        except httpx.RequestError:
            raise TemporaryWebscanError("无法连接本地 web-scan，请启动服务后重试；不会切换云端 GPU") from None
        except ValueError:
            raise ValueError("web-scan 返回内容不是有效 JSON，请检查本地服务") from None

    def health(self):
        data = self.request("GET", "/api/health")
        return {"available": data.get("status") == "ok", "brush_available": bool(data.get("brush_available")),
                "min_images": data.get("min_capture_frames", 12), "url": self.url}

    def import_views(self, asset_set_id):
        valid_id(asset_set_id)
        manifest = self.store.manifest(asset_set_id)
        images = manifest.get("assets", [])
        if (manifest.get("kind") != "multiview" or not isinstance(images, list) or len(images) != 12
                or not all(isinstance(a, dict) and isinstance(a.get("sha256"), str) for a in images)
                or len({a["sha256"] for a in images}) != 12):
            raise ValueError("重建需要 12 张不同的候选视图")
        if manifest.get("review", {}).get("consistent") is not True:
            raise ValueError("候选视图尚未通过外观一致性检查")
        with self.lock:
            key = "import:" + asset_set_id
            linked = self.store.link(key)
            capture = None
            if linked:
                session_id = valid_id(linked["session_id"])
                capture, orders = self._imported_views(session_id, asset_set_id, images)
                if linked.get("complete") and capture["status"] != "ready":
                    raise ValueError("已导入的采集不再处于冻结状态，未开始重建")
            if capture is None or capture["status"] != "ready":
                # Validate every local file before any upload and send these same bytes.
                snapshots = [self.store.read_candidate(asset_set_id, i, asset) for i, asset in enumerate(images)]
                if capture is None:
                    session = self.request("POST", "/api/sessions", json={"name": manifest["spec"]["title"][:80]})
                    session_id = valid_id(session["id"])
                    self.store.link(key, {"session_id": session_id, "complete": False})
                    capture, orders = self._imported_views(session_id, asset_set_id, images)
                if capture["status"] == "capturing":
                    for index, raw in enumerate(snapshots):
                        if index in orders:
                            continue  # A prior attempt already uploaded this view.
                        metadata = {"source": "ai-generated", "order": index, "asset_set_id": asset_set_id,
                                    "file_sha256": images[index]["file_sha256"]}
                        receipt = self.request("POST", f"/api/sessions/{session_id}/frames",
                                               files={"file": (f"{index:02d}.png", raw, "image/png")},
                                               data={"metadata": json.dumps(metadata)})
                        if not isinstance(receipt, dict) or receipt.get("duplicate") is not False:
                            raise ValueError("采集未确认保存不同的候选视图，未开始重建")
                    self.request("POST", f"/api/sessions/{session_id}/finalize")
                    capture, orders = self._imported_views(session_id, asset_set_id, images)
            if capture["status"] != "ready" or len(orders) != len(images):
                raise ValueError("采集尚未冻结本次全部候选视图，未开始重建")
            return self.store.link(key, {"session_id": session_id, "asset_set_id": asset_set_id,
                                         "complete": True, "images": len(orders)})

    def _imported_views(self, session_id, asset_set_id, images):
        capture = self.request("GET", f"/api/sessions/{session_id}")
        if (not isinstance(capture, dict) or capture.get("id") != session_id or capture.get("kind") != "photos"
                or capture.get("status") not in ("capturing", "ready") or not isinstance(capture.get("frames"), list)):
            raise ValueError("采集身份或状态不匹配，未开始重建")
        orders = set()
        for frame in capture["frames"]:
            metadata = frame.get("metadata") if isinstance(frame, dict) else None
            index = metadata.get("order") if isinstance(metadata, dict) else None
            if (type(index) is not int or not 0 <= index < len(images) or index in orders
                    or frame.get("session_id") != session_id or metadata.get("source") != "ai-generated"
                    or metadata.get("asset_set_id") != asset_set_id or not images[index].get("file_sha256")
                    or metadata.get("file_sha256") != images[index]["file_sha256"]):
                raise ValueError("采集中的视图与本次候选图不匹配，未开始重建")
            orders.add(index)
        return capture, orders

    def start(self, session_id, request_id):
        valid_id(session_id)
        if not 1 <= len(request_id) <= 160:
            raise ValueError("无效请求标识")
        config = dict(RECONSTRUCTION_CONFIG)
        with self.lock:
            key = "train:" + request_id
            linked = self.store.link(key)
            if linked and linked["session_id"] != session_id:
                raise ValueError("同一请求标识不能用于不同采集")
            if linked:
                # A durable submit intent means the POST may already have happened.
                # Repeated tool calls may only reconcile it, never send another POST.
                return self.by_request(request_id)
            session = self.request("GET", f"/api/sessions/{session_id}")
            existing = self.matching_job(session, session_id, config)
            if existing:
                jid = existing["id"]
            else:
                if any(j.get("status") in {"queued", "running"} for j in session["jobs"]):
                    raise ValueError("该采集已有其他参数的重建任务运行中，未启动新的训练")
                self.store.link(key, {"session_id": session_id, "config": config})
                job = self.request("POST", "/api/jobs", json={"session_id": session_id, **config})
                if not isinstance(job, dict) or job.get("session_id") != session_id or job.get("config") != config:
                    raise ValueError("重建返回的采集或参数不匹配，提交结果仍未知")
                jid = job["id"]
            return self.store.link(key, {"session_id": session_id, "config": config, "job_id": valid_id(jid)})

    @staticmethod
    def matching_job(session, session_id, config):
        if not isinstance(session, dict) or session.get("id") != session_id or not isinstance(session.get("jobs"), list):
            raise ValueError("重建采集任务列表无效，未选择下游任务")
        jobs = session["jobs"]
        if any(not isinstance(job, dict) for job in jobs):
            raise ValueError("重建采集任务列表无效，未选择下游任务")
        matches = [job for job in jobs if job.get("session_id") == session_id and job.get("config") == config]
        if len(matches) > 1:
            raise ValueError("存在多个匹配的重建任务，提交结果仍未知；不会自动选择或取消其中一个")
        if matches:
            valid_id(matches[0]["id"])
            return matches[0]
        return None

    def status(self, job_id):
        requested = valid_id(job_id)
        data = self.request("GET", "/api/jobs/" + requested)
        if not isinstance(data, dict) or data.get("id") != requested:
            raise ValueError("重建进度的任务标识不匹配")
        session_id = data.get("session_id")
        if not isinstance(session_id, str):
            raise ValueError("重建进度缺少有效的采集标识")
        valid_id(session_id)
        return {key: data.get(key) for key in ("id", "session_id", "status", "stage", "progress", "message", "report")}

    def by_request(self, request_id):
        with self.lock:
            key = "train:" + request_id
            linked = self.store.link(key)
            if linked and not linked.get("job_id"):
                sid = valid_id(linked["session_id"])
                # Older intents used this same fixed preset without storing it.
                config = linked.get("config", RECONSTRUCTION_CONFIG)
                session = self.request("GET", f"/api/sessions/{sid}")
                job = self.matching_job(session, sid, config)
                if job is None:
                    raise ValueError("重建提交结果仍未知，暂时无法确认下游任务已停止")
                linked = self.store.link(key, {**linked, "config": config, "job_id": valid_id(job["id"])})
            return linked

    def cancel(self, job_id):
        requested = valid_id(job_id)
        data = self.request("POST", f"/api/jobs/{requested}/cancel")
        # The local service may return a terminal state when a cancellation
        # races with completion.  Both the ID and state must still be part of
        # the acknowledgement; otherwise callers could confirm a different
        # job or an unrecognised state by accident.
        if not isinstance(data, dict) or data.get("id") != requested:
            raise ValueError("web-scan 取消回执的任务标识不匹配")
        status = data.get("status")
        if not isinstance(status, str) or status not in {"cancelled", "failed", "succeeded"}:
            raise ValueError("web-scan 取消回执状态无效")
        return {"id": requested, "status": status}

    def result(self, job_id):
        status = self.status(job_id)
        if status["status"] != "succeeded":
            raise ValueError("真实重建尚未成功，没有可展示的 3D 成品")
        manifest = self.request("GET", f"/api/jobs/{job_id}/artifacts")
        if not isinstance(manifest, dict) or manifest.get("job_id") != job_id:
            raise ValueError("重建产物清单的任务标识不匹配")
        listed = manifest.get("files")
        if not isinstance(listed, list):
            raise ValueError("重建产物清单格式无效")
        files = {}
        base = f"/api/jobs/{job_id}/artifacts/"
        for item in listed:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ValueError("重建产物清单格式无效")
            name, size = item["name"], item.get("bytes")
            if name not in {"gaussians.ply", "preview.splat", "report.json", "cameras.json"}:
                continue
            if name in files or type(size) is not int or size < 0:
                raise ValueError("重建产物清单大小或文件名无效")
            files[name] = {"name": name, "bytes": size, "url": f"{self.url}{base}{name}?download=true"}
        ply_size = files.get("gaussians.ply", {}).get("bytes", 0)
        if not ply_size:
            raise ValueError("任务报告成功，但缺少有效的 Gaussian PLY 产物")
        count = self._verify_ply(base + "gaussians.ply", ply_size)
        return {"job_id": job_id, "session_id": status["session_id"],
                "viewer_url": f"{self.url}/?p={status['session_id']}&j={job_id}",
                "files": [f for f in files.values() if f["bytes"]], "ply_vertex_count": count,
                "report": status.get("report"), "backend": "brush"}

    def _verify_ply(self, path, size):
        length = min(PLY_PREFIX_BYTES, size)
        try:
            with self.client.stream("GET", path, follow_redirects=False, headers={
                    "Range": f"bytes=0-{PLY_PREFIX_BYTES - 1}", "Accept-Encoding": "identity"}) as response:
                if response.status_code in TEMPORARY_STATUS_CODES:
                    raise TemporaryWebscanError(f"Gaussian PLY 暂不可读：HTTP {response.status_code}")
                if (response.status_code != 206 or response.headers.get("content-encoding", "identity") != "identity"
                        or response.headers.get("content-range") != f"bytes 0-{length - 1}/{size}"
                        or response.headers.get("content-length") != str(length)):
                    raise ValueError("Gaussian PLY 无法读取有效的文件头，或大小与清单不符")
                prefix = next(response.iter_raw(chunk_size=length), b"")
        except httpx.RequestError:
            raise TemporaryWebscanError("无法读取本地 Gaussian PLY，请检查 web-scan 服务") from None
        if len(prefix) != length:
            raise ValueError("Gaussian PLY 文件头读取不完整")
        return gaussian_vertex_count(prefix, size)
