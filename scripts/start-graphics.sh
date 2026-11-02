#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo '请先用 Python 3.11+ 创建 .venv，并执行 .venv/bin/pip install -e "mcp[dev]" -e "agent[dev]"。'
  exit 1
fi
.venv/bin/python -c 'import text2splat.app, graphics_mcp.server'
exec .venv/bin/python scripts/run_graphics.py
