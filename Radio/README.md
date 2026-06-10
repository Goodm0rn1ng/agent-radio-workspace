# Radio-Oshikatsu

> 自动录制日本 YouTube 直播 / 处理已有视频 → 双语逐字稿 + 高光摘要 → Telegram 推送

一个自用的内容自动化工具：到固定时间自动录一个 YouTube 直播节目，或手动提交一个已有视频 URL，处理后把"中文摘要 + 关键话题 + 高光时间戳 + 中日双语逐字稿"推到我的 Telegram。

详细产品需求见 [PRD.md](./PRD.md)，v1 实施范围见 [CHANGELOG.md](./CHANGELOG.md)。

---

## 当前状态

**v0.5.0+** — 已有视频 URL / 本地音频文件 / Radiko live / Radiko time-free / YouTube Live → 多条 Telegram 消息（含分段复盘 + 常驻环节匹配） + 双语 .txt 附件。

后续：HITL 审批流、时间加权 RAG、上云部署。详见 [CHANGELOG.md](./CHANGELOG.md)。

---

## 快速开始

### 一键启动工作台

```bash
./start.command
```

这会在后台启动调度守护进程、本地 Web/API 控制台和 Telegram 审批 Bot，并自动打开默认浏览器到
`http://127.0.0.1:8000`。日志写入 `data/logs/`。

如临时不想启动 Telegram 审批 Bot：

```bash
RADIO_START_BOT=0 ./start.command
```

停止后台服务：

```bash
./stop.command
```

合并到上层 `Agent` 工作区后，推荐从根目录运行 `../agent-up.command`。它会把本控制台作为
`radio_kg` 的 `/radio` 子应用运行在同一端口，并设置 `RADIO_KG_AUTO_INGEST_URL`，让 pipeline 成功产出新一期后自动触发 `radio_kg` 入库。

### 前置条件

- macOS（Linux 也行，未测试）
- Python 3.11+
- `ffmpeg`（音频切片、视频抽音频需要）
- 一个 Telegram Bot Token + 你自己的 chat_id
- Groq、Deepseek、Anthropic、Gemini 的 API Key

### 安装

```bash
# 1. 装 uv（Python 包管理器，比 pip 快 10 倍）
curl -LsSf https://astral.sh/uv/install.sh | sh

# 2. 装 ffmpeg
brew install ffmpeg

# 3. 克隆并进入项目
cd /path/to/Radio

# 4. 创建虚拟环境并装依赖
uv sync

# 5. 复制环境变量模板并填入真实值
cp .env.example .env
# 然后编辑 .env，填入 5 个 API key
```

### 跑一次本地音频（M1）

```bash
uv run python scripts/main_oneshot.py path/to/audio.mp3
```

会读 `.env` 和 `config/config.yaml`，把音频转写成日文 → 翻译成中文 → 总结 → 推到你 Telegram。

### 跑一次已有视频（Bili / YouTube 等）

```bash
uv run python scripts/main_video.py "https://www.bilibili.com/video/BV..."
```

会先用 `yt-dlp` 抽出音频，再复用同一条 pipeline：语音识别 → 双语翻译 → 术语库 + 常驻环节库修正 → 结构化分段总结 → Telegram。可选参数：

```bash
# 视频需登录态
uv run python scripts/main_video.py "URL" --cookies path/to/cookies.txt

# 覆盖 Telegram 推送时的节目标题
uv run python scripts/main_video.py "URL" --title "MyGO!!!!!の「迷子集会」#178"

# 切到 Claude Haiku 精翻（默认 DeepSeek Flash）
uv run python scripts/main_video.py "URL" --fine-translation
```

处理新节目时，建议始终传 `--title`，标题里保留稳定的节目系列名和期数，例如：

```bash
uv run python scripts/main_video.py "URL" --title "XXのラジオ #12" --profile hina_radio
```

系统会按标题剥出系列名，用来隔离常驻环节库和往期回忆。Prompt Profile 放在
`config/profiles/<profile_id>/`，当前内置：

- `mygo_meigo_shukai`：MyGO!!!!!の「迷子集会」
- `hina_radio`：羊宮妃那个人广播 / 相关声优节目
- `general_seiyuu_radio`：大众向声优节目 / 访谈 / 活动回顾

前端的 Prompt Profiles 面板可以新增 profile；自定义 prompt 必须保留占位符：
`translation_prompt` 里保留 `{input_json}`，`summary_prompt` 里保留 `{transcript}`。

### 跑一次 YouTube Live

```bash
uv run python scripts/main_youtube_live.py "https://www.youtube.com/@example/live" \
  --duration 60 \
  --title "节目名"
```

脚本会等待 YouTube 报告 `live_status=is_live`，然后用 yt-dlp `--live-from-start`
定长录制音频，再复用同一条 pipeline。

### 常驻调度器

```bash
uv run python scripts/main_daemon.py
```

`config/config.yaml` 的 `scheduled_programs` 支持：

- `radiko_live`：填 station、时间、持续时长。
- `radiko_timefree`：填 seed URL、时间、持续时长；系统按 `interval_days` 推导下一期 URL。
- `youtube_live`：填 live URL、时间、持续时长；系统等待开播后录制。

### Telegram 审批 Bot（HITL）

```bash
uv run python scripts/main_bot.py
```

pipeline 发现新环节时会先写入 `data/pending_segments.json`，并在 Telegram 里附
`👍 入库` / `❌ 跳过` 按钮。只有点 `入库` 后才会写入 `config/segments_library.yaml`。

Bot 命令：

- `/status`：查看最近一次运行、耗时、环节数、library 命中数、待审批数。
- `/pending`：列出最近待审批的新环节。

### 本地 API（前端对接用）

```bash
uv run python scripts/main_api.py --host 127.0.0.1 --port 8000
```

当前 API 覆盖：

- `POST /api/video-jobs`：提交单条或多条视频 URL，进入提取 → 翻译 → 总结 → Telegram。
- `POST /api/playlists/expand`：预览播放列表 index 范围，例如 `178 -> 1`。
- `POST /api/live-jobs`：提交直播 URL、开始时间、持续时长；到点先完整录制，再进入 pipeline。
- `GET /api/jobs/{job_id}`：查询后台任务状态。

### 节目知识库（v0.2.x 新增）

- `config/terminology.yaml` — 角色 / 声优 / 歌曲名等专有名词。LLM 翻译时注入 prompt；译后做 `post_corrections` 机械替换兜底。
- `config/segments_library.yaml` — 节目常驻环节（如《迷子集会》"僕、私、迷子中"）。LLM 总结时注入 prompt；命中环节自动覆盖介绍为库里的标准描述，未命中环节会在 Telegram 上打 🆕 标签。新发现的环节由你手动追加到 YAML 中。

---

## 项目结构

```
radio-oshikatsu/
├── README.md                    # 你正在看
├── CHANGELOG.md                 # 版本日志
├── PRD.md                       # 原始产品需求
├── docs/                        # 架构说明 + ADR
├── pyproject.toml               # 依赖清单
├── .env.example                 # 环境变量模板
├── config/config.yaml           # 节目配置
├── src/radio/                   # 核心代码
├── scripts/                     # CLI 入口
└── data/                        # 运行时音频/日志（gitignored）
```

---

## 文档

- [项目文件结构](./docs/project_structure.md) — 当前目录和各文件职责
- [架构说明](./docs/architecture.md) — 数据流与模块职责
- [产品架构](./docs/product_architecture.md) — 维护边界、产品对象与演进顺序
- [前端对接 API 草案](./docs/frontend_backend_api.md) — 本地 API endpoints 与请求示例
- [部署细则](./docs/deployment.md) — 服务器/守护进程配置
- [架构决策记录](./docs/decisions/) — 为什么这样选，不那样选

---

## 协作

本项目由 [Claude](https://claude.com/claude-code) 作为技术联合创始人协助构建，代码风格遵循工作区根目录的 [CLAUDE.md](../CLAUDE.md)。
