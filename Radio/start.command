#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

HOST="${RADIO_HOST:-127.0.0.1}"
PORT="${RADIO_PORT:-8000}"
CONFIG="${RADIO_CONFIG:-config/config.yaml}"
URL="http://${HOST}:${PORT}"
LOG_DIR="${RADIO_LOG_DIR:-data/logs}"
DAEMON_PID_FILE="${LOG_DIR}/radio-daemon.pid"
API_PID_FILE="${LOG_DIR}/radio-api.pid"
BOT_PID_FILE="${LOG_DIR}/radio-bot.pid"

mkdir -p "$LOG_DIR"

WORKSPACE_PYTHON="$(cd "$ROOT_DIR/.." && pwd)/.venv/bin/python"
if [[ -n "${RADIO_PYTHON:-}" ]]; then
  PYTHON_CMD=("$RADIO_PYTHON")
elif [[ -x "$WORKSPACE_PYTHON" ]]; then
  # 工作区唯一 venv（Agent/.venv，见 Agent/pyproject.toml）
  PYTHON_CMD=("$WORKSPACE_PYTHON")
else
  echo "Workspace venv missing: $WORKSPACE_PYTHON. Run: cd .. && uv sync" >&2
  exit 1
fi

if [[ ! -f "$CONFIG" ]]; then
  echo "Config file not found: $CONFIG" >&2
  exit 1
fi

if [[ ! -f ".env" ]]; then
  echo "Warning: .env not found. Copy .env.example to .env and fill credentials before running jobs." >&2
fi

is_running() {
  local pid_file="$1"
  [[ -f "$pid_file" ]] || return 1
  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  [[ -n "$pid" ]] || return 1
  kill -0 "$pid" >/dev/null 2>&1
}

start_background() {
  local name="$1"
  local pid_file="$2"
  local log_file="$3"
  shift 3

  if is_running "$pid_file"; then
    echo "$name already running (pid $(cat "$pid_file"))."
    return 0
  fi

  nohup "$@" >>"$log_file" 2>&1 &
  local pid="$!"
  echo "$pid" >"$pid_file"
  echo "$name started (pid $pid). Logs: $log_file"
}

stop_background() {
  local name="$1"
  local pid_file="$2"

  if ! is_running "$pid_file"; then
    rm -f "$pid_file"
    return 0
  fi

  local pid
  pid="$(cat "$pid_file" 2>/dev/null || true)"
  echo "Restarting $name (pid $pid)."
  kill "$pid" >/dev/null 2>&1 || true
  for _ in {1..10}; do
    if kill -0 "$pid" >/dev/null 2>&1; then
      sleep 0.5
    else
      break
    fi
  done
  if kill -0 "$pid" >/dev/null 2>&1; then
    kill -9 "$pid" >/dev/null 2>&1 || true
  fi
  rm -f "$pid_file"
}

wait_for_api() {
  local deadline=$((SECONDS + 30))
  while (( SECONDS < deadline )); do
    if curl -fsS "${URL}/api/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  return 1
}

open_browser() {
  if command -v open >/dev/null 2>&1; then
    open "$URL"
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$URL" >/dev/null 2>&1 &
  else
    echo "Open this URL in your browser: $URL"
  fi
}

if [[ "${RADIO_RESTART_DAEMON:-1}" == "1" ]]; then
  stop_background "Scheduler daemon" "$DAEMON_PID_FILE"
fi

start_background \
  "Scheduler daemon" \
  "$DAEMON_PID_FILE" \
  "${LOG_DIR}/radio-daemon.start.log" \
  "${PYTHON_CMD[@]}" scripts/main_daemon.py --config "$CONFIG"

if [[ "${RADIO_START_BOT:-1}" == "1" ]]; then
  if [[ "${RADIO_RESTART_BOT:-1}" == "1" ]]; then
    stop_background "Telegram bot" "$BOT_PID_FILE"
  fi

  start_background \
    "Telegram bot" \
    "$BOT_PID_FILE" \
    "${LOG_DIR}/radio-bot.start.log" \
    "${PYTHON_CMD[@]}" scripts/main_bot.py --config "$CONFIG"
fi

if [[ "${RADIO_RESTART_API:-1}" == "1" ]]; then
  stop_background "Web console API" "$API_PID_FILE"
fi

start_background \
  "Web console API" \
  "$API_PID_FILE" \
  "${LOG_DIR}/radio-api.start.log" \
  "${PYTHON_CMD[@]}" scripts/main_api.py --host "$HOST" --port "$PORT" --config "$CONFIG"

if wait_for_api; then
  if [[ "${RADIO_OPEN_BROWSER:-1}" == "1" ]]; then
    open_browser
  fi
  echo "Radio-Oshikatsu console is ready: $URL"
else
  echo "API did not become ready within 30 seconds. Check ${LOG_DIR}/radio-api.start.log" >&2
  exit 1
fi
