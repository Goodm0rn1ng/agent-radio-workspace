# Changelog

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)。

版本节奏（v1 阶段）：完成一个里程碑或一组实质性能力升级时升次版本号，bug 修复升补丁号。M5 完成升 v1.0.0。

## [Unreleased]

### Changed — 工作区唯一 venv + launchd 接管（2026-06-10）
- Radio 不再持有独立 `.venv`：以 editable 包装进工作区唯一 venv `Agent/.venv`（依赖声明不变，仍在 `Radio/pyproject.toml`；锁文件改用工作区根 `Agent/uv.lock`）。`start.command` 改用工作区 venv，缺失时提示 `uv sync`。
- `telegram_bot.py`：clip 切片回调注册不再做 sys.path 注入，直接 `from clip.telegram_clip import ...`（clip 同装于工作区 venv）。
- scheduler daemon / telegram bot 由 launchd 管理（RunAtLoad + KeepAlive，登录自启、崩溃自动重启）；启停统一走 `Agent/agent-up.command` / `agent-down.command`。


### Fixed — YouTube 直播录制时长上限失效 / 取消泄漏子进程
- `src/radio/youtube_live_source.py` — `--live-from-start` 会强制 yt-dlp 使用原生 `dashsegments` 下载器，导致 `--downloader ffmpeg --downloader-args ffmpeg_i:-t N` 被静默忽略，录制无时长上限、会一直拉取 DVR 回放（实测 1 分钟挂钟拉到 28.8 MB / 约 32 分钟内容）。
  - 改为在子进程外加**挂钟超时**：到 `duration_minutes` 后向 yt-dlp 发 `SIGINT`，让它停止直播下载并仍把已录片段封装/抽取为 m4a（实测「reached N min, stopping」后 1 秒内 `youtube_live.m4a` 就绪）。
  - 超时后若 `_FINALIZE_TIMEOUT_S`(120s) 内未收尾，再 `SIGKILL` 整个进程组兜底。
  - 子进程以 `start_new_session=True` 启动；任务被取消（`CancelledError`）时同样发 SIGINT 收尾并杀进程组，修复此前「取消任务后 yt-dlp 子进程仍在后台继续下载」的泄漏。
  - 我方主动停止时 yt-dlp 的非零退出码（如 SIGINT 130）不再视为失败，是否成功改由 `_find_downloaded_audio` 决定。
- 依赖：`radio_kg/.venv` 缺失 `yt-dlp`（`requirements.txt` 已声明但未安装），补装 `yt-dlp==2026.3.17`，修复直播录制报 `No module named yt_dlp`。

### Added — HITL 审批流 + 轻量时间加权历史检索
- `src/radio/approval.py` — 新环节先写入 `data/pending_segments.json` 待审批，不再由 pipeline 直接改写 `segments_library.yaml`。
- `telegram_sender.py` — 新环节推送 Telegram inline buttons：`👍 入库` / `❌ 跳过`。
- `src/radio/telegram_bot.py` / `scripts/main_bot.py` — Telegram polling bot，处理审批 callback，并提供 `/status`、`/pending`。
- `history.py` — `load_relevant_history()` 按词面相关度 + 时间衰减排序往期记录；出现“上次 / 前回 / 之前”等回溯词时提高相关度权重。
- `pipeline.py` — 落盘 `03_ja_segments.json`、`04_bilingual_segments.json`、`05_summary.json`；失败时保留切片目录，成功后才清理。
- `config.yaml` — 新增 `translation.prompt_path` 与 `summary.prompt_path`，便于按节目替换 prompt。
- `config/profiles/` — 新增 Prompt Profile 目录，内置 `mygo_meigo_shukai`、`hina_radio` 与 `general_seiyuu_radio`。
- API / 前端 — 新增 `/api/profiles`，前端可选择 profile，也可新增自定义 prompt profile。
- CLI — `main_video.py` / `main_oneshot.py` / `main_resummarize.py` 支持 `--profile <id>`。

### Changed
- `pipeline.py` 的 `summary.auto_append_new_segments` 语义从“自动入库”改为“自动进入待审批队列”，人工确认后才写入 library。
- 翻译失败 / 缺失段超过 5% 时写入 metrics warning，方便 `/status` 和报表发现质量问题。
- 默认翻译 / 总结 prompt 改为通用声优广播语境，不再硬编码 MyGO!!!!! / 迷子集会。
- `main_resummarize.py` 重跑总结时也按节目标题过滤 library/history，并沿用 HITL 待审批队列。

### Added — 前端后端准备 + YouTube Live 录制（PRD 3.1 轨道 C 收尾）
- `scheduler.py`
  - 新增 `source_type="radiko_timefree"` 分支：以 `radiko_timefree_url` 为 seed，按 `interval_days` 推导当期 URL。
  - 新增 `source_type="youtube_live"` 分支：按配置的 YouTube 地址、时间、持续时长自动录制并进入同一条 pipeline。
- `config/config.yaml`
  - 已加入 QRR 每周一 00:30 JST 实时录制任务，录制 30 分钟后进入提取、翻译、总结、推送。
- `src/radio/live_detector.py` — yt-dlp 轮询 YouTube live 状态，等待 `live_status=is_live`。
- `src/radio/youtube_live_source.py` — yt-dlp `--live-from-start` + ffmpeg 定长录制，输出音频给通用 pipeline。
- `scripts/main_youtube_live.py` — 手动跑一次 YouTube Live 的 CLI 入口。
- `src/radio/api.py` / `src/radio/jobs.py` — 本地 FastAPI 后端 + in-memory job manager，给前端提交批量视频、播放列表范围、指定时间直播录制。
- `src/radio/playlist.py` — YouTube playlist index 范围展开，支持 `178 -> 1` 这种倒序批量。
- `scripts/main_api.py` — 本地 API server 入口。
- `tests/test_scheduler.py` — 覆盖 Radiko seed URL → 下一期 URL 的 7 天周期推导。

### Changed
- `pyproject.toml` 新增 `fastapi` / `uvicorn`，用于后续前端对接。

---

## [0.5.0] - 2026-05-16

### Added — APScheduler 守护进程（PRD 3.1 "定时触发器" 落地）
- `src/radio/scheduler.py`
  - `AsyncIOScheduler` + `SQLAlchemyJobStore`（SQLite）持久化 jobs，daemon 重启自动恢复
  - `build_scheduler(settings, yaml_path)` 按 `scheduled_programs` 配置注册 CronTrigger
  - `_run_scheduled_program` 调度入口：health check → record_radiko_live → run_pipeline → 推 Telegram
  - 失败不中断守护进程；用 metrics + Telegram failure notification 记录
- `scripts/main_daemon.py` — 常驻 daemon 入口
- `deploy/radio.plist` — macOS launchd（`RunAtLoad` + `KeepAlive` + ThrottleInterval=30s）
- `config.yaml` 新增：
  - `scheduler.jobstore_path` / `scheduler.misfire_grace_seconds`
  - `scheduled_programs: [{name, source_type, station_id, schedule cron, duration, health 选项 ...}]`
- 多节目同时跟支持（每个节目独立 job_id + 独立 cron）

### 安装步骤（macOS launchd 开机自启）
```bash
cp deploy/radio.plist ~/Library/LaunchAgents/com.user.radio-oshikatsu.plist
launchctl load ~/Library/LaunchAgents/com.user.radio-oshikatsu.plist
launchctl start com.user.radio-oshikatsu
```

### 实测：调度器内部函数 smoke test
- QRR 1 分钟 live 调度任务全链路跑通：199.4s 完成
- library 按 series 过滤：13 → 6 条（同节目自动累积 + 跨节目隔离生效）
- 「リスナーメール」⭐命中之前自动入库的同节目环节
- Telegram 推送 2 条 + transcript 附件

### Changed
- `pyproject.toml` 加 `sqlalchemy>=2.0` 依赖（APScheduler SQLite JobStore 需要）

---

## [0.4.1] - 2026-05-16

### Fixed — 跨节目环节误命中 + 翻译字段缺失 KeyError
- **跨节目误命中 (P0 bug)**：summarize 注入 library 时**没按当前节目过滤**，导致跑 QRR 节目时
  prompt 里同时塞 MyGO!!!!!「迷子集会」的环节，LLM 看到「ふつおたのコーナー」「オープニング」
  这类**日本广播业通用术语**就误命中 MyGO 下登记的同名条目，输出错乱
  - 新增 `segments_library.filter_library_by_series(library, series_name)`，按 program_ja
    双向 substring 匹配
  - `summarize()` 内部第一步就用 `extract_series_name(program_name)` 过滤 library，后续
    prompt 注入 + `_apply_segments_library` 后处理全部基于 series-filtered library
  - 实测：QRR 节目 library 从 7 → 0 条，6 sections 全部正确标 🆕、自动入库归到当前节目
    program_id 下、没污染 mygo_meigo_shukai
- **translate.py `KeyError: 'zh'`**：DeepSeek 偶尔输出漏 `zh` 或 `i` 字段
  - `_parse_translation` 显式校验字段存在，缺则 raise ValueError 触发 @async_retry
  - 单段降级路径 `_translate_single` / `_translate_single_anthropic` 用 `.get("zh", "")`
    防御性兜底

### Removed
- 清理 segments_library.yaml 中的 `qrr_live_smoke_2026_05_16_958808` 测试节点（污染数据）

---

## [0.4.0] - 2026-05-16

### Added — 实时直播录制 + 自动化基础设施（用户 4 个任务一并落地）

**1. Radiko Live 实时直播录制**（`record_radiko_live` 纯 httpx 路径）
- `parse_radiko_url` 支持 `/live/STATION_ID` 形态，自动分发 RadikoLiveSpec
- `build_live_master_url` 用 `f-radiko.smartstream.ne.jp/{station}/_definst_/simul-stream.stream/...`
- 滚动拉流循环：每 ~4s poll medialist，对未见 chunk 增量 append 到 raw.aac
- **关键 bug 修复**：chunk URL path 含变动的 session token `_w<NUMBER>_`；dedup key 改用末尾的 HLS sequence number（`602533` 这种），同 lsid 全程保持
- Live 端点**不被反爬挡**（跟 time-shift 完全不一样），纯 httpx 就能跑通——比 Playwright 路径简单 10 倍

**2. recordings/ 按 program_id 分目录**（`recordings_layout.py`）
- 旧 `work_*` / `video_*` / `radiko_*` / `gemini_run_*` / `retranslated_*` 平铺杂目录全部清空
- 新布局：`data/recordings/<program_id>/<YYYY-MM-DD>_<title>/`
- segments_library 命中 → 真 program_id（如 `mygo_meigo_shukai`）；未命中 → 稳定 `slug_<sha1前6>` 子目录
- pipeline.py 接 `work_dir` 可选参数；main_video / main_radiko / main_oneshot 提前算 work_dir 传入

**3. 轻量级历史摘要库**（`src/radio/history.py`）
- 每次 summarize 完后 append 一行 JSON 到 `data/history_context.jsonl`：
  `program_series / air_date / key_topics[] / highlight_quotes[]`
- summarize 调用时 `load_recent_history(program_series, limit=5, air_date_before=...)` 拉同节目最近 5 期
- summarize.txt prompt 新增 `{recent_history}` 块；LLM 能感知"上期 X 这期是否呼应"
- `SummaryConfig.history_recent_n: int = 5` 配置上限

**4. 录制前健康检查**（`src/radio/health.py`）
- `probe_radiko_station(station_id)`：HTTP probe `radiko.jp/v3/program/station/date/YYYYMMDD/SID.xml`
- 找出当前 JST 时刻正在播的节目，返回 `(ok, title, ft, to)`
- `notify_health_failure` 把异常通过 Telegram MarkdownV2 告警
- main_radiko 加 3 个 flag：`--skip-health-check` / `--health-pre-record-minutes N` / `--fail-on-health-fail`

### Changed — STT prompt 优先级 + Telegram failure notifier
- `build_stt_prompt` 按 P0 cast → P1 当前 radio → P2 当前 library → P3 base → P4 character → P5 其他 顺序填，超 200 字符上限按"整词丢弃"，库膨胀不再挤掉关键人名/环节名（详见 v0.3.0 留尾改进）
- 多节目场景下只注入当前 series 的 library 环节
- main_radiko `run_pipeline(source="radiko")` 让 metrics.jsonl 能区分入口
- 历史上 Playwright time-shift 适配仍保留（time-shift endpoint 反爬较紧，httpx 拉 medialist 会 404）；live 直接走 httpx

### Validated — QRR 5min 直播端到端
- URL：`https://radiko.jp/#!/live/QRR`
- 总耗时 1m49s（录制 5min + STT 22s + 翻译 49s + Gemini 总结 30s + Telegram 推送 8s）
- 录到 98 chunks / 2.8 MB AAC（含 medialist 自带 3 分钟历史回放）
- LLM 识别出节目主持人「伊藤志郎、吉田照美」+ 6 个 sections（命中库 3 ⭐ + 自动入库新增 3 🆕）
- 健康检查正确识别当前在播节目：「伊東四朗 吉田照美 親父・熱愛」（15:00-17:00 JST）
- `data/history_context.jsonl` 写入 1 条 history entry

---

## [0.4.0-rc2] - 2026-05-16

### Added
- **Cookies 注入支持**：`record_radiko_via_playwright(cookies_path=...)` + `main_radiko.py --cookies`
- `_load_browser_cookies()` 支持 EditThisCookie / Cookie-Editor JSON 格式

### Known issue (deeper)
- 即使从真实浏览器导出 cookies 注入 + 首页预热再跳 time-shift，Playwright Chromium 仍被 Radiko 反爬识别
- 浏览器 console 持续报 `"Server-side cookie doesn't seem to be functioning"`，audio 元素始终未被赋 src
- 推测：底层指纹（Canvas / WebGL / Audio context）层面的 bot 检测，单靠 cookies + stealth init script 不够
- 后续可能路径：
  1. Playwright 连接到用户真实 Chrome 实例（`--remote-debugging-port=9222`），借用真实指纹
  2. BlackHole 虚拟声卡 + ffmpeg 系统级录音（不依赖反爬变动）
  3. 等 streamlink 社区找到方法

---

## [0.4.0-rc1] - 2026-05-16

### Added
- **STT prompt 优先级 + series 过滤**：超过 224-token 上限时按 P0 cast → P1 当前 radio → P2 当前 library → P3 base → P4 character → P5 其他 顺序填，丢的是低识别错率的歌曲名而不是关键人名/环节名；多节目场景下只注入当前 series 的 library / radio
  - 调用方传 `program_display_name` 启用过滤；被丢的词 INFO 日志可见
  - 4 个场景单元测试全过（无 series / MyGO / 羊宮个人广播 / 全新节目）
- **Radiko time-shift 录制 框架**（`src/radio/radiko_source.py` + `scripts/main_radiko.py`）：
  - URL 解析（`https://radiko.jp/#!/ts/QRR/YYYYMMDDhhmmss`）
  - auth1 + auth2 完整流程（拿到 JP13 东京 IP 认证）
  - master playlist URL 构造（含 streamlink 同款 8 参数 + lsid hash + type=b）
  - Python 端 HLS 拉流（同 httpx session 跟随 master → medialist → chunks）
- **Radiko Playwright 适配**（`src/radio/radiko_playwright_source.py`）：
  - 启动真 Chromium（`--headless=new` 带 audio）+ stealth init script（擦 navigator.webdriver / 模拟 chrome.runtime / 解锁 autoplay）
  - 自动处理 Radiko 隐私同意弹窗（`.js-policy-accept`）
  - 自动点击 time-shift 播放按钮（`.live-detail__play a.play-radio`）
  - 监听浏览器 master playlist 请求 → 用 `context.request` 走浏览器 session 完成 HLS 三跳

### Known issue
- **Radiko 2026 反爬把所有非浏览器客户端都挡在 medialist 这一关**：
  curl / httpx / streamlink 8.4.0 / radigo（go-radiko）一致 404
- **Playwright 还卡在最后一公里**：浏览器加载页面 + 点击播放按钮成功，
  但 Radiko player JS 检测到「Server-side cookie doesn't seem to be functioning」
  + 400 Bad Request 后拒绝给 `<audio>` 赋 src，**自然也就没有 HLS 请求可拦截**
- 下一步可能路径：从用户真实浏览器导出 cookies 注入 Playwright；
  或调用 Radiko 内部 player JS 对象绕过 cookie 检查

---

## [0.3.0] - 2026-05-16

### Added — 可观测性 + 智能 STT prompt（NEXT_STEPS P1 一波）

**Metrics 模块** (`src/radio/utils/metrics.py`)
- `PipelineMetrics` pydantic 模型记录每次跑的：run_id、耗时、节目、段数、sections 数、库命中数、自动入库数、step_durations 字典、warnings、errors、success
- `MetricsCollector.step(name)` 上下文管理器自动测各步骤耗时
- 超过阈值（STT > 600s、Summary > 120s 等）自动 logger.warning，记到 `warnings[]`
- 每次跑 append 一行 JSON 到 `data/logs/metrics.jsonl`，append-only
- `scripts/metrics_report.py` — 一行命令出周/月/全量报表（运行次数、成功率、P95 耗时、库命中率、按入口分组）

**STT prompt 动态注入** (`src/radio/stt.build_stt_prompt`)
- 每次调 Groq Whisper 前实时构造 prompt：基础 prompt + terminology cast/character/radio + segments_library 全部 title_ja
- 去重 + 截断到 200 字符（Whisper API 上限 ~224 token）
- v0.2.3 数据集下实测：184 字符，含 5 声优 + 10 角色 + 2 节目 + 7 环节标题
- 显著降低声优名、角色名、节目特有环节名识别错率（≈零成本提升）

**Telegram 失败通知** (`telegram_sender.notify_pipeline_failure`)
- pipeline 顶层 try/except 抓任何异常 → 自动发简短错误到 Telegram chat_id
- 通知本身发送失败被吞掉不二次崩溃
- 用户在 cron 模式下能立即知道哪期跑失败了

详见 ADR 0009。

### Changed
- `run_pipeline()` 新增 `source` 参数标识入口类型（video / oneshot / resummarize / live_recording），用于 metrics 分类

---

## [0.2.3] - 2026-05-16

### Added — MyGO!!!!! 完整歌曲术语库
- terminology.yaml 大幅扩库：**31 首原创歌曲 + 3 张专辑**，每首带：
  - 日文汉字写法 / 当て字假名读音 / 罗马字 / 英文意思 / 中文译名
  - 当て字与否的明确标注（如「迷路日々」读 メロディー / Melody）
- 加入 `category: album` 类别，区分单曲与专辑名（迷跡波、跡暖空、致並跡）
- post_corrections 加防御性映射：`Melody → メロディー`、`迷路日子 → 迷路日々`
- 数据已用 4 个独立源交叉验证（animatetimes、Pixiv 百科、Wikipedia EN、bandori.party）

### Added — NEXT_STEPS.md 待办与可优化清单
- 按 P0/P1/P2/P3/P4 分级列出所有"已知要做但还没做"的事
- 覆盖：M2 直播录制、HITL 升级、metrics、知识库 v2、多节目、上云、CI 等

### Why
PRD 第 3.2 节明确提到"专有名词映射"是核心能力之一。当て字歌名识别是 MyGO!!!!!/声优广播翻译的命门——「迷路日々」直译为「迷路日子」是底层硬伤，必须通过术语注入根治。

---

## [0.2.2] - 2026-05-16

### Changed — 把来信中文翻译加回 Telegram
- v0.2.1 误删了 `listener_mail`（来信中文翻译全文），按用户反馈补回。
- `summarize.txt` prompt：`listener_mail` 重新成为必填，要求按来信人顺序给出中文翻译；多封来信用 `[来信人] …… [来信人] ……` 行内区分。
- `summarize.py` Gemini responseSchema：`listener_mail` 重新进 `required`。
- `telegram_sender.py`：每条 section 在 `来信：` 署名之后追加 `来信内容：` 中文翻译。
- 其他保持 v0.2.1：仍不渲染环节中文标题、`listener_mail_ja` 日语全文、整条「✨ 高光时刻」消息。

### 验证
重跑 #175（resummarize）22 秒完成，5/5 ⭐常驻全命中（library 此时 7 条），Telegram 推送 2 条消息，包含来信中文翻译。

---

## [0.2.1] - 2026-05-16

### Changed — Telegram 渲染瘦身
- **每个 section 只保留**：环节日语原标题 (`title_ja`)、`intro`、`content`、`listener_mail_from`、`member_reactions`、`music`、`notes`
- **不再渲染**：环节中文标题、来信日语原文全文、来信中文翻译全文、整条「✨ 高光时刻」消息
- 来信现在只显示**署名**（如「電気羊さん」「松剣さんばさん」），一条 section 可能列出 2-3 个署名
- `ProgramSection` 新增 `listener_mail_from: str`；`title` / `listener_mail` / `listener_mail_ja` 仍在 model 中作向后兼容，但 LLM 现在被指示留空
- Gemini responseSchema：`title` / `listener_mail` / `listener_mail_ja` 从 required 字段中移除；`highlights` 也从 required 中移除
- `summarize.txt` prompt 同步更新：明确告诉 LLM 不要花 token 输出全文 listener_mail；只抽署名
- `Summary.highlights` / `key_topics` 改为有默认值的 `list[Highlight] = []`

### Why
跑 #178 后用户反馈：来信日语全文太长把 Telegram 撑爆，中文标题与日语标题重复，高光时刻和「分段复盘」内容已经有所重叠。瘦身后 Telegram 单期推送从 3-4 条消息变成 2 条，信息密度更高。

### 验证
真实节目 MyGO!!!!!の「迷子集会」#175（30.4 分钟、592 段 transcript）端到端跑通：
- 总耗时 5.3 分钟
- 命中库 3、新增 2（自动入库「僕たちはライブの感想をここで叫ぶ」、「選曲」）
- Telegram 推送 2 条消息，渲染干净
- 「MyGO!!!!!中心」类错译扫描归零

---

## [Unreleased / earlier]

### Added
- **常驻环节知识库**（`config/segments_library.yaml` + `src/radio/segments_library.py`）：
  - 预填 MyGO!!!!!《迷子集会》"僕、私、迷子中"作为第一条
  - LLM summarize 时把 library 注入 prompt；返回后按 `title_ja` + aliases + 子串三层匹配
  - 命中常驻环节：覆盖 `intro` 为 library 标准版、标记 `is_recurring=True`
  - 新发现环节：保留 LLM 现编 intro、标记 `is_recurring=False`、Telegram 打 🆕 标签
  - **自动入库**：新发现的环节自动追加到 YAML（带去重）；可通过 `summary.auto_append_new_segments` 开关关闭
  - `extract_series_name()` 从 `MyGO!!!!!の「迷子集会」#178` 这种带期数的标题剥出系列名
  - 详见 ADR 0008（含 2026-05-16 策略变更说明）
- `ProgramSection` 模型新增字段：`title_ja` / `intro` / `is_recurring` / `listener_mail_ja`
  （全部默认值，向后兼容）
- Telegram 摘要现按 `header / 分段复盘 / 高光时刻` 拆成多条消息发送，
  每条不超过 4096 字符；分段复盘渲染 JP/CN 标题、环节介绍、JP/CN 双语来信、成员反应、选曲、备注
- `scripts/main_resummarize.py`：跳过 STT/翻译，从已有 bilingual.txt 直接重做 summary + Telegram。
  适用于 prompt 调整、provider 切换、summarize 阶段失败后重试。
- `summarize.dump_summary_to_disk()`：每次 summarize 完成后把 Summary 序列化为 JSON 落盘，
  供事后审计、复盘、扩库使用。
- summarize 失败时自动 dump 原始/修复后响应到 `data/logs/summarize_raw_failed_*.json`。
- terminology.yaml 扩充：「迷子集会」条目新增 `マイゴセンター` / `マイゴのマイゴセンター` 等
  日语发音变体作为 aliases；post_corrections 新增 `MyGO!!!!!中心 → 迷子集会` 等防御性映射。

### Changed
- `summarize.py` 的 `Summary` schema（Gemini responseSchema）新增 4 个 section 字段
- `summarize.txt` prompt 重写：要求 LLM 保留 JP 原标题、双语来信，命中常驻环节时把 intro 留空，
  **禁止在 JSON 字符串值内使用 ASCII 双引号**（必须用 `「」` 或 `《》`），避免 JSON 解析失败
- `config.py` `SummaryConfig` 新增 `segments_library_path` 和 `auto_append_new_segments`
- summarize 重试次数从 2 提高到 3，且加入 `_repair_inner_quotes` 启发式 JSON 修复
  （把字符串内未转义的 ASCII `"` 替换成 `"`）作为兜底

### Planned
- M2: APScheduler 自动调度 + 直播开播探测（雏形已通过手动 URL 触发跑通）
- M3: 真实节目守护（Mac launchd / tmux 持久化）
- M4: metrics 日志（每次跑记录耗时与 token 用量）+ Telegram `/status` 命令
- M5: 上线打磨与可观测性（连续两期无人工干预成功后升 v1.0.0）

---

## [0.2.0] - 2026-05-16

### Added
- **轨道 A（已有视频 URL）**：新增 `scripts/main_video.py` 与 `src/radio/video_source.py`，
  支持 Bili / YouTube 等 `yt-dlp` 兼容的视频 URL 抽音频后复用现有 pipeline。
  支持 `--cookies` 登录态、`--title` 标题覆盖、`--keep-audio` 调试保留。
- **术语库系统**：新增 `config/terminology.yaml`（MyGO!!!!! / BanG Dream! / 声优广播 ~50 条术语）
  与 `src/radio/terminology.py`。提供两层防护：
  - Prompt 注入：翻译/总结 LLM 调用时把术语清单送入 prompt
  - 译后修正：`post_corrections` 字典对所有中文输出做机械替换兜底
  详见 ADR 0004。
- **Gemini 总结 provider**：日常总结默认改用 Gemini 2.5 Flash，通过 `responseSchema` 强约束输出。
  Anthropic Claude 保留为可热切换通道（`summary.provider: anthropic`）。
  `.env` 新增 `GEMINI_API_KEY`（仅切到 gemini 时必填）。详见 ADR 0005。
- **精细翻译 opt-in**：`--fine-translation` CLI 标志切到 `translation.fine_provider/fine_model`
  （默认 Claude Haiku 4.5）。适合 Live 当天或重要发表等高价值节目。详见 ADR 0006。
- **结构化分段总结**：`Summary` 新增 `sections: list[ProgramSection]`，包含 title / time_range /
  content / listener_mail / member_reactions / music / notes。Telegram 推送渲染 `🧭 分段复盘` 块。
  详见 ADR 0007。
- **ADR 0004 – 0007** 四份新决策文档落档。
- 视频标题安全文件名工具 `_safe_filename_part()`。

### Changed
- DeepSeek 翻译模型从兼容别名 `deepseek-chat` 切换为显式名 `deepseek-v4-flash`
  （官方提示兼容别名后续会废弃）。
- 日常总结默认 provider：Claude Sonnet 4.5 → **Gemini 2.5 Flash**。
- STT 默认切片长度 600 秒 → **120 秒**，并发 5 → **2**，以降低 YouTube
  高码率音频上传 Groq 的超时概率。
- `pyproject.toml` 增加 `yt-dlp>=2024.8.6` 依赖。
- `translate.py` 拆出双 provider 路径（DeepSeek HTTP + Anthropic SDK），共享 batch 段数校验
  与单段降级逻辑。
- `pipeline.py` `run_pipeline` 接受 `display_name`、`fine_translation` 两个新参数；
  既应用 `name_corrections` 也应用 `apply_terminology_corrections`。
- `models.py` 新增 `ProgramSection`；`Summary` 增加 `sections` 字段。
- `summarize.py` 增加 `SUMMARY_RESPONSE_SCHEMA` JSON Schema，
  支持 `_summarize_with_gemini` / `_summarize_with_anthropic` 双路径。
- `prompts/translate.txt`、`prompts/summarize.txt` 重写为 MyGO!!!!! / 声优广播专精版本，
  加入 `{terminology}` 占位符。
- README 增补轨道 A 用法与 `--fine-translation` 用法。

### Notes
- M1 名义目标（本地音频文件 → 双语 transcript → Telegram）已通过轨道 A
  （已有视频 URL → 同一 pipeline）等价达成；M2 不再阻塞，可优先做调度与直播探测。

---

## [0.1.0] - 2026-05-15

### Added
- 项目骨架：目录结构、`pyproject.toml`、`.env.example`、`README`、`CHANGELOG`。
- M1 模块全套：`config.py` / `utils/logging.py` / `utils/retry.py` /
  `stt.py` / `translate.py` / `summarize.py` / `transcript.py` / `telegram_sender.py` /
  `segmenter.py` / `pipeline.py` / `scripts/main_oneshot.py`。
- 翻译与总结 prompt 模板：`prompts/translate.txt`、`prompts/summarize.txt`。
- ADR 0001/0002/0003 三份初始架构决策（不引入 LangGraph、用 Groq Whisper、翻译策略）。
- Telegram bot 凭证联调验证通过。

[Unreleased]: ../../compare/v0.5.0...HEAD
[0.5.0]: ../../releases/tag/v0.5.0
[0.4.1]: ../../releases/tag/v0.4.1
[0.4.0]: ../../releases/tag/v0.4.0
[0.3.0]: ../../releases/tag/v0.3.0
[0.2.3]: ../../releases/tag/v0.2.3
[0.2.2]: ../../releases/tag/v0.2.2
[0.2.1]: ../../releases/tag/v0.2.1
[0.2.0]: ../../releases/tag/v0.2.0
[0.1.0]: ../../releases/tag/v0.1.0
