#!/usr/bin/env python3
"""Offline release smoke checks for the Justice Faite 0.26.2 source tree."""
from pathlib import Path
import ast
import re
import sys

ROOT = Path(__file__).resolve().parent


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def main():
    require((ROOT / "VERSION").read_text(encoding="utf-8").strip() == "0.26.2",
            "VERSION must be 0.26.2")
    required = {
        "bot.py", "jf_ai.py", "jf_ai_analytics.py", "jf_ai_bridge.py",
        "jf_ai_worker.py", "render.yaml", "requirements.txt",
        "tests_bridge.py", "tests_release.py", "AUDIT_REPORT.md", "DEPLOYMENT_GUIDE.md",
        "WORKER_SETUP.md", "TEST_RESULTS.md",
    }
    missing = sorted(name for name in required if not (ROOT / name).is_file())
    require(not missing, "missing release files: " + ", ".join(missing))

    for path in ROOT.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=path.name)

    bot = (ROOT / "bot.py").read_text(encoding="utf-8")
    bridge_init = bot.find("ai_bridge = AIJobBridge()", bot.find("async def main():"))
    task_schedule = bot.find("background_tasks = [", bot.find("async def main():"))
    http_startup = bot.find("http_runner = await start_http_server()", bot.find("async def main():"))
    require(bridge_init >= 0 and http_startup > bridge_init and task_schedule > http_startup,
            "AIJobBridge and HTTP endpoints must be ready before background tasks")
    http_start = bot[bot.find("async def start_http_server():"):bot.find("async def ", bot.find("async def start_http_server():") + 10)]
    require("getattr(JF_AI, \"bridge\", None)" in http_start,
            "HTTP server must reuse the main bridge instance")
    require("web.Application(client_max_size=ai_bridge.max_body)" in http_start,
            "aiohttp must enforce the bridge body limit")

    render = (ROOT / "render.yaml").read_text(encoding="utf-8")
    require(re.search(r"(?m)^\s*- key: JF_AI_ENABLED\s*\n\s*value: [\"']false[\"']\s*$", render) is not None,
            "Render must keep JF_AI_ENABLED=false by default")
    bridge = (ROOT / "jf_ai_bridge.py").read_text(encoding="utf-8")
    require("if not isinstance(data, dict):" in bridge,
            "bridge must reject non-object JSON result payloads")
    require("re.fullmatch(r\"[0-9a-f]{64}\", signature)" in bridge,
            "bridge must validate HMAC signature format")

    forbidden = []
    for path in ROOT.rglob("*"):
        if path.is_dir() and path.name in {".git", "__pycache__", "node_modules"}:
            forbidden.append(str(path.relative_to(ROOT)))
        elif path.is_file() and (
            path.name == ".env" or path.name.endswith(".db") or
            path.suffix in {".pyc", ".pyo", ".gguf"} or path.name == "bridge.secret"
        ):
            forbidden.append(str(path.relative_to(ROOT)))
    require(not forbidden, "forbidden artifacts present: " + ", ".join(forbidden))

    print("[PASS] VERSION and required release files")
    print("[PASS] AST parse for all root Python modules")
    print("[PASS] bridge and HTTP endpoints initialized before background tasks")
    print("[PASS] HTTP bridge reuse and request-size enforcement")
    print("[PASS] AI disabled by default in Render configuration")
    print("[PASS] forbidden secrets, databases, models and cache artifacts absent")
    print("ALL RELEASE SMOKE CHECKS PASSED")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("RELEASE SMOKE CHECK FAILED:", exc, file=sys.stderr)
        raise
