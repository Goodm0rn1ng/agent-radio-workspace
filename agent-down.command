#!/usr/bin/env bash
set -euo pipefail

# 停止全部本地服务：bootout launchd agents（KeepAlive 不会再复活）+ 停 Neo4j。
# 重新启动用 ./agent-up.command。

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
DOMAIN="gui/$(id -u)"
AGENTS=(com.agent.radio-kg-server com.agent.radio-scheduler-daemon com.agent.radio-telegram-bot)

echo "==> launchd agents"
for label in "${AGENTS[@]}"; do
  if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
    launchctl bootout "$DOMAIN/$label" && echo "  $label: stopped"
  else
    echo "  $label: not loaded"
  fi
done

echo
echo "==> Homebrew Neo4j"
if command -v brew >/dev/null 2>&1; then
  brew services stop neo4j || true
else
  echo "brew not available; skipped."
fi

# 兜底：手动 start.command / nohup 等方式遗留的进程
echo
LEFTOVER="$(pgrep -fl 'uvicorn src.server.app:app|scripts/main_daemon.py|scripts/main_bot.py' || true)"
if [ -n "$LEFTOVER" ]; then
  echo "WARNING: 仍有非 launchd 管理的进程在运行（手动启动的残留）："
  echo "$LEFTOVER"
  echo "$LEFTOVER" | awk '{print $1}' | xargs kill 2>/dev/null || true
  echo "已尝试 kill。"
fi

echo
echo "All targeted components stopped."
