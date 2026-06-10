# ADR 0004：术语库系统（双层防护：prompt 注入 + 译后机械修正）

**日期**：2026-05-16  
**状态**：采纳

## 背景

PRD 3.2 节提到 "Entity Resolution"：声优昵称、Live 名称、企划术语需要强制纠正。
v1 初版只在 `config/config.yaml` 留了一个空的 `name_corrections: {}` dict，作为最简单的 `str.replace` 占位。

实际跑节目（特别是 MyGO!!!!! / 《迷子集会》这类高密度术语场景）后发现：
- 声优名（羊宫妃那 / 立石凛 / 青木阳菜 / 小日向美香 / 林鼓子）转写错率高
- 角色名（高松灯 / 千早爱音 / 要乐奈 / 长崎爽世 / 椎名立希）有简繁体 + 假名等多种写法
- 歌曲名（《春日影》《迷星叫》《音一会》《壱雫空》等）必须保留原题，否则 LLM 会音译成 "Spring Sunlight" 类错误
- 名场面台词（"一辈子"、"为什么要演奏《春日影》！"）需要锁定中文表述
- 节目名（《迷子集会》）容易被翻成"迷子中心""迷子的集合"等错误

仅靠译后 `str.replace` 力度不够——LLM 翻错后再替换会破坏句子；最好让 LLM 在翻译时就知道术语。

## 决策

引入独立的术语库文件 `config/terminology.yaml`，**双层防护**：

1. **Prompt 注入（事前）**：翻译/总结调用 LLM 时，把术语清单格式化进 prompt，告诉模型应该用哪些中文译名。
2. **译后修正（事后）**：`post_corrections: {错误: 正确}` 字典在 segments、summary 全字段上做机械 `str.replace` 兜底。

文件结构：
```yaml
version: 1
description: "..."
sources: [...]               # 信息来源链接，便于复审
terms:                       # 结构化术语清单，进 LLM prompt
  - category: character|cast|song|quote|group|radio|concept
    ja: "..."
    zh: "..."
    aliases: ["..."]         # 别名/STT 常见错字
    note: "..."              # 翻译/识别注意点
post_corrections:            # 扁平 dict，对所有中文输出做替换
  "错误写法": "正确写法"
```

实现在 `src/radio/terminology.py`：
- `load_terminology(path)`：读 YAML，缺失时返回空库（不阻塞 pipeline）
- `format_terminology_for_prompt(path)`：压成 LLM 友好的短清单
- `load_post_corrections(path)`：读 `post_corrections` 扁平字典
- `apply_terminology_corrections(segments, corrections)`：对 segments.zh 字段批量替换
- `apply_summary_corrections(summary, corrections)`：递归对 Summary 全字段替换

## 理由

1. **可维护性**：术语和代码解耦。新加角色/歌曲/梗只需改 YAML，不需要碰 Python。
2. **可追溯**：`sources` 字段记录权威来源（BanG Dream 官网、Wikipedia、Wikia、萌娘百科等），方便术语回审。
3. **可读性**：`category/ja/zh/aliases/note` 五元组比扁平 dict 表达力强得多；同一个术语的多种写法、注意事项一目了然。
4. **稳健性**：YAML 文件不存在时返回空库不报错，保证项目即使没有术语库也能跑。
5. **两层防护互补**：
   - Prompt 注入只对 LLM 调用生效，但能避免根本性错译。
   - 译后 `str.replace` 对所有输出（包括 LLM 也修不好的简繁/识别错字）兜底。
   - 单层都不够。

## 后果

- ✅ MyGO!!!!! / 声优广播这类高密度术语场景，翻译质量显著提升。
- ✅ 术语库本身可作为粉丝圈知识资产沉淀。
- ⚠️ Prompt 长度变长（当前 ~50 条术语约增加 1.5K tokens）。对 DeepSeek/Gemini Flash 这类便宜模型成本影响可忽略。
- ⚠️ `post_corrections` 是无脑 `str.replace`，可能误伤——例如把"高松燈"统一替换为"高松灯"，遇到其他姓"高松"的人物时仍会被替换。当前规模可控；规模再大需考虑加词边界检测。

## 关联文件

- `config/terminology.yaml` — 术语库本体
- `src/radio/terminology.py` — 加载与应用逻辑
- `src/radio/prompts/translate.txt` — `{terminology}` 占位符
- `src/radio/prompts/summarize.txt` — `{terminology}` 占位符
- `src/radio/pipeline.py` — 调用 `apply_terminology_corrections`
- `src/radio/summarize.py` — 调用 `apply_summary_corrections`
