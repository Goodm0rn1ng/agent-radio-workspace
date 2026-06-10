"""PersonaAgent: the runtime side of the host's two-part "skill".

来信模式 (listener-mail mode). The user's message is treated as an お便り
(listener submission). The agent follows the COLLEAGUE.SKILL pipeline:
L1 hard-rule guard → recall similar PAST letters and how the host reacted to
them (radio_mail exemplars) → write an on-air reply in the host's voice
(Part B persona), grounded in the show's actual letter-reply style. A fitting
golden quote (Part A) may be woven in verbatim with its 【出处:期数+时间戳】.

Artifacts are produced offline by `src/build_persona.py`:
- persona/persona_profile.md → the five-layer persona (system-prompt body)
- radio_mail collection       → past (letter → host reaction) exemplars
- radio_insights collection   → curated quotes, for an optional anchor line

This is generative reply, NOT factual RAG. It never invents citations: only the
provided exemplars/quotes carry 【出处】. L1 red lines override everything.
"""
from __future__ import annotations

import json
import re

from config.settings import settings
from src.agents.qa_agent import QAAgent
from src.canonical import HOST
from src.llm.client import LLMClient, LLMError

PERSONA_DIR = settings.abspath("persona")

# L1 hard-rule trigger lexicon: extreme negative / self-harm / illegal / abuse.
# A match short-circuits the normal reply into serious, restrained guidance.
_REDLINE = (
    "自杀", "自殺", "想死", "去死", "不想活", "活不下去", "结束生命", "結束生命",
    "轻生", "輕生", "自残", "自傷", "自残", "割腕", "跳楼", "跳樓",
    "死にたい", "消えたい", "リストカット",
)

MAIL_SYSTEM = """你现在就是深夜电台节目《""" + settings.program_name + """》的主持人「""" + HOST + """」本人。
节目里有一个固定环节：你会念听众投来的お便り（来信），然后像在直播间里那样，对着话筒回应这封信。
现在你收到了下面这封听众来信，请**像你在节目里读信、回信那样**写一段回复。

下面是你的【人设画像】，请严格据此决定你的灵魂、语气与态度：
────────────────
{profile}
────────────────

参考下面【往期来信回复】——这些是你过去在节目里真实回应听众来信的方式，请学习这种「读信→反应」的口吻、节奏与态度（不要照抄内容，只学方式）：
────────────────
{exemplars}
────────────────

规则：
- 你就是她本人，对着话筒回应这封来信，不要分析人设、不要说自己是 AI 或助手。
- 像节目里那样：先自然地接住这封信（可称呼来信人或用一句开场），再按 L4 优先级回应（一般是先共情 → 再聊开/调侃 → 最后给点鼓励或建议），按 L5 区分对待不同处境的投稿者。
- 语气、口头禅、语气词、颜文字、开场/结尾问候都要符合上面人设与往期回信方式。
- 如果下面给了【可引用金句】，可挑最贴切的一句把它融进回复，并在那句末尾原样附上它的【出处:…】。不要为没给出处的句子编造出处，也不要捏造金句。
- L1 硬性规则优先级最高，任何时候都不可逾越。
- 像真的电台读信回信一样有温度，不要太长。

请同时给出**两个语言版本**的同一封回信（内容、情感、结构一致，只是语言不同）。
**铁律：每个版本只能使用一种语言，绝不混入另一种语言。**
- zh：**全程简体中文**。正文里不得出现任何日语——假名、日文专有写法、罗马音都不行。即使是主持人的招牌开场白、口头禅、语气词、金句，也必须翻成自然的中文（例如开场「こもりすのみなさん、こんばんは」要写成「各位小木漏，晚上好」这样）。若引用金句，用金句的中文译文。
- ja：**全程日文**，符合主持人本人的说话方式，不得混入中文。若引用金句，用金句的日文原文（一字不改）。
- 唯一例外：句末的【出处:…】标记两版完全一致、原样照抄（其中节目名含假名属正常，不算违规）。

只输出 JSON：{{"zh": "全程中文的回信", "ja": "全程日文的回信"}}"""

_REDLINE_REPLY = (
    "我有认真在读你这封信，这些话让我很担心你。\n"
    "这种时候我没办法只当成一封普通的来信轻轻念过去——你的安全比什么都重要。\n"
    "如果你正被很重的念头压着，请一定联系身边信得过的人，或拨打心理援助热线"
    "（中国大陆 24 小时心理援助热线 400-161-9995；日本「いのちの電話」0570-783-556）。\n"
    "我会一直在这里，等你愿意的时候，随时再写信给我。"
)
_REDLINE_REPLY_JA = (
    "お便り、ちゃんと読みました。今のあなたのこと、すごく心配です。\n"
    "こういうのは、普通のお便りみたいに軽く読み流すことはできません——あなたの安全が何より大切だから。\n"
    "もし重たい気持ちに押しつぶされそうなら、どうか信頼できる人に連絡してください。"
    "（日本「いのちの電話」0570-783-556／中国大陸の心理援助ホットライン 400-161-9995）\n"
    "私はいつもここにいます。あなたが話せるようになったら、いつでもまたお便りをくださいね。"
)

# e5 embeddings are L2-normalized, so Chroma L2 distance ≈ 2(1-cos). Past letters
# beyond this are too unlike the user's to teach a relevant reply style; the
# quote anchor uses a tighter cutoff so it only fires when truly on-point.
_MAIL_MAX_DIST = 1.6
_QUOTE_MAX_DIST = 1.3


class PersonaAgent:
    def __init__(self, llm: LLMClient, mail_store=None, insights_store=None):
        self.llm = llm
        self.mail_store = mail_store          # VectorStore on radio_mail
        self.insights_store = insights_store  # VectorStore on radio_insights (optional)
        self.profile = self._load_text("persona_profile.md")

    @staticmethod
    def _load_text(name: str) -> str:
        fp = PERSONA_DIR / name
        return fp.read_text(encoding="utf-8") if fp.exists() else ""

    @property
    def ready(self) -> bool:
        return bool(self.profile)

    @staticmethod
    def _is_redline(message: str) -> bool:
        return any(w in (message or "") for w in _REDLINE)

    # kana (hiragana/katakana) — a reliable "this is Japanese" signal for the
    # zh version. Kanji overlap with Chinese, so we only police kana here.
    _KANA_RE = re.compile(r"[぀-ゟ゠-ヿ]")
    _CITE_RE = re.compile(r"【出[处処]:[^】]*】")

    def _ensure_chinese(self, text: str) -> str:
        """Hard rule: the zh reply must contain no Japanese. Citation markers are
        exempt (the program name has kana). If kana leaks into the body, do one
        corrective rewrite to pure Chinese, preserving 【出处:…】 verbatim."""
        body = self._CITE_RE.sub("", text or "")
        if not self._KANA_RE.search(body):
            return text
        sys = ("把下面这段文字改写成纯简体中文：不得保留任何日语（假名/日文写法/罗马音），"
               "口头禅、招牌问候、金句也要译成自然中文。"
               "务必原样保留所有【出处:…】标记，不要改动其中内容，不要新增或删减信息。只输出改写后的文字。")
        try:
            fixed = self.llm._complete_text(sys, text, max_tokens=settings.qa_answer_max_tokens).strip()
        except LLMError:
            return text
        return fixed or text

    @staticmethod
    def _vector_match(store, message: str, k: int, max_dist: float) -> list[dict]:
        if store is None:
            return []
        try:
            res = store.query(message, n_results=k)
        except Exception:
            return []
        metas = (res.get("metadatas") or [[]])[0]
        dists = (res.get("distances") or [[]])[0]
        return [m for m, d in zip(metas, dists) if float(d) <= max_dist]

    @staticmethod
    def _exemplars_block(exemplars: list[dict]) -> str:
        lines = []
        for ex in exemplars:
            who = ex.get("mail_from") or "听众"
            mail = (ex.get("mail") or "").strip()
            react = ex.get("reactions") or ""
            lines.append(f"· 来信（{who}）：{mail[:140]}\n  你的回应方式：{react}")
        return "\n".join(lines)

    @staticmethod
    def _pack_mail(exemplars: list[dict]) -> list[dict]:
        out = []
        for ex in exemplars:
            who = ex.get("mail_from") or "听众"
            out.append({"text": f"来信（{who}）：{(ex.get('mail') or '')[:160]}",
                        "citation": ex.get("citation", ""), "origin": "mail"})
        return out

    @staticmethod
    def _pack_quote(quotes: list[dict]) -> list[dict]:
        out = []
        for it in quotes:
            quote = it.get("quote_ja") or it.get("quote_zh") or ""
            zh = it.get("quote_zh") or ""
            text = f"{quote}（{zh}）" if (it.get("quote_ja") and zh) else quote
            out.append({"text": text, "citation": it.get("citation", ""),
                        "origin": "insight"})
        return out

    def reply_mail(self, message: str, history: list[dict] | None = None) -> dict:
        if self._is_redline(message):
            return {"answer_zh": _REDLINE_REPLY, "answer_ja": _REDLINE_REPLY_JA,
                    "sources": [], "guarded": True}
        if not self.ready:
            msg = "（人设尚未生成，请先运行 `python -m src.build_persona`。）"
            return {"answer_zh": msg, "answer_ja": msg, "sources": [], "guarded": False}

        exemplars = self._vector_match(self.mail_store, message, 3, _MAIL_MAX_DIST)
        quotes = self._vector_match(self.insights_store, message, 1, _QUOTE_MAX_DIST)

        system = MAIL_SYSTEM.format(
            profile=self.profile,
            exemplars=self._exemplars_block(exemplars) or "（暂无可参考的往期来信，凭你一贯的读信风格回应即可。）",
        )
        if quotes:
            block = "\n".join(
                f"- {it.get('quote_ja') or it.get('quote_zh')}"
                f"{'（' + it['quote_zh'] + '）' if it.get('quote_ja') and it.get('quote_zh') else ''}"
                f" —【出处:{it.get('citation', '')}】"
                for it in quotes
            )
            system += f"\n\n【可引用金句】（可融入回复，原文+出处照抄）：\n{block}"

        convo = QAAgent._format_history(history)
        prefix = f"【在此之前的往来】\n{convo}\n\n" if convo else ""
        user = f"{prefix}【这封来信】\n{message}"
        sources = self._pack_mail(exemplars) + self._pack_quote(quotes)
        try:
            data = self.llm.complete_json(
                system, user, max_tokens=settings.qa_answer_max_tokens
            )
        except LLMError as e:
            err = f"(生成失败: {e})"
            return {"answer_zh": err, "answer_ja": err, "sources": sources, "guarded": False}
        zh = (data.get("zh") or "").strip()
        ja = (data.get("ja") or "").strip()
        if zh:
            zh = self._ensure_chinese(zh)
        return {"answer_zh": zh or ja, "answer_ja": ja or zh,
                "sources": sources, "guarded": False}
