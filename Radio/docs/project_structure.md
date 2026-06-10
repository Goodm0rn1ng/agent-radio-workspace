# 项目文件结构与职责

最近更新：2026-05-17

这份文档按当前工作树整理 Radio-Oshikatsu 的目录结构和文件职责。它描述“现在这套系统如何组织”，不是历史方案。

## 顶层文件

| 路径 | 作用 |
|---|---|
| `../AGENTS.md`、`../CLAUDE.md` | 编码协作规则，现统一放在工作区根目录，Radio 子项目沿用。 |
| `README.md` | 用户入口：项目简介、快速开始、常用命令和文档索引。 |
| `PRD.md` | 原始产品需求与长期方向，用来判断功能是否偏离初衷。 |
| `CHANGELOG.md` | 已完成能力和行为变化记录。 |
| `pyproject.toml` | Python 项目元数据、依赖、ruff、pytest 配置。 |
| `uv.lock` | uv 依赖锁定文件。 |
| `start.command` | 一键启动调度守护进程、本地 Web/API、Telegram Bot，并打开浏览器。 |
| `stop.command` | 停止 `start.command` 拉起的后台服务。 |
| `.env.example` | 环境变量模板；真实 `.env` 不入库。 |
| `.gitignore` | 忽略虚拟环境、缓存、运行时数据、密钥和本地截图。 |

## 配置目录

| 路径 | 作用 |
|---|---|
| `config/config.yaml` | 主配置：节目、调度、模型 provider、运行目录、并发等。 |
| `config/terminology.yaml` | 术语库和译后修正，供 STT prompt、翻译 prompt、summary 修正使用。 |
| `config/segments_library.yaml` | 常驻环节库。pipeline 总结后按节目 series 匹配，HITL approve 后会追加。 |
| `config/profiles/<profile_id>/profile.yaml` | 一个 Prompt Profile 的元数据。 |
| `config/profiles/<profile_id>/translate.txt` | 该 profile 的翻译 prompt。 |
| `config/profiles/<profile_id>/summarize.txt` | 该 profile 的总结 prompt。 |

当前内置 profile：

- `mygo_meigo_shukai`：MyGO!!!!!の「迷子集会」。
- `hina_radio`：羊宮妃那个人广播及相关声优节目。
- `general_seiyuu_radio`：通用声优广播、访谈、活动回顾。

## CLI 与服务入口

| 路径 | 作用 |
|---|---|
| `scripts/main_oneshot.py` | 本地音频文件一次性处理入口。 |
| `scripts/main_video.py` | 已有视频 URL 处理入口，先抽音频再跑 pipeline。 |
| `scripts/main_radiko.py` | Radiko live / time-free 单次处理入口。 |
| `scripts/main_youtube_live.py` | YouTube Live 单次录制入口，等待开播后定长录音。 |
| `scripts/main_daemon.py` | APScheduler 常驻调度器入口。 |
| `scripts/main_api.py` | 本地 FastAPI server 入口，服务前端控制台。 |
| `scripts/main_bot.py` | Telegram polling bot 入口，处理审批 callback 和命令。 |
| `scripts/metrics_report.py` | 聚合 `data/logs/metrics.jsonl`，输出运行报表。 |

## 核心代码目录

### 配置、模型与基础设施

| 路径 | 作用 |
|---|---|
| `src/radio/__init__.py` | Python package 标记。 |
| `src/radio/config.py` | 合并 `.env` 和 `config/config.yaml`，输出强类型 `Settings`。 |
| `src/radio/models.py` | 跨模块共享的 `Segment`、`Summary`、`ProgramSection` 等 Pydantic 模型。 |
| `src/radio/recordings_layout.py` | 生成 recordings work_dir 和安全 collection id。 |
| `src/radio/state_store.py` | SQLite 状态层，保存 job、run、artifact 快照。 |
| `src/radio/utils/logging.py` | loguru 日志初始化。 |
| `src/radio/utils/retry.py` | async retry 装饰器。 |
| `src/radio/utils/metrics.py` | pipeline metrics 收集、阈值 warning、jsonl 落盘。 |

### Source：把外部内容变成音频

| 路径 | 作用 |
|---|---|
| `src/radio/video_source.py` | 用 yt-dlp/ffmpeg 从已有视频 URL 抽音频。 |
| `src/radio/playlist.py` | 展开播放列表 index 范围，供前端批量提交。 |
| `src/radio/youtube_live_source.py` | YouTube Live 定长录制。 |
| `src/radio/live_detector.py` | 轮询 YouTube live metadata，等待开播。 |
| `src/radio/radiko_source.py` | Radiko live/time-free 录制主实现。 |
| `src/radio/radiko_playwright_source.py` | Radiko 浏览器兜底实现，用 Playwright 获取页面态。 |
| `src/radio/health.py` | 录制前健康检查和失败通知。 |

### Pipeline：单份音频处理链路

| 路径 | 作用 |
|---|---|
| `src/radio/pipeline.py` | 主编排：切片、STT、翻译、修正、总结、HITL、Telegram 推送、metrics。 |
| `src/radio/segmenter.py` | ffmpeg 音频切片。 |
| `src/radio/stt.py` | Groq Whisper 转写，含动态 STT prompt。 |
| `src/radio/translate.py` | DeepSeek 日常翻译和 Anthropic 精翻。 |
| `src/radio/terminology.py` | 术语库加载、prompt 格式化、译后修正。 |
| `src/radio/transcript.py` | 生成中日双语 transcript 文本。 |
| `src/radio/summarize.py` | Gemini / Anthropic 总结、JSON 解析修复、library 匹配。 |
| `src/radio/prompts/translate.txt` | 默认翻译 prompt 模板。 |
| `src/radio/prompts/summarize.txt` | 默认总结 prompt 模板。 |

### Knowledge 与 Review

| 路径 | 作用 |
|---|---|
| `src/radio/profiles.py` | Prompt Profile 的加载、保存和 settings 覆盖。 |
| `src/radio/segments_library.py` | 常驻环节库加载、匹配、格式化和追加。 |
| `src/radio/history.py` | 往期 key topics/highlights 的轻量 RAG 召回。 |
| `src/radio/approval.py` | 新环节 pending queue，approve 后写入 library。 |
| `src/radio/telegram_bot.py` | Telegram 审批 bot、`/status`、`/pending`。 |

### Delivery 与 API

| 路径 | 作用 |
|---|---|
| `src/radio/telegram_sender.py` | 将 summary、附件、待审批候选推送到 Telegram。 |
| `src/radio/scheduler.py` | APScheduler 注册和执行 scheduled programs。 |
| `src/radio/jobs.py` | API 后台任务管理，负责排队、等待、录制、串行 pipeline。 |
| `src/radio/api.py` | 本地 FastAPI：前端、jobs、artifacts、profiles、knowledge、metrics。 |

## 前端目录

| 路径 | 作用 |
|---|---|
| `frontend/index.html` | 本地控制台页面结构。 |
| `frontend/app.js` | 前端状态、表单提交、WebSocket、任务列表、drawer、Knowledge 操作。 |
| `frontend/styles.css` | 控制台样式。 |

前端必须通过 `scripts/main_api.py` 提供的本地 API 使用；直接打开 HTML 文件不会得到完整功能。

## 测试目录

当前测试都覆盖正在使用的模块，没有发现应删除的源码级废弃测试。

| 路径 | 覆盖内容 |
|---|---|
| `tests/test_api_artifacts.py` | artifact 路径安全、文件信息、knowledge API。 |
| `tests/test_approval.py` | pending segments approve/skip、去重、library 写入。 |
| `tests/test_history.py` | 轻量 RAG 相关度和时间衰减排序。 |
| `tests/test_jobs.py` | API JobManager 批处理、串行 pipeline、状态持久化。 |
| `tests/test_pipeline_artifacts.py` | pipeline 中间 JSON 落盘和翻译失败统计。 |
| `tests/test_playlist.py` | playlist index 范围展开。 |
| `tests/test_profiles.py` | profile 保存、列表、settings 覆盖。 |
| `tests/test_recordings_layout.py` | work_dir 和 collection id 规范化。 |
| `tests/test_scheduler.py` | Radiko time-free 周期推导和调度配置。 |
| `tests/test_segments_library.py` | 节目 series 提取。 |
| `tests/test_state_store.py` | SQLite job/artifact 持久化和 stale job 标记。 |
| `tests/test_video_source.py` | 视频站点解析辅助函数。 |

## 文档目录

| 路径 | 作用 |
|---|---|
| `docs/project_structure.md` | 当前文件结构和职责地图。 |
| `docs/architecture.md` | 数据流和模块级架构说明。 |
| `docs/product_architecture.md` | 产品对象、维护边界和演进顺序。 |
| `docs/frontend_backend_api.md` | 前端对接 API 草案。 |
| `docs/deployment.md` | 本机 Mac、launchd、API、Telegram bot、未来上云部署说明。 |
| `docs/decisions/*.md` | ADR：关键技术决策的背景和取舍。 |

## 部署与运行时目录

| 路径 | 作用 |
|---|---|
| `deploy/radio.plist` | macOS launchd 配置模板。 |
| `data/recordings/` | 运行产物：音频、transcript、summary、中间 JSON。只保留 `.gitkeep`。 |
| `data/logs/` | 运行日志和 metrics。只保留 `.gitkeep`。 |
| `data/history_context.jsonl` | 往期历史上下文，运行时生成，不入库。 |
| `data/pending_segments.json` | 待审批新环节队列，运行时生成，不入库。 |
| `data/state.sqlite` | API job/run/artifact 状态库，运行时生成，不入库。 |
| `data/scheduler.sqlite` | APScheduler jobstore，运行时生成，不入库。 |
| `data/qa/` | 本地截图/视觉 QA 临时产物，不入库。 |

## 清理原则

- 保留当前行为有测试覆盖的源码和测试。
- 删除 `.DS_Store`、`__pycache__`、`.pytest_cache`、`.ruff_cache`、本地 QA 截图等运行/系统产物。
- 不把 `.env`、cookies、logs、recordings、SQLite、pending queue 提交进仓库。
- 不删除 ADR、PRD、CHANGELOG；它们用于解释历史决策，不算废案。
