#!/bin/sh
# 打印当前"公网直播"链接。
#
# quick tunnel 的域名在 cloudflared 每次重启后都会变——Vercel 页看不到真机
# 画面时,十有八九是没带 ?backend= 或带的是过期域名。跑本脚本拿最新链接,
# 用它重新打开页面即可(参数会写入 localStorage,之后开裸域名也走真实后端)。
#
# 前提: ~/.ssh/config 里有 ascent-gx10(或传入其它主机名作为 $1),
#       且该机器上 cloudflared tunnel --url http://localhost:8090 正在跑。
set -e
HOST="${1:-ascent-gx10}"
VERCEL="https://dimos-drinks.vercel.app"

JSON=$(ssh -o ConnectTimeout=8 -o BatchMode=yes "$HOST" '
  MP=$(ss -tlnp 2>/dev/null | grep cloudflared | grep -oE "127\.0\.0\.1:[0-9]+" | head -1)
  [ -n "$MP" ] && curl -s --max-time 5 "http://$MP/quicktunnel"' || true)

TUNNEL=$(printf %s "$JSON" | sed -n 's/.*"hostname":"\([^"]*\)".*/\1/p')
if [ -z "$TUNNEL" ]; then
  echo "错误: $HOST 上没有可读的 cloudflared quick tunnel。" >&2
  echo "先在那台机器上启动: cloudflared tunnel --url http://localhost:8090" >&2
  exit 3
fi

echo "隧道:   https://$TUNNEL"
echo "直播页: $VERCEL/?backend=$TUNNEL"
