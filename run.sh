#!/usr/bin/env bash
# Start the dimOS Control panel. Serves frontend + API on one port (8090).
# Redeploy-safe: binds 0.0.0.0 and the frontend uses relative-path API calls,
# so this same server works unchanged on another machine from its own IP.
set -euo pipefail
cd "$(dirname "$0")"
exec ./venv/bin/python -m uvicorn main:app \
  --app-dir backend \
  --host 0.0.0.0 --port 8090
