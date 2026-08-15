#!/usr/bin/env bash
# One-time Signal pairing for this deployment: installs signal-cli (native
# build), links it as an extra device of your account (QR/link on the phone),
# stores signal.number and sends a test message.
set -euo pipefail
BASE="$(cd "$(dirname "$0")" && pwd)"

install_signal_cli() {
  local api url ver tmpd dest
  api="$(curl -fsSL -m 20 https://api.github.com/repos/AsamK/signal-cli/releases/latest)"
  ver="$(printf '%s' "$api" | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
  url="$(printf '%s' "$api" | python3 -c '
import json, sys
assets = json.load(sys.stdin)["assets"]
pick = [a for a in assets if "Linux-native" in a["name"] and a["name"].endswith(".tar.gz")]
if not pick:
    pick = [a for a in assets if a["name"].startswith("signal-cli-") and a["name"].endswith(".tar.gz")]
print(pick[0]["browser_download_url"] if pick else "")')"
  if [ -z "$url" ]; then
    echo "no download artifact found for signal-cli" >&2
    exit 1
  fi
  echo "Downloading signal-cli $ver: $url"
  tmpd="$(mktemp -d)"
  curl -fsSL -m 300 -o "$tmpd/sc.tgz" "$url"
  tar -xz -C "$tmpd" -f "$tmpd/sc.tgz"
  if [ -f "$tmpd/signal-cli" ]; then
    cp "$tmpd/signal-cli" /usr/local/bin/signal-cli
    chmod +x /usr/local/bin/signal-cli
  else
    dest="/opt/signal-cli-${ver#v}"
    mkdir -p "$dest"
    tar -xz -C "$dest" --strip-components=1 -f "$tmpd/sc.tgz"
    ln -sf "$dest/bin/signal-cli" /usr/local/bin/signal-cli
  fi
  rm -rf "$tmpd"
  signal-cli --version
}

if ! command -v signal-cli >/dev/null 2>&1; then
  install_signal_cli
fi

echo
echo "A pairing link will appear below. On your phone: Signal -> Settings ->"
echo "Linked Devices -> + -> scan the QR (or open the link on the phone)."
echo
LINK_OUT="$(mktemp)"
signal-cli link -n "codexloop-$(basename "$BASE")" > "$LINK_OUT" 2>&1 &
LINK_PID=$!
sleep 3
URI="$(grep -o 'sgnl://[^ ]*' "$LINK_OUT" | head -1 || true)"
if [ -n "$URI" ]; then
  echo "Pairing link: $URI"
  if command -v qrencode >/dev/null 2>&1; then
    qrencode -t ANSIUTF8 "$URI"
  fi
fi
wait "$LINK_PID"
cat "$LINK_OUT"
rm -f "$LINK_OUT"

NUM="$(timeout 30 signal-cli listAccounts 2>/dev/null | grep -oE '\+[0-9]+' | head -1)"
if [ -z "$NUM" ]; then
  echo "could not read the account number after pairing" >&2
  exit 1
fi
echo "$NUM" > "$BASE/signal.number"
echo "Wrote number to $BASE/signal.number"
timeout 45 signal-cli -u "$NUM" send -m "codexloop: pairing OK, notifications live" "$NUM"
echo "Sent a test message (Note to Self)."
