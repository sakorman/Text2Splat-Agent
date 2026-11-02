from dataclasses import dataclass, field
import os
from pathlib import Path

from graphics_mcp.settings import local_url
from graphics_mcp.text_models import text_model_from_env


@dataclass(frozen=True)
class AgentSettings:
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("GRAPHICS_DATA_DIR", ".graphics")).resolve())
    creative_url: str = field(default_factory=lambda: local_url(os.getenv("CREATIVE_MCP_URL", "http://127.0.0.1:8766")))
    webscan_url: str = field(default_factory=lambda: local_url(os.getenv("WEBSCAN_MCP_URL", "http://127.0.0.1:8767")))
    poll_interval: float = 2.0
    job_timeout: float = 7200.0
    mcp_timeout: float = 90.0
    zhipu_key: str = field(default_factory=lambda: os.getenv("ZHIPU_API_KEY", ""), repr=False)
    text_model: str = field(default_factory=text_model_from_env)
    model_timeout: float = 45.0
