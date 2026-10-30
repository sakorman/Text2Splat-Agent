#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo '请先用 Python 3.11+ 创建 .venv，并执行 .venv/bin/pip install -e "mcp[dev]"。'
  exit 1
fi
if [[ -z "${JAVA_HOME:-}" && -d /opt/homebrew/opt/openjdk@25/libexec/openjdk.jdk/Contents/Home ]]; then
  export JAVA_HOME=/opt/homebrew/opt/openjdk@25/libexec/openjdk.jdk/Contents/Home
fi
if [[ -n "${JAVA_HOME:-}" ]]; then export PATH="$JAVA_HOME/bin:$PATH"; fi
java_major=$(java -version 2>&1 | head -1 | sed -E 's/.*version "([0-9]+).*/\1/')
if ! [[ "$java_major" =~ ^[0-9]+$ ]] || (( java_major < 17 )); then
  if [[ -d /opt/homebrew/opt/openjdk@25/libexec/openjdk.jdk/Contents/Home ]]; then
    export JAVA_HOME=/opt/homebrew/opt/openjdk@25/libexec/openjdk.jdk/Contents/Home
    export PATH="$JAVA_HOME/bin:$PATH"
    echo '当前默认 Java 版本过旧，使用已安装的 Homebrew JDK 25。'
  else
    echo '需要 JDK 17 或更高版本，请设置 JAVA_HOME。'
    exit 1
  fi
fi
mvn -q -DskipTests package
exec .venv/bin/python scripts/run_graphics.py
