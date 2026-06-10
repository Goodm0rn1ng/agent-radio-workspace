# ADR 0009：可观测性 + STT prompt 动态注入

**日期**：2026-05-16  
**状态**：采纳

## 背景

v0.2.x 已经把 pipeline 跑通并能稳定输出。但是：

1. **看不见用量**：每跑一次到底花了多少时间在哪一步？月度跑了多少期？库命中率多少？
   这些信息分散在多份 loguru 日志里，无法横向对比。
2. **STT 静态 prompt 浪费机会**：节目里出现的人名、角色名、环节名都已经在术语库
   和 segments_library 里了，但 Whisper 调用时只用了 config.yaml 里一个固定字符串
   `"声優、ラジオ、ライブ、安野希世乃、悠木碧"`，没充分利用这些上下文。
3. **失败静默**：pipeline 抛错只写到本地日志，用户在 Telegram 里看不到，
   也不知道哪期跑失败了。

## 决策

### A. Metrics 模块 `src/radio/utils/metrics.py`

每次 pipeline 跑完写一行 JSON 到 `data/logs/metrics.jsonl`，append-only：

```python
class PipelineMetrics(BaseModel):
    run_id: str              # 20260516-022054
    started_at: str          # ISO 8601
    duration_s: float
    source: str              # video / oneshot / resummarize / live_recording
    program_name: str
    air_date: str
    segments_count: int
    batches_count: int
    sections_count: int
    library_hits: int        # 命中常驻环节库数
    library_added: int       # 自动入库新增数
    telegram_messages_sent: int
    step_durations: dict[str, float]
    warnings: list[str]
    errors: list[str]
    success: bool
```

提供 `MetricsCollector.step(name)` 上下文管理器自动测耗时；
超过 `DEFAULT_THRESHOLDS_S`（如 STT > 600s、Summary > 120s）触发 warning。

`scripts/metrics_report.py` 聚合 jsonl 出周/月/全量报表：
- 运行次数、成功率
- 平均 / P95 / 最长耗时
- 平均段数 / sections 数 / 命中率
- 按入口 / 节目分组统计
- 失败列表

### B. STT prompt 动态注入 `src/radio/stt.build_stt_prompt`

调 Whisper 之前实时构造 prompt：
- `config.yaml` 或 profile 的 `stt.prompt` 作为基础
- terminology.yaml 中当前 series 的 radio 类别 `ja` 字段（节目名）
- segments_library.yaml 中当前 series 的 `title_ja`（常驻环节名）

去重后用 `、` 拼接，截断到 200 字符（Whisper API prompt 上限约 224 tokens）。

注意：不再自动注入全局 cast / character / song / quote / concept 词库。Groq Whisper
会在安静片段或不确定片段中把 `、` 分隔的 prompt 词表复读成转写文本，表现为
“羊宮妃那、立石凛、青木陽菜、長崎そよ、春日影”这类音频中不存在的清单。
代码还会在 `_transcribe_one()` 中过滤明显的 prompt 回声片段，避免污染后续翻译和总结。

### C. Telegram 失败通知 `telegram_sender.notify_pipeline_failure`

`pipeline.py` 的顶层 try/except 捕获任何异常 → 调 `notify_pipeline_failure`
发一条 Telegram：

```
❌ Pipeline 失败
节目：MyGO!!!!!の「迷子集会」#178
日期：2026-05-13
错误：JSONDecodeError: Expecting property name enclosed in double quotes…
```

注：失败通知本身的发送失败被 try/except 兜底，不会二次崩溃。

## 理由

1. **零外部依赖**：jsonl 是最简单的"时序数据库"，append-only 不会损坏；
   pydantic 已在用；不引入 Prometheus、OpenTelemetry 等重型工具。
2. **STT 注入 ROI 极高**：~50 行代码，复用已有数据，识别率提升立即体现在后续
   翻译质量上。"既然术语库里已经有这些名字，让 Whisper 也知道"——成本：零。
3. **失败通知止血**：用户配置了 cron 后不会一直盯日志；失败几小时才发现等于
   损失一期节目。Telegram 通知是最便宜的"被动监控"。
4. **不动用户的 config.yaml**：STT prompt 动态注入是程序内部行为；
   `stt.prompt` 仍作为可选 base prompt 保留，向后兼容。

## 后果

- ✅ 月度运行情况一行命令出报表 (`uv run python scripts/metrics_report.py`)
- ✅ STT 对节目特有环节名识别率提升（实际改善需 5+ 期数据验证）
- ✅ Pipeline 失败时 Telegram 第一时间收到，含错误首行
- ⚠️ `data/logs/metrics.jsonl` 会持续增长，但每行 < 1KB；100 期 < 100KB，
  即使一年 200+ 期也只是 200KB，无需轮转
- ⚠️ STT prompt 接近 224 token 上限时，新加术语会优先级越来越关键。当前
  hard-code 优先级 base > radio > library。若术语库膨胀到一定程度需要更细的
  优先级策略（如按"该节目实际出现频率"动态加权）

## 关联文件

- `src/radio/utils/metrics.py` — `PipelineMetrics` / `MetricsCollector`
- `src/radio/pipeline.py` — `run_pipeline` 全程包裹 metrics + 顶层 try/except
- `src/radio/stt.py` — `build_stt_prompt()` + `_transcribe_one(prompt=...)`
- `src/radio/telegram_sender.py` — `notify_pipeline_failure()`
- `scripts/metrics_report.py` — jsonl 聚合报表
