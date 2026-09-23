#!/bin/bash
# 致富经：本机启动（后端 8002，避免和悟道之路 8000 抢端口）
set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [ ! -d .venv ]; then
  echo "缺少 .venv，请先: python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt"
  exit 1
fi

if lsof -ti:8002 >/dev/null 2>&1; then
  echo "8002 已在监听，打开 http://127.0.0.1:8002 （若只起了 Vite 则用 http://127.0.0.1:5173）"
  exit 0
fi

export $(grep -v '^#' .env | xargs) 2>/dev/null || true
# 本机代理常拖死 iFinD；采集务必直连
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
cd backend
exec ../.venv/bin/python -m app.main
