#!/usr/bin/env bash
set -euo pipefail

# 本地服务全部交给 launchd 管理（RunAtLoad + KeepAlive：登录自启、崩溃自动重启）。
# 本脚本是 launchctl 的薄包装：
#   1. 同步 scripts/com.agent.*.plist 到 ~/Library/LaunchAgents（有差异时覆盖并重载）
#   2. brew services start neo4j（brew 自身注册 launchd 登录自启）
#   3. 各服务：未加载 -> bootstrap；已加载 -> kickstart -k 重启（加载最新代码）
# 改了代码想生效：直接重跑本脚本即可。

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="gui/$(id -u)"
PLIST_SRC="$PROJECT_DIR/scripts"
PLIST_DST="$HOME/Library/LaunchAgents"
RADIO_DIR="$PROJECT_DIR/Radio"
PORT="${PORT:-8000}"
URL="http://127.0.0.1:$PORT"

AGENTS=(com.agent.radio-kg-server com.agent.radio-scheduler-daemon com.agent.radio-telegram-bot)

# Telegram bot 没配 token 会在 KeepAlive 下无限崩溃重启，未配置时跳过它
BOT_TOKEN=""
if [ -f "$RADIO_DIR/.env" ]; then
  BOT_TOKEN="$(awk -F= '/^[[:space:]]*TELEGRAM_BOT_TOKEN[[:space:]]*=/{sub(/^[^=]*=/,"",$0); gsub(/[[:space:]"]/,"",$0); print; exit}' "$RADIO_DIR/.env")"
fi

wait_for_port() {
  local port="$1" secs="$2"
  for _ in $(seq 1 "$secs"); do
    nc -z 127.0.0.1 "$port" >/dev/null 2>&1 && return 0
    sleep 1
  done
  return 1
}

echo "==> Neo4j (brew services, 登录自启)"
if command -v brew >/dev/null 2>&1; then
  if ! nc -z 127.0.0.1 7687 >/dev/null 2>&1; then
    # restart 而非 start：清掉 launchd 偶发的 error 残留状态（start 会假成功）
    brew services restart neo4j
  else
    echo "Neo4j already listening on 7687."
  fi
  if ! wait_for_port 7687 60; then
    echo "Retrying neo4j restart once..."
    brew services restart neo4j
    wait_for_port 7687 60 || { echo "Neo4j did not come up on 7687."; exit 1; }
  fi
  echo "Neo4j ready."
else
  wait_for_port 7687 5 || { echo "brew 不可用且 Neo4j 未在 7687 监听。"; exit 1; }
fi

echo
echo "==> launchd agents"
mkdir -p "$RADIO_DIR/data/logs" "$PROJECT_DIR/radio_kg/data"
for label in "${AGENTS[@]}"; do
  src="$PLIST_SRC/$label.plist"
  dst="$PLIST_DST/$label.plist"
  if [ "$label" = "com.agent.radio-telegram-bot" ] && [ -z "$BOT_TOKEN" ]; then
    echo "  $label: skipped (Radio/.env 没有 TELEGRAM_BOT_TOKEN)"
    launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || true
    continue
  fi
  if ! cmp -s "$src" "$dst" 2>/dev/null; then
    cp "$src" "$dst"
    launchctl bootout "$DOMAIN/$label" >/dev/null 2>&1 || true
  fi
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl kickstart -k "$DOMAIN/$label"
    echo "  $label: restarted (kickstart -k)"
  else
    launchctl bootstrap "$DOMAIN" "$dst"
    echo "  $label: loaded (bootstrap)"
  fi
done

echo
echo "==> Waiting for $URL ..."
for _ in $(seq 1 120); do
  curl -fsS "$URL/api/health" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS "$URL/api/health" >/dev/null 2>&1; then
  echo "Server did not become healthy. Logs:"
  tail -30 "$PROJECT_DIR/radio_kg/data/server.launchd.err.log" 2>/dev/null || true
  exit 1
fi

open "$URL/?radio_kg_refresh=$(date +%s)"

echo
echo "============================================================"
echo "Agent services are managed by launchd (auto-start at login,"
echo "auto-restart on crash)."
echo
echo "Status:   launchctl print $DOMAIN/com.agent.radio-kg-server | head"
echo "Stop all: $PROJECT_DIR/agent-down.command"
echo
echo "Endpoints:"
echo "  Frontend:  $URL/"
echo "  Dashboard: $URL/dashboard"
echo "  Radio:     $URL/radio"
echo "  Clipper:   $URL/clipper"
echo "  Health:    $URL/api/health"
echo
echo "Logs:"
echo "  server:    $PROJECT_DIR/radio_kg/data/server.launchd.{log,err.log}"
echo "  scheduler: $RADIO_DIR/data/logs/radio-daemon.launchd.{log,err.log}"
echo "  bot:       $RADIO_DIR/data/logs/radio-bot.launchd.{log,err.log}"
echo "============================================================"
