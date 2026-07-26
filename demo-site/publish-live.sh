#!/bin/sh
# 一键发布"公网直播"版:把当前 quick tunnel 域名烤进 config.js 并部署 Vercel 生产。
# 之后裸域名 https://dimos-drinks.vercel.app 直连真实后端,无需 ?backend= 参数。
#
# cloudflared 每次重启域名都会变——重启隧道后重新跑一次本脚本即可。
# 前提: ssh ascent-gx10 可用(或传入其它主机名作为 $1);
#       vercel CLI 已登录,demo-site 已 link(.vercel/ 存在)。
set -e
cd "$(dirname "$0")"
HOST="${1:-ascent-gx10}"

JSON=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$HOST" '
  MP=$(ss -tlnp 2>/dev/null | grep cloudflared | grep -oE "127\.0\.0\.1:[0-9]+" | head -1)
  [ -n "$MP" ] && curl -s --max-time 5 "http://$MP/quicktunnel"' || true)
TUNNEL=$(printf %s "$JSON" | sed -n 's/.*"hostname":"\([^"]*\)".*/\1/p')
if [ -z "$TUNNEL" ]; then
  echo "错误: $HOST 上没有可读的 cloudflared quick tunnel。" >&2
  echo "先在那台机器上启动: cloudflared tunnel --url http://localhost:8090" >&2
  exit 3
fi

./sync.sh                                    # 共享前端核心保持最新
printf 'window.VENDOR_DEFAULT_BACKEND = "https://%s";\n' "$TUNNEL" > config.js
echo "config.js -> https://$TUNNEL"

vercel deploy --prod
echo
echo "完成。直接打开(无需参数): https://dimos-drinks.vercel.app"
