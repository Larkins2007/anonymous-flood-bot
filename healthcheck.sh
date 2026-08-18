#!/bin/sh
set -eu
curl -fsS --max-time 5 http://127.0.0.1:10000/health >/tmp/health.json
python - <<'PY'
import json
with open('/tmp/health.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
if not data.get('polling_running'):
    raise SystemExit('polling_running is false')
if not data.get('db_ok'):
    raise SystemExit('db_ok is false')
if data.get('fatal_error'):
    raise SystemExit(f"fatal_error={data['fatal_error']}")
print('healthcheck OK')
PY
