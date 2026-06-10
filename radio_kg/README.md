# radio_kg — 广播时序知识图谱入库管线（第一阶段）

把《羊宮妃那のこもれびじかん》等广播的带时间戳转写，经多 Agent 协同抽成
**时序知识图谱（Neo4j）+ 语义向量库（Chroma）**，全程经 **MCP 协议**读写存储。
对应 [PRD](../PRD.md) 的第一阶段：入库管线（DocAgent → ExtractorAgent → InspectorAgent → SyncAgent）。

## 架构

```
ingest.py (CLI)
   └─ LangGraph 状态机:  parse → index(向量) → extract → inspect → sync(CDC+冲突)
         │ DocAgent        ExtractorAgent       InspectorAgent       SyncAgent
         ▼
   存储访问层
         ├─ mcp-neo4j-cypher  → Neo4j   (图谱: 实体 + 时序关系边)
         └─ Chroma persistent → Chroma  (向量: 对话切片 + 溯源元数据)
```

- **安全边界 (PRD 7.3)**：LLM 绝不生成 Cypher。只有 `src/mcp_layer/graph_store.py`
  里的固定参数化模板构造 Cypher，变量一律走 `$params`；关系统一用 `:REL` 通用类型
  （语义标签作属性），杜绝关系名注入。
- **时序溯源 (PRD 4.2)**：每条关系边带 `episode / start_time / end_time / citation`；
  无时间戳的外部材料降级为 `文件名 + 页/段`。
- **审核纠偏 (PRD 3.3/5)**：InspectorAgent 在写入前校验 ASR 近音误抓、行业专名误写
  与历史图谱孤立噪声，例如自动修正 `青鬼プロダクション` → `青二プロダクション`。
- **向量检索模型**：默认使用 `intfloat/multilingual-e5-base` 本地 embedding；
  查询加 `query:` 前缀、文档加 `passage:` 前缀。旧版 `VECTOR_EMBEDDING_MODEL=default`
  仍可走 Chroma MCP 内置 MiniLM collection。问答阶段可用 `LLM_PROVIDER=mimo`
  接入小米 MiMo-V2.5-Pro API 做查询理解、跨语义扩展与答案生成。
- **中文问答召回**：chunk 向量库写入中日双语检索文本，抽取仍只读日文原文；
  中文问题会优先检索中文翻译/总结，同时并行日文原文词与模糊相关词。检索库覆盖
  所有带期号且含 segments/summary 的广播文件夹（含 `アーカイブ` 与 `こもればなし`）。
- **完整回答优先**：问答默认使用两段式检索，把结构化摘要与原文窗口一起交给模型；
  来信者/来信/投稿问题会尽量列出上下文中可搜集到的全部相关信息。可通过
  `QA_ANSWER_MAX_TOKENS`、`QA_TOP_N`、`QA_SUMMARY_K` 调整完整性预算。
- **CDC 与人机协同 (PRD 3.4/4.3)**：单值关系（担当/負責 等）对象变更触发
  `interrupt()` 挂起，由 CLI 审批 `确认(保留历史线) / 覆盖 / 忽略`，经 Checkpointer 恢复。

## 部署方式

本项目**只支持本地运行**：工作区唯一 venv（`Agent/.venv`）+ Homebrew Neo4j，由 launchd 管理进程。
Docker 部署已于 2026-06-10 移除（原 compose 配置仍挂载早已删除的 `../hina_radio` 数据源，且
容器 Neo4j 空库与本地 Homebrew 图谱割裂，对单机单用户场景无收益）。如未来需要上云，
需重写部署配置，并先解决 Neo4j 数据迁移。

本地运行时，`RADIO_DATA_DIR` 可以指向两种布局：

- `../hina_radio`：整理后的扁平数据集，形如 `../hina_radio/<episode>/05_summary.json`。
- `../Radio/data/recordings`：上游 Radio 生产端原生输出，形如 `../Radio/data/recordings/<collection>/<episode>/05_summary.json`。

`src/source_data.py` 会递归发现合法 episode 目录，因此两种布局可以无缝切换。

## 本地环境准备

```bash
# 1. Neo4j（本地，已用 Homebrew 安装）
brew services start neo4j
# 首次设密码（已设为 your_neo4j_password）：neo4j-admin dbms set-initial-password your_neo4j_password
# 唯一性约束（MCP write 工具不支持 DDL，单独建一次）：
cypher-shell -u neo4j -p your_neo4j_password \
  "CREATE CONSTRAINT entity_eid IF NOT EXISTS FOR (e:Entity) REQUIRE e.eid IS UNIQUE;"

# 2. Python 依赖（工作区唯一 venv：Agent/.venv，依赖声明在 Agent/pyproject.toml）
cd .. && uv sync && cd radio_kg

# 3. MCP server 由 uvx 按需拉起（mcp-neo4j-cypher / chroma-mcp），无需手动安装
#    Chroma 内置嵌入模型首次会下载 ~79MB 到 ~/.cache/chroma

# 4. 配置：复制 .env.example -> .env，填入至少一个 LLM key
cp .env.example .env   # 设 LLM_PROVIDER=anthropic|openai|deepseek|mimo 及对应 KEY
```

## 运行

```bash
# 入库（Phase 1）
.venv/bin/python -m src.ingest --episode 1            # 入库第 1 期
.venv/bin/python -m src.ingest --all --auto confirm   # 全部 28 期，自动按"保留历史线"解冲突
.venv/bin/python -m src.ingest --dir "<folder>"       # 指定文件夹

# 问答（Phase 2，GraphRAG 混合检索）
.venv/bin/python -m src.ask "羊宮妃那はどの事務所に所属している？"
.venv/bin/python -m src.ask --show-context "节目名最终为什么叫こもれびじかん？"

# 使用 MiMo-V2.5-Pro API 做问答/query 扩展：.env 中设置
# LLM_PROVIDER=mimo
# MIMO_API_KEY=<your-key>

# 更新检索文本或 embedding/provider 后，建议同时重建 chunk DB 与 summary DB
# 这不会跑 LLM 抽取，也不会写 Neo4j
.venv/bin/python -m src.rebuild_vectors
.venv/bin/python -m src.build_summary_db

# 修复已确认的存量 ASR 脏实体（保留关系边出处）
.venv/bin/python -m src.repair_graph

# 人机协同审批看板（Phase 3，PRD 4.3）
.venv/bin/python -m uvicorn src.server.app:app --port 8000
# 浏览器开 http://127.0.0.1:8000 ，对某期点「入库」；冲突/审核高风险项以卡片浮现，三选一后恢复状态机
# 统一工作区启动时用 ../agent-up.command；Radio 生产端控制台会作为 /radio 子页面挂进来。

# 问答页 + 溯源悬停卡片（Phase 4，PRD 4.2）：同一服务，浏览器开 http://127.0.0.1:8000/ask

# 二级检索（Phase 6，摘要路由→原文窗口回查，省 token + 抗 Lost-in-the-Middle）
.venv/bin/python -m src.build_summary_db                 # 先建摘要向量库（一次）
.venv/bin/python -m src.ask --two-stage "<问题>"          # 两段式问答；服务端 POST /api/ask2

# 统计聚合（Phase 7，StatsAgent 工具，自动路由）：先建来信者节点（一次）
.venv/bin/python -m src.build_listener_db
.venv/bin/python -m src.ask "一共有多少位来信者？"          # 自动识别统计类→StatsAgent

# RAG 质量评测（需先起服务）：Faithfulness / Answer Relevance / Source Grounding(±30s) + 上下文规模
.venv/bin/python -m src.eval_qa --server http://127.0.0.1:8000 --mode single
.venv/bin/python -m src.eval_qa --server http://127.0.0.1:8000 --mode two_stage

# 性能基准（PRD 7.1，无需起服务，直接打本地 Neo4j-MCP + Chroma）：MCP 延迟<200ms / 混合检索<2s
.venv/bin/python -m src.bench_perf      # 实测 MCP p95≈16–70ms、混合检索 mean≈0.55s，均大幅达标

# 全维度基准记分卡（PM 视角：覆盖/性能/问答/来信，四维逐项对标目标，落 data/scorecard_*.json）
.venv/bin/python -m src.scorecard           # 一次跑完全部维度（含 LLM 评测）
.venv/bin/python -m src.scorecard --quick   # 仅覆盖+性能，跳过 LLM 密集项

# 主持人「双层技能」人设 · 来信模式（Phase 13，借鉴 COLLEAGUE.SKILL）：先离线蒸馏（可重跑）
.venv/bin/python -m src.build_persona                    # 产出 persona/{topic_insights.md, persona_profile.md, insights.json, mail_exemplars.json} + Chroma 集合 radio_insights / radio_mail
# 然后起服务，浏览器开 http://127.0.0.1:8000 ，把顶部切到「来信模式」，把想说的写成一封来信投稿
# 系统把它当作お便り，按节目里真实「读信→回信」的方式回复，引用往期金句时原文输出并带【出处】悬停卡片
```

### 来信模式（Phase 13）

借鉴论文 COLLEAGUE.SKILL 的「两层可调用技能」：
- **Part A — 话题观点库**（`persona/topic_insights.md`）：核心话题分类 + 每个话题的情感态度倾向 + 往期金句共鸣库（带出处）。
- **Part B — 主持人人设画像**（`persona/persona_profile.md`）：严格五层模型 L1 硬性规则 / L2 身份认同 / L3 表达风格 / L4 决策与判断 / L5 听众互动行为。

运行时 `PersonaAgent.reply_mail` 管线：把用户发言当作一封来信 → L1 红线护栏 → 按**语义向量**召回若干「往期来信→主持人反应」范例（集合 `radio_mail`，作读信回信的风格示范）+ 最贴切的一条金句（集合 `radio_insights`）→ 以五层人设口吻像节目里读信那样回信，金句原文+出处嵌入。**回复为双语**：一次生成中文版与日文版同一封信，回复卡片内置「中文 / 日本語」切换（默认中文，可切全日文，金句随之切中译/日文原文，出处不变）。`build_persona` 可随新增剧集重跑覆盖（同时重建两个向量库）；`persona/versions/` 为后续「对话式修正 / 版本回滚」预留。

## Clipper — 数据驱动型内容二次创作（独立功能）

用 **B 站市场热度信号** 驱动素材的二次剪辑，产出适配热点的成片。与既有 ingestion/QA/录制
完全独立，只读复用现有数据/能力。技术路线：`[市场情报]→[向量检索]→[自动切片]→[字幕烧录]`。

两条分支：
- **past（过往节目）**：B 站分区热榜 → 爆款特征 → 在已有摘要向量库跨模态匹配高相关片段（带期数+时间戳）→ 切片 → 字幕 → 成片。
- **new（新上传直播）**：`yt-dlp` 下载 YouTube VTuber 直播（视频+音频）→ **自动总结入库（无审查，`auto_policy=confirm`）** → 结合 B 站热点（大火歌曲/话题）判断各章节爆火潜力 → 高潜力片段 → 切片 → 字幕 → 成片。

```bash
# Branch A：选材预览（不切片，真实拉 B 站 + 匹配，产 plan.json）
python -m src.clipper.cli past --dry-run --topk 5
# Branch A：全流程出成片（需对应期有源媒体）
python -m src.clipper.cli past --partition music,game,vtuber --topk 5
# Branch B：下载 + 爆火分析 + 全流程（出成片 + 自动入库）
python -m src.clipper.cli new --url <youtube-url> --res 720
```

输出在 `data/clips/<run>/`：`plan.json`（热点→片段→时间戳→标题/文案→理由）、切片、`.srt`、`*_final.mp4`。

依赖：`Pillow`（已在 requirements）。**WhisperX 词级对齐装在独立 venv**（避免降级主 venv 的 torch/transformers）：
```bash
uv venv .venv_whisperx --python 3.12 && uv pip install --python .venv_whisperx/bin/python whisperx
```
未装 WhisperX 时字幕自动回退到已有逐句转写（Branch A）。歌词策略由 `.env` 的
`LYRICS_MODE` 控制：`metadata` 默认只显示曲名/原唱，必要时用 LLM 根据歌唱 ASR 生成检索 query；
`netease` 调用本地/自有授权 NeteaseCloudMusicApi 服务取原文+中文翻译，
`file` 只用用户自备 `.srt/.lrc`，`placeholder` 只显示歌名占位。其余配置见 `.env`
（`BILIBILI_PARTITIONS`/`CLIP_*`/`LYRICS_*`/`WHISPERX_*`）。

## 目录

| 路径 | 职责 |
|---|---|
| `config/settings.py` | env 配置（LLM provider 切换、Neo4j、Chroma、MCP server 命令） |
| `src/llm/client.py` | 可切换 LLM（anthropic/openai/deepseek，统一 JSON 输出） |
| `src/mcp_layer/client.py` | 同步桥接 MCP 异步 SDK（长连接 stdio 会话） |
| `src/mcp_layer/graph_store.py` | 图谱语义层（仅暴露受限安全工具） |
| `src/mcp_layer/vector_store.py` | 向量语义层（Chroma + e5-base / legacy Chroma MCP） |
| `src/embeddings/e5.py` | 本地 E5 embedding（query/passage 前缀 + 归一化） |
| `src/agents/doc_agent.py` | 解析文件夹元数据 + 时间窗口分块 |
| `src/agents/annotator_agent.py` | 前置话者标注（Host/Guest/Listener 标签） |
| `src/canonical.py` | 规范实体词典（昵称归一、代词表） |
| `src/agents/transcript_normalizer.py` | 已知 ASR 专名错误规范化 |
| `src/agents/extractor_agent.py` | 三元组抽取 + 实体消歧 |
| `src/agents/inspector_agent.py` | 入库前审核纠偏（行业词典 + 读音近似 + 历史图谱频次） |
| `src/agents/sync_agent.py` | CDC 增量更新 + 冲突检测/解决 |
| `src/graph/ingestion_graph.py` | 入库 LangGraph 状态机（含审核/冲突 interrupt） |
| `src/ingest.py` | 入库 CLI 入口 |
| `src/rebuild_vectors.py` | 仅重建向量库 CLI |
| `src/repair_graph.py` | 已知 ASR 脏实体的存量图谱修复 CLI |
| `src/retrieval/retrievers.py` | 双路检索器（向量 + 图谱多跳） |
| `src/retrieval/fusion.py` | RRF 融合重排 + 上下文构建 |
| `src/agents/qa_agent.py` | 问题分析 + 带溯源答案生成 |
| `src/graph/qa_graph.py` | 问答 LangGraph 状态机（并行检索分支） |
| `src/ask.py` | 问答 CLI 入口（`--two-stage` 二级检索） |
| `src/build_summary_db.py` | 摘要向量库构建（二级检索 Stage1） |
| `src/retrieval/two_stage.py` | 二级检索器（摘要路由→窗口回查+子图+兜底） |
| `src/graph/qa_graph2.py` | 两段式 QA 状态机 |
| `src/server/app.py` | 看板+问答后端（FastAPI：pending/ingest/resume/ask） |
| `src/server/static/index.html` | 审批看板前端（原生 HTML/JS） |
| `src/server/static/ask.html` | 问答前端 + 溯源悬停卡片 |
| `src/eval_qa.py` | RAG 质量评测（忠实度/相关性/出处命中率±30s） |
| `src/clipper/bilibili_source.py` | 真实 B 站爬虫（WBI 签名 + 分区排行 + stat/tags/热评） |
| `src/clipper/trend_features.py` | 动量筛选 + LLM 爆款特征蒸馏（含大火歌曲） |
| `src/clipper/matcher.py` | Branch A：热点→向量匹配过往片段+LLM 打分/标题文案 |
| `src/clipper/youtube_source.py` | Branch B：yt-dlp 下载直播视频+音频+章节元数据 |
| `src/clipper/viral_analyzer.py` | Branch B：交叉比对热点判断片段爆火潜力 |
| `src/clipper/kb_ingest.py` | Branch B：复用 Radio 转写摘要 + auto_policy 无审查入库 |
| `src/clipper/slicer.py` / `aligner.py` / `packager.py` | ffmpeg 切片 / WhisperX 词级字幕 / 字幕硬烧成片 |
| `src/clipper/whisperx_worker.py` | WhisperX 对齐 worker（独立 `.venv_whisperx` 运行） |
| `src/clipper/pipeline.py` / `cli.py` | 两条分支编排 + 命令行入口（`past`/`new`） |

## 验证查询（Neo4j Browser / cypher-shell）

```cypher
MATCH (s)-[r:REL]->(o)
RETURN s.name, r.relation, o.name, r.episode, r.citation
ORDER BY r.episode LIMIT 25;
```
