#!/usr/bin/env zsh
set -euo pipefail

echo "$$" > /Users/USERNAME/Agent/Radio/data/logs/radio-daemon.pid
cd /Users/USERNAME/Agent/Radio

export RADIO_KG_AUTO_INGEST="${RADIO_KG_AUTO_INGEST:-1}"
export RADIO_KG_AUTO_INGEST_URL="${RADIO_KG_AUTO_INGEST_URL:-http://127.0.0.1:8000/api/ingest}"

exec /Users/USERNAME/Agent/.venv/bin/python /Users/USERNAME/Agent/Radio/scripts/main_daemon.py --config /Users/USERNAME/Agent/Radio/config/config.yaml
