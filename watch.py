#!/usr/bin/env bash
# Live view of the running codex TUI session. Detach: Ctrl-a d.
# When no screen session is alive: dumps the latest TUI transcript (less -R).
set -u
BASE="$(cd "$(dirname "$0")" && pwd)"
NAME="${1:-$(grep -E '^SCREEN_NAME=' "$BASE/codexloop.conf" 2>/dev/null | cut -d= -f2-)}"
NAME="${NAME:-$(grep -E '^NAME=' "$BASE/codexloop.conf" 2>/dev/null | cut -d= -f2-)}"
NAME="${NAME:-$(basename "$BASE")}"
if ! screen -ls | grep -q "[.]${NAME}[[:space:]]" && screen -ls | grep -q "[.]codexloop-${NAME}[[:space:]]"; then
  NAME="codexloop-$NAME"
fi
if screen -ls | grep -q "[.]${NAME}[[:space:]]"; then
  exec screen -x "$NAME"
fi
LAST="$(ls -t "$BASE"/logs/iter-*.screen.log 2>/dev/null | head -1 || true)"
if [ -n "$LAST" ]; then
  echo "no live screen session; latest transcript: $LAST"
  exec less -R "$LAST"
fi
echo "no session and no logs yet"
