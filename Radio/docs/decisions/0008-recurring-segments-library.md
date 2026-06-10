# ADR 0008：常驻广播环节知识库（segments_library）

**日期**：2026-05-16  
**状态**：采纳

## 背景

声优广播节目（例如 MyGO!!!!! 的《迷子集会》）有一组**长期固定的环节**：
- 「僕、私、迷子中」：征集听众"还在犹豫要不要做"的事
- 「迷子的迷子の57577」：57577 短诗投稿（假想环节）
- 「お便りコーナー」：常规来信

这些环节**每期都会出现**，但在我们的 LLM 总结里：

1. 每次都让模型现编一句"这个环节是干什么的"，结果每期表述不一样，无法做跨期对比
2. 环节名常被模型翻成不一致的中文（"迷子集会"→"迷子中心"/"迷路的集会"/"迷茫集会"），破坏术语统一
3. 用户看 Telegram 摘要时拿不到"这个环节本来就是干嘛的"作为上下文

ADR 0004 的术语库解决了"专有名词正确性"，但**没有解决"环节是什么"这种持续性知识**。
PRD 1.2 节提到的"知识沉淀"长期愿景，需要一个能逐步积累的常驻数据。

## 决策

引入 `config/segments_library.yaml` 作为节目常驻环节的知识库，配套 `src/radio/segments_library.py` 模块。

### 数据结构

```yaml
programs:
  - program_id: mygo_meigo_shukai
    program_ja: "MyGO!!!!!の「迷子集会」"
    program_zh: "MyGO!!!!!的《迷子集会》"
    recurring_segments:
      - id: bokuwatashi_maigochuu
        title_ja: "僕、私、迷子中"
        aliases: ["僕私迷子中", "ぼく わたし まいごちゅう", ...]
        intro: |
          这个环节征集听众们还在犹豫要不要做、迟迟无法迈出第一步、
          优柔寡断无法决定的事情的来信……
```

### Pipeline 接入

1. **Prompt 注入**：summarize prompt 中加入 `{segments_library}` 占位符；
   library 内容被格式化为"节目 → 环节 → 别名 + 介绍"的层级列表，告诉 LLM 哪些环节已登记
2. **强制原文标题**：prompt 要求每个 section 输出 `title_ja`（日语原标题）
3. **后处理匹配**：LLM 返回后，对每个 section 在 library 中按 title_ja + aliases + 子串规则匹配
   - 命中：覆盖 `intro` 为 library 标准版，`is_recurring=True`
   - 未命中：保留 LLM 输出，`is_recurring=False`，Telegram 渲染时打 🆕 标签
4. **自动入库（v0.2.x 更新）**：新发现的环节自动追加到 YAML，由内置去重逻辑
   防止重复污染。详见下文「Update 2026-05-16」

### `ProgramSection` 模型扩展

新增字段（全部默认值，向后兼容）：
- `title_ja: str`：日语原标题
- `intro: str`：环节介绍
- `is_recurring: bool`：是否命中 library
- `listener_mail_ja: str`：来信日语原文（与中文 `listener_mail` 配对）

## 理由

1. **统一性**：同一环节在不同期之间用同一份介绍，做跨期检索/合集时一致。
2. **可消费性**：用户在 Telegram 里看到 `JP: 僕、私、迷子中 / 介绍：……` 而不是模糊感想，
   能立即理解上下文。
3. **演进性**：每发现一个新环节，手工往 YAML 里加一行就行；YAML 在 Git 里变成可审计的"节目知识资产"。
4. **匹配宽松度可控**：精确 → 别名 → 子串三层匹配，对 LLM 偶发的"在标题里加修饰词"等情况鲁棒；
   误匹配率可通过维护 aliases 字段控制。
5. **保留 LLM 创造力**：未命中环节由 LLM 现编 intro，用户事后审核入库——
   半自动而非完全人工，工作量小但质量高。
6. **不破坏现有逻辑**：所有新字段默认空字符串/False，旧测试和历史数据不受影响。

## 后果

- ✅ 节目常驻环节描述跨期一致，sections 输出质量显著提升。
- ✅ YAML 知识库随时间增长，逐步沉淀粉丝圈的"广播节目知识图谱"
  （未来 PRD Phase 2 向量数据库引入时，这份 YAML 是天然冷启数据）。
- ✅ Telegram 渲染区分常驻 ⭐ vs 新环节 🆕，用户一眼看出"这期有新栏目"。
- ⚠️ Prompt token 数继续增长（library 注入约几百到几 K tokens），需要在 library 膨胀时
  考虑按节目 ID 过滤——v1 阶段单节目用例下不需要。
- ⚠️ 后处理匹配的"子串规则"可能误匹配（如某新环节标题包含旧环节关键字）。
  当前规模可控；遇到误匹配时可在 aliases 里反例标注或细化匹配规则。
- ⚠️ 新发现的环节不自动入库，用户每次需要手动维护 YAML。这是有意的——
  自动入库会让低质量描述污染长期知识库。

## Update 2026-05-16：从「手动维护」改为「自动追加 + 内置去重」

### 背景

实战跑 MyGO!!!!!の「迷子集会」#178 后，用户明确要求"以后从第一期开始重头烤制，
新环节自动入库"。最初版本的"不自动入库以避免噪音"策略不再适用——逐期手动维护
对节目热爱者来说工作量过大，且 v1 单用户场景下"噪音"是可承担成本。

### 新策略

每次 summarize 完成后，pipeline 检查所有 `is_recurring=False` 的 sections：

1. 用 `extract_series_name(program_name)` 抽出系列名（如「MyGO!!!!!の「迷子集会」」从「…#178」剥离）
2. 在 library 中查找匹配的 program 节点（程序名 substring 双向匹配）
3. 找不到则**自动新建 program 节点**
4. 对每个新环节运行去重检查：
   - `title_ja` 精确等于已有标题或其 aliases → 跳过
   - `title_ja` 与任一已有标题双向 substring 包含 → 跳过（防止变种重复，如「迷子の57577」vs「迷子の57577コーナー」）
5. 通过去重的环节用 LLM 现编的 `intro` 作为初版描述追加
6. 写回 YAML

通过 `config/config.yaml` 的 `summary.auto_append_new_segments: true`（默认开启）控制。

### 为什么改

- **PRD 1.2 节"知识沉淀"愿景**：library 越早积累越值钱。手动维护把"沉淀速度"绑在人的耐心上。
- **半自动审核**：自动追加只是"提案"，git 记录每次新增；用户可以事后批量编辑 intro、合并别名、删除噪音。
- **小用户群可承担噪音**：v1 自用阶段，即使偶尔加进一些"非典型环节"，也好过永远空着。
- **dedup 兜底**：双向 substring 匹配能压制 80% 的变种问题。剩下的 20% 后续靠 aliases 字段
  收编（用户编辑时把变种标题塞进同条 aliases）。

### 关联文件（更新）

- `config/segments_library.yaml` — 知识库本体（运行时被程序追加）
- `src/radio/segments_library.py` — 新增 `append_new_segments_to_library` /
  `extract_series_name` / `_slugify`
- `src/radio/config.py` — `SummaryConfig.auto_append_new_segments: bool = True`
- `src/radio/pipeline.py` — summarize 后调用 append
- `scripts/main_resummarize.py` — 同步逻辑（resummarize 也自动入库）

## 关联文件

- `config/segments_library.yaml` — 知识库本体
- `src/radio/segments_library.py` — `SegmentEntry` / `load_segments_library` /
  `match_segment` / `format_library_for_prompt` / `append_new_segments_to_library` / `extract_series_name`
- `src/radio/models.py` — `ProgramSection` 新字段
- `src/radio/prompts/summarize.txt` — `{segments_library}` 占位符 + 输出规则
- `src/radio/summarize.py` — `_apply_segments_library` 后处理 + schema 更新
- `src/radio/telegram_sender.py` — JP/CN 标题 + intro + ⭐/🆕 标签渲染
- `src/radio/config.py` — `SummaryConfig.segments_library_path` / `auto_append_new_segments`
- `src/radio/pipeline.py` — summarize 后调用 append
- `scripts/main_resummarize.py` — 同步逻辑
