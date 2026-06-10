#!/usr/bin/env zsh
set -euo pipefail

echo "$$" > /Users/USERNAME/Agent/Radio/data/logs/radio-bot.pid
cd /Users/USERNAME/Agent/Radio

exec /Users/USERNAME/Agent/.venv/bin/python /Users/USERNAME/Agent/Radio/scripts/main_bot.py --config /Users/USERNAME/Agent/Radio/config/config.yaml
