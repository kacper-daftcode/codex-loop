#!/usr/bin/env bash
# Notyfikator fire-and-forget. Zawsze loguje do logs/notify.log; gdy jest
# signal.number i signal-cli, wysyla wiadomosc na Signal (Note to Self).
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
NAME="${CODEXLOOP_NOTIFIER_NAME:-$(grep -E '^NAME=' "$BASE/codexloop.conf" 2>/dev/null | cut -d= -f2-)}"
NAME="${NAME:-codexloop}"
MSG="$NAME: $*"
TS="$(date -Is)"
mkdir -p "$BASE/logs"
echo "$TS $MSG" >> "$BASE/logs/notify.log"
NUM="$(grep -oE '\+[0-9]+' "$BASE/signal.number" 2>/dev/null | head -1 || true)"
if [ -n "$NUM" ] && command -v signal-cli >/dev/null 2>&1; then
  if ! timeout 45 signal-cli -u "$NUM" send -m "$MSG" "$NUM" >> "$BASE/logs/notify.log" 2>&1; then
    echo "$TS signal-cli send FAILED" >> "$BASE/logs/notify.log"
  fi
fi
exit 0
