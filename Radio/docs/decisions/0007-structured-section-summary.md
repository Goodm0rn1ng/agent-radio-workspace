# ADR 0007：结构化分段总结（ProgramSection 数组）

**日期**：2026-05-16  
**状态**：采纳

## 背景

v1 初版的 `Summary` 数据模型只有 3 个字段：
```python
class Summary(BaseModel):
    summary: str          # 中文整体摘要
    key_topics: list[str] # 3-6 个话题
    highlights: list[Highlight]  # N 个高光时刻
```

实测后发现一个真实问题：声优广播节目（如《迷子集会》、《こもれびじかん》）**本身有强结构**：

- 开场 → 公开录音/活动回顾 → 听众来信若干轮 → 中间选曲 → 闲聊环节 → 结尾告知

让 LLM 输出"一段 400 字的整体摘要"，结果都是"成员们聊了 xx 和 yy，很热闹，最后告知了下期"这种
**信息密度极低的"流水账感想"**。粉丝需要的是：
- 哪封来信讲了什么？
- 哪位成员对来信的反应是什么？
- 中间选曲是哪首？
- 节目最后告知了什么（Live、新歌、活动）？

这些是分散在节目时间线里的事实，整体摘要写不出来。

## 决策

把 `Summary` 升级为**主摘要 + 分段复盘**的混合结构：

```python
class ProgramSection(BaseModel):
    title: str                          # 环节标题
    time_range: str                     # "00:00:00-00:03:20"
    content: str                        # 80-180 字这一段讲什么
    listener_mail: str = ""             # 该段的来信内容（若是来信环节）
    member_reactions: list[str] = []    # 成员各自的反应/吐槽
    music: list[str] = []               # 中间选曲歌名
    notes: list[str] = []               # 梗、术语、告知事项

class Summary(BaseModel):
    summary: str                        # 节目主线摘要（不替代 sections）
    sections: list[ProgramSection]      # 6-8 个环节复盘
    key_topics: list[str]
    highlights: list[Highlight]
```

并把 `summarize.txt` prompt 改写为强约束的分段要求，关键约束：

- 6-8 个 sections，按时间顺序
- 每个 section 必须覆盖：title / time_range / content / listener_mail / member_reactions / music / notes
- 明确禁止臆造未出现的人名、选曲、告知
- 中间选曲若 transcript 未明确出现，`music` 留空数组 + `notes` 写"本段 transcript 未明确出现选曲"
- 配合 ADR 0005 的 Gemini `responseSchema` 强约束输出

## 理由

1. **匹配节目天然结构**：声优广播 80% 时间是来信+反应+选曲，硬塞进"整体摘要"反而抹平了信息。
2. **粉丝可消费**：分段后每段独立成行，Telegram 消息可读性大幅提升；粉丝跳读、复制、引用都方便。
3. **可作为知识库素材**：未来接入向量数据库（PRD Phase 2）时，每个 section 是天然的检索粒度，
   比整篇摘要的语义检索效果好。
4. **Gemini Schema 友好**：嵌套数组 + 明确字段在 `responseSchema` 里强约束效果最好，
   返回失败率明显低于自由文本。
5. **不丢主摘要**：保留顶层 `summary` 字段是为了在 Telegram 消息开头给一句"节目主线"，
   方便用户在打开附件之前就知道这期讲了什么。

## 后果

- ✅ Telegram 消息从"一段感想"变成"分段复盘清单"，信息密度提升 3-5 倍。
- ✅ 双语 transcript .txt 仍是底层原始数据，分段总结是上层提炼，两层互补。
- ⚠️ Prompt 字数显著增加（约 2K tokens），但 Gemini Flash 成本完全可吸收。
- ⚠️ 输出 token 数从 ~600 字升到 ~1500-2000 字，仍在 8192 max_tokens 之内。
- ⚠️ Telegram 单条消息上限 4096 字符，`telegram_sender.py` 已加截断逻辑兜底；
  超长会发送被截断的"摘要 + sections 前 8 个 + highlights"，完整内容在 .txt 附件里。

## 关联文件

- `src/radio/models.py` — `ProgramSection` / `Summary.sections`
- `src/radio/prompts/summarize.txt` — sections 强约束 prompt
- `src/radio/summarize.py` — `SUMMARY_RESPONSE_SCHEMA` 包含 sections schema
- `src/radio/telegram_sender.py` — 渲染 `*🧭 分段复盘*` 块
