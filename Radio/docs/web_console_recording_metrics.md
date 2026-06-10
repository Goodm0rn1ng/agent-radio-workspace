# 前端录制与运行指标说明

本文记录当前 Web 控制台中录制任务、长期计划与运行指标的实际行为，面向日常使用和排障。

## Radiko 录制

### 实时预约录制

在“新建任务”中选择 Radiko，并选择“实时录制”。广播地址应使用 live 形态：

```text
https://radiko.jp/#!/live/QRR
```

填写“开始时间”和“录制时长”后，任务会先进入等待状态，到指定时间再开始录制。适合未来还没有进入 time-free 的直播节目。

如果拿到的是未来节目表中的 time-free 形态地址，例如：

```text
https://radiko.jp/#!/ts/QRR/20260518003000
```

仅把 `ts` 改成 `live`，并保留电台代码即可用于预约：

```text
https://radiko.jp/#!/live/QRR
```

开始时间仍以表单里的时间为准，URL 中的 `20260518003000` 不会被当成预约时间。

### 回听录制

在“新建任务”中选择 Radiko，并选择“回听录制”。广播地址应使用 ts 形态：

```text
https://radiko.jp/#!/ts/QRR/20260518003000
```

回听录制代表节目已经可回放，会立即启动下载与处理流程；它不使用“开始时间”等待未来开播。

## YouTube 直播录制

YouTube 直播任务同样支持填写开始时间。任务创建后会等待到指定时间，再启动直播录制。没有填写开始时间时，会按立即任务处理。

## 长期定时计划

“定时计划”用于维护长期重复任务，例如每周固定录制某个节目。前端会把节目名、来源、URL、星期、时间、时区、录制时长、处理选项写入后端的计划配置，由守护进程按计划触发。

定时计划适合固定档期；一次性的未来直播更适合在“新建任务”里填写开始时间。

## 运行指标与 token

每次 pipeline 结束后，后端会向 `data/logs/metrics.jsonl` 追加一行 JSON。当前指标包括：

- 基础运行信息：`run_id`、`started_at`、`duration_s`、`source`、`program_name`、`air_date`
- 处理规模：`segments_count`、`batches_count`、`sections_count`
- 知识库与推送：`library_hits`、`library_added`、`telegram_messages_sent`
- 步骤耗时：`step_durations`
- token 总量：`input_tokens`、`output_tokens`、`total_tokens`
- 分模型 token：`token_usage`，例如 `translation.deepseek`、`summary.gemini`
- 状态：`warnings`、`errors`、`success`

Web 控制台的“运行指标”卡片会直接显示本次 token 合计；鼠标悬停在卡片上可以查看输入、输出、合计，以及各模型调用的 token 分布。

历史旧指标不会自动补齐 token 字段；只有本次更新之后新跑完的任务才会记录 token。

## 总结 JSON 被截断时

总结阶段要求 LLM 返回结构化 JSON。长节目或分段很多时，如果模型输出达到 `summary.max_output_tokens` 上限，返回内容可能停在数组或对象中间，表现为：

```text
json.decoder.JSONDecodeError: Expecting ',' delimiter
```

当前配置将 `summary.max_output_tokens` 设为 `32768`，并且后端会识别 Gemini 的 `MAX_TOKENS` 结束原因，或识别末尾没有闭合的 JSON，转成明确的“模型输出被截断”错误并触发自动重试。原始响应会 dump 到 `data/logs/summarize_raw_truncated_*.json`，用于事后排查。
