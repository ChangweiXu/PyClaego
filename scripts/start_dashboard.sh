#!/usr/bin/env bash
# PyClaego Dashboard 启动脚本
# 安装 npm 依赖并启动开发服务器

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_DIR="$SCRIPT_DIR/../pyclaego/dashboard"

cd "$DASHBOARD_DIR"

if [[ ! -d "node_modules" ]]; then
    echo "[dashboard] 安装 npm 依赖 …"
    npm install
fi

echo "[dashboard] 启动开发服务器 …"
exec npm run dev
