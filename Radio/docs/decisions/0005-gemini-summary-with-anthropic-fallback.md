# ADR 0005：日常总结改用 Gemini 2.5 Flash，Anthropic 保留为可切换备选

**日期**：2026-05-16  
**状态**：采纳（替代 ADR 中关于"总结一律 Claude Sonnet"的部分）

## 背景

v1 初版规划（参见 plan 文档）默认总结用 Claude Sonnet 4.5：
- 中日翻译/总结质量顶级
- 长上下文（200K）足够吃完整集
- JSON 输出稳定

实际推进时引入了两个变化：

1. **总结结构升级**（见 ADR 0007）：从"summary + key_topics + highlights"扩展为
   `summary + sections[] + key_topics + highlights`，其中 `sections` 是 6-8 段
   带 listener_mail/member_reactions/music/notes 的结构化复盘。
   这个 schema 越来越复杂，LLM 不严格遵守会让 pipeline 解析失败。

2. **成本敏感度**：节目变成每周/每两天一期的常规节奏，单期 Claude Sonnet 总结成本约 $0.14，
   月度可能上探到 $5-10。

## 决策

把日常总结 provider 默认改为 **Google Gemini 2.5 Flash**，并通过 `summary.provider`
配置项保留 **Anthropic Claude** 作为可热切换的精修通道。

关键实现细节（`src/radio/summarize.py`）：

- **Gemini 调用走 `responseSchema`**：在请求体中直接传入 `Summary` 的完整 JSON Schema（`SUMMARY_RESPONSE_SCHEMA`），强制结构化输出。这是 Gemini Flash 上的一等公民功能，比 Claude 靠 prompt 约束更可靠。
- **Anthropic 调用保持原路径**：prompt 强约束 + `_strip_json_fence` + `_extract_json_object` 容错解析。
- **provider 切换零代码改动**：改 `config/config.yaml` 里 `summary.provider: gemini|anthropic` 即可。
- **`.env` 新增 `GEMINI_API_KEY`**：但允许为空（`Secrets.gemini_api_key: SecretStr | None`），只有切到 gemini provider 时才必填。

## 理由

1. **成本**：Gemini 2.5 Flash 输入约 $0.30/1M tokens、输出约 $2.50/1M tokens；
   Claude Sonnet 4.5 对应 $3/$15。单期总结成本从约 $0.14 降到约 $0.02，**便宜 7 倍**。
2. **结构化输出更稳**：`responseSchema` 是真正的 schema 校验，不依赖 LLM 的 "请输出 JSON" 自觉性。
   对当前 7 个必填字段、嵌套 sections 数组的 schema 特别合适。
3. **质量足够**：对"日本声优广播的 6-8 段结构化复盘"任务，Gemini Flash 的中文写作质量已达可用线。
   术语库（ADR 0004）和 prompt 工程已能把这类小品类作品写得相当像样。
4. **不破坏后路**：保留 Anthropic 路径意味着一旦发现 Gemini 在某一期翻车（例如新企划名理解不到位），
   一行配置切回 Claude Sonnet。

## 后果

- ✅ 月度总结成本下降约 7 倍。
- ✅ 输出结构稳定性提升（schema 强约束 > prompt 自觉）。
- ⚠️ 引入 Google 账号 + API key 依赖。Gemini 国内访问可能需要代理（v1 用户在日本，无影响）。
- ⚠️ 代码里多了一条调用路径（`_summarize_with_gemini`），但读起来仍只有一层 if/elif；可接受。
- ⚠️ Gemini Flash 偶尔在长 transcript 上 `maxOutputTokens` 不够，已配置 8192 上限；超过单期需要监控。

## 关联文件

- `src/radio/summarize.py` — `_summarize_with_gemini` / `_summarize_with_anthropic` / `_call_summary_model`
- `src/radio/config.py` — `SummaryConfig.provider/model`，`Secrets.gemini_api_key`
- `config/config.yaml` — `summary.provider: gemini`
- `.env.example` — `GEMINI_API_KEY`
