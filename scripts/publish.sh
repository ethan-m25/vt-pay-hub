#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$HOME/vt-pay-hub"
DATA_FILE="$REPO_DIR/data/jobs.json"
TODAY="${TODAY:-$(date +%Y-%m-%d)}"
SKIP_NOTIFY="${SKIP_NOTIFY:-0}"
DISCORD_WEBHOOK="https://discord.com/api/webhooks/1496112180704051259/bGcHy1oDkDWgQVKClowYdaZCxcI4L0GoPVd4Rtqcfmp4FV2l15cLQLWrVD8ga4QmOL1A"

notify_discord() {
  [[ "$SKIP_NOTIFY" == "1" ]] && return 0
  python3 -c "
import http.client, ssl, json, sys
ctx = ssl.create_default_context()
conn = http.client.HTTPSConnection('discord.com', context=ctx, timeout=15)
path = '$DISCORD_WEBHOOK'.replace('https://discord.com', '')
payload = json.dumps({'content': sys.stdin.read()}).encode()
conn.request('POST', path, body=payload, headers={'Content-Type': 'application/json'})
resp = conn.getresponse(); conn.close()
" <<< "$1" || true
}

read NEW_TODAY ACTIVE_COUNT NEW_COUNT < <(python3 -c "
import json
m = json.load(open('$DATA_FILE')).get('meta', {})
print(m.get('new_today',0), m.get('active',0), m.get('count',0))
" 2>/dev/null || echo "0 0 0")

cd "$REPO_DIR"
git add data/jobs.json
if git diff --cached --quiet; then
  notify_discord "ℹ️ Vermont Pay Hub [$TODAY]: no changes ($ACTIVE_COUNT active)"
  exit 0
fi

git commit -m "data: VT daily update $TODAY (+$NEW_TODAY new, $ACTIVE_COUNT active)"
git push origin main

export PATH="/Users/clawii/.npm-global/bin:$PATH"
wrangler pages deploy . --project-name vt-payhub --branch main 2>&1 | tail -3 || true

notify_discord "✅ Vermont Pay Hub updated [$TODAY]
📊 +$NEW_TODAY new | $ACTIVE_COUNT active | $NEW_COUNT total
🌐 https://vt.payhub.fyi
💱 USD"

echo "Published data/jobs.json"
