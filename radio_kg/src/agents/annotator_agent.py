"""AnnotatorAgent: pre-extraction speaker segmentation (Text Annotation).

Rather than letting the ExtractorAgent guess who is speaking, this lightweight
agent runs first (DocAgent stage) and wraps each part of a chunk in speaker-role
tags using the episode's Session Constants (host / guest / listener):

  <Host_Section> ... </Host_Section>
  <Guest_Section name="..."> ... </Guest_Section>
  <Listener_Section name="ペンネーム"> ... </Listener_Section>

It also returns the listener names it found, which the pipeline accumulates as
Session Constants (future "count letter-writers" features). With explicit tags,
first-/second-person pronoun resolution in extraction becomes deterministic.
"""
from __future__ import annotations

import re

from src.llm.client import LLMClient, LLMError

# structural cue: radio shows announce listener mail with these markers
_MAIL_CUE = re.compile(r"(ラジオネーム|ペンネーム|お便り|メール|投稿)")

SYSTEM = """あなたはラジオ番組の書き起こしを「話者」で分割するアノテーターです。
番組の Session Constants:
- パーソナリティ(主持人): {host}
- ゲスト: {guest}

入力の書き起こし（各行 [時刻] 本文）を、内容ごとに次のタグで包んでください。
- <Host_Section>…</Host_Section> : パーソナリティ({host})本人のトーク。
- <Guest_Section name="名前">…</Guest_Section> : ゲストの発言（ゲストがいる場合）。
- <Listener_Section name="ペンネーム">…</Listener_Section> : リスナー投稿（お便り/メール）の読み上げ部分。
  name は「ラジオネーム◯◯さん」「ペンネーム：◯◯」等から取る。読み取れなければ name="不明"。

厳守:
- [時刻] と本文は改変・要約しない。原文のまま該当タグで包む。
- 投稿の前後でパーソナリティが導入・応答している部分は Host_Section に入れる。
- 投稿本文（「…」で読み上げる部分）だけを Listener_Section に入れる。
- listeners にはこの抜粋で登場したリスナーのペンネームを列挙する。

必ず次の JSON のみを出力:
{{"annotated": "タグ付きテキスト全体", "listeners": ["ペンネーム", ...]}}"""


class AnnotatorAgent:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    def annotate(self, text: str, host: str, guest: str = "") -> tuple[str, list[str]]:
        # fast path: no mail cue and no guest -> entire chunk is host talk
        if not _MAIL_CUE.search(text) and not guest:
            return f"<Host_Section>\n{text}\n</Host_Section>", []
        system = SYSTEM.format(host=host, guest=guest or "なし")
        try:
            data = self.llm.complete_json(system, text, max_tokens=4096)
            annotated = (data.get("annotated") or "").strip()
            listeners = [n for n in data.get("listeners", [])
                         if isinstance(n, str) and n.strip() and n.strip() != "不明"]
            if not annotated:
                raise LLMError("empty annotation")
            return annotated, listeners
        except LLMError:
            # safe fallback: treat as host talk (won't misattribute to listeners)
            return f"<Host_Section>\n{text}\n</Host_Section>", []
