#!/usr/bin/env bash
set -euo pipefail

URL="http://127.0.0.1:${PORT:-10000}/health"

for _ in 1 2 3; do
  RESP="$(curl -fsS --max-time 3 "$URL" || true)"

  if [ -n "$RESP" ]; then
    if RESP="$RESP" python - <<'PY'
import json
import os
import sys

try:
    data = json.loads(os.environ.get("RESP", "{}"))
except Exception:
    sys.exit(1)

if (
    data.get("polling_running") is True
    and data.get("db_ok") is True
    and not data.get("fatal_error")
):
    sys.exit(0)

sys.exit(1)
PY
    then
      exit 0
    fi
  fi

  sleep 1
done

exit 1
