#!/usr/bin/env bash
# Usage: set_render_secret.sh KEY FILE -- set env var KEY on the dynasty-commish Render service from
# FILE, then redeploy. The value is piped straight into the API request and never printed.
set -euo pipefail

SERVICE=srv-darrhanavr4c73ftca50
KEY=${1:?usage: set_render_secret.sh KEY FILE}
FILE=${2:?usage: set_render_secret.sh KEY FILE}

[ -s "$FILE" ] || { echo "missing or empty: $FILE" >&2; exit 1; }

# The Render CLI has no env-var command, so reuse its login token for the REST API.
render whoami -o text >/dev/null   # refreshes the token if it has expired
TOKEN=$(awk '/^api:/{a=1} a && $1=="key:"{print $2; exit}' ~/.render/cli.yaml | tr -d '"')
[ -n "$TOKEN" ] || { echo "no API token in ~/.render/cli.yaml; run 'render login'" >&2; exit 1; }

status=$(jq -Rs '{value: (. | rtrimstr("\n") | rtrimstr("\r"))}' "$FILE" |
  curl -sS -o /dev/null -w '%{http_code}' -X PUT \
    -K <(printf 'header = "Authorization: Bearer %s"\n' "$TOKEN") \
    -H 'Content-Type: application/json' --data @- \
    "https://api.render.com/v1/services/$SERVICE/env-vars/$KEY")

[ "$status" = 200 ] || { echo "setting $KEY failed: HTTP $status" >&2; exit 1; }
echo "$KEY set on $SERVICE; redeploying so it takes effect..."

render deploys create "$SERVICE" --wait --confirm -o text
