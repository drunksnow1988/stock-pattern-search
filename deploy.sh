#!/bin/bash
set -e

echo "=== 安装系统依赖 ==="
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git

echo "=== 克隆代码 ==="
cd /root
rm -rf stock-pattern-search
git clone https://github.com/drunksnow1988/stock-pattern-search.git
cd stock-pattern-search

echo "=== 安装 Python 依赖 ==="
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt

echo "=== 下载股票缓存 ==="
.venv/bin/python download_cache.py

echo "=== 配置系统服务（开机自启）==="
cat > /etc/systemd/system/stock-search.service << 'EOF'
[Unit]
Description=Stock Pattern Search
After=network.target

[Service]
WorkingDirectory=/root/stock-pattern-search
ExecStart=/root/stock-pattern-search/.venv/bin/gunicorn backend:app --workers 1 --timeout 300 --bind 0.0.0.0:5001
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable stock-search
systemctl restart stock-search

echo ""
echo "=== 部署完成 ==="
systemctl status stock-search --no-pager
echo ""
echo "访问地址：http://$(curl -s ifconfig.me):5001"
