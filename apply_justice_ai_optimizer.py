
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Apply from the Justice Faite repository root:
#   python apply_justice_ai_optimizer.py
# The script checks every baseline fragment before writing and backs up all files.

from pathlib import Path
from datetime import datetime
import argparse
import os
import ast
import shutil
import sys
import tempfile

def replace_once(text, old, new, label, expected=1):
    n = text.count(old)
    if n != expected:
        raise RuntimeError(f"{label}: expected {expected} exact match(es), found {n}")
    return text.replace(old, new)

def read(root, name):
    return (root / name).read_text(encoding="utf-8")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=".", help="Repository root")
    root = Path(ap.parse_args().root).resolve()

    required = [
        "jf_ai_bridge.py", "jf_ai.py", "jf_ai_analytics.py",
        "jf_ai_worker.py", "render.yaml", "start_llama.sh",
        "test_ai_bridge_worker.py",
    ]
    missing = [x for x in required if not (root / x).is_file()]
    if missing:
        print("Not a Justice Faite source root; missing: " + ", ".join(missing), file=sys.stderr)
        return 2

    updated = {}
    try:
        s = read(root, "jf_ai_bridge.py")
        s = replace_once(s,
            'JOB_TTL_SECONDS = int(os.getenv("JF_AI_JOB_TTL", "3600"))\nLEASE_SECONDS = int(os.getenv("JF_AI_LEASE_SECONDS", "900"))\n',
            'JOB_TTL_SECONDS = max(600, min(86400, int(os.getenv("JF_AI_JOB_TTL", "1800") or "1800")))\nLEASE_SECONDS = max(300, min(3600, int(os.getenv("JF_AI_LEASE_SECONDS", "600") or "600")))\n',
            "bridge deadlines")
        s = replace_once(s,
            '        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=32)\n',
            '        # 0 = live user request; 10 = optional background analysis.\n        self.queue: asyncio.PriorityQueue = asyncio.PriorityQueue(maxsize=8)\n',
            "priority queue")
        s = replace_once(s,
            '        timeout: float,\n        response_format: Optional[dict] = None,\n    ) -> str:\n',
            '        timeout: float,\n        response_format: Optional[dict] = None,\n        priority: int = 0,\n    ) -> str:\n',
            "generate priority argument")
        s = replace_once(s,
            '        future: asyncio.Future = loop.create_future()\n\n        job = {\n',
            '        future: asyncio.Future = loop.create_future()\n        try:\n            job_priority = max(0, min(100, int(priority)))\n        except (TypeError, ValueError):\n            job_priority = 0\n\n        job = {\n',
            "normalize priority")
        s = replace_once(s,
            '            "queued": True,\n            "lease_until": 0.0,\n        }\n',
            '            "queued": True,\n            "lease_until": 0.0,\n            "priority": job_priority,\n        }\n',
            "store priority")
        s = replace_once(s,
            '        try:\n            self.queue.put_nowait(job_id)\n        except asyncio.QueueFull:\n',
            '        try:\n            self.queue.put_nowait((job_priority, time.monotonic(), job_id))\n        except asyncio.QueueFull:\n',
            "enqueue priority")
        s = replace_once(s,
            '                    self.queue.put_nowait(job_id)\n                    job["queued"] = True\n',
            '                    self.queue.put_nowait((int(job.get("priority", 0)), time.monotonic(), job_id))\n                    job["queued"] = True\n',
            "requeue priority")
        s = replace_once(s,
            '            job_id = await asyncio.wait_for(self.queue.get(), timeout=20.0)\n',
            '            _, _, job_id = await asyncio.wait_for(self.queue.get(), timeout=20.0)\n',
            "dequeue priority")
        updated["jf_ai_bridge.py"] = s

        s = read(root, "jf_ai.py")
        s = replace_once(s,
            '        # Long-running local generation support (up to 1 hour). Default 30 minutes.\n        self.timeout = max(60, min(3600, int(os.getenv("JF_AI_TIMEOUT", "1800") or "1800")))\n',
            '        # Interactive deadline aligned with the single tablet worker.\n        self.timeout = max(60, min(900, int(os.getenv("JF_AI_TIMEOUT", "300") or "300")))\n',
            "interactive timeout")
        s = replace_once(s,
            '        self.context_recent_limit = max(20, min(200, int(os.getenv("JF_AI_CONTEXT_RECENT_LIMIT", "100") or "100")))\n',
            '        self.context_recent_limit = max(20, min(120, int(os.getenv("JF_AI_CONTEXT_RECENT_LIMIT", "60") or "60")))\n        self.context_archive_lookup_limit = max(100, min(5000, int(os.getenv("JF_AI_CONTEXT_ARCHIVE_LOOKUP_LIMIT", "1200") or "1200")))\n',
            "context limits")
        s = replace_once(s,
            '            archive_rows = self.store.run(lambda c: c.execute(\n                f"SELECT {select} FROM jf_chat_message_log WHERE chat_id=? AND direction=\'in\' ORDER BY message_at DESC,id DESC LIMIT 25000",\n                (int(self.primary_chat_id),),\n            ).fetchall())\n',
            '            # Keep archive context bounded; fetch a specific old reply separately.\n            archive_rows = self.store.run(lambda c: c.execute(\n                f"SELECT {select} FROM jf_chat_message_log WHERE chat_id=? AND direction=\'in\' ORDER BY message_at DESC,id DESC LIMIT ?",\n                (int(self.primary_chat_id), self.context_archive_lookup_limit),\n            ).fetchall())\n',
            "archive memory bound")
        s = replace_once(s,
            '            reply = getattr(message, "reply_to_message", None)\n            reply_id = int(getattr(reply, "message_id", 0) or 0)\n            if reply_id and reply_id in by_message and not any(int(r["message_id"]) == reply_id for r in rows):\n                rows.insert(0, by_message[reply_id])\n',
            '            reply = getattr(message, "reply_to_message", None)\n            reply_id = int(getattr(reply, "message_id", 0) or 0)\n            if reply_id and not any(int(r["message_id"]) == reply_id for r in rows):\n                reply_row = by_message.get(reply_id)\n                if reply_row is None:\n                    reply_row = self.store.run(lambda c: c.execute(\n                        f"SELECT {select} FROM jf_chat_message_log WHERE chat_id=? AND direction=\'in\' AND message_id=? LIMIT 1",\n                        (int(self.primary_chat_id), reply_id),\n                    ).fetchone())\n                if reply_row is not None:\n                    rows.insert(0, reply_row)\n',
            "direct old-reply lookup")
        s = replace_once(s,
            '        # SPEED mode: even tighter defaults (env JF_AI_SPEED=1)\n        speed = os.getenv("JF_AI_SPEED", "").strip().casefold() in {"1", "true", "yes", "on"}\n        if detailed:\n            return 420 if speed else 520\n        return 140 if speed else 180\n',
            '        # Speed-first defaults suit a single CPU worker on a tablet.\n        speed = os.getenv("JF_AI_SPEED", "true").strip().casefold() in {"1", "true", "yes", "on"}\n        if detailed:\n            return 300 if speed else 420\n        return 128 if speed else 180\n',
            "speed-first generation budget")
        s = replace_once(s, 'os.getenv("JF_AI_SPEED", "")', 'os.getenv("JF_AI_SPEED", "true")',
                         "speed default in remaining context paths", expected=2)
        updated["jf_ai.py"] = s

        s = read(root, "jf_ai_analytics.py")
        s = replace_once(s,
            'self.enabled = (os.getenv("JF_AI_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"} and os.getenv("JF_AI_ANALYTICS_ENABLED", "true").strip().casefold() in {"1", "true", "yes", "on"})',
            'self.enabled = (os.getenv("JF_AI_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"} and os.getenv("JF_AI_ANALYTICS_ENABLED", "false").strip().casefold() in {"1", "true", "yes", "on"})',
            "analytics opt-in")
        s = replace_once(s, 'self.semantic_enabled = os.getenv("JF_AI_WATCH_SEMANTIC_ENABLED", "true")',
                         'self.semantic_enabled = os.getenv("JF_AI_WATCH_SEMANTIC_ENABLED", "false")',
                         "semantic analysis opt-in")
        s = replace_once(s, 'self.flood_memory_enabled = os.getenv("JF_AI_FLOOD_MEMORY_ENABLED", "true")',
                         'self.flood_memory_enabled = os.getenv("JF_AI_FLOOD_MEMORY_ENABLED", "false")',
                         "flood summary opt-in")
        s = replace_once(s, 'self.owner_memory_enabled = os.getenv("JF_AI_OWNER_MEMORY_ENABLED", "true")',
                         'self.owner_memory_enabled = os.getenv("JF_AI_OWNER_MEMORY_ENABLED", "false")',
                         "owner summary opt-in")
        s = replace_once(s,
            '            payload.get("response_format"),\n        )\n        return {"choices": [{"message": {"content": str(answer or "")}}]}\n',
            '            payload.get("response_format"),\n            priority=10,\n        )\n        return {"choices": [{"message": {"content": str(answer or "")}}]}\n',
            "background queue priority")
        updated["jf_ai_analytics.py"] = s

        s = read(root, "jf_ai_worker.py")
        s = replace_once(s,
            'LOCAL_GENERATE_TIMEOUT = int(os.getenv("JF_LOCAL_GENERATE_TIMEOUT", "900"))\n',
            'try:\n    # Finish before the Render-side interactive request deadline.\n    LOCAL_GENERATE_TIMEOUT = max(30, min(600, int(os.getenv("JF_LOCAL_GENERATE_TIMEOUT", "240") or "240")))\nexcept (TypeError, ValueError):\n    LOCAL_GENERATE_TIMEOUT = 240\n',
            "worker deadline")
        updated["jf_ai_worker.py"] = s

        s = read(root, "render.yaml")
        s = replace_once(s,
            '      - key: JF_AI_ENABLED\n        value: "false"\n',
            '      - key: JF_AI_ENABLED\n        value: "false"\n'
            '      - key: JF_AI_SPEED\n        value: "true"\n'
            '      - key: JF_AI_TIMEOUT\n        value: "300"\n'
            '      - key: JF_AI_ANALYTICS_ENABLED\n        value: "false"\n'
            '      - key: JF_AI_WATCH_SEMANTIC_ENABLED\n        value: "false"\n'
            '      - key: JF_AI_FLOOD_MEMORY_ENABLED\n        value: "false"\n'
            '      - key: JF_AI_OWNER_MEMORY_ENABLED\n        value: "false"\n'
            '      - key: JF_AI_JOB_TTL\n        value: "1800"\n'
            '      - key: JF_AI_LEASE_SECONDS\n        value: "600"\n'
            '      - key: JF_AI_CONTEXT_ARCHIVE_LOOKUP_LIMIT\n        value: "1200"\n'
            '      - key: JF_AI_CONTEXT_RECENT_LIMIT\n        value: "60"\n',
            "Render performance profile")
        updated["render.yaml"] = s

        s = read(root, "test_ai_bridge_worker.py")
        s = replace_once(s,
            '    def test_job_ttl_constants(self):\n        self.assertGreaterEqual(jf_ai_bridge.JOB_TTL_SECONDS, 1800)\n        self.assertGreaterEqual(jf_ai_bridge.LEASE_SECONDS, 600)\n\n\nclass WorkerTests(unittest.TestCase):\n',
            '    def test_job_ttl_constants(self):\n        self.assertGreaterEqual(jf_ai_bridge.JOB_TTL_SECONDS, 1800)\n        self.assertGreaterEqual(jf_ai_bridge.LEASE_SECONDS, 600)\n\n'
            '    def test_priority_queue_prefers_interactive_jobs(self):\n'
            '        async def run():\n'
            '            low_task = asyncio.create_task(self.bridge.generate([{"role": "user", "content": "background"}], 8, 120, priority=10))\n'
            '            await asyncio.sleep(0)\n'
            '            low_id = next(k for k, v in self.bridge.jobs.items() if v["priority"] == 10)\n'
            '            high_task = asyncio.create_task(self.bridge.generate([{"role": "user", "content": "interactive"}], 8, 120, priority=0))\n'
            '            await asyncio.sleep(0)\n'
            '            high_id = next(k for k, v in self.bridge.jobs.items() if v["priority"] == 0)\n'
            '            first = await self.bridge.queue.get()\n'
            '            second = await self.bridge.queue.get()\n'
            '            self.assertEqual(first[2], high_id)\n'
            '            self.assertEqual(second[2], low_id)\n'
            '            low_task.cancel()\n'
            '            high_task.cancel()\n'
            '            await asyncio.gather(low_task, high_task, return_exceptions=True)\n'
            '        asyncio.run(run())\n\n\nclass WorkerTests(unittest.TestCase):\n',
            "priority regression test")
        updated["test_ai_bridge_worker.py"] = s

        updated["start_llama.sh"] = r'''#!/data/data/com.termux/files/usr/bin/bash
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
'''
    except Exception as exc:
        print("SOURCE CHECK FAILED:", exc, file=sys.stderr)
        print("No files were changed.", file=sys.stderr)
        return 3

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = root / f".justice-faite-backup-{stamp}"
    backup.mkdir()
    for name in updated:
        shutil.copy2(root / name, backup / name)

    written = []
    try:
        for name, content in updated.items():
            fd, tmp_name = tempfile.mkstemp(prefix=f".{name}.", dir=root)
            os.close(fd)
            tmp = Path(tmp_name)
            tmp.write_text(content, encoding="utf-8", newline="\n")
            os.replace(tmp, root / name)
            written.append(name)

        for py_file in sorted(root.glob("*.py")):
            ast.parse(py_file.read_text(encoding="utf-8"), filename=py_file.name)
        os.chmod(root / "start_llama.sh", 0o755)
    except Exception as exc:
        for name in written:
            shutil.copy2(backup / name, root / name)
        print("PATCH FAILED AND WAS ROLLED BACK:", exc, file=sys.stderr)
        print("Backup:", backup, file=sys.stderr)
        return 4

    print("PATCH APPLIED")
    print("Files updated:", ", ".join(updated))
    print("Backup:", backup)
    print("Python AST syntax check: PASS")
    print("Run next: python tests_bridge.py")
    print("          python -m unittest -v test_ai_bridge_worker")
    print("          python tests_release.py")
    print("Keep JF_AI_ENABLED=false until the tablet worker and short end-to-end test pass.")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
