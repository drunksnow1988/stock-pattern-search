#!/bin/bash
cd "$(dirname "$0")"

PYTHON="$(dirname "$0")/.venv/bin/python"

# 如果 venv 不存在则创建
if [ ! -f "$PYTHON" ]; then
  echo "🔧 创建虚拟环境…"
  python3 -m venv .venv
  echo "📦 安装依赖…"
  .venv/bin/pip install -q flask flask-cors akshare numpy
fi

echo "🚀 启动 A股形态搜索服务…"
echo "   访问：http://localhost:5001"
echo ""
$PYTHON backend.py
