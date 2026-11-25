from dataclasses import dataclass, field
from pathlib import Path
import os
from urllib.parse import urlsplit

from .text_models import text_model_from_env


GENERATION_CONFIG_ERRORS = {
    "missing_zhipu_key": "请在本地 .env 设置 ZHIPU_API_KEY 后重启 MCP 服务",
    "unsupported_image_provider": "IMAGE_PROVIDER 仅支持 cogview 或 seedream",
    "paid_images_disabled": "Seedream 的付费生图开关 ALLOW_PAID_IMAGE_API 已关闭，可将 IMAGE_PROVIDER 设为 cogview",
    "missing_ark_key": "Seedream 缺少 ARK_API_KEY，可将 IMAGE_PROVIDER 设为 cogview",
}


def local_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment:
        raise ValueError("本地服务地址必须是无凭据的 loopback HTTP 地址")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("GRAPHICS_DATA_DIR", ".graphics")).resolve())
    zhipu_key: str = field(default_factory=lambda: os.getenv("ZHIPU_API_KEY", ""), repr=False)
    text_model: str = field(default_factory=text_model_from_env)
    ark_key: str = field(default_factory=lambda: os.getenv("ARK_API_KEY", ""), repr=False)
    image_provider: str = field(default_factory=lambda: os.getenv("IMAGE_PROVIDER", "cogview"))
    allow_paid: bool = field(default_factory=lambda: os.getenv("ALLOW_PAID_IMAGE_API", "false").lower() == "true")
    webscan_url: str = field(default_factory=lambda: local_url(os.getenv("WEBSCAN_URL", "http://127.0.0.1:8787")))

    def generation_config_error(self) -> str | None:
        if not self.zhipu_key:
            return "missing_zhipu_key"
        if self.image_provider not in {"cogview", "seedream"}:
            return "unsupported_image_provider"
        if self.image_provider == "seedream":
            if not self.allow_paid:
                return "paid_images_disabled"
            if not self.ark_key:
                return "missing_ark_key"
        return None

    def check_generation(self):
        issue = self.generation_config_error()
        if issue:
            raise ValueError(GENERATION_CONFIG_ERRORS[issue])
