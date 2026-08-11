#!/usr/bin/env bash
# =============================================================================
#  MCTB installer for Raspberry Pi (Scout / RPi 1 and Brain / RPi 2)
#
#    ./scripts/install_pi.sh scout
#    ./scripts/install_pi.sh brain
#
#  Idempotent — safe to re-run after a failure or to upgrade.
# =============================================================================
set -euo pipefail

ROLE="${1:-}"
if [[ "$ROLE" != "scout" && "$ROLE" != "brain" ]]; then
  echo "Usage: $0 <scout|brain>"
  echo
  echo "  scout  — RPi 1: data collection, screening, wallet tracking"
  echo "  brain  — RPi 2: scoring, execution, position management, local LLM"
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_DIR/.venv"
cd "$REPO_DIR"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
warn()  { printf "\033[33m%s\033[0m\n" "$*"; }

bold "=============================================="
bold " MCTB installer — role: $ROLE"
bold "=============================================="
echo

# --- Sanity ------------------------------------------------------------------
if [[ $EUID -eq 0 ]]; then
  warn "Do not run this as root. Run it as your normal user; it will use sudo where needed."
  exit 1
fi

ARCH="$(uname -m)"
if [[ "$ARCH" != "aarch64" && "$ARCH" != "x86_64" ]]; then
  warn "Architecture is $ARCH. A 64-bit OS is required — reflash with Raspberry Pi OS (64-bit)."
  exit 1
fi

if [[ -f /proc/device-tree/model ]]; then
  MODEL="$(tr -d '\0' < /proc/device-tree/model)"
  echo "Device: $MODEL"
  if [[ "$MODEL" == *"Pi 4"* || "$MODEL" == *"Pi 3"* ]]; then
    warn "A Pi 5 with 8GB is strongly recommended, especially for the Brain."
  fi
fi

TOTAL_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
echo "RAM: ${TOTAL_MB} MB"
if [[ "$ROLE" == "brain" && "$TOTAL_MB" -lt 3500 ]]; then
  warn "Under 4GB of RAM. The local LLM will struggle — consider running it on the host PC instead."
fi
echo

# --- System packages ---------------------------------------------------------
bold "[1/7] System packages"
sudo apt-get update -qq
sudo apt-get install -y -qq \
  python3 python3-pip python3-venv python3-dev \
  git curl build-essential pkg-config libssl-dev \
  redis-tools sqlite3 chrony
green "  installed"

# --- Clock -------------------------------------------------------------------
# A Pi has no battery-backed clock. A drifting clock breaks transaction
# building and every age-based rule in the strategy, in ways that look like
# bad luck rather than a bug.
bold "[2/7] Time synchronisation"
sudo systemctl enable --now chrony >/dev/null 2>&1 || true
sudo timedatectl set-ntp true 2>/dev/null || true
green "  NTP enabled ($(date -u '+%Y-%m-%d %H:%M:%S UTC'))"

# --- Python environment ------------------------------------------------------
bold "[3/7] Python virtual environment"
if [[ ! -d "$VENV" ]]; then
  python3 -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip setuptools wheel

PY_VER=$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
echo "  Python $PY_VER"
if python -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)'; then
  green "  version OK"
else
  warn "  Python 3.10+ is required. On Raspberry Pi OS Bookworm you already have 3.11."
  exit 1
fi

bold "[4/7] MCTB and dependencies"
if [[ "$ROLE" == "brain" ]]; then
  # The Brain signs transactions and encrypts the keystore.
  pip install --quiet -e ".[trade]"
else
  pip install --quiet -e .
fi
green "  installed"

# --- Redis -------------------------------------------------------------------
bold "[5/7] Message bus"
if [[ "$ROLE" == "scout" ]]; then
  echo "  The Scout connects to Redis; it does not need to host it."
  echo "  Point MCTB_REDIS_URL at whichever machine runs redis-server."
fi
green "  ok"

# --- Config ------------------------------------------------------------------
bold "[6/7] Configuration"
if [[ ! -f config/config.yaml ]]; then
  cp config/config.example.yaml config/config.yaml
  green "  created config/config.yaml"
else
  echo "  config/config.yaml already exists — left alone"
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  green "  created .env (chmod 600)"
else
  echo "  .env already exists — left alone"
fi

mkdir -p data logs secrets
chmod 700 secrets
green "  directories ready"

# --- systemd -----------------------------------------------------------------
bold "[7/7] systemd service"
SERVICE="/etc/systemd/system/mctb-${ROLE}.service"
sudo tee "$SERVICE" >/dev/null <<EOF
[Unit]
Description=MCTB ${ROLE}
Documentation=https://github.com/z440-prod/meme-coin-trade-bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER}
WorkingDirectory=${REPO_DIR}
Environment="PATH=${VENV}/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
ExecStart=${VENV}/bin/mctb ${ROLE}

Restart=always
RestartSec=10
# Give up restarting only if it is crash-looping badly, so a genuine
# configuration error is visible instead of hidden by endless restarts.
StartLimitBurst=5
StartLimitIntervalSec=300

# The Brain holds a decrypted key in memory; keep it out of core dumps and
# off the swap file.
LimitCORE=0
MemoryDenyWriteExecute=false
NoNewPrivileges=true
PrivateTmp=true

StandardOutput=journal
StandardError=journal
SyslogIdentifier=mctb-${ROLE}

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
green "  ${SERVICE} written"

echo
bold "=============================================="
green " Installation complete"
bold "=============================================="
cat <<EOF

Next steps on this device:

  1. Edit .env and set at minimum:
         MCTB_REDIS_URL   (LAN IP of the machine running Redis)
         MCTB_RPC_URL     (your Solana endpoint — the public one is too slow)

     nano .env

  2. Copy the SAME config/config.yaml to all three devices.

EOF

if [[ "$ROLE" == "brain" ]]; then
cat <<EOF
  3. Set up the local LLM (optional but recommended):
         ./scripts/setup_llm.sh install
         ./scripts/setup_llm.sh serve

  4. Create the encrypted wallet keystore (ONLY on this device):
         source .venv/bin/activate
         mctb keystore create

EOF
fi

cat <<EOF
  Check everything before starting:
         source .venv/bin/activate
         mctb preflight

  Start it:
         sudo systemctl enable --now mctb-${ROLE}
         journalctl -u mctb-${ROLE} -f

EOF
