# 架构说明

最近更新：Unreleased（2026-05-17）

## 概览

Radio-Oshikatsu 把一段日语广播内容（来自直播录制、已有视频 URL、或本地音频）
跑通同一条**通用音频 pipeline**，最终把"中文摘要 + 关键话题 + 高光时间戳 + 分段复盘 +
中日双语逐字稿"推到 Telegram。

四种入口共用同一条核心 pipeline；差异只在"如何拿到一份音频文件"这一步。

```
入口：
  scripts/main_oneshot.py  本地音频文件
  scripts/main_video.py    Bili / YouTube 等已有视频 URL（v0.2.0 加入）
  scripts/main_radiko.py   Radiko live / time-free 单次处理
  scripts/main_youtube_live.py  YouTube Live 单次处理
  scripts/main_daemon.py   APScheduler 守护进程 + Radiko/YouTube 定时录制
  scripts/main_api.py      本地 FastAPI，供前端提交后台任务
        │
        ▼
   ┌──────────────────────────────────────────────────────────┐
   │            run_pipeline(audio_path, settings, ...)        │
   │                                                            │
   │  segment_audio  →  transcribe_segments  →  translate      │
   │       │                  │                     │           │
   │       │                  │                     ▼           │
   │       │                  │            apply_terminology    │
   │       │                  │             apply_name_corr     │
   │       │                  │                     │           │
   │       │                  │                     ▼           │
   │       │                  │            write_bilingual_txt  │
   │       │                  │                     │           │
   │       │                  │                     ▼           │
   │       │                  │                summarize        │
   │       │                  │            (Gemini / Anthropic) │
   │       │                  │                     │           │
   │       │                  │                     ▼           │
   │       │                  │            send_to_telegram     │
   │       │                  │                     │           │
   │       │                  │                     ▼           │
   │       │                  │                  cleanup        │
   └──────────────────────────────────────────────────────────┘
```

## 数据流

### 轨道 A：已有视频 URL（v0.2.0 已实现）

```
[scripts/main_video.py]
   ↓
[video_source.extract_audio_from_video_url]
   yt-dlp 下载最佳音频 → ffmpeg 抽 m4a
   ↓
[pipeline.run_pipeline]  ← 通用音频 pipeline 起点
```

支持 `--cookies`（登录态视频）、`--title`（覆盖 Telegram 标题）、
`--keep-audio`（保留抽出的音频文件，便于排查）、
`--fine-translation`（切换到 Claude Haiku 精翻）。

### 轨道 B：本地音频文件（v0.1.0 起）

```
[scripts/main_oneshot.py]  接收本地音频文件
   ↓
[pipeline.run_pipeline]
```

### 轨道 C：定时直播录制（v0.5.0+ 已实现）

```
[APScheduler 唤醒]
   ↓
[radiko_live]     record_radiko_live 定长拉流
   or
[radiko_timefree] seed URL + interval_days 推导当期 /ts/ URL
   or
[youtube_live]    live_detector 等开播 → yt-dlp --live-from-start 定长录制
   ↓
[pipeline.run_pipeline]
   ↓
[cleanup]         删除本地音频，记录运行指标
```

### 前端 API：任务提交层（Unreleased）

```
[frontend]
   ↓
[FastAPI /api/video-jobs]        批量视频 URL / playlist index 范围
[FastAPI /api/live-jobs]         直播 URL + start_at + duration
[FastAPI /api/jobs/{job_id}]     轮询任务状态
   ↓
[jobs.JobManager]                后台 task 顺序处理 / 等待到点 / 更新状态
   ↓
[video_source / youtube_live_source / radiko_source]
   ↓
[pipeline.run_pipeline]
```

API server 仍由本机单进程执行后台任务，但 job / run / artifact 状态会写入
`data/state.sqlite`，供前端跨刷新、跨重启查看。重启后不会自动恢复正在跑的进程；
旧的 `queued/running/waiting` job 会标记为失败，后续如需断点恢复再单独实现。
产品对象与维护边界见 [Product Architecture](./product_architecture.md)。

## 通用音频 pipeline（`pipeline.run_pipeline`）

```
1. segment_audio       ffmpeg 把长音频按 120s 切成小段
2. transcribe_segments asyncio.gather 并发提交 Groq Whisper-large-v3（日文，含时间戳）
3. translate_segments  日常 DeepSeek V4 Flash；fine_translation=True 时切 Claude Haiku
4. apply_name_corrections          配置中遗留的轻量替换
5. apply_terminology_corrections   术语库 post_corrections 机械修正
6. write_bilingual_txt             拼接双语 .txt（每段：[HH:MM:SS] JP / CN）
7. summarize                       Gemini Flash（默认）/ Claude Sonnet
                                   responseSchema 强约束输出
                                   {summary, sections[], key_topics, highlights}
                                   prompt 注入 segments_library
8. apply_summary_corrections       对 Summary 全字段递归术语修正
8b. _apply_segments_library        按 title_ja 匹配常驻环节库；
                                   命中则覆盖 intro，标记 is_recurring=True
9. send_to_telegram                Markdown 多条消息（header / sections / highlights）
                                   + 双语 .txt 附件
10. cleanup                        删除音频切片，保留 .txt 产物
```

## 模块职责

| 模块 | 输入 | 输出 | 备注 |
|---|---|---|---|
| `config.py` | `.env` + `config/config.yaml` | 强类型 `Settings` | pydantic-settings + yaml |
| `api.py` | HTTP request | 后台 job id / job status | FastAPI，本地前端入口 |
| `jobs.py` | API 请求参数 | `JobRecord` | in-memory 任务管理，后台跑 pipeline |
| `state_store.py` | `JobRecord` 快照 | SQLite job / run / artifact 索引 | API 重启后仍可查询历史状态 |
| `models.py` | — | `Segment` / `Highlight` / `ProgramSection` / `Summary` | 跨模块共享数据结构 |
| `utils/logging.py` | logs_dir | 全局 loguru | 控制台彩色 + 文件按天轮转 14 天 |
| `utils/retry.py` | — | `@async_retry` 装饰器 | 指数退避，可配异常白名单 |
| `video_source.py` | 视频 URL | `VideoAudio(audio_path, title, source_url)` | 轨道 A 入口；yt-dlp + ffmpeg |
| `playlist.py` | playlist URL + index 范围 | `list[PlaylistItem]` | 前端批量导入用，支持倒序范围 |
| `live_detector.py` | YouTube live URL | `YouTubeLiveInfo` | 轮询 yt-dlp metadata 直到 `live_status=is_live` |
| `youtube_live_source.py` | YouTube live URL + 时长 | `YouTubeLiveAudio` | yt-dlp `--live-from-start` + ffmpeg 定长录制 |
| `segmenter.py` | 音频文件 | `[(切片路径, 偏移秒)]` | ffmpeg `-f segment -c copy` |
| `stt.py` | 切片列表 | `list[Segment]`（日文） | Groq AsyncGroq + Semaphore 并发控 |
| `translate.py` | 日文 segments + provider | 中文回填的 segments | DeepSeek HTTP / Anthropic SDK 双路径 |
| `terminology.py` | terminology.yaml | prompt 用清单 + post_corrections dict | 双层防护 |
| `transcript.py` | 双语 segments | 双语 .txt 文件 | 含轻量 `name_corrections` |
| `summarize.py` | 完整双语 segments | `Summary` 对象 | Gemini / Anthropic provider 切换 |
| `telegram_sender.py` | Summary + .txt | Telegram 消息 + 文档 | MarkdownV2，超长自动截断 |
| `pipeline.py` | audio_path + Settings | (副作用：Telegram 已推送) | 编排上述所有模块的直线 pipeline |

## 配置体系

两层配置，合并在 `Settings`：

- `.env`（敏感）：`GROQ_API_KEY` / `DEEPSEEK_API_KEY` / `ANTHROPIC_API_KEY` /
  `GEMINI_API_KEY` / `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID`
- `config/config.yaml`（行为）：节目 schedule / STT 参数 / translation 双 provider /
  summary provider / runtime（目录、并发）/ `name_corrections`
- `config/terminology.yaml`（术语库）：MyGO!!!!! / 声优广播专属词条 + post_corrections

## 关键决策（ADR 索引）

| 编号 | 主题 |
|---|---|
| [0001](./decisions/0001-why-no-langgraph.md) | v1 不引入 LangGraph |
| [0002](./decisions/0002-why-groq-whisper.md) | STT 选 Groq Whisper-large-v3 |
| [0003](./decisions/0003-translation-strategy.md) | 翻译策略：分批 + 段对齐 + 校验重试 |
| [0004](./decisions/0004-terminology-system.md) | 术语库双层防护（prompt + 译后） |
| [0005](./decisions/0005-gemini-summary-with-anthropic-fallback.md) | Gemini 默认总结，Anthropic 备选 |
| [0006](./decisions/0006-fine-translation-opt-in.md) | `--fine-translation` opt-in（Claude Haiku） |
| [0007](./decisions/0007-structured-section-summary.md) | 结构化分段总结（ProgramSection） |
