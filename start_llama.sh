#!/data/data/com.termux/files/usr/bin/bash
Максимальная скорость: Qwen2.5-1.5B на Redmi Pad SE (Snapdragon 680)
set -e
MODEL_PATH="${MODEL_PATH:-$HOME/justice-ai/models/qwen2.5-1.5b-instruct-q4_k_m.gguf}"
THREADS="${THREADS:-4}"
CTX="${CTX:-2048}"
BATCH="${BATCH:-512}"
HOST="127.0.0.1"
PORT="8080"
if [ ! -f "$MODEL_PATH" ]; then
echo "Модель не найдена: $MODEL_PATH"
exit 1
fi
if command -v llama-server >/dev/null 2>&1; then
BIN="llama-server"
elif [ -x "$HOME/llama.cpp/build/bin/llama-server" ]; then
BIN="$HOME/llama.cpp/build/bin/llama-server"
elif [ -x "$HOME/llama.cpp/llama-server" ]; then
BIN="$HOME/llama.cpp/llama-server"
else
echo "llama-server не найден."
exit 1
fi
echo "=============================================="
echo " Justice AI — Qwen2.5-1.5B (FAST)"
echo "=============================================="
echo "Model   : $MODEL_PATH"
echo "Threads : $THREADS"
echo "Context : $CTX"
echo "Batch   : $BATCH"
echo "Listen  : http:// {PORT}"
echo "=============================================="
exec "$BIN" 
--model "$MODEL_PATH" 
--host "$HOST" 
--port "$PORT" 
--ctx-size "$CTX" 
--batch-size "$BATCH" 
--ubatch-size 256 
--threads "$THREADS" 
--threads-batch "$THREADS" 
--parallel 1 
--n-gpu-layers 0 
--cache-prompt 
--no-mmap 
--flash-attn off 
--cont-batching false
