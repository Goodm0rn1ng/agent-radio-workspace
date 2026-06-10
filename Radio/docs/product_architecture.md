# Product Architecture

最近更新：2026-05-17

这份文档定义 Radio-Oshikatsu 的产品级对象和维护边界。后续新增节目、入口、输出渠道或前端页面时，先判断它属于哪一层，再落代码。

## 分层

| 层 | 职责 | 主要模块 |
|---|---|---|
| Source | 把外部内容变成一份本地音频和基础 metadata | `video_source.py`, `youtube_live_source.py`, `radiko_source.py`, `playlist.py` |
| Job | 接收用户请求、排队、等待、记录状态 | `api.py`, `jobs.py`, `state_store.py` |
| Pipeline | 对单份音频做 STT、翻译、总结、推送 | `pipeline.py`, `segmenter.py`, `stt.py`, `translate.py`, `summarize.py` |
| Knowledge | 节目策略与可复用知识 | `profiles.py`, `terminology.py`, `segments_library.py`, `history.py` |
| Review | 人工确认系统发现的新知识 | `approval.py`, `telegram_bot.py` |
| Delivery | 将结果交付给用户或存档 | `telegram_sender.py`, `recordings_layout.py`, `data/recordings/` |

## 产品对象

| 对象 | 定义 | 持久化位置 |
|---|---|---|
| Collection | 用户选择的收纳分类，决定 `recordings/` 下的一级目录 | profile id 或 API 请求；运行时进入 job state |
| Profile | 一套 prompt、术语和节目策略 | `config/profiles/<profile_id>/` |
| Program | 一个节目系列，如 `羊宮妃那のこもれびじかん` | `config/segments_library.yaml` / profile |
| Episode | 一期具体内容，如 `#3 2025年4月20日放送` | `data/recordings/<collection>/<episode>/` |
| SourceItem | 一条用户导入的 URL、播放列表 item 或 live 录制请求 | `state.sqlite` job item |
| Job | 用户提交的一次批处理或 live 任务 | `state.sqlite` job |
| Run | 一次实际 pipeline 执行，必须有稳定 `run_id` | `state.sqlite` run + `metrics.jsonl` |
| Artifact | 一次 run 产出的文件或外部投递结果 | `state.sqlite` artifact + 文件系统 |
| Approval | 一个待人工确认的新环节候选 | `data/pending_segments.json`，后续迁入 SQLite |

## 状态原则

- `config/` 只放人工维护的策略和知识，不放运行中状态。
- `data/recordings/` 放可阅读、可归档的内容产物。
- `data/logs/` 放 append-only 日志和 metrics。
- `data/state.sqlite` 放前端/API 需要查询的任务状态、run 状态和 artifact 索引。
- API 重启后不自动恢复正在跑的进程；旧的 `queued/running/waiting` job 会标记为 `failed`，原因是 `server restarted`。恢复执行能力单独作为后续功能做。

## 新功能落点

新增视频站点或直播平台：优先增加 Source adapter，不改 pipeline。

新增总结风格：优先增加 Profile，不改翻译/总结核心逻辑。

新增输出渠道：优先增加 Delivery adapter，不改 pipeline 上游。

新增审批对象：优先扩展 Review 层，避免把人工判断写进 Source 或 Pipeline。

新增前端页面：优先从 `state.sqlite` 读状态，不扫日志文本。

## 近期演进顺序

1. P0：SQLite job/run/artifact 状态层，保留现有内存任务执行方式。
2. P1：前端 Job Dashboard 和 Artifact 页面。
3. P1：Knowledge 管理 UI，减少手改 YAML。
4. P2：Source / Delivery adapter 接口化。
5. P2：Profile 版本化和 golden case 质量评估。
