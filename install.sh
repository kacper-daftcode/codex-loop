#!/usr/bin/env bash
# Instalator deploymentu codexloop.
#
# Uzycie:
#   bash install.sh /path/to/project [--name NAZWA] [--start] [--no-systemd]
#                                   [--with-signal | --no-signal(domyślne)]
#
# Tworzy <project>/codexloop/ (conf + skrypty + logi/state), jednostke systemd
# codexloop-<name>.service i opcjonalnie ja uruchamia. Istniejacego conf nie
# nadpisuje.
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
      sed -n '2,10p' "$0"; exit 0;;
    *) PROJECT="$1"; shift;;
  esac
done

if [ -z "$PROJECT" ]; then
  echo "podaj katalog projektu: bash install.sh /path/to/project [--name N] [--start]" >&2
  exit 2
fi
PROJECT="$(realpath "$PROJECT")"
NAME="${NAME:-$(basename "$PROJECT")}"
DEST="$PROJECT/codexloop"

# --- zaleznosci -----------------------------------------------------
MISSING=""
for dep in python3 screen; do
  command -v "$dep" >/dev/null 2>&1 || MISSING="$MISSING $dep"
done
if ! command -v codex >/dev/null 2>&1; then
  MISSING="$MISSING codex(CLI)"
fi
if [ -n "$MISSING" ]; then
  echo "UWAGA: brakujace zaleznosci:$MISSING" >&2
  echo "  (screen: apt-get install screen; codex: npm i -g @openai/codex)" >&2
fi

# --- trust codexa ----------------------------------------------------
CODEX_CFG="$HOME/.codex/config.toml"
if [ -f "$CODEX_CFG" ] && ! grep -qF "\"$PROJECT\"" "$CODEX_CFG" 2>/dev/null; then
  echo "UWAGA: dodaj do $CODEX_CFG (unika promptu trust w TUI):" >&2
  echo "  [projects.\"$PROJECT\"]" >&2
  echo '  trust_level = "trusted"' >&2
fi

# --- pliki deploymentu ----------------------------------------------
mkdir -p "$DEST/logs" "$DEST/state"
for f in codexloop.py notify.sh watch.py signal-setup.sh; do
  cp "$SRC/$f" "$DEST/$f"
  chmod +x "$DEST/$f"
done
if [ ! -f "$DEST/codexloop.conf" ]; then
  sed -e "s|^NAME=.*|NAME=$NAME|" -e "s|^PROJECT_DIR=.*|PROJECT_DIR=$PROJECT|" \
      "$SRC/codexloop.conf" > "$DEST/codexloop.conf"
  echo "utworzono $DEST/codexloop.conf - SPRAWDZ go przed startem"
else
  echo "zachowano istniejacy $DEST/codexloop.conf"
fi

# --- systemd ----------------------------------------------------------
UNIT=""
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
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  echo "utworzono $UNIT"
fi

echo
echo "Gotowe. Deployment: $DEST"
echo "  podglad:     $DEST/watch.py       (albo: screen -x codexloop-$NAME)"
echo "  stop miekki: touch $DEST/STOP"
echo "  stop twardy: touch $DEST/STOPNOW"
if [ "$WITH_SIGNAL" -eq 1 ]; then
  bash "$DEST/signal-setup.sh" || echo "signal-setup przerwany - powiadomienia zostana w logs/notify.log"
else
  echo "  signal:      POMINIETY (opcjonalny; kiedy chcesz: bash $DEST/signal-setup.sh)"
fi
if [ "$USE_SYSTEMD" -eq 1 ]; then
  if [ "$DO_START" -eq 1 ]; then
    systemctl enable --now "codexloop-$NAME"
    echo "  usluga:      codexloop-$NAME (enabled + started)"
  else
    echo "  usluga:      systemctl enable --now codexloop-$NAME"
  fi
else
  echo "  start reczny: python3 $DEST/codexloop.py"
fi
