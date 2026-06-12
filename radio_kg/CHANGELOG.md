# Changelog

本项目所有功能性的完善与新增都记录于此。日期为开发日期 (YYYY-MM-DD)。

## 审批中断废除：LLM 上下文终审直接入库 + QA 延迟再降（~15.7s→~9-11s，重复问 0.04s）— 2026-06-13

### Changed｜入库审批从「人工确认」改为「LLM 上下文终审」（用户决策）
- 背景：/api/pending 实测 27 项积压里 26 项是 no-op（建议名=原名），人工甄别在大量文本涌入时不可行。
- `InspectorAgent` 新增 `adjudicate()`：对前置审核拿不准的高风险项做第二次 LLM 裁决，上下文比初审更全——**节目逐字稿原文片段**（新增 `transcript_excerpt()` 按三元组时间窗 ±45s 截取）+ 历史图谱关联 + 行业词典 + 初审疑点。裁决 accept_correction（可给出比建议更准的 final_triplet）/ keep_original / drop，结果直接入库；LLM 失败时 fail-open 保留原文（不丢转写事实）。
- `InspectorAgent` 新增 `adjudicate_conflicts()`：单值关系冲突（如『担当する』新旧环节不一致）同样带逐字稿上下文裁决 confirm（保留历史线）/ overwrite / ignore；失败时 fail-safe 为 ignore（图谱不变）。
- `ingestion_graph.py`：inspect_node 与 sync_node 的 `interrupt()` 全部移除，改调上述裁决直写；裁决结果以 `severity=adjudicated_*` 记入 inspection_issues、`resolution/resolution_reason` 记入 conflicts 留审计痕迹（models.py severity Literal 扩展）。`auto_policy` 仅保留对 sync 冲突的快捷路径。
- 积压清理：15 条挂起线程（5/30 起）经 `/api/resume`（空决策，重入新裁决流程）入库，pending 清零。dashboard 待办卡片今后不再产生。
- Fixed（清理首轮发现）：`adjudicate_conflicts` 给 `relationship_object_counts` 传了字符串 relation（签名要求 `list[str]`），Cypher `$terms = []` 类型不匹配致 5 条含冲突的线程 500；改传 `[conflict.relation]` 后重跑成功。

### Changed｜QA 链路 LLM 调用优化（实测瓶颈：route 2.6s + analyze 3.7s + 生成+核查 9.0s 串行）
- **route ∥ analyze 并行**（`app.py`）：`_run_qa` 以线程池预取 `QA_AGENT.analyze`，与 `STATS_AGENT.route` 并行；`qa_graph2.analyze` 节点检测到上游已传 search_queries 时跳过 LLM。省 ~2.6s，质量零影响（stats/dossier 路由时 analyze 结果丢弃）。
- **verify 本地预核**（`qa_agent._verify_facts`）：引用图谱事实（入库前已审计）的 fact 与「字符 bigram 包含度 ≥0.65」的 fact 本地免核，仅存疑事实送 LLM；全部免核时跳过整次 verify 调用（省 ~3-4s）。跨语种/改写事实仍走 LLM 判定，防编造红线不动。
- **答案缓存**（`app.py`）：首轮问题（无会话历史）按「归一化问题 + index_version 注册表 mtime」键控缓存 128 条；任何入库都会重戳注册表自然失效，KB 订正路径显式 `clear()`。重复提问 0.04s 返回。
- Verified（线上）：stats 确定性路径 0.13s；检索路径热态 ~9-11s（原 ~15.7s）；重复问 0.04s；放送日期/事务所/吉他话题答案正确带出处；弃答行为仅在资料确实不足时出现。冒烟测试：裁决三态映射、final_triplet 覆写、逐字稿注入、两处 fail-safe、verify 预核免调用/存疑仍判、跨语种回退全部通过。

## 性能优化：检索热路径提速 47%（769ms→408ms）+ e5 模型去重 — 2026-06-13

- **Changed｜e5 模型进程内共享**（`src/embeddings/e5.py`）：原来每个 `VectorStore` 实例各自 `E5Embedder` 各自加载一份 SentenceTransformer——server 持有 5 个 store（chunks/summaries/insights/mail/retriever 复用），权重最多重复加载 5 份（每份 ~0.5GB 内存 + 数秒加载）。改为模块级 `_MODEL_CACHE` 按模型名共享单例（双检锁线程安全），全进程只加载一次。
- **Added｜查询向量 LRU 缓存**（同文件）：同一 QA 请求里相同 search_query 会在 summary 库与 chunk 库各编码一次；跨请求重复提问亦然。`encode_queries` 增加 (model, text)→vec 的 512 条 LRU 记忆，批量调用混合命中/未命中保持顺序正确。
- **Added｜全量扫描缓存**（`src/mcp_layer/vector_store.py`）：`keyword_query`/`distinct_labels` 依赖的 `_get_all_documents()` 每次问答都把全集合 2700+ 文档从 Chroma 拉一遍（实测单次 ~349ms，且每问触发多次）。增加按 (collection uuid, count) 键控的记忆——`add_chunks`/`reset_collection` 显式失效，外部重建（uuid 变）或他进程写入（count 变）自动失效；命中 0.4ms。
- Verified：bench_perf 混合检索 mean **769ms→408ms**、p95 923ms→475ms（目标 <2s）；MCP 调用仍全数达标。冒烟测试通过：模型单例、缓存向量与直接编码 allclose、混合批次顺序、keyword_query 结果正常。`agent-up` 重启后 `/api/health` 全绿，线上 `/api/ask2` 端到端回答正确带出处。

## clip 节目方案支持 stt_prompt 覆盖 — 2026-06-10

- **Added**：`clip/programs/<id>.yaml` 的 `processing.stt_prompt` 可按节目替换全局 Whisper prompt；
  `kb_ingest` 入库前应用。配合 Radio STT 链路的幻听/听不全治理（详见 Radio/CHANGELOG.md），
  解决全局 prompt 中异节目人名（安野希世乃/悠木碧）被音乐段幻听成转写内容、污染图谱实体的问题
  （minetsuki 各期 1-12 处 → 0）。`minetsuki_ritsu.yaml` 已配本节目专属 prompt。

## 工作区基建：唯一 venv + 移除 Docker + launchd 接管 — 2026-06-10

- **Changed｜三 venv 合一（根治依赖漂移）**：新增根 `Agent/pyproject.toml`（uv workspace，成员 Radio/clip；radio_kg 依赖直接声明于根，chromadb/sentence-transformers/torch/langgraph/mcp 等锁定现装版本保证数据兼容）。Radio 与 clip 以 editable 包装进唯一 venv `Agent/.venv`；删除 `Radio/.venv`(367M)、`radio_kg/.venv`(1.0G)、`radio_kg/requirements.txt`、`Radio/uv.lock`。今后装依赖只有一条命令：`cd Agent && uv sync`。
- **Changed｜移除全部跨 venv sys.path 注入**：`src/server/app.py`（Radio src+site-packages 注入）、`clip/__init__.py`（Radio src+venv）、`clip/kb_ingest._ensure_radio_on_path`、`clip/telegram_clip._ensure_radio`、Radio `telegram_bot` 的 clip 路径注入、`clip/cli.py` 自注入。仅保留 clip→radio_kg 根目录一处源码路径插入（radio_kg 顶层模块名为 src/config，非安装包）。WhisperX 仍隔离在 `clip/.venv_whisperx` 子进程调用。
- **Removed｜Docker 部署**：删除 docker-compose.yml / Dockerfile / .dockerignore / scripts/docker-{up,down}.sh。compose 仍挂载 2026-05-29 已删除的 `../hina_radio`，且容器 Neo4j 空库与本地 Homebrew 图谱割裂，单机单用户无收益。README 改为「只支持本地运行」。
- **Changed｜启动全面迁移 launchd**：三个 `com.agent.*` plist（RunAtLoad + KeepAlive + ThrottleInterval 15s；权威副本入库 `Agent/scripts/`，agent-up 自动同步到 ~/Library/LaunchAgents）+ brew neo4j 登录自启。`agent-up.command` 改为 launchctl 薄包装（未加载 bootstrap / 已加载 kickstart -k 重启加载新代码；未配 TELEGRAM_BOT_TOKEN 时跳过 bot 防崩溃循环）；`agent-down.command` = bootout + 停 neo4j + 残留进程兜底。
- Verified：唯一 venv 导入冒烟（server app + RADIO mount + clip 全模块 + radio 核心模块，sys.path 无外部 venv）通过；`/`、`/dashboard`、`/radio`、`/clipper`、`/api/health` 全部正常；`kill -9` server 后 launchd 15 秒内自动复活并恢复健康；bot 单实例 polling；调度器任务保留（下次触发 2026-06-15 00:30 JST）。

## 切片字幕逐句审核/修改 + 视频预览 + 译文自动修复 — 2026-06-06

- **Fixed｜译文「一大段相同 / 整段缺译」**：前端再切片经 `build_clip_cues` 复用原 `04_bilingual` 译文（按时间重叠），二次精听把一句拆成多条时会复制同一句译文、重识别处无对应译文则缺中文。新增 `lyrics._repair_talk_zh`：检出**空译**与**与相邻条同译（重复段）**的谈话条，逐条 LLM 重译填补。`build_clip_cues(..., llm=, terminology=)` 新增可选参数（`llm=None` 时行为不变）。
- **Added｜补译复用入库精翻 prompt**：`aligner.translate_lines` 复用 Radio 同一份 `prompts/translate.txt`（术语库可注入），让再切片补译与原字幕语气/术语一致；读不到时回退简易逐行 prompt。（注：此前 `_translate_missing` 用的是无术语的薄 prompt。）
- **Added｜两段式切片 + 视频预览 + 逐句审核**：
  - `render.py` 拆出 `prepare_segment`（切片+生成 cue，不烧录）/ `assemble_segment`（烧录），`render_segment` 改为二者的薄封装（Telegram/CLI 一气通贯行为不变）。
  - `server_routes.py` 新增 `POST /api/slice/preview` → `GET /api/slice/preview/{id}`（返回可编辑 cues）→ `GET /api/slice/preview_video/{id}`（无字幕切片，支持 Range 拖动）→ `POST /api/slice/assemble`（按编辑后 cues 烧录）。setlist 取区间逻辑抽成 `_setlist_spans` 复用。
  - `static/clipper.html` 新增「④′ 字幕审核 & 视频预览」：`<video>` + 实时字幕叠层（编辑右侧表格即时反映、点时间跳转），逐行可改日文/中文、可删行，确认后「组装成片」才烧录。
- Verified：`clip.{aligner,lyrics,render,server_routes}` 真实导入通过；`_repair_talk_zh` 用桩 LLM 验证「重复段两条都重译、空译补译、唯一译保留、无日文跳过」；`translate.txt` 模板成功加载。

## 切片输出文件夹按标题命名 + 项目瘦身 — 2026-06-06

- **Changed｜切片文件夹改为可读命名**（原 `tg_2_1780729381` / `manual_d41b97b5` 难辨识）：复用 `youtube_source.safe_dirname`。
  - Telegram 点击切片（`telegram_clip._render_job_item`）：`{曲名/片段名}_{时间戳}`。
  - 前端手动切片（`server_routes._run_slice`）：`{直播标题(episode_dir 名)}_{起}-{止}s_{job_id}`。
- **Changed｜项目瘦身**：删除全部 `__pycache__`/`.pytest_cache`（15）、`.DS_Store`（28，非 venv；已在 .gitignore）、空的残留数据目录（`Radio/data/recordings/.tmp_api_radiko_*`、`mygo_meigo_shukai`、空 `radio_kg/data/clips/new_20260604_233242`）、`radio_kg/persona/versions`（build_persona 以 exist_ok 重建）。**经用户确认删除全部测试**（`Radio/tests` 16 文件 + `radio_kg/tests` 3 文件，无 git 备份、不可恢复）。
- Verified：两处改动 `py_compile` 通过；瘦身后非 venv/node_modules 下无空目录残留。

## 修复 Telegram 点击切片：超 50MB / Radio venv 缺 Pillow — 2026-06-04

- **完整性优先**：去掉点击切片的 5 分钟限长与自动压缩；按识别出的真实区间**完整切片**。`telegram_clip._handle_clip_callback`：成片 >50MB（Telegram bot 发送上限）或发送失败时，**不截断不压缩**，回「已切出完整片段 + 体积/原因 + 本地路径」，不再静默失败。
- **修复 `No module named PIL`**：切片回调注册进 Radio 既有 bot、跑在 **Radio/.venv** 里，而 `packager.py` 需要 Pillow（原只装在 radio_kg venv）。已 `uv pip install --python Radio/.venv/bin/python Pillow`。**注意**：clipper 的渲染代码在 bot 进程（Radio venv）里执行，故 Radio venv 需有 Pillow；已验证从 Radio venv 跑通完整 render（cut→WhisperX 锚点→烧字幕）。
- Verified：从 Radio venv 端到端切出带字幕成片；本期 9.8min 那首会切出完整成片(~98MB)，因超 50MB 改回「已保存本地+路径」提示。

## 热点聚焦相关分区 + 各页回主页 + 12 首逐曲推送 — 2026-06-04

- **「近期热点」聚焦相关分区**：不再扫全站。`bilibili_source.fetch_trends(partitions, keywords)` 新增按关键词搜索 + `search()`；config `trends_partitions="music,dance,douga"` / `trends_keywords`（VTuber/バンドリ/翻唱/歌枠/声優…）。`/clipper/api/trends` 取这些分区排行 + 个人兴趣话题，再用关键词把榜单**过滤到歌曲/虚拟主播/bangdream 相关项优先**（实测surface到 Ave Mujica 等 bangdream 内容）。**B 站限制**：虚拟主播无排行榜接口、搜索接口已被风控(返回 `v_voucher` 验证挑战)，故以「相关分区排行 + 关键词过滤」近似，niche 覆盖受 B 站 API 限制。
- **各页回主页**：`chat.html` 侧栏、`Radio/frontend/index.html` 顶部各加「← 主页」链接（→ `/`），与 `/clipper` 一致。
- **逐曲推送**：本期以**逐首拆分的 12 首**重推 Telegram 切片菜单（每首一个按钮，点击即切）。重启栈后 bot 回调已生效。

## 「直播录制和切片」前端 + 逐首拆分 + 方案自动推 Telegram — 2026-06-04

- **主页改为三入口**：`/` 现为 `home.html` 落地页（对话 / Radio 录制 / 直播录制和切片）；原聊天 SPA 移到 `/chat`（其 API 均为绝对路径，迁移无碍）。
- **新增独立分块「直播录制和切片」`/clipper`**（`server_routes.py` APIRouter，挂进主服务；缺失静默跳过）：
  - 页面 `static/clipper.html` 三区：① 上传/录制链接 + 参数（节目方案/分辨率/时长/是否推 Telegram）+ 任务状态轮询；② 我感兴趣的话题（增删，存 `data/clipper_interests.json`）；③ 最近可能火爆的因素 + 涨播放最快的前 X 个 B 站视频及数据（动量/播放/投币/弹幕/时长）。
  - API：`/clipper/api/{programs,interests,trends,record,jobs}`。`trends` 复用 B 站爬虫 + `distill_features`，5 分钟缓存；`record` 后台线程跑 `pipeline_new`。
- **逐首拆分歌曲**：deepseek 对「列/映射歌曲」请求一律返回空（内容过滤），故 `setlist._per_song_from_overview` 改为**确定性**：从摘要总览按出现顺序抽取括号内曲名/作品名（仅标题），再按各歌唱小节时长比例分配 start/end（近似分轨，点击切片时由短片 WhisperX 二次识别细化）。本期得 12 首。
- **方案级自动推送**：`archiving.auto_telegram: true`（峰月律方案已开）→ Branch B 处理入库后自动推 Telegram 切片菜单，无需 `--telegram`。
- **修复**：Radio bot 注册 clipper 回调的路径 `parents[4]→parents[3]`（原指向 `/Users/USERNAME/radio_kg`，应为 `Agent/radio_kg`）；修复后 `agent-down/up` 重启，bot 成功注册 `clip:` 回调，点击 Telegram 按钮即切片。
- Verified（重启后真实）：`/` 三入口、`/clipper` 页面、`/clipper/api/programs|interests|trends`(返回 5 条带动量数据的最快视频) 均正常；bot 新进程无「未注册」告警；逐首拆分 12 首。


## Clipper 节目方案化 + KG 主持人参数化（峰月律端到端处理+归档）— 2026-06-04

- 背景：用 clipper Branch B 总结 BanG Dream 虚拟乐队「夢限大みゅーたいぷ」吉他手 **峰月律（真名 立石凛 / Minetsuki Ritsu）** 的 YouTube 直播。需要一套**可独立保存、可复用**的节目处理方案 + 归档方案。
- 新增「节目方案」机制：`src/clipper/programs/<id>.yaml`（processing 处理口径 + archiving 归档口径）+ `program_profile.py` 加载器；`cli new --program <id>` 启用。首个方案 `minetsuki_ritsu`（+ 同名 .md 调研/方案说明）。
  - processing：源/译语言、专名纠正词典（并入 Radio `name_corrections`，不改其配置）、中文术语表、总结/爆火侧重、成员名册、**KG 主持人身份**。
  - archiving：collection、`<日期_标题>` 目录、`auto_policy=confirm`（无审查）、保留源视频、产物清单。
- **KG 主持人参数化（关键修复）**：radio_kg 的实体抽取此前把主持人**硬编码**为「羊宮妃那」(`canonical.py HOST`)，导致给别的节目入库时第一人称(私/僕)被错挂到羊宮妃那、污染共享图谱。新增 `canonical.set_host(name, aliases, type)` / `reset_host()`，`kb_ingest.ingest_folder_auto(profile=...)` 入库前按方案切主持人、`finally` 还原。峰月律方案把 `立石凛/りつ/…` 归一到 `峰月律`。
- **归档 label 修复**：`parse_folder_metadata` 会把目录名首个 `【…】` 当 episode_label → 坍缩成非唯一的「歌枠」。`youtube_source.safe_dirname` 去掉 `【】「」『』` 等括号，label 回退为唯一的「日期_标题」。
- **Branch B 健壮性**：B 站热点/爆火分析（B-2）失败不再阻断核心的处理+归档（B-3），改为告警跳过；`distill_features` token 上限 2048→4096（10 条热点 JSON 截断会 JSONDecodeError）；视频源（常为 AV1）先抽 aac 纯音频供 STT（Radio 分段器无法把 AV1 塞进 m4a），视频原样保留供切片；`kb_ingest` 注入 Radio `.venv` site-packages（radio_kg venv 缺 loguru/groq 等 Radio 依赖，与 server app.py 一致）；渲染跳过 >10min 的整场候选。
- **端到端实测（真实链接 https://www.youtube.com/watch?v=1OGimKtu4Gs，86 分钟 アニソン歌枠）**：下载 599MB(AV1 720p)→抽音频→STT 847 段→翻译→Gemini 摘要（正确识别 立石凛 + 全 setlist：ガブリールドロップキック/マクロス/らんま1/2/マジLOVE1000%…，按 歌枠 分小节）→归档到 `Radio/data/recordings/minetsuki_ritsu/2026-05-19_…/`(03/04/05+source.mp4)→`auto_policy=confirm` 无审查入库。**首轮发现并修复**：facts 错挂羊宮妃那（已精确删除 50 条 `label=歌枠` 边 + 同 label 向量 chunks，用户授权）。**修复后重入库**：39 边、host=峰月律，验证 `峰月律-[歌う]->らんま1/2の曲`、`峰月律-[食べた]->おにぎり` 等正确归属，**羊宮妃那 本期 0 边**（未污染）；唯一 label 生效。图谱 1971→1983 实体 / 2891→2929 关系。
- 已知残留：首轮污染清理只删了边与 chunk，未删 42 个孤儿实体（无害噪音，`repair_graph` 可后续清理）；归档目录名去括号后含双空格/💙（仅外观）；该期 `broadcast_date` 空（folder 用 ISO 日期前缀，非 Radio 的「年月日放送」式，`parse_folder_metadata` 不解析——不影响入库）。
- 名称纠正（用户反馈）：峰月律真名为 **峰乐律**（非 立石凛，早前 Gemini 摘要误判），自称「りっちゃん」译为 **律酱**（非凛酱）。已更新方案 yaml/md 的 host 别名+术语表，并对归档的 04/05 做了 str.replace 纠正。
- 歌枠歌词版权安全（`src/clipper/lyrics.py`）：歌唱段不沿用 ASR 听写（易错且属歌词版权），`SongSpan` 内的句子改为整段占位「♪ 曲名 ♪」，或用**用户提供的已授权歌词字幕**（`lyrics_srt`）替代；谈话句正常上中日字幕。**不复刻任何歌词。**
- 交付样片：从本期切出 **らんま1/2 演唱段**（00:52:00–00:59:10，430s）→ `data/clips/ranma_*/clip_00_final.mp4`：谈话部分烧中日字幕，演唱部分烧「♪ らんま1/2 メドレー ♪」占位（抽帧确认）。
- Telegram 联动 + 字幕样式再调（用户反馈）：
  - 字幕**去黑色描边**：`packager.py` 改为全透明背景 + faux-bold（仅 fill 色微偏移加粗），无描边。
  - **本场曲清单**：`setlist.py` 从 05_summary 提取曲名+时间；deepseek 对「列歌曲」请求会返回空（其内容过滤），故 LLM 失败时**回退用摘要分区**（节目自带分区标题 + time_range，仅标题非歌词）。
  - **Telegram 切片联动**：`telegram_clip.py` —— Branch B `--telegram` 处理入库后，把「可能爆火片段 + 本场歌枠区块」推送到 Telegram（复用 Radio bot token/chat），每项一个按钮；点击 → 回调 `_render_job_item` 用 `render.render_segment` 切该段（谈话中日字幕、歌唱占位）并把成片发回。任务映射落盘 `data/clip_jobs.json` 跨进程共享。回调处理器 `register_clip_handlers(app)` 注册进 Radio 既有 bot（避免同 token 多消费者冲突；`Radio/src/radio/telegram_bot.py` 加可选 import，缺失静默跳过），也提供独立 `run_clipper_bot`。**需 `agent-down/up` 重启使 bot 装载新回调**。
  - `render.py`：抽出可复用 `render_segment`（切+字幕+烧录），CLI 渲染与 Telegram 回调共用。
  - Verified：setlist 回退出 7 个歌枠区块；菜单文案/按钮(1 爆火+7 区块)/任务映射 round-trip；模拟点击回调成功切出带占位字幕的成片；らんま 样片以去描边新样式重渲染（抽帧确认）。Telegram 实际收发需用户侧 bot 运行。
- 后续修正（用户反馈）：① 名字统一为 **峰月律**（撤销「峰乐律」，连同早前的「立石凛」一并纠正，归档 04/05 同步改回）。② 字幕样式按用户参考图重做（`packager.py`）：**全透明背景**（去底框）、**加粗**（stroke + faux-bold）、黑描边、**日文白/中文应援色**、贴画面**最下部**。③ 应援色 **#4477CC** 绑定到节目方案（yaml `accent_color` → `ProgramProfile.accent_rgb()`），渲染时由方案传入，CLI 渲染路径同样生效。④ `lyrics.py` 的歌唱占位/替换支持用户自备 `.srt`/`.lrc`（`load_lyrics`，纯格式解析，不内置歌词抓取/复刻）。

## 新增「数据驱动型内容二次创作」Clipper（独立功能，两条分支）— 2026-06-03

- 背景：用 B 站市场热度信号驱动素材的二次剪辑。目标内容是 YouTube 上虚拟主播（VTuber）的直播（含画面）。与既有功能完全独立，只读复用现有数据/能力，不改 ingestion/QA/来信/录制逻辑。
- 新增自包含子包 `src/clipper/`，技术路线 [市场情报]→[向量检索]→[自动切片]→[字幕烧录]，两条分支：
  - **Branch A（过往节目，market-pull）**：B 站分区热榜 → 爆款特征 → 在已有摘要向量库跨模态匹配高相关片段（带期数+时间戳）→ 切片 → 字幕 → 成片。复用 `SummaryRetriever`/`VectorStore`/`E5Embedder`。
  - **Branch B（新上传节目，content-push）**：`yt-dlp` 下载指定 YouTube 直播（视频+音频）→ **自动总结入库（无人工审查）** → 分析直播章节/标题，结合 B 站当前热点（大火歌曲/话题）判断爆火潜力 → 高潜力片段 → 切片 → 字幕 → 成片。
- 关键实现：
  - `bilibili_source.py`：真实 B 站爬虫。需先访问首页种 `buvid3/b_nut` cookie + WBI 签名，否则 `ranking/v2` 被风控拒（-352）。动量指标 = 播放/小时、投币点赞比、弹幕密度；以**本批最新发布时间**为基准（规避系统时钟偏差）。
  - `kb_ingest.py`：复用 Radio `run_pipeline` 出 03/04/05（关闭其 Telegram 推送与 handoff），再以 `auto_policy="confirm"` 跑 radio_kg 入库图——in-graph 解决冲突/纠偏，**不触发 interrupt 审查**（与 `ingest_batch.py` 同款）。
  - `aligner.py`+`whisperx_worker.py`：WhisperX 词级 forced alignment，**隔离在独立 venv `.venv_whisperx`** 经子进程调用（whisperx 会把 torch 2.12→2.8、transformers 5.9→4.57，直接装会破坏现有 e5/QA 栈）；未装/失败/超时回退到已有逐句转写（Branch A 用 04_bilingual）。两个本机踩坑已修：① HF 新 Xet 下载后端卡死 → worker 内置 `HF_HUB_DISABLE_XET=1` 走经典下载；② venv 的 `bin/python` 是 symlink，`Path.resolve()` 会解析到基础解释器而丢掉 venv → 用 `venv_python()` 不解析符号链接；并对子进程加超时 + 清理父 venv 环境变量。
  - `packager.py`：本机 ffmpeg 未编译 libass/drawtext，故用 Pillow 把每条字幕渲染成透明 PNG，再用 `overlay` 滤镜按时间区间硬烧；音频源（过往广播）先 `showwaves` 合成波形画面再烧。
- CLI：`python -m src.clipper.cli past|new [--dry-run|--no-render] [--partition ...] [--topk N] [--url ...]`。
- 新增依赖：`Pillow`（主 venv）；`whisperx` 装在独立 `.venv_whisperx`（不污染主 venv）。
- Verified：① B 站真实拉榜（music/game/douga 各 30 条）+ 动量排序 + LLM 特征蒸馏（检出大火歌曲「八方来財」）。② Branch A dry-run 真实匹配：热点「网络梗爆红」→ #104 段「羊宫妃那模仿网络梗"绘本"」score 0.60，plan.json 带【出处:期数+时间戳】，无媒体期正确标记不崩。③ 切片+字幕+烧录在 QRR 期（有媒体）端到端出成片：音频→波形视频+双语硬字幕 mp4（抽帧确认中日文渲染正确）。④ Branch B：yt-dlp 真实下载视频+info.json+章节解析；爆火分对「歌枠翻唱热门曲」章节给 0.90/0.85 且命中信号正确，无关章节正确忽略；dry-run 全流程产 plan.json。⑤ WhisperX 词级对齐经独立 venv 子进程端到端跑通（load/transcribe/align→词级时间戳+LLM 中译，烧成 mp4，字幕标注 whisperx(词级对齐)）；主 venv 仍 torch 2.12.0/transformers 5.9.0 未变。
- 未在本机端到端验证（依赖用户已配置的运行环境）：Branch B 全量入库需 Neo4j 在线 + Radio 的 STT/摘要 API key + 一条带章节的真实长直播；本机仅验证了 `auto_policy` 无审查路径的代码正确性与各组件接口。

## 修复直播录制失败：venv 缺失 yt-dlp — 2026-06-02

- 现象：YouTube 直播录制任务失败，`server.log` 报 `RuntimeError: yt-dlp YouTube live 录制失败 (exit 1): No module named yt_dlp`（`Radio/src/radio/youtube_live_source.py:136`）。
- 根因：录制通过子进程调用 `sys.executable -m yt_dlp`（`youtube_live_source.py:95`），而服务运行所在的 `radio_kg/.venv` 从未安装 yt-dlp——尽管 `requirements.txt:26` 已声明 `yt-dlp>=2024.8.6`。ffmpeg 正常。
- Fix：`uv pip install --python .venv/bin/python "yt-dlp>=2024.8.6"`，装入 `yt-dlp==2026.3.17`。
- 全流程验证中又发现并修复 2 个直播录制问题（代码在 Radio 仓，详见 `Radio/CHANGELOG.md`）：① `--live-from-start` 让 `-t` 时长上限失效→加挂钟 SIGINT 收尾；② 取消任务不杀 yt-dlp 子进程→改进程组收尾。需重启服务（`agent-down`/`agent-up`）使代码生效，因为 uvicorn 与 scheduler daemon 都在进程内 import 录制代码。
- Verified（真实端到端）：触发 ANN 直播录制 1 分钟 → 21:55:38「reached 1 min, stopping」→ m4a 就绪 → STT/翻译/总结/Telegram 推送 → `POST /api/ingest 200 status=completed written:1`，job=succeeded。验证后已清理测试集（删除 16 条 chunk 向量、history 行与录音目录，KG 复位 chunks=1606/summaries=675）。

## 期数放送日期问答 + 图谱卫生（长尾噪音剪枝 / 同名碎片合并）— 2026-05-29

### 1. 「第N期什么时候放送」能答了
- 现象：问具体某期放送日期 → 路由到 two_stage 检索 → 召回的逐字稿/摘要里根本没有日期 → 防编造校验诚实弃答「资料からは確認できません」。
- 根因：放送日期是 `doc_agent` 从文件夹名解析的 `broadcast_date`，存成**每条 REL 边的属性**（2398/2847 条边有），既不在正文也没做成可检索实体。是检索/路由盲区，非数据缺失。
- Fix：
  - `GraphStore.episode_broadcast_date(episode, program_hint)`：按期号直接查 `broadcast_date`（跨节目同期号按 program/label 分组，可用《节目名》缩小）。
  - `StatsAgent` 新增**确定性**期数元数据分支（不靠 LLM）：`_episode_meta_q` 正则识别「期号 + 放送/日期/什么时候…」→ `route()` 短路到 stats，`_episode_meta_answer` 查库并返回带【出处:《节目》第N期】的引用答案；查不到则 `fallback` 交回检索（不臆造日期）。
- Verified：第37期→2025年12月14日、第26期→2025年9月28日（mode=stats，带出处）；第999期未知→回退检索→诚实弃答。

### 2. 实体/关系比≈1 的诊断与治理
- 诊断（实测）：entities=2011 / relations=2847，比 1.42、平均度 2.83；**74%(1496) 实体度≤1**，type=`Other` 占 51%(1032)。抽样发现这些 degree-1 `Other` 多是被错误提升为实体的**整句/从句**（如「メールを読ませていただきます」「承知しました」，甚至邮箱「こもれび@jwqr.net」）。
- 结论：比值≈1 的主因是**长尾抽取噪音未过滤**，不是消歧问题——同名多类型碎片仅 28 个节点，对比值影响微乎其微（但影响 QA 质量，仍一并修）。
- Fix（两手）：
  - **预防（治本）**：`canonical.is_clause_fragment()` 保守启发式（标点/动词活用尾/格助词）；`ExtractorAgent._build_triple` 在抽取期丢弃 clause-fragment 实体，杜绝再生。
  - **存量清理**：`src/cleanup_graph.py`（默认 dry-run，`--apply` 写库）——① 同名多类型碎片合并到规范类型（`GraphStore.merge_entity` 改写边并删冗余节点，类型优先级 Program>Org>Person>…）；② 剪除 degree≤1 的 `Other` 句子/超长/邮箱噪音（`detach_delete_entities`）。
- Verified（已 `--apply`）：合并 28 + 剪枝 212 → entities 2011→**1771**、relations 2847→**2634**、比 1.42→**1.49**；同名多类型组 0；`羊宮妃那のこもれびじかん` 收敛为单一 Program 节点（274 边保留）；合并后的听众 `おさしみさん`/`ゆぐどらしる` 边完好、dossier 问答正常；脚本幂等。保守保留 `漫画`/`酒`/`理想のプロポーズ` 等真实短名词（不为刷比值误删）。
- 注：`Agent/` 非 git 仓库，清理为不可逆写操作，已经用户确认后执行。后续如需更激进可调 `--max-noise-len`。

## 聊天 UX：欢迎页示例卡片 / 等待期分阶段进度流 / 答案 👍👎 反馈 — 2026-05-29

以产品经理视角做的三项 UX 改进，均在 `chat.html` + `server/app.py` + `conv_store.py`：

### 1. 欢迎页示例卡片
- 空对话欢迎页新增 4 张可点击卡片（问答 / 统计 / 档案 / 来信），点一下即 `setMode + 自动发送`，解决冷启动「不知道能问什么」。统计/档案归在问答模式（由 `StatsAgent.route` 自动判路由），来信卡片切到来信模式。
- 同时把原先藏在欢迎页的「订正/记住」提示移走（见第 3 项）。

### 2. 等待期分阶段进度流（SSE，非 token 流）
- 新增 `GET /api/conversations/{cid}/ask_stream?q=&mode=`：`text/event-stream`，实时推送阶段事件 `{type:stage|done|error}`，最终 `done` 带新 assistant 消息 id。
- 为什么不是逐字 token 流：问答答案走「`complete_json` 生成结构化事实 → 第二次 LLM 逐条校验丢弃无依据事实 → 渲染」管线，可读文本只在两次调用后才存在，token 流会暴露 JSON 中间态并绕开防编造校验。故选分阶段进度，保留硬约束。
- `_run_qa(question, history, progress=None)`：新增 progress 回调；两阶段检索由 `QA2_GRAPH.invoke` 改为 `.stream()` 逐节点累积（结果与 invoke 等价），在 `analyze→retrieve2→generate` 边界吐「规划检索 / 检索资料 / 已召回 N 条，生成并校验」。POST 路径传 no-op，行为不变。
- SSE 端点在 worker 线程跑 `_run_qa`（持 reader 锁），progress 经 `queue.Queue` 实时 yield 给客户端，避免「攒到最后一次性出」。来信/订正分支也走该端点，吐对应阶段。
- 前端 `send()` 改用 `EventSource`：占位气泡显示带 spinner 的实时阶段文案；`done`/`error`/`onerror` 后重新拉取会话渲染最终答案。

### 3. 答案 👍/👎 反馈（替代欢迎页订正提示的承载位）
- `conv_store`：`get()` 暴露每条消息 `id`；`add_message` 返回 `last_message_id`；新增 `set_feedback(cid,msg_id,value)` 把 👍/👎 写进消息 meta（`value=null` 清除）。
- 新增 `POST /api/conversations/{cid}/feedback` `{message_id,value:"up"|"down"|null}` 持久化并记日志。
- 前端：qa/mail 答案下方加 👍/👎 条；点 👎 就地展开「订正/记住」引导（含示例 `订正：羊宮妃那 所属 青二プロダクション`）——把改知识库的入口放到「用户觉得答得不准」的那一刻。

### Verified
- `app.py` / `conv_store.py` 语法 + 导入通过；`ask_stream`、`feedback` 两条路由在 import 期成功注册（无需起 Neo4j）。
- 线上 `chat.html` 为新版（FileResponse no-store 直读磁盘）；客户端 JS 新函数齐全、括号配平。
- ⚠️ 端到端运行未验证：:8000 上的进程是改动前启动的，未热加载新后端路由（`ask_stream`/`feedback` 当前返回 Not Found）。需 `agent-down` + `agent-up` 重启栈（会一并重启 Neo4j 与 Radio 调度 daemon）后方可生效与联调。

## Fix：chromadb 集合 UUID 漂移导致 `/api/ask` 500（前端看到的 "json 错误"）— 2026-05-28

### 现象
- `/api/ask` 报 `chromadb.errors.NotFoundError: Collection [b4c2ac06-...] does not exist`，前端拿到非 JSON 的 500 文本去 `JSON.parse` 触发「json 错误」。
- 根因：`VectorStore.__enter__` 把 `Collection` 句柄缓存进 `_direct_collection`，句柄里冻结了集合 UUID。一旦有人重建过 `radio_summaries`（例如重跑 `python -m src.build_summary_db`），UUID 就变了，老句柄全部 404。

### Fix `src/mcp_layer/vector_store.py`
- 拆分 `_ensure_direct_collection`（首次：建 client + collection + embedder）与 `_refresh_direct_collection`（仅重新 `get_or_create_collection` 拿当前 UUID 的句柄，不重载 e5 模型）。
- 新 `_safe_direct_call(fn)` 包裹所有 direct-chroma 调用（`query` / `add_chunks` / `count` / `_get_all_documents` / `get_window`）：捕到「集合不存在」类异常时刷新一次句柄重试。
- `_is_collection_missing` 跨 chromadb 版本识别：`NotFoundError` / `InvalidCollectionException` / `ValueError("... does not exist")` 都算。

### Verified
- 模拟脚本：startup→add 1 doc→外部 `client.delete_collection`→再 `count()`/`query()`，旧版抛 NotFoundError，新版自动刷新返回 0 与空结果。
- 配合上一条架构加固里的 `/api/health`，向量集合漂移会以 `degraded` 主动暴露，不再要等到下一次 QA 才发现。

## 架构加固：读写锁 / 持久化 pending / 健康探测 / MCP 重连 / 结构化日志 — 2026-05-28

### Changed — 全局 `_LOCK` 拆为读/写锁 `src/server/rwlock.py`
- 原 `threading.Lock()` 把摄入步骤、KB 写入和所有 QA 检索串行成一个队列。新 `ReadWriteLock`（writer-priority）允许并发 QA 读，写者（`_step` 摄入、`MEMORY_AGENT.apply` 知识库写）独占。
- `_run_qa` / `MEMORY_AGENT.parse` / `PERSONA_AGENT.reply_mail` 走 `_RW.reader()`；`_step` / `kb_confirm` 走 `_RW.writer()`。读者计数+等待写者条件变量保证写者不被读流量饿死。

### Added — 持久化 pending store `src/server/pending_store.py`
- 原 `PENDING` / `KB_PENDING` 是进程内 dict，重启就丢，但 FastAPI 前端依然在拉。新 `PendingStore`（SQLite `data/pending.sqlite`）三张表：
  - `pending_interrupts`（摄入审批中断）：put/pop/get/list。
  - `pending_kb_edits`（KB 订正预览待确认）：put/pop。
  - `ingest_commit_log`（每个 stage 一行 started/ok/failed/interrupted/committed/cancelled）：解决 Neo4j+Chroma+checkpoint+index_version 四套存储无事务、部分失败不可见的问题。`incomplete_threads()` 在启动时报最后非终态线程，新增 `GET /api/ingest_log/{thread_id}` 暴露逐步日志。

### Added — `/api/health` 健康探测端点
- 五个组件并行 ping：`graph`（Neo4j MCP `RETURN 1`）、`chunk_vector` / `summary_vector`（`count()`）、`conversations`（SQLite）、`pending_store`、`scheduler_daemon`（读 `Radio/data/logs/radio-daemon.pid` + `os.kill(pid,0)`）。
- 每个组件返回 `{ok, ...}`；整体 `status` 为 `ok` 仅当全部 ok，否则 `degraded`，便于外部 watchdog/page-alert。响应里带 `uptime_sec` 与当前 `trace_id`。
- 配套 `GraphStore.ping()` / `VectorStore.ping()`、`McpStdioClient.is_alive()`。

### Added — MCP 客户端自动重连 `src/mcp_layer/client.py`
- 原实现 stdio 子进程崩溃后 `_session` 变 None，后续所有 `call_tool` 永久 `RuntimeError`，只能整服重启。新版：
  - `_run_loop` 包裹异常并把 `_dead=True`、`_session=None` 置位，记录 `_exit_reason`。
  - `start()` 检测 dead/None 时通过 `_restart_lock` 排他重建 loop+thread+session。
  - `call_tool` 调用过程中若拿不到 session 或 loop 已关闭，标记 dead 并自动重连一次再重试。
  - `is_alive()` 供 `/api/health` 直接拿。

### Added — 结构化日志 + 请求级 trace_id `src/server/logging_setup.py`
- `JsonFormatter`：每条日志 JSON 化 (`ts/level/logger/trace_id/msg + extra`)。`trace_id` 走 `ContextVar`，QA / ingest / KB 路径里随便调 `log.info("...", extra={...})` 都会自动带上。
- `TraceIdMiddleware`：每个 HTTP/WebSocket 请求生成或继承 `X-Trace-Id`，响应头回写；每个请求自动落一条 `request` 日志（method/path/status/dur_ms）。
- `_step` 把 `trace_id` 写进 `ingest_commit_log.detail`，串起多 agent 链路调试。

### Verified
- 模块 `import` 通过；`app.routes` 出现 `/api/health` 与 `/api/ingest_log/{thread_id}`；`TraceIdMiddleware` 已挂载；`_RW` 是 `ReadWriteLock`。
- 读写锁单测：5 并发读者可同时持锁、写者请求会等所有读者退出。
- PendingStore 单测：interrupts/kb_edits put-pop-list、commit log 终态/非终态分流均符合预期。
- 日志单测：trace_id 由 `new_trace_id()` 写入 ContextVar 后落入 JSON 记录。

## 新一轮入库 + 结构化回答 + 索引版本指纹 + 路由行为知识 — 2026-05-28

### Added — 批量入库脚本 `src/ingest_batch.py`（全合集 / 全程序 / 无人值守）
- 不像 `python -m src.ingest --all`（强约束 `archives_only=True`+`require_number=True`，会跳过未编号直播和别番组），新脚本走 `iter_collections` 发现**所有**合集和**所有**带 segments 的文件夹，过滤掉图谱已 ingested 的，剩下的一次性走完。
- 每一期都做完整三件套：图谱 + 块向量（流水线内置）+ 摘要向量（与 server 同款 `_index_summary_folder` 等价处理），避免「图谱新、向量旧」。
- 无人值守（`--auto confirm` 默认），thread_id 加 uuid 后缀避免与历史 checkpoint 撞车。失败按条记账，连接错误等瞬时问题再跑一遍就自动只补失败的。
- 本轮入库实测：31 新文件夹 → 第一轮 15 ok / 15 connection-error，重跑 15/15 ok。当前图谱 **2011 实体 / 2847 关系 / 98 期**，包含こもれびじかん新话 #34-50（こもればなし+アーカイブ两版）、别番组 EXTEND STEP HOOOOPE！#102-114、文化放送 QRR 直播、NACK5 直播。

### Fixed — 未编号直播 `start_epoch=null` 入库失败
- 现象：番号なしライブ（QRR、NACK5、HOOOOPE 増刊号）触发 Neo4j MERGE 因 `start_epoch=null` 报 SemanticError。
- 修复：`sync_agent.epoch_for(src)` 帮助函数：有 episode# 用之，否则从 `broadcast_date`/`episode_label` 抠出 `YYYYMMDD` 当作稳定 epoch，最差兜底 0。`_write_edge` 改用它。
- 效果：QRR/NACK5/HOOOOPE 全部成功入库。

### Added — 索引统一版本号 + 构建指纹 + 漂移检测 `src/index_version.py`
- 让 graph / chunk_vectors / summary_vectors / persona 各 store 同框比较：单一 registry JSON（`data/index_version.json`）记录每个 store 的 `version`（构建计数器）+ `fingerprint`（覆盖期 `episode_label` 集合的 sha1）+ `n_labels`+`updated_at`。
- 自动增量同步：server `/api/ingest` 完成后调 `_restamp_indices()` 重新打戳；`ingest_batch.py` 末尾打戳；`build_summary_db.py`、`build_persona.py` 各自构建完后打戳自己。CLI 不再让某条索引偷偷落后。
- 新端点 `GET /api/index_status`：对每个 store 报版本/指纹，并做**集合差漂移检测**（`missing_from_index` = 图谱有但索引缺，`extra_in_index` = 索引有但图谱无），不只是「fingerprint 不等就 STALE」。
- 实测当前：graph v1 / chunk_vectors v1 / summary_vectors v2 / persona v1；summary 和 persona 指纹一致（同源 `parse_folder_metadata.episode_label`），与 graph 之间 label 命名约定历史不齐（graph 有 `#1` 风格、vector 有完整 folder name），漂移会报但实际数据已经覆盖到位——这是后续 label 规范化的目标，已暴露给端点便于追踪。

### Added — 结构化回答：事实句 → source_id → 引文 + 后置校验
- 新 `QAAgent.answer_structured(question, passages, history)`：把检索到的每条 passage 编号 `[1..N]`，LLM 必须输出 JSON `{"abstain":bool, "facts":[{"fact":"...", "source_id":N}, ...]}`——每条事实的 source_id 必须是真实出现在 context 里的整数编号，不能编造。
- 后置校验流水线：
  1. 结构过滤：剔除 source_id 不在 context 范围内的事实（防止「引用一个没检索到的出处」）；
  2. 内容核查：批量 LLM 核查器 `VERIFY_STRUCT_SYSTEM` 对每条 `(fact, source_text)` 判 supported，宽松到「概括/改写也算支撑」，仅拒绝**完全凭空**的具体细节（人名/数字/情节）；
  3. 渲染：保留下来的事实以 `- 事实句【出处:citation】` 形式逐行输出；全军覆没就转「资料からは確認できません」诚实回答。
- 接线：`qa_graph2.generate` 在 `settings.qa_structured_answer=True`（默认开）时走 `answer_structured` 替代旧自由文本生成；`debug.facts_kept` / `facts_dropped` 可观测。
- 同时改 `ANSWER_STRUCT_SYSTEM`：明确「**同一事象在多个 source 出现合并为 1 件**」（避免 Q17 那种把一句话用 4 个出处重复 4 次）。

### Added — 路由行为知识：60 条范本 + 路由 few-shot 注入
- `src/build_route_exemplars.py`：以「顶级声优广播领域知识工程师」身份，针对 stats / dossier / retrieval 三条路由**各**生成 20 条**真粉丝口吻**（自然口语、亲昵称呼）的代表性提问，每条带 `routing_rationale`（为什么走这条路由）和 `method`（系统该怎么作答 + 答案结构），以及一个 `standard_answer` 模板（重点在结构与出处占位，不在背诵具体数字）。落 `persona/route_exemplars.json`。
- 运行时注入：`StatsAgent.__init__` 加载范本，挑每路由前 4 条拼成 few-shot block 拼在 `ROUTE_SYSTEM` 之后——「学习方式而不是答案」：只让路由学会判断**该走哪条**，不喂任何具体答案。
- 兼容：未生成范本时降级为空字符串、原 ROUTE_SYSTEM 行为不变。

### Verified — 20 问最终评估
- 入库前优化后基线（structured 关）：rel≈0.83 / comp≈0.71 / grnd≈0.69，0–1 punt。
- 入库后 + structured 开 + verifier 严格：rel 0.78 / comp 0.38 / grnd **0.83**，1 punt（verifier 把合法事实也丢掉了）。
- 入库后 + structured 开 + verifier 宽松（最终态）：rel 0.71 / comp 0.50 / grnd 0.66，**0 punt**。
- 不变的强项：Q5 来信主题（新语料 184→**336 封**反映全部入库，top3 给比例+出处）、Q6/Q7 来信排名（にらら 10 次反映全语料）、Q12/Q16/Q18/Q19 retrieval 路由稳定满分。
- 已知 trade-off：结构化+核查使**每一条事实都有真实出处**且**未支撑句被拒**，但相应地，retrieval 路由会更常诚实棄権（rel/grnd 因「棄権是正确行为」仍能拿到分，comp 自然降低）。这是「不允许编造」与「全面回答」之间的取舍，符合本次的硬性需求方向。
- 单次评估抖动可见（同问跨 run 0.3–1.0），主要来自 LLM 生成与 judge 的非确定性。详细数据：`data/eval_qa20_baseline.json`、`data/eval_qa20_final2.json`。

## 20 问实验评估 + 问答能力补强（来信分析 / 不再死路 / 主持人 dossier 护栏）— 2026-05-27

### Added — 20 问评估实验台 `src/eval_qa20.py`
- 用户设计的 20 个分析型问题（梗文化/高频词/来信图谱/嘉宾人脉/个人成长/运营编年史）跑通整条路由（stats|dossier|retrieval），用 LLM-judge 评 `relevance/completeness/grounding` 并标注 `punt`（消极回避）；记录每题命中的路由，落 `data/eval_qa20_*.json`。
- judge 健壮化：max_tokens 400→800、要求 `reason_zh` ≤25 字且不引用原文（避免日文引号/换行破坏 JSON）；新增 `_salvage()` 在 JSON 截断/转义出错时用正则抢救四项分值——修复了「回答本身正确、仅因 judge JSON 故障被记 0 分」的假阴性。

### Added — 全语料来信分析 `src/agents/mail_analytics.py`（解决统计死路与误答）
- 背景：图谱 `投稿` 边能排发信人，但不带来信**内容/环节**；而 windowed 检索每次只看到几期，二者都答不了「来信主题 top3」「传奇听众最常投哪个环节」这类**全局**问题。
- `MailAnalytics` 直接扫 `persona/mail_exemplars.json`（每封被读到的来信：发信人/主题文本/环节/出处），语料完整而非 top-k：
  - `sender_ranking`：敬称归一计数 + 最常投的**规范化环节** + 示例出处（Q6/Q7）。
  - `theme_distribution`：**分批**（每 50 封，规避单次 184 键 JSON 被截断）LLM 逐封归入固定主题表；**计数在标签上确定式完成**（数字不由 LLM 杜撰），输出 top-N + 占比 + 示例出处（Q5）。
- 接线：`StatsAgent` 新增 `mail_analytics` 依赖；mail 类问题在 `answer()` 内按「主题关键词→主题分布 / 排名关键词→发信人排名」分派；`top_subjects(投稿)` 也改走 MailAnalytics 以带上环节与出处。

### Changed — StatsAgent 不再死路 + 路由澄清
- `answer()` 映射不到安全工具时返回 `{"fallback": True}`（取代「暂不支持该统计维度」），server `_run_qa` 据此**回落到二阶段检索**而非中断（修复 Q4 惩罚游戏/神回、Q9 嘉宾）。
- 「嘉宾/ゲスト」类问题无干净图谱关系（`出演` 是主持人本人出演而非来宾），直接回落检索（Q9 因此正确找到首位嘉宾桜谷梨子）。
- `ROUTE_SYSTEM` 澄清：dossier 仅用于**具体人名**的「全部记录」，话题/关键词（如「失眠」）应走 retrieval（修复 Q3 误入 dossier 只列 1 期）。
- `StatsAgent.dossier()` 新增**主持人护栏**：解析到的名字若是主持人（羊宮妃那及别名），主持人的 dossier＝整张图谱，无意义且会空答/中断——直接返回 None 回落检索（修复 Q15 清单、Q16 风格趋势被误判为 dossier 后空答）。

### Verified — 20 问前后对比（DeepSeek judge，单次运行有 LLM 抖动）
- 基线：Relevance 0.59 / Completeness 0.51 / Grounding 0.51，消极回避 6/20（其中 4 例实为 judge JSON 故障导致的假 0）。
- 优化后：Relevance ≈0.83 / Completeness ≈0.67–0.71 / Grounding ≈0.63–0.69，消极回避降至 1（且该 1 例为 judge 空响应的假阴性，回答本身正确）。
- 确证的能力增量：Q5 来信主题(0→满分)、Q9 嘉宾(死路→正确)、Q15 清单(空答→分类罗列)、Q6/Q7 来信排名(补环节+出处)、Q13 情绪追踪(judge 抢救后满分)。
- 仍存在的局限（已知、非本次范围）：① Q4「惩罚游戏/神回」节目本身无此环节，系统如实答「未找到」属正确行为；② Q11 最常提及的声优朋友、Q16 说话风格随期演变、Q17 重大发表完整时间轴——都属「跨全期枚举/趋势」型，windowed 检索结构上召回不全，需各自的离线聚合工件（与 mail_analytics 同范式）方能补全；③ Q1/Q3/Q8 等检索题存在明显的 run-to-run 抖动（LLM 生成 + judge 噪声）。

## 修复：合并后被丢掉的后台进程（调度守护进程 + Telegram bot）+ 全功能体检 — 2026-05-24

### Fixed — 调度守护进程（定时录制）
- 现象：合并为单端口后，`scheduled_programs` 的 cron 定时录制不再触发。日志显示调度守护进程 `main_daemon` 最后一次运行停在合并前（旧 `Radio/start.command` 启动的进程），之后无人拉起。
- 根因：调度器是**独立进程** `Radio/scripts/main_daemon.py`（APScheduler，jobstore=`Radio/data/scheduler.sqlite`）；Radio 的 FastAPI 只提供 `/api/scheduler/programs` 的增删查，并不跑调度。合并后的 `agent-up.command` 只启动 radio_kg 的 uvicorn（内挂 Radio API 子应用），从未启动这个守护进程。
- 修复：`agent-up.command` 新增 `start_scheduler_daemon()`，在「正常启动」和「服务已在跑」两条路径都调用——在 `Radio/` 工作目录下以 `Radio/.venv` 拉起 `main_daemon.py`，pid 写 `Radio/data/logs/radio-daemon.pid`，幂等；注入 `RADIO_KG_AUTO_INGEST_URL` 使定时录制完成后自动入库 radio_kg。`AGENT_START_SCHEDULER=0` 可关闭。停止沿用 `agent-down.command → Radio/stop.command`。
- 实测：`agent-up` 后 `main_daemon` 在跑，注册「文化放送 QRR 00:30 定期番組」(cron mon 00:30 Asia/Tokyo)，下次触发 2026-06-01。

### Fixed — Telegram HITL bot（审批/小红书保存 的交互回调）
- 现象（全功能体检发现，与调度器同源）：旧 `Radio/start.command` 启动 daemon+bot+api 三个进程，合并后的 `agent-up` 只起 api(+daemon)，**Telegram bot `main_bot.py` 从未被拉起**——Telegram 审批按钮、小红书保存回调失效。
- 第二根因：`main_bot.py` 缺少其它入口脚本都有的 `sys.path.insert(0, ROOT/"src")`，而 `Radio/.venv` 的 editable 安装 `.pth` 仍指向合并前的死路径 `/Users/USERNAME/Radio/src`，导致 `ModuleNotFoundError: No module named 'radio'`。
- 修复：① 给 `main_bot.py` 补上与 `main_daemon` 等同款的 `sys.path.insert(src)`；② `agent-up.command` 新增 `start_telegram_bot()`（两条路径都调用，仅当 `Radio/.env` 有 `TELEGRAM_BOT_TOKEN` 时启动，pid `radio-bot.pid`，幂等，`AGENT_START_BOT=0` 可关）。
- 实测：bot 进程在跑并 `启动 Telegram HITL bot polling`。

### 全功能体检结论（合并后逐项核对，均通过）
- HTTP：radio_kg `/ /dashboard /ask /api/episodes /api/pending /api/conversations` 全 200；Radio 子应用 `/radio/api/{health,collections,profiles,scheduler/programs,metrics,knowledge/*,jobs,credentials}` 全 200；根级 `/api/*`、`/assets/*` 307 跳 `/radio/*`。
- radio_kg CLI 模块（ask/ingest/eval_qa/bench_perf/scorecard/build_summary_db/build_persona/build_listener_db/rebuild_vectors/repair_graph）全部 import 通过；数据发现在新 `RADIO_DATA_DIR=../Radio/data/recordings` 下正常（85 期含摘要+编号，3 合集）。
- Radio 入口脚本（main_api/bot/daemon/oneshot/radiko/resummarize/video/youtube_live/metrics_report）`--help` 全 rc=0。
- 残留（已记录、非阻断）：`Radio/.venv` 的 editable `.pth` 指向死路径——因所有入口都自插 `src` 故无实际影响；`Radio/deploy/radio.plist` 仍写旧路径 `/Users/USERNAME/Radio` 且未被 launchd 加载（仅作参考文件）。

## 实体档案（完整追溯）+ 多合集消费 + 浅色统一前端 — 2026-05-24

### Added — 实体档案 / 完整追溯（解决「统计到发信人却追溯不到其全部来信」）
- `GraphStore.entity_records(eids)`：固定只读模板，返回某实体**双向全部**边（主体或客体），含 relation/对象/期数/时间戳/出处/end_epoch，按期数+时间排序，limit 4000——完整无截断的实体轨迹。
- `GraphStore.resolve_entities(name)` + `_strip_honorific`：把名字链接到图谱节点，**忽略尾部敬称**（さん/ちゃん/くん/様/氏…），所以「にらら」与「にららさん」、以及同一指代的 Person/Listener 多节点会被并集召回。
- `StatsAgent.route()`：单次 LLM 三分类 `dossier|stats|retrieval`(+实体名)，替代 server 端原 `is_stats` 调用（不增加延迟）。`StatsAgent.dossier(name)`：拉全部 `entity_records` + 为本人主体的每条记录用 `VectorStore.get_window` 取对应广播原话（去重，封顶 150），以 `qa_answer_max_tokens` 高预算 LLM 完整汇总、逐条保留【出处】、结尾给总量。**完整性优先、不计 token/时延**。
- server `_run_qa` 接入 dossier 分支（名字无法解析则回落普通检索）；`chat.html` 元信息显示「实体档案（完整追溯）」+ 图谱记录条数。
- 实测：にらら → 并集 2 节点 12 条记录跨 6 期，6 封来信出处全召回（第3/6/17/18/26/30期）；与 StatsAgent 排名计数一致。

### Changed — radio_kg 直接消费 Radio 生产端 + 多合集
- `RADIO_DATA_DIR` 默认改为 `../Radio/data/recordings`：radio_kg 直接读取 Radio 听取/总结后的产物，不再依赖单独的 `hina_radio` 扁平目录。
- `source_data.iter_collections()` 按合集（recordings 下一级子目录）分组发现，放宽「必须有 #期数」限制；无编号直播（QRR/NACK5 等）也纳入。`doc_agent.derive_program()` 从文件夹名推导节目名（非 hina 合集各自成节目），`SourceRef.citation()` 无期数时降级为「《节目》日期」。
- `GraphStore.ingested_labels()` + `/api/episodes` 改为返回按合集分组（含 ingested 状态），`chat.html` 后台文档面板按合集折叠展示、入库带 in-progress→完成/待审批/失败 状态流转。

### Changed — 前端设计统一为浅色
- `chat.html` 整体改为 Radio 控制台同款浅色 sage-green 配色/字体；右上角新增醒目「🎙 Radio 控制台 ↗」入口；左上角标识更正为「radio_kg 知识库」以与 Radio 控制台区分（早前误用「Radio Oshikatsu」眉标导致看起来像进错页）。

## Agent 本地一键启停 command — 2026-05-24

### Added
- `agent-up.command`（项目根 `Agent/` 下）：一键拉起本地全部运行进程——确保 Neo4j（Homebrew service）在跑并等 7687 就绪，再以 `nohup` 后台拉起 FastAPI 服务（`.venv` 内 uvicorn :8000，前端为同服务托管的 vanilla HTML，无独立前端进程），pid 写 `radio_kg/data/server.pid`、日志写 `radio_kg/data/server.log`；幂等（已在跑则跳过），支持 `PORT` 覆盖端口。
- `agent-down.command`（项目根 `Agent/` 下）：一键关闭——按 pid 文件停服务并 `pgrep` 兜底清残留，默认一并 `brew services stop neo4j`。
- 对应当前默认的「本地 `.venv` + Homebrew Neo4j」运行方式（与已蛰伏的 Docker 路径 `scripts/docker-up.sh`/`docker-down.sh` 区分）。启停逻辑上一轮已端到端实测通过（GET / → 200，关闭后端口释放）。

## [Phase 16] 实体碎片化治理：同名多类型节点归并 + 入库侧防御 — 2026-05-24

记分卡 Phase 15 暴露的「40 组同名多类型重复实体」清零。先调研成因，再「存量修复 + 入库防御」双管：存量一次性归并，入库侧补防御避免复发（复合名词碎片本轮仅调研、未动）。

### 成因（调研结论）
- 实体身份键为 `eid = type:norm(name)`，故**同名不同 type = 不同节点**。
- **来信人 Listener/Person 碰撞（26/40 组，主因）**：`build_listener_db.py` 盲写 `type:'Listener'`（从不查重），而 ExtractorAgent 的 `ENTITY_TYPES` **不含 Listener**，把同一来信人抽成 `Person` → 两条流水线各建一点。
- **节目名 7-way 碎裂**：「羊宮妃那のこもれびじかん」across Organization/Project/Person/Other/Segment/Work（抽取类型漂移）+ Program（build_listener_db）。
- **Extractor 类型漂移 + 表外 type**：`_resolve` 不校验白名单，LLM 乱编的 `Service/Character/Program` 直接入库。

### Added
- `GraphStore.duplicate_name_groups()`：固定只读模板，返回同名跨 type 分组（守住「只走 graph_store 模板」安全边界）。
- `src/repair_graph.py` 同名跨 type 归并：type 优先级裁决器（`Listener` 特例——保住 StatsAgent 来信人统计；程序名→`Program`；其余按 Organization>Project>Work>Segment>Event>Place>Person>… 优先级），遍历分组复用 `redirect_entity` 搬边（期数/时间戳/出处元数据全保留）。

### Changed（入库侧防御，避免复发）
- `ExtractorAgent`：`ENTITY_TYPES` 加入 `Listener`（与 build_listener_db 共用词表）；`_resolve` 建新节点时对白名单外 type 降级为 `Other`。
- `build_listener_db.py`：建 Listener 节点后吸收同名异 type 节点（复用 `redirect_entity`），不再留同名重复点。

### Verified（本机运行）
- `repair_graph`：一次性归并 40 组（另手工校正 2 个 Person-first 误判：文化放送エクステンド→Organization、思想の葉物語→Project）。
- `scorecard --quick`：**同名多节点 40 → 0**；主持人单节点且度 963；A/B 全 6 项达标。来信人 `count_by_type('Listener')`=153（统计未损）。
- QA 回归：来信人枚举 / 节目名释义 / 事务所归属三问答案与出处均正常，无回归。

### 仍未处理（下一轮，仅调研）
- **复合名词过度抽取**：「羊宮妃那の父親/姉/母/出演作/運動習慣/ラジオ/番組」本应是关系（羊宮妃那 -[父]-> X）却被抽成独立节点；外加 ひな/妃那 读音碎片。属抽取语义层，风险较高，单列后续。

## [Phase 15] 项目基准记分卡（全维度测量与评估）— 2026-05-24

以产品经理视角，把项目里所有「值得测量」的基准收敛成一张可重跑记分卡 `src/scorecard.py`（全程 in-process，无需起服务），四维度逐项对标目标并落 JSON 供趋势追踪。

### Added
- `src/scorecard.py`（`python -m src.scorecard`；`--quick` 跳过 LLM 密集的 C/D）：
  - **A 覆盖与规模**：实体/关系数、已入库期数(=可用，按 distinct #N)、四个向量库条数、主持人共指健康（同名节点=1、中心节点度）、同名多类型重复实体组数（图谱卫生）。
  - **B 性能(PRD 7.1)**：MCP `search_nodes`/`neighbors` p95(<200ms)、混合检索 p95/mean(<2s)；LLM 分析在计时段外，测前预热 e5/MCP（避免冷加载污染）。
  - **C 问答质量**：复用 `eval_qa` 的 LLM-judge 与 ±30s 接地校验，in-process 跑两段式 QA；Faithfulness/Relevance/Grounding/不可答正确棄権/上下文规模。
  - **D 来信模式质量**：中文版纯中文率（假名检测）、命中往期来信率、引用出处可解析率、红线护栏召回、正常来信不误拦。
  - 每项 value vs target vs ✓/✗；末尾总达标数 + 未达标清单；明细写 `data/scorecard_<时间>.json`。

### Verified（本机一次完整运行：14/15 达标）
- A：实体 1048 / 关系 1444；28/28 期；向量 704/333/336/184；主持人单节点且度 969（共指成功）。
- B：MCP p95 ~14/28ms、混合检索 p95 ~828ms / mean ~589ms —— 全达标（与 bench_perf 一致）。
- C：Faithfulness 1.00、Relevance 0.83~1.00、不可答正确棄権 1.00。
- D：纯中文 1.00、命中往期来信 1.00、出处可解析 1.00、红线召回 1.00、不误拦 1.00 —— 全达标。

### 记分卡暴露的待办（本次只测量、未修复）
- **图谱卫生：40 组同名多类型重复实体**（如「羊宮妃那のこもれびじかん」被切成 Organization/Project/Person/Segment/Program/Work 等多个节点；「羊宮妃那の父親 / 姉 / ラジオ」等复合碎片实体）——抽取/类型归一待清理。
- **Source Grounding 偏低且波动**：本轮 0.60（目标 0.8），历史在 0.60–0.86 间随 LLM-judge + 仅 6 题波动；可由 4.1 Reranker / 扩大评测题量 / 收紧 top_n 改善，列为观察项。
- **问答上下文 ~46k 字/问偏大**：top_n=48「完整性优先」所致，token 成本/延迟观察项。

## [Phase 14] PRD 7.1 性能基准验证 — 2026-05-24

PRD 功能需求已全部交付；补上唯一未正式验证的非功能性需求 7.1（性能指标）。

### Added
- **性能基准脚本** `src/bench_perf.py`（`python -m src.bench_perf`，对准 PRD 7.1）：
  - MCP 单次工具调用延迟（Neo4j-MCP `search_nodes` / `neighbors` 2 跳 read_neo4j_cypher 往返），目标 <200ms。
  - 混合检索（摘要路由 + 图 2 跳 + 原文窗口回查 + 直检兜底 + RRF 融合）总耗时，**LLM 问题分析放在计时段之外**，目标 <2s。
  - 每项做预热（e5 模型加载 / MCP 冷启动 / 查询计划缓存）后再测，报告 mean/p50/p95/max 与达标率。

### Verified（本机 M2 + Homebrew Neo4j + 本地 Chroma/e5，957/876 实体级图谱）
- MCP `search_nodes`：mean 13.4ms / p95 16.0ms / max 24.3ms（20/20 ≤200ms ✓）。
- MCP `neighbors` 2 跳：mean 31.3ms / p95 70.3ms / max 95.1ms（20/20 ≤200ms ✓）。
- 混合检索总耗时：mean 553ms / p50 468ms / p95=max 872ms（6/6 ≤2s ✓）。
- 结论：PRD 7.1 两项指标均**大幅达标**。（运行时 `mcp-neo4j-cypher` 打印的 `EXPLAIN parameter missing` 为上游计划缓存检查的良性提示，不影响正确性与计时。）

## [Phase 13] 主持人「双层技能」人设 + 来信模式 — 2026-05-23

借鉴论文 COLLEAGUE.SKILL（专家数字痕迹→两层可调用技能：工作技能 + 五层人设；运行时「任务→人设决定态度→技能执行→用其口吻输出」）。
把范式套到本项目：数据源是 28+ 期节目的摘要与逐字稿，蒸馏出主持人羊宮妃那的「话题观点库 + 人设画像」，并新增**来信模式**——
把用户发言当作一封お便り（投稿），按节目里真实「读信→回信」的方式拟写一段回复。

### Added
- **离线蒸馏脚本** `src/build_persona.py`（对应论文 analyzer + builder，可重跑覆盖，`python -m src.build_persona`）：
  - 复用 `build_summary_db` 的折扫描与时间解析、`doc_agent.parse_folder_metadata` 取期数/出处。
  - 每期一次 LLM：从摘要 `member_reactions` + 逐字稿提取主持人**逐字金句**（带话题/情感/时间戳）与「声音特征」信号。
  - 全局两次 LLM：按话题汇总「情感态度倾向」；汇总声音信号构建五层人设。
  - 产物入库 `persona/`：`topic_insights.md`（Part A，话题域+态度+金句共鸣库）、`persona_profile.md`
    （Part B，L1 硬性规则/L2 身份/L3 表达风格/L4 决策/L5 互动）、`insights.json`；预留 `persona/versions/`（增量进化为后续阶段）。
  - 两个向量库（multilingual-e5，供运行时语义召回）：`radio_insights`（金句 336 条）；
    `radio_mail`（**真实「来信→主持人反应」范例 184 条**，无需 LLM，直接取 summary 的 `listener_mail`/`member_reactions`，
    `mail_exemplars.json` 留底）。
- **PersonaAgent 来信回复** `agents/persona_agent.py`：L1 红线护栏（自伤/违法/人身攻击→严肃引导并给求助热线）→
  按语义召回若干**往期来信范例**（e5，L2 阈值 1.6）作 few-shot「读信→回信」风格示范 + 召回最贴切的一条金句（阈值 1.3）→
  以五层人设口吻像节目里读信那样回信，金句**原文+【出处:期数+时间戳】**嵌入。生成式回复，绝不为未给出的句子编造出处。
  **双语输出**：一次 `complete_json` 同时产出 `{zh, ja}` 同一封回信（zh 全程中文+金句中译，ja 全程日文+金句日文原文，两版【出处】一致）。
  **强制单语**：zh 正文不得混入任何日语（连招牌开场「こもりすのみなさんこんばんは」也译成「各位小木漏，晚上好」），仅【出处】标记里含假名的节目名豁免；
  运行时 `_ensure_chinese` 兜底——检测到 zh 正文有假名（排除【出处】）就一次性改写为纯中文，保留出处标记。
- **界面模式开关 + 路由**：chat.html 顶部「问答模式 / 来信模式」切换；`AskReq` 加 `mode`（qa|mail），
  `ask_in_conversation` 在 mail 模式走 `PersonaAgent.reply_mail`（meta `kind=mail`，含 `answer_ja`；来源 `往期来信参考`/`往期金句` 标签 + 🔍 悬停出处）。
  来信回复卡片内置 **中文/日本語 切换按钮**（默认中文，可切全日文，`setMailLang`）。事实问答 `_run_qa`、eval、入库路径不受影响。

### Verified
- 蒸馏产物：`persona/` 三件产物 + `radio_insights`(336) + `radio_mail`(184) 生成；金句出处经逐字稿核对准确（第1期 00:05:13）。
- 来信：浏览器实测「高三学生焦虑」「职场新人迷茫/换工作」均召回对口往期来信范例，回复以「读信→共情→分享自身经历→引金句→鼓励」展开，
  角色显示「羊宮妃那 · 来信回复」，引金句带 🔍 悬停出处；来源面板渲染 3×往期来信参考 + 1×往期金句。
  双语：卡片默认中文，点「日本語」即切到全日文版（金句变日文原文，出处不变）。红线测试句触发严肃引导。
- 隔离：切回问答模式事实题 `kind=qa/two_stage` 28 条来源，行为未回归。

## [Phase 12] 会话落盘 + 自进化记忆 + 统计/共指修复 — 2026-05-23

三件事：①修统计问答偶发「无法解析」；②修嘉宾期 ひなたん 被误指为 青木陽菜；③会话历史落盘 SQLite + 用户自然语言实时订正知识库（以用户为准）。

### Fixed
- **StatsAgent 不再硬失败**：映射 LLM 偶发返回空/非法 JSON 时，原会答「无法解析该统计问题」。
  现 `_map()` 重试一次，再退回确定性关键词启发式 `_heuristic_plan`（来信+最多→top_subjects(投稿)、
  +多少→count_type(Listener)、+逐期→per_episode、+列出→list_type）。「来信最多的人是谁？」稳定答 にららさん(5次)。
- **ひなたん 共指修复**：第30期嘉宾 桜谷理子 称主持人为「ひなたん」，原被拆成独立 Person，且与真人名
  青木陽菜（陽菜读 ひな）音近易混。`canonical.py` 把 ひなたん/ヒナたん/ひなたんさん 归一到 羊宮妃那（影响后续入库）；
  `repair_graph.py` 增 ひなたん→羊宮妃那 重定向并已对存量图执行（旧节点删除、边并入 羊宮妃那）。

### Added
- **会话历史落盘** `server/conv_store.py`：SQLite（`data/conversations.sqlite`，可配 `CONVERSATIONS_DB`）。
  表 conversations / messages（meta 存 sources/anchors/kb 负载）。服务重启后历史仍在；端点行为不变。
- **自进化记忆 / 自然语言订正知识库** `agents/memory_agent.py`（用户为准，先预览后写）：
  - 显式触发：消息以「记住：/订正：/更正：/改为：…」等前缀开头才进入编辑（不自动分类，避免误改图谱）。
  - LLM 把陈述解析为 add/update/delete 三元组操作（经规范名归一），先在聊天里弹「待确认变更」卡片。
  - 确认后经既有安全 GraphStore 工具写入，附 `source_type=user`、`citation=用户订正 @ 日期`；
    冲突时按 CDC **保留历史线**：旧活动边 expire、用户新边生效。「订正/更正/改为」类触发词使 add 自动转 update（替换语义）。
  - 端点 `POST /api/conversations/{id}/kb_confirm`；聊天前端渲染确认/取消卡片。

### Verified
- 统计：「来信最多的人是谁？」连测稳定返回 にららさん(5次)；启发式单测全绿。
- 共指：`canonical_name('ひなたん')→羊宮妃那`；存量图 ひなたん 节点已删、边并入 羊宮妃那(にらら ×4)。
- 会话落盘：建会话→问答→重新 GET 历史仍在；标题自动取首问。
- 知识订正端到端：「订正：羊宮妃那 所属 X」→预览→确认→图谱写入 `source_type=user` 边；
  连续两次「订正：ZZ测试人物 所属 A/B」→ B 生效、A expire（历史线保留，用户最新为准）。测试实体已清理。

## [Phase 11] 多轮对话 + Claude 风格网页 — 2026-05-23

把单轮问答升级为「每个会话独立记忆」的多轮对话，并把前端改为类 Claude 网页版：左侧功能区（上会话列表 + 下可折叠后台文档管理），右侧聊天。

### Added
- **对话存储（内存）** `server/app.py`：进程内 `CONVERSATIONS` 字典（重启即丢，按用户选择不落盘）。
  端点 `GET/POST /api/conversations`、`GET/DELETE /api/conversations/{id}`、
  `POST /api/conversations/{id}/ask`（多轮问答，自动用首条问题生成标题）。
- **指代改写** `QAAgent.contextualize(history, question)`：用最近会话历史把追问（「她」「那个」等）
  改写成可独立检索的问题；新话题则原样返回。检索走改写后的独立问题。
- **历史入答案** `QAAgent.answer(question, context, history=None)` + `qa_graph2` 透传 `history`：
  生成时把近几轮对话作为连续性参考（事实仍只取检索上下文）。
- **共享 QA 入口** `_run_qa(question, history)`：统一 StatsAgent 统计路由与两段式检索；`/api/ask` 与对话端点复用。
- **类 Claude 前端** `server/static/chat.html`（原生 HTML/JS）：左栏会话列表（新建/选择/删除）+
  底部可折叠「后台文档管理」（期数入库 + 待审批卡片，复用原看板逻辑）；右栏聊天气泡 + 🔍 出处悬停卡 + 融合来源折叠。
  `/` 改为聊天页，原审批看板移到 `/dashboard`；旧 `/ask` 单轮页保留。

### Verified
- 语法编译通过；`/` 返回聊天页、`/dashboard` 返回看板；对话 CRUD（create/get/delete/list）端到端通过。
- `QAAgent._format_history` / 无历史时 `contextualize`、`answer` 兜底分支单测通过。
- 多轮端到端通过（DeepSeek `deepseek-v4-flash`）：Q1「羊宮妃那はどの事務所？」→ 青二プロダクション（带出处）；
  追问「那她主持的节目叫什么名字？」中的「她」由会话记忆正确归到羊宮妃那，答《こもれびじかん》并溯源第1期；
  历史持久 4 条、标题自动取首问。
- 注：本次把 `.env` 的 `LLM_MODEL` 从误填的 API key 改为 `deepseek-v4-flash`（旧默认 `deepseek-chat` 已被端点拒绝）。

## [Phase 10] Docker Compose 组件容器化 — 2026-05-23

> **备注（2026-05-23 更新）：暂不启用本阶段的 Docker Compose 容器化。** 默认运行方式回到
> 本地 `.venv` + Homebrew Neo4j。原因：现有 28 期全量图谱在本地 Homebrew Neo4j 中，而 compose
> 用独立命名卷，启动即空库；且容器内需运行时 `uvx` 拉 MCP server、重下 e5-base 模型，对单机单用户
> 项目收益有限。相关文件（`Dockerfile`/`docker-compose.yml`/`scripts/docker-*.sh`）保留作为未来部署参考，
> 待补齐 Neo4j 数据迁移后再启用。

新增 Docker 化运行入口，用一条命令启动/停止 Neo4j 与 Web/API 服务。

### Added
- `Dockerfile`：构建 FastAPI 应用容器，安装 Python 依赖与 `uv/uvx`，用于运行 MCP 子进程。
- `docker-compose.yml`：编排 `neo4j`、一次性 `neo4j-init` 约束初始化任务、`app` 服务；挂载 `./data`、`../hina_radio`、HF/uv 缓存卷。
- `scripts/docker-up.sh` / `scripts/docker-down.sh`：一键启动/停止全部 Compose 组件。
- `.dockerignore`：排除 `.env`、`.venv`、Chroma 数据、SQLite checkpoint 等本地大文件/私密文件。

### Run
- 启动：`./scripts/docker-up.sh`
- 停止：`./scripts/docker-down.sh`
- 服务：问答页 `http://127.0.0.1:8000/ask`，Neo4j Browser `http://127.0.0.1:7474`。

## [Phase 9] 完整回答优先 + 来信上下文扩容 — 2026-05-23

针对“问来信人/信件时只回答一条”的问题，把问答链路从省 token 的简短回答改为完整性优先。

### Changed
- **答案生成完整性优先**：移除“简洁回答”倾向，要求多问逐项覆盖；来信者/お便り/邮件/投稿/推荐内容类问题必须列出上下文内能确认的全部相关项目。
- **回答 token 预算放大**：新增 `QA_ANSWER_MAX_TOKENS=8192`，并把 `QA_TOP_N / QA_VECTOR_K / QA_SUMMARY_K / QA_DIRECT_K / QA_FALLBACK_K` 做成配置。
- **默认问答页改走两段式**：`POST /api/ask` 的非统计问题改用 summary route + 原文窗口回查，避免页面仍走单段检索而拿不到结构化来信字段。
- **结构化摘要入上下文**：两段式不再只把 summary 当路由线索，也会把命中的 summary 文本作为 `结构化摘要` passage 交给生成模型；来信字段（来信人、来信内容、主持反应、备注）因此可直接用于回答。
- **来信类结构化补全**：当问题带期数或 `こもればなし/アーカイブ` 过滤词并询问来信/信件/投稿时，会补齐该范围内所有含 `来信：` 的 summary section。
- **期数/类型硬过滤**：两段式识别 `第22期`、`#22`、`こもればなし`、`アーカイブ`，避免跨期或不同节目类型混入。
- **StatsAgent 列表不截断**：`list_type` 不再只显示前 50 个来信者，改为输出可查询到的完整列表。

### Verified
- 语法编译通过；「第22期こもればなし有哪些来信人和信件？」可返回 5 条结构化摘要 + 对应原文窗口，且不混入其他期。

## [Phase 8] MiMo API 问答处理 + 中文优先召回 — 2026-05-23

针对中文提问时只把问题改写成日文、导致「电影 / 综艺 / 最近看什么」等泛话题召回不全的问题，升级问答检索链路。

### Added
- **MiMo provider**：`LLM_PROVIDER=mimo` 接入小米 MiMo-V2.5-Pro OpenAI-compatible API（`MIMO_API_KEY` / `MIMO_BASE_URL`）。
- **中日双语检索文本**：`Chunk.retrieval_text` 写入 Chroma，包含同一时间窗口的 `JA/ZH` 对照；抽取、标注与图谱审核仍使用日文 `Chunk.text`，避免中文翻译污染三元组。
- **多查询召回**：`QAAgent.analyze()` 产出中文总结检索词、日文原文检索词与模糊相关词；`VectorRetriever` 与两段式 `SummaryRetriever` 会并行多查询召回并去重。
- **中文优先 summary 路由**：两段式 Stage1 先用中文原问/中文关键词打 `radio_summaries`，再并入日文原文词，避免直译词不在原文时漏召回；summary 建库也纳入 `intro / listener_mail / member_reactions / notes` 等中文字段。

### Changed
- 回答生成要求多问逐项覆盖，并把回答 token 上限从 1024 提升到 2048；服务端两段式融合上下文从 8 条提升到 12 条。
- 前端与 CLI 展示扩展后的多路检索式，方便排查召回方向。
- 检索库构建范围从仅 `*アーカイブ*` 扩展到所有带期号且含 `segments/summary` 的广播文件夹；向量 ID 与窗口回查增加 `episode_label`，避免 `#16 アーカイブ` 与 `#16 こもればなし` 串线。

### Verified
- 已重建 chunk DB：56 个文件夹 / 704 个双语 chunk；summary DB：333 个 section。
- 中文问题「她最近看什么综艺？」可命中 `#16 こもればなし` 中「テレビからバラエティ番組が聞こえてくる」片段；「她看过什么电影？」可命中 `#22 こもればなし` 漫威/复仇者联盟电影讨论。

### Run
- `.env` 设 `LLM_PROVIDER=mimo`、`MIMO_API_KEY=<your-key>`。
- 重建检索库：`.venv/bin/python -m src.rebuild_vectors`；`.venv/bin/python -m src.build_summary_db`。

## [Phase 7] StatsAgent 统计工具 + 青木 ASR 修复 — 2026-05-23

实际测试反馈两问题：所属事务所答成「青木プロダクション」；统计类问题（来信者数量等）检索无法回答。

### Fixed
- **青木 ASR 变体**：原始转写中「青二」出现 0 次，全被听成「青鬼(8)/青木(7)」。`transcript_normalizer` 增 `青木プロナクション/青木プロダクション/青木プロ→青二プロダクション`（长形优先，**保留真人名「青木陽菜」**）；InspectorAgent confusable 增青木形式+读音あおき。重建向量库后对话切片「青木プロ」命中归零、青二保留、青木陽菜完好，问答正确答青二。

### Added
- **来信者节点** `build_listener_db.py`：从各期 `05_summary.json` 的 `listener_mail_from` 确定性建 `:Entity{type:Listener}` + `投稿` 关系（拆分逗号多名）。结果：153 位去重来信者 / 193 条投稿边。
- **graph_store 只读聚合工具**：`count_by_type / count_relation / type_distribution / top_subjects_by_relation / relation_per_episode / list_by_type`（均参数化模板，守安全边界）。
- **StatsAgent** `agents/stats_agent.py`：作为工具 Agent 解决检索无法处理的统计聚合问题。LLM 把统计问题映射到固定菜单的安全聚合工具并执行；**答案数字来自查询结果而非 LLM，杜绝数字幻觉**。含 `is_stats()` 问题分类器。
- **自动路由**：`/api/ask` 与 `ask.py` 先分类——统计聚合类走 StatsAgent，其余走（两段式/单段式）检索问答。

### Verified
- 「一共有多少位来信者？」→ 153；「哪位来信最多？」→ にららさん(5次) 等排名；「来信逐期分布」→ 各期条目数；普通事实问题仍正确路由到检索（青二プロダクション）。

## [Phase 6] 二级检索（摘要路由 → 原文窗口回查）— 2026-05-23

实现粗到细的 hierarchical / parent-document 检索，缓解 Lost-in-the-Middle 与 Token 爆炸：先用密集摘要做低成本路由拿到 [期数+时间区间] 线索，再按线索精确回查原文窗口 + 实体子图，仅把浓缩上下文喂给 LLM。

### Added
- **摘要向量库** `build_summary_db.py`：从各期 `05_summary.json` 的 section（含 time_range）建独立 collection `radio_summaries`（178 段），元数据带 episode/start_sec/end_sec/citation；无需 LLM。
- **二级检索器** `retrieval/two_stage.py`：Stage1 `SummaryRetriever` 摘要路由（线索取自元数据，无额外 LLM 调用）；Stage2 `VectorStore.get_window()` 按 [期数+窗口] 元数据精确回查对话切片 + 实体锚点子图；RRF 融合。
- **两段式 QA 图** `graph/qa_graph2.py`：analyze→retrieve2→generate；`ask.py --two-stage`；server `POST /api/ask2`。
- `eval_qa.py` 增 `--mode {single,two_stage}` 与平均上下文规模（token 代理）对比。

### Design / 隐患规避（经测试确认）
- **摘要召回天花板**：摘要误路由（自信地命中错误 section）会丢长尾——加**始终在线的小路直检安全网**并入融合；摘要最高相似度过弱则整体回退直检。
- **图谱可答问题不退化**：实体锚点图谱分支始终并行。
- **第一阶段无额外 LLM**：线索取自摘要元数据，守住延迟预算。
- **去重统一**：窗口分支与直检分支统一按 `chunk:{chroma_id}` 去重（`get_window` 返回 id）。

### Verified（28 期，DeepSeek，6 题）
- 单段(top_n14): Faithfulness 1.0 / Relevance 1.0 / Grounding 6/9=0.667 / 上下文 **6284 字/问**。
- 两段(top_n8): Faithfulness 1.0 / Grounding **6/6=1.000** / 上下文 **3336 字/问（↓约47%）**；Relevance 逐轮在 0.667–1.0 波动（LLM 非确定性，非两段式特有）。
- 抽查：事务所/赞助商/命名理由/标题候选/弃权(生日) 流程与溯源均正确；摘要误路由场景由安全网纠正（命名理由仍正确引用第1期 00:01:38-00:03:25）。

### Run
- 先建摘要库：`.venv/bin/python -m src.build_summary_db`
- CLI：`.venv/bin/python -m src.ask --two-stage "<问题>"`；服务 `POST /api/ask2`。

## [Phase 5] 上下文感知共指消解（Session Constants + 前置标注）— 2026-05-23

解决「羊宮妃那 / 羊宮 / 私」被拆成多个节点的问题。核心：不让抽取盲猜，而在 DocAgent 阶段前置「话者标注」，按广播结构把对话/念信用标签包裹，使代词归属由标签**确定**。

### Added
- **规范实体词典** `canonical.py`：HOST=羊宮妃那 + 昵称别名 + 代词表；`canonical_name()` 昵称归一、`is_pronoun()` 判定。
- **前置标注 Agent** `agents/annotator_agent.py`：轻量 LLM + 结构线索（ラジオネーム/お便り 等），把 chunk 切分为 `<Host_Section>` / `<Guest_Section name>` / `<Listener_Section name>`，并抽取来信者名。
- **Session Constants**：`PipelineState` 增 host/guest/listeners（来信者逐个累加，便于日后统计来信者）；`Chunk.annotated_text`。
- 入库状态机增 `annotate` 节点：`parse→annotate→index→extract→inspect→sync`（index 用原始文本，extract 用标注文本）。

### Changed
- **ExtractorAgent 按标签确定代词归属**：Host 区间一人称→主持人，Guest→嘉宾，Listener→该来信者（不明则破棄）；昵称确定性归一为羊宮妃那；裸代词丢弃。

### Verified
- 第1期念信「かんべんさん：僕は4月から大学生活」「もくらむ：私事ですが新社会人」未被误并到羊宮，主持人自述「私」正确归羊宮妃那。
- 清库重抽 28 期后：羊宮/私/僕/ひな 等不再独立成节点，全部并入 **羊宮妃那（单节点，度数 933）**；全图 876 实体 / 1204 关系。

## [Phase 4] 问答前端溯源卡片 + RAG 质量评测 — 2026-05-23

实现 PRD 4.2 的溯源 UX（答案内事实的悬停出处卡片），并按忠实度/相关性/出处命中率三项指标建立可复跑评测。

### Added
- **问答前端** `server/static/ask.html`（原生 HTML/JS）：搜索框→答案；把答案中的 `【出处:期数+时间戳】` 解析为 🔍 悬停徽标，hover 弹气泡卡显示 [节目/期数] + [时间戳区间]；可展开「融合来源」列表（图谱事实 / 对话片段 + 出处）。
- **问答后端** `server/app.py` 新增 `POST /api/ask`（复用常驻 GraphStore/VectorStore/LLM 跑 QA 图）与 `GET /ask` 页面路由。
- **RAG 质量评测** `eval_qa.py`：一组中日测试问题（事实/跨期/情绪/无答案弃权），经服务 `/api/ask` 取答案，LLM-as-judge 评 Faithfulness 与 Answer Relevance；Source Grounding 解析答案 `【出处:期数+时间戳】`，回到该期 `segments.json` 取 `[start-30s, end+30s]` 实际转写校验该结论是否被支撑且时间戳误差 ≤±30s。

### Fixed
- **弃权不再带出处**：`qa_agent.answer` 在答案含弃权措辞（確認できません等）时剥除 `【出处】`，避免「无法确认」却附引用的逻辑矛盾。
- **评测归因更精确**：grounding 改为按「引用前一句」作为该引用的主张（而非整段答案），多引用答案归因公平；并对转写窗口套用 `normalize_transcript_text`，使（已纠偏的）答案与（已纠偏的）转写苹果对苹果比较。

### Verified（全 28 期图谱，judge=DeepSeek，因 LLM 非确定性逐轮小幅波动）
- **Faithfulness = 1.000**（三轮稳定满分，无公网知识幻觉，无依据时正确弃权）。
- **Answer Relevance ≈ 0.83**（主要被合理弃权题，如"生日"数据中确无，拉低）。
- **Source Grounding Rate：0.455 → 0.80–0.857**（修复后），达标引用时间戳误差多为 0s（≤±30s 口径）。
- 例：赞助商问题 4/4 引用全部命中（第3/8/27/16期，误差 0s）；命名理由、标题候选、所属事务所均正确溯源到第1期对应时间戳。

### Run
- 问答页：起服务后浏览器开 `http://127.0.0.1:8000/ask`。
- 评测：`.venv/bin/python -m src.eval_qa --server http://127.0.0.1:8000`（需先起服务）。

## [Phase 3] 人机协同审批看板 (PRD 4.3) — 2026-05-23

实现 PRD 4.3 前端 Dashboard：常驻服务持有 LangGraph + MCP 存储，使冲突/审核 `interrupt()` 与 `Command(resume=...)` 在同进程内闭环。

### Added
- **审批后端** `server/app.py`（FastAPI）：启动时构建单例入库状态机（`auto_policy=None`，所有 interrupt 浮现）+ 常驻 GraphStore/VectorStore/Checkpointer。
  - `GET /api/episodes`：列出 28 期及是否已入库 + 图谱 stats。
  - `POST /api/ingest {episode|dir}`：触发入库，返回 `completed` 或 `interrupted`（含 conflicts / inspection_issues）。
  - `GET /api/pending`：列出待审批线程。
  - `POST /api/resume {thread_id, decisions}`：按决定恢复状态机，可继续浮现后续 interrupt。
- **看板前端** `server/static/index.html`（原生 HTML/JS，无需 Node）：期数侧栏 + 待处理变更卡片；冲突卡显示「已有 vs 新提取」对比 + `确认变更(保留历史线)/覆盖/忽略`；审核卡显示「原始抽取 vs 建议纠偏」+ 双语理由 + `采用纠偏/保留原文/忽略`；提交即恢复状态机。
- `graph_store.ingested_episodes()`：只读查询已入库期数，供看板状态展示。

### Fixed
- 看板每次入库使用**唯一 thread_id**（label+uuid）。此前复用 thread_id 会命中批量入库残留的完成检查点，叠加 `chunks` 累加 reducer 导致 chunk 重复、Chroma upsert `DuplicateIDError`。

### Verified
- 启动后 `GET /api/episodes`：28 期全部 ingested，stats 957 实体 / 1146 关系。
- `POST /api/ingest #2` → `interrupted` + 3 条审核高风险项（如「聖書族」疑似 ASR 误识但无明确修正目标，转人工）。
- `POST /api/resume`（keep_original×3）→ `completed`，写入边，`/api/pending` 清空。整条 interrupt→resume HTTP 闭环通过。

### Run
- `.venv/bin/python -m uvicorn src.server.app:app --port 8000`，浏览器开 `http://127.0.0.1:8000`。

## [Phase 2.3] InspectorAgent 升级为 LLM 三层防御审核 — 2026-05-23

将入库前审核从纯规则（difflib + 词典硬匹配）升级为「资深日语广播/ACGN 数据校对专家」LLM 审核，覆盖规则难以穷举的同音/近音幻觉。

### Changed
- **InspectorAgent 重写为纯 LLM 批量审核** `agents/inspector_agent.py`：按 chunk/整期批量送审（一次 LLM 调用审多条三元组），三层防御逻辑：
  1. 行业词典比对（`local_domain_dictionary` 喂入声优事务所专名、别名、读音、ASR 混淆形）；
  2. 日语读音相似度（假名/罗马字注音对比，识别长音/元音混淆，如 あおに vs あおおに）；
  3. 历史图谱拟合（`historical_graph_context` 用 `relationship_object_counts` 提供实体历史稳定关联）。
- 输出严格 JSON `audit_results`，每条判级 `APPROVED / AUTO_CORRECTED / HIGH_RISK_INTERRUPT`，含 `reason_ja` / `reason_zh` 双语理由。
- 判级映射回既有 `InspectionResult / InspectionIssue`：`APPROVED` 放行、`AUTO_CORRECTED` 采用修正三元组、`HIGH_RISK_INTERRUPT` 触发 `inspection_issues` 人工复核 interrupt（CLI 三选一不变）。
- `ingestion_graph.inspect_node` 改为调用 `inspect_batch`；`ingest.py` 向 `InspectorAgent` 注入 `LLMClient`。
- 对齐保护：LLM 返回的 `audit_results` 与输入按序对齐，数量/形状不符时默认 APPROVED，避免漏审导致丢数据。

### Verified
- 合成三元组「羊宮妃那 -[所属する]-> 青鬼プロダクション」→ AUTO_CORRECTED → 青二プロダクション，理由同时引用词典读音(あおに長音差)与历史图谱证据（三层防御均生效）。
- 正常三元组「羊宮妃那 -[主持する]-> こもれびじかん」→ APPROVED。
- 入库状态机节点链确认为 `parse→index→extract→inspect→sync`。

### Notes
- 规则版的 difflib/Jaro 阈值匹配与 `DomainTerm` confusable 字段被 LLM 审核取代；词典内容保留并作为 LLM 的 `local_domain_dictionary` 上下文。
- 仍建议结合 `transcript_normalizer` 处理裸词「青鬼」(如「青鬼の事務所」)；当前 normalizer 只匹配全称以避免误伤游戏名「青鬼」。

## [Phase 2.2] e5-base 日文/跨语义向量检索 — 2026-05-23

针对 Chroma 默认 MiniLM 对日文广播片段召回偏弱的问题，将本地向量模型切换为更适合检索的 multilingual E5。

### Added
- **本地 E5 embedding 层** `embeddings/e5.py`：默认 `intfloat/multilingual-e5-base`，统一使用 `query:` / `passage:` 前缀并归一化。
- **e5-base Chroma collection**：默认 collection 名按模型生成，例如 `radio_chunks_intfloat_multilingual_e5_base`，避免污染旧 `radio_chunks`。
- **向量库重建 CLI** `rebuild_vectors.py`：只基于 DocAgent 分块重建向量库，不跑 LLM 抽取，也不写 Neo4j。
- **转写文本专名规范化** `transcript_normalizer.py`：在 DocAgent 分块前修正已知 ASR 专名错误，避免错误文本进入向量库。
- **存量图谱修复 CLI** `repair_graph.py`：将 InspectorAgent 上线前写入的已知 ASR 脏实体重定向到标准实体，并保留关系边溯源属性。

### Changed
- `VectorStore` 支持两条路径：`VECTOR_EMBEDDING_MODEL=default` 走旧 Chroma MCP 内置 embedding；其他模型走本地 Chroma persistent client + 自算 embeddings。
- 默认配置改为 `VECTOR_EMBEDDING_MODEL=intfloat/multilingual-e5-base`，适配 16GB M2 的质量/速度平衡。
- Q&A 分析步骤增加 `search_query`，将中文问题改写为更贴近日文转写文本的检索词，避免单纯加大模型尺寸解决查询表达不匹配。
- 向量分支增加少量关键词召回兜底，图谱分支按问题关系词做轻量排序，提升“所属事务所”等硬事实问题的三元组前排命中。
- Q&A 上下文显式输出 `SOURCE`，并将模型偶发输出的上下文编号引用自动展开为真实期数与时间戳出处。

### Verified
- 用 e5-base 重建 28 个 archive 文件夹，共 487 个 chunks；collection `radio_chunks_intfloat_multilingual_e5_base` count=487。
- 向量库中 `青鬼プロダクション` 命中数为 0，`青二プロダクション` 命中数为 5。
- Neo4j 中 `青鬼プロダクション` 已清空，羊宮妃那的所属关系指向 `青二プロダクション` 并保留第 1 期 00:03:25-00:05:00 出处。
- 问答「羊宮妃那はどの事務所に所属している？」图谱前排召回 `羊宮妃那 —[所属する]→ 青二プロダクション`，回答为 `青二プロダクション`。
- 问答「节目名最终为什么叫こもれびじかん？」召回第 1 期 00:01:38-00:03:25，并回答“小动物躺在树下、沐浴光线安睡”的命名理由。

## [Phase 2.1] InspectorAgent 入库前审核纠偏 — 2026-05-22

针对 ASR 同音误抓导致的脏实体写入问题，新增 ExtractorAgent 与 SyncAgent 之间的审核层。

### Added
- **InspectorAgent** `agents/inspector_agent.py`：行业词典 + 近似字符串/读音混淆 + 历史图谱频次校验，首批覆盖声优事务所专名。
- **典型纠偏**：`青鬼プロダクション` 会在写入前被识别为 `青二プロダクション` 的 ASR 混淆，并自动修正。
- **审核 interrupt**：低置信高风险事实通过 `inspection_issues` 挂起，CLI 可选择采用纠偏 / 保留原文 / 忽略。
- **图谱历史查询** `relationship_object_counts()`：供审核 Agent 检查主体过去稳定连接到的对象，辅助判断孤立噪声。

### Changed
- 入库状态机由 `parse→index→extract→sync` 调整为 `parse→index→extract→inspect→sync`。
- `PipelineState` 增加 `inspected_triples` 与 `inspection_issues`，保留原始抽取结果以便审计。

## [Phase 2] GraphRAG 混合检索 + 问答 Agent — 2026-05-22

实现 PRD 4.1/3.4：向量 + 图谱双路检索，融合重排后由 LLM 生成带 `[出处:期数+时间戳]` 的溯源答案。

### Added
- **图谱多跳检索** `graph_store.neighbors(eids, hops)`：只读、bounded 变长路径（hops 代码控制字面量，注入安全），返回带 citation 的三元组。
- **双路检索器** `retrieval/retrievers.py`：`VectorRetriever`（Chroma 语义召回切片）+ `GraphRetriever`（问题实体锚点 → 2 跳拓扑检索），统一 `Passage` 携带溯源。
- **融合层** `retrieval/fusion.py`：Reciprocal Rank Fusion (RRF) 融合两路结果并重排，产出带溯源标签的上下文；预留 cross-encoder 接口。
- **Q&A Agent** `agents/qa_agent.py`：`analyze`（抽问题锚点/意图）+ `answer`（仅据上下文作答、句末附【出处】、按提问语言回答）。
- **Q&A 状态机** `graph/qa_graph.py`：LangGraph 并行分支 analyze→(graph ∥ vector)→fuse→generate。
- **CLI** `ask.py`：`python -m src.ask "问题" [--hops N] [--show-context]`。

### Changed
- `llm/client.py`：`_complete_text` 增加 `json_mode` 参数，仅结构化抽取启用 `json_object`；修复自由文本生成（问答）误用 JSON 模式导致 DeepSeek 400 的问题。

### Verified
- 中文问答「候选名字与选名理由」→ 列全 4 个候选 + 选名缘由，引用第1期 00:01:38-00:03:25。
- 混合检索：图谱 ~25 条 + 向量 6 条 → RRF 融合 10 条。

### Notes
- 初版问答曾暴露 `青二プロダクション` 被 ASR/抽取链路误写为 `青鬼プロダクション` 的数据污染问题；已在 Phase 2.1 通过 InspectorAgent 入库前审核纠偏处理。
- 当前仅入库第 1 期，跨期数多跳推理待批量入库后更显威力。

## [Phase 1] 入库管线 — 2026-05-22

首个可运行主线：把广播带时间戳转写经多 Agent 协同抽成时序知识图谱 + 向量库，全程经真实 MCP server 读写。

### Added
- **基础设施**：Homebrew 本地 Neo4j（`neo4j/your_neo4j_password`）；`Entity.eid` 唯一性约束。
- **MCP 统一数据访问层**：官方 `mcp-neo4j-cypher` 与 `chroma-mcp` server，经 `mcp` Python SDK 的 stdio 长连接调用；`McpStdioClient` 提供异步→同步桥接。
- **图谱语义层** `graph_store.py`：仅暴露受限安全工具（`search_nodes` / `merge_node` / `merge_directed_relationship` / `expire_relationship` / `delete_relationship`），LLM 永不接触 Cypher，关系统一 `:REL` 通用类型防注入。
- **向量语义层** `vector_store.py`：Chroma 本地持久化 + 内置 MiniLM 嵌入，切片携带溯源元数据。
- **可切换 LLM 层** `llm/client.py`：anthropic / openai / deepseek 统一 JSON 输出接口。
- **DocAgent**：文件夹名解析元数据（期数/放送日/类型/嘉宾）+ 时间窗口分块；外部材料降级页/段。
- **ExtractorAgent**：LLM 抽三元组 + `search_nodes` 实体消歧 + 曖昧代词丢弃。
- **SyncAgent**：CDC 增量更新；单值关系对象变更触发冲突；`confirm` 保留历史线 / `overwrite` 覆盖 / `ignore` 忽略。
- **LangGraph 入库状态机** `ingestion_graph.py`：parse→index→extract→inspect→sync，含 `interrupt()` 人机协同 + SqliteSaver Checkpointer 恢复。
- **CLI** `ingest.py`：`--episode N` / `--all` / `--dir` / `--auto`。

### Verified
- 第 1 期端到端入库：52 实体 / 44 关系边 / 17 向量切片，0 丢弃，全部带 `期数+时间戳` 溯源。
- CDC 冲突路径：单值关系 张三(ep5→12) → 李四(ep12→今) 历史线正确。

### Known gaps (待后续阶段调优)
- 实体消歧未跨 chunk 归并第一人称/简称（私 / 羊宮 / 羊宮妃那 成独立节点）。
- Chroma 默认 MiniLM 嵌入对日文偏弱。
