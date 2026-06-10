# 部署细则

最近更新：2026-05-17

当前部署目标是“本机 Mac 上长期运行”。上云仍是 v2 议题，只有当本机休眠、关机或网络环境导致漏录时再做。

## 本机 Mac 部署

### 前置条件

- Python 3.11+。
- `uv`。
- `ffmpeg`。
- Playwright 浏览器依赖。
- Telegram Bot Token 和 chat id。
- Groq、DeepSeek、Anthropic、Gemini API key。

推荐安装：

```bash
brew install ffmpeg
uv sync
uv run playwright install chromium
cp .env.example .env
```

然后手动编辑 `.env`。不要把 `.env`、cookies、logs、recordings 或 SQLite 文件提交进仓库。

### 一键启动工作台

```bash
./start.command
```

默认行为：

- 后台启动 APScheduler 守护进程。
- 后台启动本地 Web/API 控制台。
- 后台启动 Telegram 审批 Bot。
- 等待 API ready。
- 自动打开默认浏览器到 `http://127.0.0.1:8000`。
- 日志写入 `data/logs/radio-daemon.start.log`、`data/logs/radio-api.start.log` 和 `data/logs/radio-bot.start.log`。
- pid 写入 `data/logs/radio-daemon.pid`、`data/logs/radio-api.pid` 和 `data/logs/radio-bot.pid`，重复运行不会重复启动同一个服务。

可选环境变量：

```bash
RADIO_PORT=8010 ./start.command
RADIO_CONFIG=config/config.yaml ./start.command
RADIO_START_BOT=0 ./start.command
RADIO_OPEN_BROWSER=0 ./start.command
```

`RADIO_START_BOT=0` 会跳过 Telegram 审批 Bot。

停止由 `start.command` 拉起的后台服务：

```bash
./stop.command
```

### 一次性任务

本地音频：

```bash
uv run python scripts/main_oneshot.py path/to/audio.mp3
```

已有视频 URL：

```bash
uv run python scripts/main_video.py "https://example.com/video" --profile general_seiyuu_radio
```

YouTube Live 单次录制：

```bash
uv run python scripts/main_youtube_live.py "https://www.youtube.com/@example/live" --duration 60 --title "节目名"
```

### 常驻调度器

调度器读取 `config/config.yaml` 的 `scheduled_programs`：

```bash
uv run python scripts/main_daemon.py
```

支持的 source：

- `radiko_live`
- `radiko_timefree`
- `youtube_live`

调度器使用 APScheduler 和 SQLite jobstore。任务注册和下次触发时间会写入日志。

### Telegram 审批 Bot

HITL 审批需要单独启动 bot：

```bash
uv run python scripts/main_bot.py
```

bot 当前处理：

- 新环节 `入库 / 跳过` inline callback。
- `/status`
- `/pending`

全文审批和 library 编辑还未实现。

### 本地 Web/API 控制台

启动 API：

```bash
uv run python scripts/main_api.py --host 127.0.0.1 --port 8000
```

然后打开：

```text
http://127.0.0.1:8000
```

当前 API 是本地控制台，不应直接暴露公网。Credentials 面板只显示配置状态；真实密钥应通过 `.env`、环境变量或部署平台 secret 管理。

## launchd

`deploy/radio.plist` 是 macOS launchd 模板，用于常驻调度器。使用前需要确认：

- `ProgramArguments` 指向当前仓库路径。
- 工作目录是当前仓库。
- 环境变量或 `.env` 已准备好。
- 日志目录存在。

安装示例：

```bash
cp deploy/radio.plist ~/Library/LaunchAgents/com.radio-oshikatsu.daemon.plist
launchctl load ~/Library/LaunchAgents/com.radio-oshikatsu.daemon.plist
launchctl start com.radio-oshikatsu.daemon
```

卸载：

```bash
launchctl stop com.radio-oshikatsu.daemon
launchctl unload ~/Library/LaunchAgents/com.radio-oshikatsu.daemon.plist
```

## 运行时数据

这些文件属于本地运行状态，不入库：

- `data/recordings/`
- `data/logs/`
- `data/history_context.jsonl`
- `data/pending_segments.json`
- `data/state.sqlite`
- `data/scheduler.sqlite`
- `radiko_cookies*.json`
- `cookies*.json`

需要长期备份时，优先备份：

- `config/terminology.yaml`
- `config/segments_library.yaml`
- `config/profiles/`
- `data/history_context.jsonl`
- `data/recordings/`

## 上云计划

暂不做 Docker 或 VPS 一键部署。上云前要先补齐：

- API 鉴权。
- systemd unit。
- 数据备份策略。
- Radiko 地域/IP 可用性验证。
