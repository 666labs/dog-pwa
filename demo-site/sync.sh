#!/bin/sh
# 同步共享前端核心到 demo-site（Vercel 部署目录）。
# frontend/vendor-app.js 是唯一事实源——勿直接改 demo-site/vendor-app.js。
set -e
cd "$(dirname "$0")"
cp ../frontend/vendor-app.js vendor-app.js
echo "synced: frontend/vendor-app.js -> demo-site/vendor-app.js"
