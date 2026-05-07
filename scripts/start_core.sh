#!/bin/bash
# PyClaego Core 服务器启动脚本

cd "$(dirname "$0")/.."
exec uv run pyclaego-core "$@"
