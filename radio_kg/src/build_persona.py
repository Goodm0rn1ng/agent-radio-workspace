"""Build the host's two-part "skill" (COLLEAGUE.SKILL paradigm) by distilling
the host's broadcast traces into persona artifacts under `persona/`.

Part A — Topic & Insight: topic domains + per-topic sentiment + curated golden
quotes (with 【出处:期数+时间戳】). Part B — Persona: a five-layer behavior model.

Mirrors `build_summary_db.py`: enumerate every episode folder with a
05_summary.json, but here we run an LLM analyzer/builder over the host's
reactions and the verbatim transcript to mine views and voice.

Re-runnable; overwrites the artifacts each time.

Run:  .venv/bin/python -m src.build_persona
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.agents.doc_agent import parse_folder_metadata  # noqa: E402
from src.build_summary_db import _parse_range, _sec_to_ts, _source_folders  # noqa: E402
from src.canonical import HOST  # noqa: E402
from src.llm.client import LLMClient, LLMError  # noqa: E402
from src.mcp_layer.vector_store import VectorStore  # noqa: E402

PERSONA_DIR = settings.abspath("persona")
INSIGHTS_COLLECTION = "radio_insights"
MAIL_COLLECTION = "radio_mail"      # real listener-mail → host-reaction exemplars

# canonical life-topic domains the host recurs to (user-defined seed taxonomy);
# the analyzer maps observations to these where possible, may add new ones.
SEED_TOPICS = [
    "深夜焦虑", "职场人际", "恋爱关系", "推活/追星(Oshikatsu)", "孤独感",
    "自我接纳", "生活小确幸", "作品与演出", "季节与情绪",
]

# the final, reader-facing taxonomy the long tail collapses into (避免话题碎片化)
CANON_TOPICS = [
    "深夜焦虑", "职场人际", "恋爱关系", "推活/追星", "孤独感",
    "自我接纳", "生活小确幸", "作品与演出", "季节与情绪",
    "人际关系", "亲子与家庭", "青春回忆", "情绪调节",
]

_TRANSCRIPT_CHAR_CAP = 22000   # bound per-episode LLM input


# ── per-episode analyzer (Part A candidates + Part B signals) ──────────
EPISODE_ANALYZER = """你在为一档深夜电台节目的主持人「""" + HOST + """」建立「话题观点库 + 人设画像」。
输入是某一期节目的结构化摘要与逐字稿（[时间戳] 日文原文）。这是一档以主持人独白与读听众来信为主的节目，逐字稿里绝大多数发言来自主持人本人。

请只针对**主持人本人**的发言与态度，产出两部分：

1. insights：主持人在生活类话题上表达的「金句/观点」。每条：
   - topic：话题域，尽量归入这些之一：""" + "、".join(SEED_TOPICS) + """；都不合适再自拟简短话题名。
   - sentiment：用一句中文概括主持人面对该话题时的底层态度/逻辑。
   - quote_ja：从逐字稿中**逐字照抄**一句最能代表该观点的主持人原话（不要改写、不要翻译，必须是逐字稿里出现过的原文）。若该观点只在中文摘要里、逐字稿中找不到合适原句，则留空字符串。
   - quote_zh：该金句的中文翻译或转述（自然口语）。
   - time：该金句对应的时间戳 HH:MM:SS。优先用 quote_ja 所在那一行 [时间戳] 的值；若取自摘要则用所在环节的起始时间。
   只收录治愈系、清醒系、幽默系等有「共鸣价值」的生活观点；纯节目流程/事务性内容不要收。每期 3~8 条即可。

2. persona_signals：观察到的主持人「声音文字化」特征（用于人设画像）：
   - catchphrases：标志性口头禅/感叹（如「天哪」「絶対」等，照抄日文原文）
   - particles：惯用语气词
   - emoji_kaomoji：颜文字/emoji 偏好（若有）
   - greeting：开场问候原文（若该期出现）
   - closing：结尾问候原文（若该期出现）
   - tone_notes：语气/句子长短/节奏的观察（中文短句）
   - attitude_to_listeners：对不同投稿者（如迷茫学生、备考者、职场人）的态度分层观察（中文短句）

只输出 JSON：
{"insights":[{"topic":"","sentiment":"","quote_ja":"","quote_zh":"","time":"HH:MM:SS"}],
 "persona_signals":{"catchphrases":[],"particles":[],"emoji_kaomoji":[],"greeting":"","closing":"","tone_notes":[],"attitude_to_listeners":[]}}"""


# ── global builders (Part A consolidation + Part B five-layer profile) ──
TOPIC_MAPPER = """你在归并电台节目的话题标签，避免碎片化。给你一组原始话题名，请把每个映射到下面这份固定话题表里**语义最接近**的一个：
""" + "、".join(CANON_TOPICS) + """。
只有当某原始话题确实无法归入上表任何一项时，才保留它原本的简短名称。
只输出 JSON：{"map":{"原始话题":"归并后话题", ...}}"""

TOPIC_BUILDER = """你在整理电台主持人「""" + HOST + """」的「话题观点库」。
输入是按话题分组的若干「情感态度」观察句。请为每个话题输出一段凝练的中文「情感态度倾向」描述（2~4 句，概括主持人面对该话题时的底层逻辑与一贯立场）。
只输出 JSON：{"topics":[{"topic":"话题名","sentiment":"该话题下主持人的情感态度倾向"}]}"""

PERSONA_BUILDER = """你在为电台主持人「""" + HOST + """」（节目《""" + settings.program_name + """》）构建严格的「五层人设画像」，用于驱动一个以她口吻陪用户聊天的助手。
输入是从多期节目里汇总的声音特征与态度观察。请综合成五层结构。

- L1 硬性规则（红线，不可逾越，优先级最高）：列出绝不触碰的底线。务必包含：涉及极端负面情绪（自伤/轻生）、违法、严重人身攻击的来信一律不照常回信，改为严肃、克制的引导并建议求助。可补充其他合理红线。
- L2 身份认同：name=""" + HOST + """，program=《""" + settings.program_name + """》，role（声优/电台DJ 等），age_feel（年龄感），mbti（若无法判断填""未知""），culture（声优/偶像/电台圈层文化）。
- L3 表达风格：catchphrases（口头禅，日文原文）、particles（语气词）、emoji_kaomoji、greeting（标志性开场）、closing（标志性结尾）、sentence_style（句子长短/节奏的中文描述）。
- L4 决策与判断：面对听众烦恼时的回复策略与优先顺序（如「先温柔共情 → 再开导拉扯 → 最后给实际建议」），以及何时切换到吐槽/调侃模式。
- L5 听众互动行为：对不同投稿者的态度分层（如对迷茫/备考/毕业的学生倍加温柔，对职场老油条用平辈口气调侃）。

忠实于输入观察，证据不足处给出克制、合理的设定，不要臆造具体八卦。
只输出 JSON：
{"l1_hard_rules":[],
 "l2_identity":{"name":"","program":"","role":"","age_feel":"","mbti":"","culture":""},
 "l3_expression":{"catchphrases":[],"particles":[],"emoji_kaomoji":[],"greeting":"","closing":"","sentence_style":""},
 "l4_decision":[],
 "l5_interpersonal":[]}"""


def _episode_payload(folder: Path, episode: int, label: str) -> str:
    summary = json.loads((folder / "05_summary.json").read_text(encoding="utf-8"))
    parts = [f"# 第{episode}期 {label}"]
    kt = summary.get("key_topics") or []
    if kt:
        parts.append("关键话题：" + "、".join(str(x) for x in kt))
    for sec in summary.get("sections", []):
        start, _ = _parse_range(sec.get("time_range", ""))
        title = sec.get("title") or sec.get("title_ja") or ""
        content = sec.get("content") or ""
        reactions = sec.get("member_reactions") or []
        block = [f"## [{_sec_to_ts(start)}] {title}".rstrip()]
        if content:
            block.append(content)
        if reactions:
            block.append("主持人反应：" + "；".join(str(r) for r in reactions))
        parts.append("\n".join(block))

    transcript = _transcript_lines(folder)
    body = "\n\n".join(parts)
    if transcript:
        body += "\n\n# 逐字稿（[时间戳] 日文原文）\n" + transcript
    return body[:_TRANSCRIPT_CHAR_CAP]


def _transcript_lines(folder: Path) -> str:
    for fname in ("04_bilingual_segments.json", "03_ja_segments.json"):
        fp = folder / fname
        if not fp.exists():
            continue
        segs = json.loads(fp.read_text(encoding="utf-8"))
        out = []
        for s in segs:
            ja = (s.get("ja") or s.get("text") or "").strip()
            if not ja:
                continue
            out.append(f"[{_sec_to_ts(int(s.get('start', 0)))}] {ja}")
        return "\n".join(out)
    return ""


def _citation(episode: int, time: str) -> str:
    time = time.strip() or "00:00:00"
    return f"《{settings.program_name}》第{episode}期 {time}"


def build():
    llm = LLMClient()
    folders = _source_folders(settings.abspath(settings.radio_data_dir))
    if not folders:
        raise SystemExit("no episode folders with 05_summary.json found")

    built_labels: list[str] = []
    insights: list[dict] = []
    signals: list[dict] = []
    for folder in folders:
        import re
        m = re.search(r"#(\d+)", folder.name)
        episode = int(m.group(1)) if m else 0
        label = parse_folder_metadata(str(folder), settings.program_name).episode_label
        built_labels.append(label)
        payload = _episode_payload(folder, episode, label)
        try:
            data = llm.complete_json(EPISODE_ANALYZER, payload, max_tokens=8192)
        except LLMError as e:
            print(f"  ! ep{episode} {label}: analyzer failed ({e}); skipped")
            continue
        for it in data.get("insights", []):
            topic = (it.get("topic") or "").strip()
            quote_ja = (it.get("quote_ja") or "").strip()
            quote_zh = (it.get("quote_zh") or "").strip()
            if not topic or not (quote_ja or quote_zh):
                continue
            time = (it.get("time") or "").strip()
            insights.append({
                "topic": topic,
                "sentiment": (it.get("sentiment") or "").strip(),
                "quote_ja": quote_ja,
                "quote_zh": quote_zh,
                "time": time,
                "episode": episode,
                "episode_label": label,
                "citation": _citation(episode, time),
            })
        sig = data.get("persona_signals") or {}
        if sig:
            signals.append(sig)
        print(f"  ✓ ep{episode} {label}: +{len(data.get('insights', []))} insights")

    if not insights:
        raise SystemExit("no insights distilled; aborting")

    _canonicalize_topics(llm, insights)
    by_topic = _group_by_topic(insights)
    sentiments = _build_topic_sentiments(llm, by_topic)
    profile = _build_persona_profile(llm, signals)

    PERSONA_DIR.mkdir(parents=True, exist_ok=True)
    (PERSONA_DIR / "versions").mkdir(exist_ok=True)  # reserved for future evolution
    _write_topic_insights(by_topic, sentiments)
    _write_insights_json(insights)
    _write_persona_profile(profile)
    _build_insight_db(insights)

    exemplars = _collect_mail_exemplars(folders)
    _write_mail_json(exemplars)
    _build_mail_db(exemplars)

    from src import index_version as iv
    print("stamped:", iv.stamp(iv.PERSONA, built_labels))
    print(f"\npersona built under {PERSONA_DIR}: "
          f"{len(insights)} insights / {len(by_topic)} topics / {len(signals)} episodes of signals")


def _canonicalize_topics(llm: LLMClient, insights: list[dict]) -> None:
    """Collapse the analyzer's fragmented topic labels onto CANON_TOPICS in place."""
    raw = sorted({it["topic"] for it in insights})
    payload = json.dumps(raw, ensure_ascii=False)
    try:
        data = llm.complete_json(TOPIC_MAPPER, payload, max_tokens=4096)
        mapping = {k: (v or k).strip() for k, v in (data.get("map") or {}).items()}
    except LLMError as e:
        print(f"  ! topic mapper failed ({e}); keeping raw topics")
        return
    for it in insights:
        it["topic"] = mapping.get(it["topic"], it["topic"])


def _group_by_topic(insights: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for it in insights:
        out.setdefault(it["topic"], []).append(it)
    # stable order: most-covered topics first
    return dict(sorted(out.items(), key=lambda kv: (-len(kv[1]), kv[0])))


def _build_topic_sentiments(llm: LLMClient, by_topic: dict[str, list[dict]]) -> dict[str, str]:
    payload = json.dumps(
        [{"topic": t, "notes": [i["sentiment"] for i in items if i["sentiment"]]}
         for t, items in by_topic.items()],
        ensure_ascii=False,
    )
    try:
        data = llm.complete_json(TOPIC_BUILDER, payload, max_tokens=4096)
        return {t["topic"]: (t.get("sentiment") or "").strip()
                for t in data.get("topics", []) if t.get("topic")}
    except LLMError as e:
        print(f"  ! topic sentiment builder failed ({e}); using raw notes")
        return {}


def _build_persona_profile(llm: LLMClient, signals: list[dict]) -> dict:
    payload = json.dumps(signals, ensure_ascii=False)
    try:
        return llm.complete_json(PERSONA_BUILDER, payload, max_tokens=4096)
    except LLMError as e:
        print(f"  ! persona builder failed ({e}); writing minimal profile")
        return {}


# ── renderers ──────────────────────────────────────────────────────────
def _write_topic_insights(by_topic: dict[str, list[dict]], sentiments: dict[str, str]):
    lines = ["# Part A — 话题观点库（Topic & Insight）",
             "",
             f"主持人「{HOST}」在生活类话题上的核心观点与往期金句。来信回复时按话题召回，命中金句以原话输出并标注【出处】。",
             ""]
    for topic, items in by_topic.items():
        lines.append(f"## {topic}")
        sent = sentiments.get(topic) or "；".join(
            dict.fromkeys(i["sentiment"] for i in items if i["sentiment"]))
        if sent:
            lines.append("")
            lines.append(f"**情感态度倾向：** {sent}")
        lines.append("")
        lines.append("**往期金句与共鸣库：**")
        lines.append("")
        for it in items:
            quote = it["quote_ja"] or it["quote_zh"]
            zh = f"（{it['quote_zh']}）" if it["quote_ja"] and it["quote_zh"] else ""
            lines.append(f"> {quote}{zh} —【出处:{it['citation']}】")
            lines.append("")
        lines.append("")
    (PERSONA_DIR / "topic_insights.md").write_text("\n".join(lines), encoding="utf-8")


def _write_insights_json(insights: list[dict]):
    (PERSONA_DIR / "insights.json").write_text(
        json.dumps(insights, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_insight_db(insights: list[dict]):
    """Embed each curated quote into Chroma so PersonaAgent can recall by meaning
    (multilingual e5), not just lexical overlap."""
    ids, docs, metas = [], [], []
    for i, it in enumerate(insights):
        # embed a Chinese+Japanese view so either-language chat matches
        doc = "。".join(p for p in [
            it["topic"], it.get("sentiment", ""), it.get("quote_zh", ""),
            it.get("quote_ja", ""),
        ] if p)
        ids.append(f"insight-{i:04d}")
        docs.append(doc)
        metas.append({
            "topic": it["topic"], "sentiment": it.get("sentiment", ""),
            "quote_ja": it.get("quote_ja", ""), "quote_zh": it.get("quote_zh", ""),
            "time": it.get("time", ""), "episode": it.get("episode", 0),
            "episode_label": it.get("episode_label", ""),
            "citation": it.get("citation", ""),
        })
    with VectorStore(collection_name=INSIGHTS_COLLECTION) as v:
        v.reset_collection()
        B = 64
        for j in range(0, len(ids), B):
            v.add_chunks(ids[j:j + B], docs[j:j + B], metas[j:j + B])
        print(f"insight DB built: collection={INSIGHTS_COLLECTION} count={v.count()}")


def _collect_mail_exemplars(folders: list[Path]) -> list[dict]:
    """Real (listener mail → host reaction) pairs straight from the summaries.
    These ground the on-air reply STYLE for 来信模式 — no LLM needed."""
    import re
    out = []
    for folder in folders:
        m = re.search(r"#(\d+)", folder.name)
        episode = int(m.group(1)) if m else 0
        label = parse_folder_metadata(str(folder), settings.program_name).episode_label
        data = json.loads((folder / "05_summary.json").read_text(encoding="utf-8"))
        for sec in data.get("sections", []):
            mail = (sec.get("listener_mail") or "").strip()
            mail_from = (sec.get("listener_mail_from") or "").strip()
            reactions = [str(r).strip() for r in (sec.get("member_reactions") or []) if str(r).strip()]
            # a mail-reply moment: there is a letter (or named sender) and a reaction
            if not reactions or not (mail or mail_from):
                continue
            start, _ = _parse_range(sec.get("time_range", ""))
            out.append({
                "mail_from": mail_from,
                "mail": mail or (sec.get("content") or "").strip(),
                "reactions": reactions,
                "title": (sec.get("title") or sec.get("title_ja") or "").strip(),
                "episode": episode,
                "episode_label": label,
                "time": _sec_to_ts(start),
                "citation": _citation(episode, _sec_to_ts(start)),
            })
    return out


def _write_mail_json(exemplars: list[dict]):
    (PERSONA_DIR / "mail_exemplars.json").write_text(
        json.dumps(exemplars, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_mail_db(exemplars: list[dict]):
    """Embed each past letter so a user's submission recalls similar letters and
    the way the host reacted to them."""
    ids, docs, metas = [], [], []
    for i, ex in enumerate(exemplars):
        doc = "。".join(p for p in [ex["title"], ex["mail"]] if p)
        ids.append(f"mail-{i:04d}")
        docs.append(doc)
        metas.append({
            "mail_from": ex["mail_from"], "mail": ex["mail"][:600],
            "reactions": "；".join(ex["reactions"]), "title": ex["title"],
            "episode": ex["episode"], "episode_label": ex["episode_label"],
            "time": ex["time"], "citation": ex["citation"],
        })
    if not ids:
        print("no mail exemplars found; skipped radio_mail")
        return
    with VectorStore(collection_name=MAIL_COLLECTION) as v:
        v.reset_collection()
        B = 64
        for j in range(0, len(ids), B):
            v.add_chunks(ids[j:j + B], docs[j:j + B], metas[j:j + B])
        print(f"mail DB built: collection={MAIL_COLLECTION} count={v.count()}")


def _write_persona_profile(p: dict):
    ident = p.get("l2_identity") or {}
    expr = p.get("l3_expression") or {}

    def _bullets(xs):
        return "\n".join(f"- {x}" for x in (xs or [])) or "- （待补充）"

    def _kv(label, v):
        return f"- **{label}：** {v or '未知'}"

    lines = [
        f"# Part B — 主持人人设画像（Persona）：{HOST}",
        "",
        "五层行为特征模型。运行时作为 system prompt 主体，决定回复的「灵魂与语气」。",
        "",
        "## L1 硬性规则（Hard Rules）",
        "绝对红线，优先级高于其它所有层。",
        "",
        _bullets(p.get("l1_hard_rules")),
        "",
        "## L2 身份认同（Identity）",
        "",
        _kv("称呼", ident.get("name") or HOST),
        _kv("节目", ident.get("program") or f"《{settings.program_name}》"),
        _kv("社会角色", ident.get("role")),
        _kv("年龄感", ident.get("age_feel")),
        _kv("MBTI", ident.get("mbti")),
        _kv("圈层文化", ident.get("culture")),
        "",
        "## L3 表达风格（Expression Style）",
        "",
        _kv("口头禅", "、".join(expr.get("catchphrases") or []) or "未知"),
        _kv("语气词", "、".join(expr.get("particles") or []) or "未知"),
        _kv("Emoji/颜文字", "、".join(expr.get("emoji_kaomoji") or []) or "未知"),
        _kv("开场问候", expr.get("greeting")),
        _kv("结尾问候", expr.get("closing")),
        _kv("句子风格", expr.get("sentence_style")),
        "",
        "## L4 决策与判断（Decision & Judgment）",
        "面对听众烦恼时的回复策略与优先顺序。",
        "",
        _bullets(p.get("l4_decision")),
        "",
        "## L5 听众互动行为（Interpersonal Behavior）",
        "对不同投稿者的态度分层。",
        "",
        _bullets(p.get("l5_interpersonal")),
        "",
    ]
    (PERSONA_DIR / "persona_profile.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    build()
