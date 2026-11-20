#!/data/data/com.termux/files/usr/bin/bash
# CPU-friendly llama-server launcher for Redmi Pad SE / Snapdragon 680.
set -euo pipefail

MODEL_PATH="${MODEL_PATH:-$HOME/justice-ai/models/qwen2.5-1.5b-instruct-q4_k_m.gguf}"
THREADS="${THREADS:-4}"
CTX="${CTX:-2048}"
BATCH="${BATCH:-128}"
UBATCH="${UBATCH:-64}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"

if [ ! -f "$MODEL_PATH" ]; then
    printf 'Модель не найдена: %s\n' "$MODEL_PATH" >&2
    printf 'Задай MODEL_PATH, если у тебя установлена другая GGUF-модель.\n' >&2
    exit 1
fi

if command -v llama-server >/dev/null 2>&1; then
    BIN="$(command -v llama-server)"
elif [ -x "$HOME/justice-ai/llama-server" ]; then
    BIN="$HOME/justice-ai/llama-server"
elif [ -x "$HOME/llama.cpp/build/bin/llama-server" ]; then
    BIN="$HOME/llama.cpp/build/bin/llama-server"
elif [ -x "$HOME/llama.cpp/llama-server" ]; then
    BIN="$HOME/llama.cpp/llama-server"
else
    printf 'llama-server не найден. Проверь установку llama.cpp.\n' >&2
    exit 1
fi

printf '%s\n' '=============================================='
printf '%s\n' ' Justice AI — Qwen2.5-1.5B (CPU FAST)'
printf 'Model   : %s\n' "$MODEL_PATH"
printf 'Binary  : %s\n' "$BIN"
printf 'Threads : %s\n' "$THREADS"
printf 'Context : %s\n' "$CTX"
printf 'Batch   : %s / %s\n' "$BATCH" "$UBATCH"
printf 'Listen  : http://%s:%s\n' "$HOST" "$PORT"
printf '%s\n' '=============================================='

# Leave mmap enabled; disabling it can increase Android memory pressure.
exec "$BIN" \
    --model "$MODEL_PATH" \
    --host "$HOST" \
    --port "$PORT" \
    --ctx-size "$CTX" \
    --batch-size "$BATCH" \
    --ubatch-size "$UBATCH" \
    --threads "$THREADS" \
    --threads-batch "$THREADS" \
    --n-gpu-layers 0
