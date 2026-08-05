#!/bin/sh
set -eu

ENV_FILE=/home/luketye/pi-mcp/pi-mcp.env
PUSH_ENV=/home/luketye/pi-mcp/healthcheck.env
ENDPOINT=http://127.0.0.1:8799/mcp
KUMA=http://127.0.0.1:3001
EXPECT=406

read_var() {
  grep -m1 "^$2=" "$1" | cut -d= -f2-
}

TOKEN=$(read_var "$ENV_FILE" PI_MCP_TOKEN) || TOKEN=""
PUSH=$(read_var "$PUSH_ENV" KUMA_PUSH_TOKEN) || PUSH=""

[ -n "$TOKEN" ] || { echo "PI_MCP_TOKEN not readable" >&2; exit 2; }
[ -n "$PUSH" ]  || { echo "KUMA_PUSH_TOKEN not readable" >&2; exit 2; }

out=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' \
        --max-time 10 \
        -H "Authorization: Bearer $TOKEN" \
        "$ENDPOINT") || out="000 0"

code=${out%% *}
secs=${out##* }
ms=$(awk "BEGIN{printf \"%d\", $secs*1000}")

if [ "$code" = "$EXPECT" ]; then
  status=up
else
  status=down
fi

curl -sf --max-time 10 --get "$KUMA/api/push/$PUSH" \
  --data-urlencode "status=$status" \
  --data-urlencode "msg=HTTP $code" \
  --data-urlencode "ping=$ms" >/dev/null
