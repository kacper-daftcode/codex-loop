#!/usr/bin/env bash
# codexloop deployment installer.
#
# Usage:
#   bash install.sh /path/to/project [--name NAME] [--start] [--no-systemd]
#                                    [--with-signal | --no-signal (default)]
#
# Creates <project>/codexloop/ (conf + scripts + logs/state), a systemd unit
# codexloop-<name>.service, and optionally starts it. An existing conf is
# never overwritten.
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
PROJECT=""
NAME=""
DO_START=0
USE_SYSTEMD=1
WITH_SIGNAL=0

while [ $# -gt 0 ]; do
  case "$1" in
    --name) NAME="$2"; shift 2;;
    --start) DO_START=1; shift;;
    --no-systemd) USE_SYSTEMD=0; shift;;
    --with-signal) WITH_SIGNAL=1; shift;;
    --no-signal) WITH_SIGNAL=0; shift;;
    -h|--help)
      sed -n '2,11p' "$0"; exit 0;;
    *) PROJECT="$1"; shift;;
  esac
done

if [ -z "$PROJECT" ]; then
  echo "usage: bash install.sh /path/to/project [--name NAME] [--start]" >&2
  exit 2
fi
PROJECT="$(realpath "$PROJECT")"
NAME="${NAME:-$(basename "$PROJECT")}"
DEST="$PROJECT/codexloop"

# --- dependencies -----------------------------------------------------
MISSING=""
for dep in python3 screen; do
  command -v "$dep" >/dev/null 2>&1 || MISSING="$MISSING $dep"
done
if ! command -v codex >/dev/null 2>&1; then
  MISSING="$MISSING codex(CLI)"
fi
if [ -n "$MISSING" ]; then
  echo "WARNING: missing dependencies:$MISSING" >&2
  echo "  (screen: apt-get install screen; codex: npm i -g @openai/codex)" >&2
fi

# --- codex project trust ------------------------------------------------
CODEX_CFG="$HOME/.codex/config.toml"
if [ -f "$CODEX_CFG" ] && ! grep -qF "\"$PROJECT\"" "$CODEX_CFG" 2>/dev/null; then
  echo "WARNING: add to $CODEX_CFG (avoids the trust prompt blocking the TUI):" >&2
  echo "  [projects.\"$PROJECT\"]" >&2
  echo '  trust_level = "trusted"' >&2
fi

# --- deployment files -------------------------------------------------
mkdir -p "$DEST/logs" "$DEST/state"
for f in codexloop.py notify.sh watch.py signal-setup.sh; do
  cp "$SRC/$f" "$DEST/$f"
  chmod +x "$DEST/$f"
done
if [ ! -f "$DEST/codexloop.conf" ]; then
  sed -e "s|^NAME=.*|NAME=$NAME|" -e "s|^PROJECT_DIR=.*|PROJECT_DIR=$PROJECT|" \
      "$SRC/codexloop.conf" > "$DEST/codexloop.conf"
  echo "created $DEST/codexloop.conf - REVIEW it before starting"
else
  echo "kept existing $DEST/codexloop.conf"
fi

# --- systemd --------------------------------------------------------------
if [ "$USE_SYSTEMD" -eq 1 ]; then
  UNIT="/etc/systemd/system/codexloop-$NAME.service"
  PYBIN="$(command -v python3)"
  cat > "$UNIT" <<EOF
[Unit]
Description=codexloop - autonomous codex sessions ($NAME)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$PROJECT
Environment=HOME=$HOME
Environment=PATH=$PATH
ExecStart=$PYBIN $DEST/codexloop.py
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  echo "created $UNIT"
fi

echo
echo "Done. Deployment: $DEST"
echo "  watch live:  $DEST/watch.py     (or: screen -x codexloop-$NAME)"
echo "  soft stop:   touch $DEST/STOP"
echo "  hard stop:   touch $DEST/STOPNOW"
if [ "$WITH_SIGNAL" -eq 1 ]; then
  bash "$DEST/signal-setup.sh" || echo "signal-setup aborted - alerts will stay in logs/notify.log"
else
  echo "  signal:      SKIPPED (optional; whenever you want it: bash $DEST/signal-setup.sh)"
fi
if [ "$USE_SYSTEMD" -eq 1 ]; then
  if [ "$DO_START" -eq 1 ]; then
    systemctl enable --now "codexloop-$NAME"
    echo "  service:     codexloop-$NAME (enabled + started)"
  else
    echo "  service:     systemctl enable --now codexloop-$NAME"
  fi
else
  echo "  manual run:  python3 $DEST/codexloop.py"
fi
