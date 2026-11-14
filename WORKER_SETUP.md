# Worker Setup — Termux + llama-server + Qwen

The worker runs on the tablet, polls the Render bridge over HTTPS, forwards jobs to the local `llama-server`, and posts signed results back. No inbound ports are required.

## 1. Directory layout (recommended)

```text
~/justice-ai/
├── models/
│   └── qwen2.5-3b-instruct-q4_k_m.gguf
├── llama-server          # binary 0.7.0
└── worker/
    └── jf_ai_worker.py  # copy from this release
```

## 2. Secret file

```bash
mkdir -p ~/.config/justice-ai
read -rsp "Bridge secret: " JF_SECRET
printf '\n'
printf '%s' "$JF_SECRET" > ~/.config/justice-ai/bridge.secret
unset JF_SECRET
chmod 600 ~/.config/justice-ai/bridge.secret
```

Never put the secret in the worker script or in environment variables that appear in process listings if you can avoid it.

## 3. Start llama-server (CPU)

Because Vulkan 16-bit storage is unavailable on this device:

```bash
~/justice-ai/llama-server \
  -m ~/justice-ai/models/qwen2.5-3b-instruct-q4_k_m.gguf \
  --host 127.0.0.1 \
  --port 8080 \
  -c 2048 \
  -t 8 \
  -b 8 \
  -ngl 0 \
  --device none
```

Confirm:

```bash
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/v1/models
```

## 4. Environment for the worker

```bash
export JF_AI_BRIDGE_URL="https://your-service.onrender.com"
export JF_LOCAL_MODEL_URL="http://127.0.0.1:8080"
export JF_LOCAL_MODEL="local-model"   # any label; llama-server ignores it
```

## 5. Run the worker

```bash
cd ~/justice-ai/worker
python jf_ai_worker.py
```

Expected output:

```text
Justice AI Worker запущен.
Локальная модель: http://127.0.0.1:8080
Ожидание заданий...
```

When a job arrives:

```text
Получено задание: a1b2c3d4
Ответ передан Render: a1b2c3d4
```

## 6. Keep-alive tips (Termux)

- Use `termux-wake-lock` while the worker is active.
- Consider a simple loop or `tmux`/`screen` session so the process survives terminal disconnects.
- The worker already reconnects after transient network errors (3 s sleep).

## 7. Security notes

- The worker only makes *outbound* HTTPS requests. Do not open any ports on the tablet to the internet.
- HMAC protects authenticity of poll/result messages. Content is still visible to Render (by design).
- Keep `bridge.secret` mode 600 and never commit it.
