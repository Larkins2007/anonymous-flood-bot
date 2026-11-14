# Justice AI — Qwen2.5-1.5B (максимальная скорость)

Рекомендуемая быстрая конфигурация для Redmi Pad SE (Snapdragon 680).

## 1. Скачать модель (один раз)

В Termux:

```bash
mkdir -p ~/justice-ai/models
cd ~/justice-ai/models

# Вариант A: huggingface-cli (если установлен)
# pip install -U "huggingface_hub[cli]"
# huggingface-cli download Qwen/Qwen2.5-1.5B-Instruct-GGUF \
#   qwen2.5-1.5b-instruct-q4_k_m.gguf --local-dir .

# Вариант B: прямая ссылка (пример, проверь актуальную на Hugging Face)
# Ищи: Qwen2.5-1.5B-Instruct GGUF Q4_K_M
# Файл должен называться примерно:
#   qwen2.5-1.5b-instruct-q4_k_m.gguf
# Размер ~1.0 GB
```

Проверка:

```bash
ls -lh ~/justice-ai/models/*.gguf
```

## 2. Render / код

Положи из архива:

- `jf_ai_bridge.py`
- `jf_ai_worker.py`
- `jf_ai.py`
- `test_ai_bridge_worker.py`

```bash
cd ~/justice-ai-audit
python -m py_compile jf_ai_bridge.py jf_ai_worker.py jf_ai.py
python -m unittest -v test_ai_bridge_worker

git add jf_ai_bridge.py jf_ai_worker.py jf_ai.py test_ai_bridge_worker.py
git commit -m "Speed: Qwen2.5-1.5B + long jobs"
git push
```

На Render:

```
JF_AI_SPEED=1
```

## 3. Запуск на планшете

**Вкладка 1 — модель:**

```bash
cd ~/justice-ai-audit
chmod +x start_llama.sh

export MODEL_PATH="$HOME/justice-ai/models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
export THREADS=4
export CTX=2048

./start_llama.sh
```

**Вкладка 2 — Worker:**

```bash
cd ~/justice-ai-audit

export JF_AI_BRIDGE_URL="https://anonymous-flood-bot.onrender.com"
export JF_LOCAL_MODEL_URL="http://127.0.0.1:8080"
export JF_LOCAL_MODEL="local"
export JF_LOCAL_GENERATE_TIMEOUT=600

python jf_ai_worker.py
```

## Ожидаемая скорость (1.5B vs 3B)

| Запрос | 3B (было) | 1.5B (цель) |
|--------|-----------|-------------|
| Короткий | 30–70 с | **15–40 с** |
| Обычный | 50–100 с | **25–55 с** |
| Подробный | 2–5 мин | **1–2.5 мин** |

Скорость генерации на Snapdragon 680: примерно **4–7 tok/s** (вместо ~2.2 у 3B).

## Если имя файла другое

```bash
MODEL_PATH=$HOME/justice-ai/models/ИМЯ_ФАЙЛА.gguf ./start_llama.sh
```
