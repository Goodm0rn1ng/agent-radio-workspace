# 项目结构

最近更新：2026-06-10

## 顶层

| 路径 | 职责 |
|---|---|
| `README.md` | 工作区入口，说明 Radio 与 radio_kg 的职责边界。 |
| `AGENTS.md` / `CLAUDE.md` | 协作与编码规则。 |
| `PRD.md` | 下游知识图谱/问答系统的产品需求。 |
| `pyproject.toml` / `uv.lock` / `.venv/` | 工作区唯一 venv（uv workspace）；Radio 与 clip 为 editable 包，radio_kg 依赖声明在根。`uv sync` 一键同步。 |
| `agent-up.command` / `agent-down.command` | launchctl 薄包装：启停全部本地服务。 |
| `scripts/` | launchd plist（`com.agent.*.plist` 权威副本）与对应 wrapper 脚本。 |
| `Radio/` | 广播生产端项目，原 Radio-Oshikatsu。 |
| `radio_kg/` | 广播数据消费端，GraphRAG/问答/人设/审批服务。默认从 `Radio/data/recordings` 入库。 |
| `clip/` | 二创端：B 站热度驱动切片、二次精听（独立 `.venv_whisperx`）、烧字幕、Telegram 点击即切。 |

进程模型：launchd 管理 `com.agent.radio-kg-server`（uvicorn 单进程，挂载 `Radio` 于 `/radio`、
clip 路由于 `/clipper`）、`com.agent.radio-scheduler-daemon`（定时录制）、`com.agent.radio-telegram-bot`
（审批/切片回调），均 RunAtLoad + KeepAlive（登录自启、崩溃自动重启）；Neo4j 由 `brew services` 同样登录自启。
生产完成后经 `RADIO_KG_AUTO_INGEST_URL` 调同服务 `/api/ingest` 自动入库。Docker 部署已于 2026-06-10 移除。

## Radio：广播生产端

| 路径 | 职责 |
|---|---|
| `Radio/src/radio/` | 录制、音频处理、STT、翻译、总结、推送等核心代码。 |
| `Radio/scripts/` | 单次音频、视频、Radiko、YouTube Live、API、daemon、bot 入口。 |
| `Radio/config/` | 节目配置、术语库、常驻环节库、prompt profiles。 |
| `Radio/frontend/` | 本地生产端控制台。 |
| `Radio/data/recordings/` | 生产端原生运行产物，布局为 `<collection>/<episode>/`。 |
| `Radio/data/logs/` | 生产端日志、metrics、pid 文件。 |
| `Radio/docs/` | 生产端架构、部署、前后端 API 与决策记录。 |

生产端的一集标准产物至少包含：

- `03_ja_segments.json`
- `04_bilingual_segments.json`
- `05_summary.json`

这些文件就是下游 `radio_kg` 的数据契约。

## radio_kg：知识消费端

| 路径 | 职责 |
|---|---|
| `radio_kg/config/settings.py` | 下游服务环境变量与路径配置。依赖装在工作区 venv（声明见根 `pyproject.toml`），radio_kg 自身非安装包、以 `cwd=radio_kg` 运行。 |
| `radio_kg/src/source_data.py` | 广播产物目录发现，兼容扁平布局和生产端嵌套布局。 |
| `radio_kg/src/agents/` | DocAgent、Extractor、Inspector、Sync、QA、Persona 等 Agent。 |
| `radio_kg/src/mcp_layer/` | Neo4j/Chroma 存储访问层。 |
| `radio_kg/src/retrieval/` | Graph/vector 混合检索与两段式检索。 |
| `radio_kg/src/server/` | FastAPI 问答与审批看板。 |
| `radio_kg/data/` | 下游 Chroma、checkpoint、pending、conversation、scorecard 等运行状态。 |
| `radio_kg/persona/` | 来信模式与人设蒸馏产物。 |

`radio_kg` 默认读取 `../Radio/data/recordings`（由 `radio_kg/.env` 的 `RADIO_DATA_DIR` 指定）：

```env
RADIO_DATA_DIR=../Radio/data/recordings
```

`src/source_data.py` 会递归发现合法 episode 目录，并同时兼容扁平布局（`<episode>/03_ja_segments.json`）
与生产端嵌套布局（`<collection>/<episode>/`），因此 `RADIO_DATA_DIR` 可无缝切换到任意符合数据契约的目录。

`radio_kg` 主服务同时提供 `/radio` 子页面，直接挂载 `Radio` 生产端控制台与它的 `/radio/api/*`
接口。浏览器只需要记住 `http://127.0.0.1:8000/radio`。

## 数据目录策略

- `Radio/data/recordings/`：生产端真实输出源，也是 `radio_kg` 默认的入库数据源，保留 collection 分组和临时运行痕迹。
- `radio_kg/data/`：只放下游索引、图谱辅助状态和服务运行状态，不放原始广播产物。

清理时只删除缓存、日志、临时数据库、`.DS_Store`、`__pycache__` 等可再生文件；不要误删 `Radio/data/recordings/*/<episode>/` 下的广播产物。

> 注：早期的 `Agent/hina_radio/` 扁平数据集（Radio 合并前的旧数据源）已于 2026-05-29 移除——其内容是 `Radio/data/recordings/hina_radio` 的逐字节子集，运行时已不再读取。
