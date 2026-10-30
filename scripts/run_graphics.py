"""Run owned subprocesses together; Ctrl-C stops only processes started here."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from urllib.parse import urlsplit

from dotenv import load_dotenv
from graphics_mcp.settings import local_url

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
load_dotenv(ROOT / ".env")
children = []
stopping = False
runtime_directory = tempfile.TemporaryDirectory(prefix="text2splat-runtime-")


def stop(_signum=None, _frame=None):
    global stopping
    stopping = True


def spawn(command, cwd=ROOT):
    process = subprocess.Popen(command, cwd=cwd, start_new_session=True)
    children.append(process)
    return process


def healthy(url):
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


for sig in (signal.SIGINT, signal.SIGTERM):
    signal.signal(sig, stop)

try:
    webscan_url = local_url(os.getenv("WEBSCAN_URL", "http://127.0.0.1:8787"))
    repo = Path(os.getenv("WEBSCAN_REPO", "../web-scan")).resolve()
    scan_python = repo / ".venv/bin/python"
    if not healthy(webscan_url):
        if scan_python.exists() and (repo / "webscan").is_dir():
            spawn([str(scan_python), "-m", "webscan", "--host", "127.0.0.1", "--port", str(urlsplit(webscan_url).port or 8787)], repo)
        else:
            print("web-scan 未启动：仍可生成概念图，3D 需要安装并启动本地 web-scan。", flush=True)
    for kind, env, default in [("creative", "CREATIVE_MCP_URL", "http://127.0.0.1:8766"), ("webscan", "WEBSCAN_MCP_URL", "http://127.0.0.1:8767")]:
        url = local_url(os.getenv(env, default))
        spawn([sys.executable, "-m", "graphics_mcp.server", kind, "--port", str(urlsplit(url).port)])
    java = str(Path(os.environ["JAVA_HOME"]) / "bin/java") if os.getenv("JAVA_HOME") else shutil.which("java")
    # Maven replaces target/ during verification; a live JVM must keep an immutable JAR.
    runtime_jar = Path(runtime_directory.name) / "agent.jar"
    shutil.copy2(ROOT / "target/SpringAITest-0.0.1-SNAPSHOT.jar", runtime_jar)
    spawn([java, "-jar", str(runtime_jar), "--spring.profiles.active=graphics"])
    print(f"\nText2Splat Agent · 造物方舟启动中：http://127.0.0.1:{os.getenv('PORT', '8080')}/graphics/\n按 Ctrl-C 停止本次启动的服务。\n", flush=True)
    while not stopping:
        if any(process.poll() is not None for process in children):
            print("一个子服务已退出，正在停止本次启动的其余服务。请查看上方错误。", flush=True)
            break
        time.sleep(0.5)
finally:
    for process in reversed(children):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    for process in reversed(children):
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    runtime_directory.cleanup()
