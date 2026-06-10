# Agent Radio Workspace

本工作区把两个相关但职责不同的系统放在同一棵目录下：

- `Radio/`：上游广播生产端。负责录制/抓取音频，转写、翻译、总结，并把结果推送到 Telegram/小红书等渠道。
- `radio_kg/`：下游知识消费端。读取广播产物，构建图谱、向量库、人设材料，并提供问答与审批看板。`radio_kg` 默认读取 `Radio/data/recordings`（由 `RADIO_DATA_DIR` 指定）。

## 数据流

```text
Radio
  录制 / 下载 / 转写 / 翻译 / 总结 / 推送
        |
        v
  Radio/data/recordings/<collection>/<episode>/
        |
        |  radio_kg 递归读取生产端目录
        v
radio_kg
  DocAgent / Extractor / Inspector / Sync / GraphRAG / Persona
```

`radio_kg` 默认读取生产端原生布局 `Radio/data/recordings/<collection>/<episode>/`。
`src/source_data.py` 同时兼容扁平布局（`<episode>/03_ja_segments.json`），因此 `RADIO_DATA_DIR`
可指向任意符合数据契约的目录。

## 环境与启动

三个子项目共用唯一 venv `Agent/.venv`（uv workspace，依赖声明见根 `pyproject.toml`）：

```bash
uv sync                # 安装/同步全部依赖（首次或依赖变更后）
./agent-up.command     # 启动（launchd 接管：登录自启 + 崩溃自动重启；改代码后重跑即重启生效）
./agent-down.command   # 停止全部服务（bootout launchd + 停 Neo4j）
```

服务由 launchd 管理（plist 权威副本在 `scripts/com.agent.*.plist`）：radio_kg server、
录制调度 daemon、Telegram bot，外加 brew services 的 Neo4j。

`./agent-up.command` 会拉起唯一的本地前端入口：

- `radio_kg` 对话首页：`http://127.0.0.1:8000`
- 审批看板：`http://127.0.0.1:8000/dashboard`
- Radio 生产控制台子页面：`http://127.0.0.1:8000/radio`

在这个统一启动方式下，`Radio` 作为 `radio_kg` 的 FastAPI 子应用运行在同一端口。pipeline 成功产出一期后会自动调用
`radio_kg` 的 `/api/ingest`，把该期目录写入图谱与向量库；如遇知识冲突，会出现在
`/dashboard` 或对话页侧栏的待审批区。

## 维护边界

- 改广播抓取、转写、翻译、总结、推送：优先改 `Radio/`。
- 改知识图谱、向量检索、问答、人设、审批看板：优先改 `radio_kg/`。
- 改两者之间的数据契约：先确认 `03_ja_segments.json`、`04_bilingual_segments.json`、`05_summary.json` 三个产物格式，再同步更新两个项目文档。

详细结构见 [docs/project_structure.md](docs/project_structure.md)。
