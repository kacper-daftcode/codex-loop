#!/usr/bin/env bash
# Podglad na zywo dzialajacej sesji codex TUI. Odpiecie: Ctrl-a d.
# Bez zywych sesji screen: ostatni zrzut TUI z logow (less -R).
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
  echo "sesja screen nie zyje; ostatni zrzut: $LAST"
  exec less -R "$LAST"
fi
echo "brak sesji i logow"
