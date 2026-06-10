#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

LOG_DIR="${RADIO_LOG_DIR:-data/logs}"
DAEMON_PID_FILE="${LOG_DIR}/radio-daemon.pid"
API_PID_FILE="${LOG_DIR}/radio-api.pid"
BOT_PID_FILE="${LOG_DIR}/radio-bot.pid"

stop_launch_agent() {
  local label="$1"
  local domain="gui/$(id -u)"
  if ! command -v launchctl >/dev/null 2>&1; then
    return 0
  fi
  if launchctl print "$domain/$label" >/dev/null 2>&1; then
    echo "Booting out launchd $label."
    launchctl bootout "$domain/$label" >/dev/null 2>&1 || true
  fi
}

stop_pid_file() {
  local name="$1"
  local pid_file="$2"

  if [[ ! -f "$pid_file" ]]; then
    echo "$name not running (no pid file)."
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  if [[ -z "$pid" ]]; then
    rm -f "$pid_file"
    echo "$name pid file was empty; removed it."
    return 0
  fi

  if kill -0 "$pid" >/dev/null 2>&1; then
    echo "Stopping $name (pid $pid)."
    kill "$pid" >/dev/null 2>&1 || true
    for _ in {1..10}; do
      if kill -0 "$pid" >/dev/null 2>&1; then
        sleep 0.5
      else
        break
      fi
    done
    if kill -0 "$pid" >/dev/null 2>&1; then
      echo "$name did not stop after 5 seconds; sending SIGKILL."
      kill -9 "$pid" >/dev/null 2>&1 || true
    fi
  else
    echo "$name was already stopped (pid $pid)."
  fi

  rm -f "$pid_file"
}

stop_launch_agent "com.agent.radio-scheduler-daemon"
stop_launch_agent "com.agent.radio-telegram-bot"

stop_pid_file "Web console API" "$API_PID_FILE"
stop_pid_file "Telegram bot" "$BOT_PID_FILE"
stop_pid_file "Scheduler daemon" "$DAEMON_PID_FILE"

EXTRA_PIDS="$(pgrep -f "$ROOT_DIR/scripts/main_daemon.py|$ROOT_DIR/scripts/main_bot.py|scripts/main_daemon.py --config config/config.yaml|scripts/main_bot.py --config config/config.yaml" || true)"
if [[ -n "$EXTRA_PIDS" ]]; then
  echo "Stopping extra Radio daemon/bot pids: $EXTRA_PIDS"
  echo "$EXTRA_PIDS" | xargs kill >/dev/null 2>&1 || true
fi

echo "Radio-Oshikatsu background services stopped."
