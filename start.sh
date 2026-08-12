#!/bin/bash
# WorkBuddy AI 启动脚本（本地开发）
# 用法: ./start.sh  [port]
set -e
cd "$(dirname "$0")"

PORT="${1:-8787}"
export HARNESS_PORT="$PORT"

if [ ! -d ".venv" ]; then
  echo "未找到 .venv，正在创建虚拟环境..."
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

echo "🚀 WorkBuddy AI 启动中 → http://localhost:${PORT}"
exec .venv/bin/python web/server.py