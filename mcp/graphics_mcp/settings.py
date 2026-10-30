from dataclasses import dataclass, field
from pathlib import Path
import os
from urllib.parse import urlsplit


def local_url(value: str) -> str:
    url = urlsplit(value)
    if url.scheme != "http" or url.hostname not in {"127.0.0.1", "localhost", "::1"} or url.username or url.password or url.query or url.fragment:
        raise ValueError("本地服务地址必须是无凭据的 loopback HTTP 地址")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("GRAPHICS_DATA_DIR", ".graphics")).resolve())
    zhipu_key: str = field(default_factory=lambda: os.getenv("ZHIPU_API_KEY", ""), repr=False)
    ark_key: str = field(default_factory=lambda: os.getenv("ARK_API_KEY", ""), repr=False)
    image_provider: str = field(default_factory=lambda: os.getenv("IMAGE_PROVIDER", "cogview"))
    allow_paid: bool = field(default_factory=lambda: os.getenv("ALLOW_PAID_IMAGE_API", "false").lower() == "true")
    webscan_url: str = field(default_factory=lambda: local_url(os.getenv("WEBSCAN_URL", "http://127.0.0.1:8787")))

    def check_generation(self):
        if not self.zhipu_key:
            raise ValueError("请在本地 .env 设置 ZHIPU_API_KEY 后重启 MCP 服务")
        if self.image_provider not in {"cogview", "seedream"}:
            raise ValueError("IMAGE_PROVIDER 仅支持 cogview 或 seedream")
        if self.image_provider == "seedream" and (not self.allow_paid or not self.ark_key):
            raise ValueError("Seedream 需要 ARK_API_KEY 和 ALLOW_PAID_IMAGE_API=true；不会自动切换到付费模型")
