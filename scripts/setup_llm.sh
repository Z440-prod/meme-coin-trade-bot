#!/usr/bin/env bash
# =============================================================================
#  Local LLM setup (llama.cpp) for the Brain — RPi 2
#
#    ./scripts/setup_llm.sh install     build llama.cpp
#    ./scripts/setup_llm.sh model       download a model
#    ./scripts/setup_llm.sh serve       run the server in the foreground
#    ./scripts/setup_llm.sh service     install it as a systemd unit
#    ./scripts/setup_llm.sh bench       measure tokens/sec on this hardware
#
#  A word on expectations. On a Pi 5, a 3B model in 4-bit quantization
#  generates roughly 5-12 tokens per second. A 300-token answer therefore takes
#  25-60 seconds. That is why MCTB does not put the LLM in the trade decision
#  path — see docs/ARCHITECTURE.md. It is used for qualitative red-flag checks
#  on candidates that already qualify, and for the nightly trade review, where
#  taking a minute costs nothing.
#
#  If you want the LLM to be genuinely useful on live candidates, run it on the
#  host PC's GPU instead and point MCTB_LLM_BASE_URL at that machine.
# =============================================================================
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LLAMA_DIR="$REPO_DIR/vendor/llama.cpp"
MODEL_DIR="$REPO_DIR/models"
PORT="${MCTB_LLM_PORT:-8080}"

bold()  { printf "\033[1m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
warn()  { printf "\033[33m%s\033[0m\n" "$*"; }

# Model choices, smallest first. Sizes are the 4-bit quantized download.
declare -A MODELS=(
  [qwen2.5-1.5b]="https://huggingface.co/Qwen/Qwen2.5-1.5B-Instruct-GGUF/resolve/main/qwen2.5-1.5b-instruct-q4_k_m.gguf|1.0GB|fastest, adequate for red-flag checks"
  [llama3.2-3b]="https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/Llama-3.2-3B-Instruct-Q4_K_M.gguf|2.0GB|best balance on a Pi 5 8GB — recommended"
  [qwen2.5-7b]="https://huggingface.co/Qwen/Qwen2.5-7B-Instruct-GGUF/resolve/main/qwen2.5-7b-instruct-q4_k_m.gguf|4.7GB|noticeably better reasoning, too slow for live candidates"
)

cmd_install() {
  bold "Building llama.cpp"
  sudo apt-get update -qq
  sudo apt-get install -y -qq build-essential cmake git libcurl4-openssl-dev

  mkdir -p "$(dirname "$LLAMA_DIR")"
  if [[ -d "$LLAMA_DIR/.git" ]]; then
    echo "  updating existing checkout"
    git -C "$LLAMA_DIR" pull --ff-only
  else
    git clone --depth 1 https://github.com/ggerganov/llama.cpp "$LLAMA_DIR"
  fi

  cd "$LLAMA_DIR"
  # -DGGML_NATIVE=ON lets it use this Pi's NEON instructions, which is a large
  # part of the achievable speed on ARM.
  cmake -B build -DCMAKE_BUILD_TYPE=Release -DGGML_NATIVE=ON -DLLAMA_CURL=OFF
  cmake --build build --config Release -j"$(nproc)"

  green "Built. Binaries in $LLAMA_DIR/build/bin"
  echo
  echo "Next: ./scripts/setup_llm.sh model"
}

cmd_model() {
  local choice="${1:-}"
  mkdir -p "$MODEL_DIR"

  if [[ -z "$choice" ]]; then
    bold "Available models"
    echo
    for key in "${!MODELS[@]}"; do
      IFS='|' read -r _url size desc <<< "${MODELS[$key]}"
      printf "  %-16s %-8s %s\n" "$key" "$size" "$desc"
    done
    echo
    echo "Usage: $0 model <name>"
    echo "Recommended on a Pi 5 8GB:  $0 model llama3.2-3b"
    return 0
  fi

  if [[ -z "${MODELS[$choice]:-}" ]]; then
    warn "Unknown model '$choice'. Run '$0 model' to list them."
    exit 1
  fi

  IFS='|' read -r url size _desc <<< "${MODELS[$choice]}"
  local dest="$MODEL_DIR/${choice}.gguf"

  if [[ -f "$dest" ]]; then
    green "Already downloaded: $dest"
    return 0
  fi

  # Warn before spending gigabytes on an SD card that may not have them.
  local avail_gb
  avail_gb=$(df -BG --output=avail "$MODEL_DIR" 2>/dev/null | tail -1 | tr -dc '0-9' || echo 99)
  bold "Downloading $choice ($size) — $avail_gb GB free"
  echo "  from $url"
  curl -L --fail --progress-bar -o "$dest.partial" "$url"
  mv "$dest.partial" "$dest"
  green "Saved to $dest"
  echo
  echo "Next: ./scripts/setup_llm.sh serve $choice"
}

_find_model() {
  local choice="${1:-}"
  if [[ -n "$choice" && -f "$MODEL_DIR/${choice}.gguf" ]]; then
    echo "$MODEL_DIR/${choice}.gguf"; return 0
  fi
  local found
  found="$(find "$MODEL_DIR" -maxdepth 1 -name '*.gguf' 2>/dev/null | head -1)"
  if [[ -z "$found" ]]; then
    warn "No model found in $MODEL_DIR. Run: $0 model llama3.2-3b" >&2
    exit 1
  fi
  echo "$found"
}

_server_bin() {
  for candidate in "$LLAMA_DIR/build/bin/llama-server" "$LLAMA_DIR/build/bin/server" "$(command -v llama-server || true)"; do
    [[ -x "$candidate" ]] && { echo "$candidate"; return 0; }
  done
  warn "llama-server not found. Run: $0 install" >&2
  exit 1
}

cmd_serve() {
  local model bin threads
  model="$(_find_model "${1:-}")"
  bin="$(_server_bin)"
  # Leave one core free so the trading loops stay responsive while the model
  # is generating. This matters — a fully saturated Pi delays your exits.
  threads=$(( $(nproc) > 1 ? $(nproc) - 1 : 1 ))

  bold "Starting llama.cpp server"
  echo "  model:   $(basename "$model")"
  echo "  threads: $threads of $(nproc)"
  echo "  port:    $PORT"
  echo

  exec "$bin" \
    --model "$model" \
    --host 0.0.0.0 \
    --port "$PORT" \
    --threads "$threads" \
    --ctx-size 4096 \
    --batch-size 256 \
    --parallel 1 \
    --cont-batching \
    --mlock \
    --log-disable
}

cmd_service() {
  local model bin threads user
  model="$(_find_model "${1:-}")"
  bin="$(_server_bin)"
  threads=$(( $(nproc) > 1 ? $(nproc) - 1 : 1 ))
  user="$(whoami)"

  sudo tee /etc/systemd/system/mctb-llm.service >/dev/null <<EOF
[Unit]
Description=MCTB local LLM (llama.cpp)
After=network.target

[Service]
Type=simple
User=${user}
WorkingDirectory=${REPO_DIR}
ExecStart=${bin} --model ${model} --host 0.0.0.0 --port ${PORT} \\
  --threads ${threads} --ctx-size 4096 --batch-size 256 \\
  --parallel 1 --cont-batching --mlock --log-disable
Restart=always
RestartSec=15
# Below the trading services in priority — if the Pi is under pressure,
# inference should yield, never the exit loop.
Nice=5
CPUWeight=50

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable --now mctb-llm
  green "mctb-llm installed and started"
  echo "  logs: journalctl -u mctb-llm -f"
}

cmd_bench() {
  local model bin
  model="$(_find_model "${1:-}")"
  bin="$(_server_bin)"

  bold "Benchmarking $(basename "$model") on this hardware"
  echo "Starting a temporary server…"
  "$bin" --model "$model" --host 127.0.0.1 --port 18080 \
    --threads "$(nproc)" --ctx-size 2048 --log-disable &
  local pid=$!
  trap 'kill $pid 2>/dev/null || true' EXIT

  for _ in $(seq 1 60); do
    curl -sf "http://127.0.0.1:18080/health" >/dev/null 2>&1 && break
    sleep 2
  done

  echo "Generating 200 tokens…"
  local start end elapsed
  start=$(date +%s.%N)
  curl -sf "http://127.0.0.1:18080/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d '{"messages":[{"role":"user","content":"Write two paragraphs about liquidity."}],"max_tokens":200,"temperature":0.7}' \
    >/dev/null
  end=$(date +%s.%N)
  elapsed=$(echo "$end - $start" | bc)

  kill $pid 2>/dev/null || true
  echo
  green "200 tokens in ${elapsed}s"
  echo "  ≈ $(echo "scale=1; 200 / $elapsed" | bc) tokens/sec"
  echo
  echo "Set llm.timeout_s in config/config.yaml above the time a ~150 token"
  echo "answer takes, or the advisor will be skipped on every candidate."
  echo "If that number is larger than a few seconds, that is expected on a Pi —"
  echo "the LLM still earns its keep on the nightly review."
}

case "${1:-}" in
  install) cmd_install ;;
  model)   cmd_model "${2:-}" ;;
  serve)   cmd_serve "${2:-}" ;;
  service) cmd_service "${2:-}" ;;
  bench)   cmd_bench "${2:-}" ;;
  *)
    cat <<EOF
Usage: $0 <command>

  install         build llama.cpp from source (10-25 min on a Pi 5)
  model [name]    list or download a model
  serve [name]    run the server in the foreground
  service [name]  install and start it as a systemd unit
  bench [name]    measure generation speed on this hardware

Typical first run on the Brain:

  $0 install
  $0 model llama3.2-3b
  $0 bench
  $0 service
EOF
    ;;
esac
