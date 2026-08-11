#!/usr/bin/env bash
# =============================================================================
#  MCTB installer for the host PC (Console + Redis broker)
#
#    ./scripts/install_host.sh
#
#  The host PC also hosts Redis by default, since it is the machine most
#  likely to be on a wired connection and easiest to reach from both Pis.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_DIR/.venv"
cd "$REPO_DIR"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
warn()  { printf "\033[33m%s\033[0m\n" "$*"; }

OS="$(uname -s)"

bold "=============================================="
bold " MCTB installer — host PC (console)"
bold "=============================================="
echo "Detected OS: $OS"
echo

# --- Python ------------------------------------------------------------------
bold "[1/5] Python environment"
if ! command -v python3 >/dev/null 2>&1; then
  warn "python3 not found. Install Python 3.10+ and re-run."
  exit 1
fi

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' || {
  warn "Python 3.10+ is required (found $(python3 --version))."
  exit 1
}

[[ -d "$VENV" ]] || python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --quiet --upgrade pip setuptools wheel
green "  $(python --version)"

bold "[2/5] MCTB and dependencies"
pip install --quiet -e ".[console]"
green "  installed"

# --- Redis -------------------------------------------------------------------
bold "[3/5] Redis (the message bus all three devices share)"
if command -v redis-server >/dev/null 2>&1; then
  green "  redis-server already installed"
else
  case "$OS" in
    Linux)
      if command -v apt-get >/dev/null 2>&1; then
        sudo apt-get update -qq && sudo apt-get install -y -qq redis-server
      elif command -v dnf >/dev/null 2>&1; then
        sudo dnf install -y redis
      elif command -v pacman >/dev/null 2>&1; then
        sudo pacman -S --noconfirm redis
      else
        warn "  Could not detect your package manager — install redis-server manually."
      fi
      ;;
    Darwin)
      if command -v brew >/dev/null 2>&1; then
        brew install redis
      else
        warn "  Install Homebrew (https://brew.sh) then: brew install redis"
      fi
      ;;
    *)
      warn "  On Windows, run Redis under WSL2 or Docker:"
      warn "    docker run -d --name mctb-redis -p 6379:6379 redis:7-alpine"
      ;;
  esac
fi

# Redis must accept connections from the two Pis, not just localhost.
if [[ "$OS" == "Linux" && -f /etc/redis/redis.conf ]]; then
  bold "  Configuring Redis for LAN access"
  if grep -qE '^bind 127\.0\.0\.1' /etc/redis/redis.conf; then
    sudo sed -i 's/^bind 127\.0\.0\.1.*/bind 0.0.0.0/' /etc/redis/redis.conf
    green "    bind set to 0.0.0.0"
  fi
  if grep -qE '^protected-mode yes' /etc/redis/redis.conf; then
    sudo sed -i 's/^protected-mode yes/protected-mode no/' /etc/redis/redis.conf
    green "    protected-mode disabled"
    warn "    Redis is now reachable from your LAN with no password."
    warn "    Set 'requirepass <something-long>' in /etc/redis/redis.conf and put the"
    warn "    same value in MCTB_REDIS_PASSWORD on all three devices."
  fi
  sudo systemctl enable --now redis-server >/dev/null 2>&1 || \
    sudo systemctl enable --now redis >/dev/null 2>&1 || true
fi

if redis-cli ping >/dev/null 2>&1; then
  green "  redis responding to PING"
else
  warn "  redis is not responding yet — start it before running the console"
fi

# --- Config ------------------------------------------------------------------
bold "[4/5] Configuration"
[[ -f config/config.yaml ]] || { cp config/config.example.yaml config/config.yaml; green "  created config/config.yaml"; }
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env 2>/dev/null || true
  green "  created .env"
fi
mkdir -p data logs
green "  directories ready"

# --- LAN address -------------------------------------------------------------
bold "[5/5] Network"
LAN_IP=""
if command -v hostname >/dev/null 2>&1; then
  LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')" || true
fi
if [[ -z "$LAN_IP" && "$OS" == "Darwin" ]]; then
  LAN_IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"
fi
[[ -n "$LAN_IP" ]] && green "  this machine: $LAN_IP" || warn "  could not detect LAN IP — find it yourself"

echo
bold "=============================================="
green " Installation complete"
bold "=============================================="
cat <<EOF

On BOTH Raspberry Pis, set this in their .env files:

    MCTB_REDIS_URL=redis://${LAN_IP:-<this-machine-ip>}:6379/0

On this machine:

  1. Edit .env (the console does not need an RPC URL, but Redis must match):
         nano .env

  2. Check everything:
         source .venv/bin/activate
         mctb preflight

  3. Start the dashboard:
         mctb console

     Then open http://localhost:8787
     (or http://${LAN_IP:-<this-machine-ip>}:8787 from your phone on the same network)

EOF
