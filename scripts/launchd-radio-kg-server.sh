#!/usr/bin/env zsh
set -euo pipefail

echo "$$" > /Users/USERNAME/Agent/radio_kg/data/server.pid
cd /Users/USERNAME/Agent/radio_kg

export RADIO_KG_AUTO_INGEST="${RADIO_KG_AUTO_INGEST:-1}"
export RADIO_KG_AUTO_INGEST_URL="${RADIO_KG_AUTO_INGEST_URL:-http://127.0.0.1:8000/api/ingest}"

exec /Users/USERNAME/Agent/.venv/bin/python -m uvicorn src.server.app:app --host 127.0.0.1 --port 8000
