import threading

from .providers import ANGLES, ProviderError, Providers
from .settings import Settings
from .store import Store


class Cancelled(Exception):
    pass


class Creative:
    def __init__(self, settings: Settings, store: Store, providers=None):
        self.settings, self.store = settings, store
        self.providers = providers or Providers(settings)
        self.stop = threading.Event()

    def health(self):
        return {"configured": bool(self.settings.zhipu_key), "image_provider": self.settings.image_provider,
                "paid_enabled": self.settings.allow_paid, "reference_conditioned": self.settings.image_provider == "seedream",
                "text_model": "glm-4.7-flash", "vision_model": "glm-4.6v-flash",
                "image_model": "cogview-3-flash" if self.settings.image_provider == "cogview" else "doubao-seedream-4-0-250828"}

    def reference(self, prompt, request_id):
        self.settings.check_generation()
        if not isinstance(prompt, str) or not 2 <= len(prompt.strip()) <= 2000:
            raise ValueError("请提供 2–2000 字的物件描述")
        return self.store.submit(request_id, "reference", {"prompt": prompt.strip()})

    def multiview(self, reference_id, request_id):
        self.settings.check_generation()
        manifest = self.store.manifest(reference_id)
        if manifest["kind"] != "reference":
            raise ValueError("需要已完成的参考图集合")
        return self.store.submit(request_id, "multiview", {"reference_id": reference_id})

    def checkpoint(self, jid):
        if self.stop.is_set() or self.store.get(jid)["status"] == "cancelled":
            raise Cancelled()

    def execute(self, jid, kind, payload):
        try:
            self.checkpoint(jid)
            if kind == "reference":
                self.store.update(jid, stage="planning", message="正在整理物件设计")
                spec = self.providers.plan(payload["prompt"])
                self.checkpoint(jid)
                self.store.update(jid, stage="reference", message="正在生成参考图", spec=spec)
                raw = self.providers.image(spec["object_prompt"] + "。单张完整物件摄影，三分之四前视角，物体完整入镜，画面四周留白。")
                self.checkpoint(jid)
                asset = self.store.save_image(jid, 0, raw)
                manifest = {"kind": kind, "spec": spec, "assets": [asset], "provider": self.settings.image_provider}
            else:
                reference = self.store.manifest(payload["reference_id"])
                spec = reference["spec"]
                ref_raw = (self.store.directory(payload["reference_id"]) / reference["assets"][0]["name"]).read_bytes()
                assets, views = [], []
                for index, angle in enumerate(ANGLES):
                    self.checkpoint(jid)
                    self.store.update(jid, stage="multiview", message=f"正在生成视图 {index + 1}/12", assets=assets)
                    prompt = (spec["object_prompt"] + f"。拍摄同一个物件，{angle}。仅改变相机位置，保持形状、纹理、"
                              "颜色、光照不变。单张图片，不要拼图。物体完整入镜、居中、四周留白，无道具。")
                    raw = self.providers.image(prompt, ref_raw)
                    self.checkpoint(jid)
                    assets.append(self.store.save_image(jid, index, raw))
                    views.append(raw)
                    self.store.update(jid, assets=assets)
                if len({a["sha256"] for a in assets}) != 12:
                    self.store.update(jid, status="needs_review", message="视图存在重复，未开始重建", assets=assets)
                    return
                self.checkpoint(jid)
                self.store.update(jid, stage="review", message="正在检查物件外观一致性")
                review = self.providers.review(ref_raw, views)
                self.checkpoint(jid)
                manifest = {"kind": kind, "spec": spec, "assets": assets, "review": review,
                            "provider": self.settings.image_provider, "reference_id": payload["reference_id"],
                            "geometry_verified": False, "reference_conditioned": self.settings.image_provider == "seedream"}
                self.store.save_manifest(jid, manifest)
                if not review["consistent"]:
                    self.store.update(jid, status="needs_review", stage="review", message=review["reason"], assets=assets, review=review)
                    return
            self.checkpoint(jid)
            self.store.save_manifest(jid, manifest)
            self.store.update(jid, status="succeeded", stage="complete", message="图片已生成", asset_set_id=jid,
                              assets=manifest["assets"], spec=spec, review=manifest.get("review"))
        except Cancelled:
            self.store.update(jid, status="cancelled", message="已取消，后续生成不会继续")
        except (ProviderError, ValueError) as exc:
            self.store.update(jid, status="failed", message=str(exc)[:600])
        except Exception:
            # Provider URLs, request headers and response bodies must not reach logs/UI.
            self.store.update(jid, status="failed", message="本地图片处理失败，请检查图片格式及服务配置")

    def start(self):
        self.store.recover()
        def run():
            while not self.stop.is_set():
                task = self.store.claim()
                if task:
                    self.execute(*task)
                else:
                    self.stop.wait(0.25)
        self.thread = threading.Thread(target=run, name="creative-worker", daemon=True)
        self.thread.start()

    def cancel(self, job_id):
        return self.store.update(job_id, status="cancelled", message="已停止后续步骤；已发送的模型请求可能仍由供应商完成")
