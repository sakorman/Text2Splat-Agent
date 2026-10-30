import json
import threading

import httpx

from .settings import Settings
from .store import Store, valid_id


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
            error = TemporaryWebscanError if exc.response.status_code in {408, 429, 500, 502, 503, 504} else ValueError
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
        if manifest.get("kind") != "multiview" or len(images) != 12 or len({a["sha256"] for a in images}) != 12:
            raise ValueError("重建需要 12 张不同的候选视图")
        if manifest.get("review", {}).get("consistent") is not True:
            raise ValueError("候选视图尚未通过外观一致性检查")
        with self.lock:
            key = "import:" + asset_set_id
            linked = self.store.link(key)
            if linked and linked.get("complete"):
                return linked
            if linked:
                session_id = linked["session_id"]
            else:
                session = self.request("POST", "/api/sessions", json={"name": manifest["spec"]["title"][:80]})
                session_id = valid_id(session["id"])
                self.store.link(key, {"session_id": session_id, "complete": False})
            session = self.request("GET", f"/api/sessions/{session_id}")
            if session["status"] != "ready":
                for index, asset in enumerate(images):
                    name = f"{index:02d}.png"
                    if asset["name"] != name:
                        raise ValueError("图片清单格式错误")
                    raw = (self.store.directory(asset_set_id) / name).read_bytes()
                    self.request("POST", f"/api/sessions/{session_id}/frames",
                                 files={"file": (name, raw, "image/png")},
                                 data={"metadata": json.dumps({"source": "ai-generated", "order": index})})
                self.request("POST", f"/api/sessions/{session_id}/finalize")
            return self.store.link(key, {"session_id": session_id, "complete": True, "images": 12})

    def start(self, session_id, request_id):
        valid_id(session_id)
        if not 1 <= len(request_id) <= 160:
            raise ValueError("无效请求标识")
        config = {"backend": "brush", "steps": 3000, "resolution": 512, "max_points": 150000,
                  "camera_mode": "auto", "evaluation_mode": "all"}
        with self.lock:
            key = "train:" + request_id
            linked = self.store.link(key)
            if linked and linked["session_id"] != session_id:
                raise ValueError("同一请求标识不能用于不同采集")
            if linked and linked.get("job_id"):
                return linked
            session = self.request("GET", f"/api/sessions/{session_id}")
            # Recover a lost POST response without accidentally launching training twice.
            existing = next((j for j in session.get("jobs", []) if j.get("config") == config), None)
            if existing:
                jid = existing["id"]
            else:
                self.store.link(key, {"session_id": session_id})
                job = self.request("POST", "/api/jobs", json={"session_id": session_id, **config})
                jid = job["id"]
            return self.store.link(key, {"session_id": session_id, "job_id": valid_id(jid)})

    def status(self, job_id):
        data = self.request("GET", "/api/jobs/" + valid_id(job_id))
        return {key: data.get(key) for key in ("id", "session_id", "status", "stage", "progress", "message", "report")}

    def by_request(self, request_id):
        linked = self.store.link("train:" + request_id)
        if linked and not linked.get("job_id"):
            session = self.request("GET", f"/api/sessions/{valid_id(linked['session_id'])}")
            jobs = session.get("jobs", [])
            if jobs:
                linked = {**linked, "job_id": valid_id(jobs[0]["id"])}
            else:
                raise ValueError("重建提交结果仍未知，暂时无法确认下游任务已停止")
        return linked

    def cancel(self, job_id):
        data = self.request("POST", f"/api/jobs/{valid_id(job_id)}/cancel")
        return {"id": data["id"], "status": data["status"]}

    def result(self, job_id):
        status = self.status(job_id)
        if status["status"] != "succeeded":
            raise ValueError("真实重建尚未成功，没有可展示的 3D 成品")
        listed = self.request("GET", f"/api/jobs/{job_id}/artifacts")["files"]
        files = [{"name": f["name"], "bytes": f["bytes"],
                  "url": f"{self.url}/api/jobs/{job_id}/artifacts/{f['name']}?download=true"}
                 for f in listed if f["name"] in {"gaussians.ply", "preview.splat", "report.json", "cameras.json"} and f["bytes"] > 0]
        if not any(f["name"] == "gaussians.ply" for f in files):
            raise ValueError("任务报告成功，但缺少有效的 Gaussian PLY 产物")
        return {"viewer_url": f"{self.url}/?p={valid_id(status['session_id'])}&j={job_id}",
                "files": files, "report": status.get("report"), "backend": "brush"}
