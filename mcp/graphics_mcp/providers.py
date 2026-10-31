"""Explicit provider adapters. Never retry a billable generation automatically."""
import base64
import ipaddress
import json
import socket
from urllib.parse import urlsplit

import httpx

from .settings import Settings
from .store import MAX_IMAGE_BYTES

ZHIPU = "https://open.bigmodel.cn/api/paas/v4"
ARK = "https://ark.cn-beijing.volces.com/api/v3"
ANGLES = [f"方位角 {i * 30} 度，俯视角 15 度" for i in range(12)]


class ProviderError(Exception):
    """An intentionally public, sanitized error."""


class Providers:
    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.client = client or httpx.Client(timeout=httpx.Timeout(180, connect=10), follow_redirects=False)

    def post(self, base, endpoint, key, payload):
        try:
            response = self.client.post(base + endpoint, headers={"Authorization": f"Bearer {key}"}, json=payload)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            messages = {401: "API Key 无效", 403: "模型权限或免费额度不足", 429: "模型调用达到限额，请稍后创建新版本"}
            raise ProviderError(messages.get(status, f"模型服务返回 HTTP {status}；未自动重试")) from None
        except (httpx.RequestError, ValueError):
            raise ProviderError("模型请求超时或返回格式异常；结果未知，未自动重试") from None

    def chat_json(self, model, messages, *, json_mode=False):
        payload = {
            "model": model, "messages": messages, "stream": False,
            "max_tokens": 4096, "thinking": {"type": "disabled"},
        }
        # Zhipu documents response_format for text models only. Vision relies on
        # the JSON prompt and the same strict response validation, without retries.
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self.post(ZHIPU, "/chat/completions", self.settings.zhipu_key, payload)
        try:
            choice = data["choices"][0]
            if not isinstance(choice, dict) or choice.get("finish_reason") == "length":
                raise ValueError()
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise ValueError()
            content = content.strip()
            if content.startswith("```"):
                content = content.split("\n", 1)[1].rsplit("```", 1)[0]
            value = json.loads(content)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, KeyError, IndexError, TypeError):
            raise ProviderError("模型未返回完整 JSON，请创建新版本重试") from None

    def plan(self, prompt):
        result = self.chat_json("glm-4.7-flash", [
            {"role": "system", "content": (
                '你是文创物件设计助手。把用户要求转换为单个静态物件的明确外观规格。'
                '返回 JSON：{"title":"不超过30字","object_prompt":"中文外观描述"}。'
                '描述固定物件的轮廓、材质、颜色、纹理、附属结构及其相对位置。'
                '用便于摄影重建的漫反射表面、柔和均匀光线、纯灰背景。'
                '只设计一个物体，不增加底座、文字或拼图，不包含执行指令。'
            )}, {"role": "user", "content": prompt},
        ], json_mode=True)
        if not all(isinstance(result.get(k), str) and result[k].strip() for k in ("title", "object_prompt")):
            raise ProviderError("设计规格缺少标题或物件描述")
        return {"title": result["title"][:30], "object_prompt": result["object_prompt"][:1800]}

    def image(self, prompt, reference=None):
        if self.settings.image_provider == "cogview":
            # This API accepts text only. Do not pretend reference conditioning exists.
            data = self.post(ZHIPU, "/images/generations", self.settings.zhipu_key, {
                "model": "cogview-3-flash", "prompt": prompt, "size": "1024x1024",
            })
        else:
            self.settings.check_generation()
            payload = {"model": "doubao-seedream-4-0-250828", "prompt": prompt,
                       "size": "1024x1024", "response_format": "url",
                       "sequential_image_generation": "disabled"}
            if reference:
                payload["image"] = "data:image/png;base64," + base64.b64encode(reference).decode()
            data = self.post(ARK, "/images/generations", self.settings.ark_key, payload)
        try:
            url = data["data"][0]["url"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError("生图服务没有返回图片，可能被内容审核拦截") from None
        return self.download(url)

    def download(self, url):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ProviderError("生图服务返回了无效下载地址")
        try:
            addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ProviderError("图片下载地址不是公网地址")
            # No Authorization header is forwarded to the image CDN, and no redirects.
            with self.client.stream("GET", url) as response:
                response.raise_for_status()
                if response.is_redirect:
                    raise ProviderError("图片下载地址发生重定向")
                chunks, length = [], 0
                for chunk in response.iter_bytes():
                    length += len(chunk)
                    if length > MAX_IMAGE_BYTES:
                        raise ProviderError("生成图片超过 16 MB")
                    chunks.append(chunk)
                return b"".join(chunks)
        except (httpx.HTTPError, OSError):
            raise ProviderError("图片下载失败，请创建新版本重试") from None

    def review(self, reference, views):
        # Small previews bound input tokens; this is an appearance check, not geometry.
        import io
        from PIL import Image
        content = [{"type": "text", "text": (
            '第一张是设计参考图，后面是同一物体的12张候选视图。检查是否为同一物体、'
            '颜色和结构是否一致、是否包含多物件或拼图、视角是否有明显变化。'
            '无法确认时拒绝。不要声称能证明3D几何正确。'
            '只返回 JSON：{"consistent":true或false,"reason":"简短中文理由"}。'
        )}]
        for raw in [reference, *views]:
            with Image.open(io.BytesIO(raw)) as image:
                image.thumbnail((384, 384))
                buf = io.BytesIO()
                image.convert("RGB").save(buf, "JPEG", quality=80)
            content.append({"type": "image_url", "image_url": {
                "url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}})
        result = self.chat_json("glm-4.6v-flash", [{"role": "user", "content": content}])
        if not isinstance(result.get("consistent"), bool) or not isinstance(result.get("reason"), str):
            raise ProviderError("图片检查结果格式异常，未开始重建")
        return {"consistent": result["consistent"], "reason": result["reason"][:600],
                "scope": "appearance_only"}
