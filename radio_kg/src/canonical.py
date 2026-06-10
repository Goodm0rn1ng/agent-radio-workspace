"""Canonical entity registry for coreference / alias resolution.

Solves the "羊宮妃那 / 羊宮 / 私 split into separate nodes" problem:
- proper nicknames collapse onto the canonical entity (deterministic);
- first-/second-person pronouns are speaker-relative and handled context-aware
  by the ExtractorAgent (host-speech -> host; listener-mail -> dropped). This
  module just supplies the host identity, alias map, and the pronoun list.
"""
from __future__ import annotations

import re
import unicodedata

_DEFAULT_HOST = "羊宮妃那"
_DEFAULT_HOST_ALIASES = [
    "羊宮", "ひな", "ヒナ", "ひなちゃん", "羊宮ちゃん", "羊ちゃん",
    "みゃーたん", "妃那", "ひなぴ", "ひなたん", "ヒナたん", "ひなたんさん",
]

HOST = _DEFAULT_HOST

CANONICAL_ENTITIES = [
    {
        "canonical": _DEFAULT_HOST,
        "type": "Person",
        "is_host": True,
        # nicknames / abbreviations heard in the show
        "aliases": list(_DEFAULT_HOST_ALIASES),
    },
]

# speaker-relative pronouns — never a stable graph entity on their own
PRONOUNS = {
    "私", "わたし", "わたくし", "あたし", "僕", "ぼく", "俺", "おれ",
    "自分", "あなた", "君", "きみ", "あなたたち",
}


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower().strip()
    return re.sub(r"[\s・･_\-ー]+", "", text)


# alias(normalized) -> canonical name
_ALIAS_TO_CANON: dict[str, str] = {}
for _e in CANONICAL_ENTITIES:
    _ALIAS_TO_CANON[_norm(_e["canonical"])] = _e["canonical"]
    for _a in _e["aliases"]:
        _ALIAS_TO_CANON[_norm(_a)] = _e["canonical"]

_TYPE_BY_CANON = {e["canonical"]: e["type"] for e in CANONICAL_ENTITIES}


def _rebuild_maps() -> None:
    """Rebuild alias/type lookup tables after CANONICAL_ENTITIES changes."""
    global _ALIAS_TO_CANON, _TYPE_BY_CANON
    _ALIAS_TO_CANON = {}
    for _e in CANONICAL_ENTITIES:
        _ALIAS_TO_CANON[_norm(_e["canonical"])] = _e["canonical"]
        for _a in _e["aliases"]:
            _ALIAS_TO_CANON[_norm(_a)] = _e["canonical"]
    _TYPE_BY_CANON = {e["canonical"]: e["type"] for e in CANONICAL_ENTITIES}


def set_host(name: str, aliases: list[str] | None = None, type: str = "Person") -> None:
    """Override the host identity per ingestion (e.g. 节目处理方案 drives a VTuber's
    name) so first-person speech / nicknames resolve to the RIGHT person, not the
    default 羊宮妃那. Pronoun list is unchanged (always resolves to current HOST).
    Call reset_host() afterwards. Process-local; safe in CLI ingestion."""
    global HOST, CANONICAL_ENTITIES
    HOST = name
    CANONICAL_ENTITIES = [
        {"canonical": name, "type": type, "is_host": True, "aliases": list(aliases or [])}
    ]
    _rebuild_maps()


def reset_host() -> None:
    global HOST, CANONICAL_ENTITIES
    HOST = _DEFAULT_HOST
    CANONICAL_ENTITIES = [
        {"canonical": _DEFAULT_HOST, "type": "Person", "is_host": True,
         "aliases": list(_DEFAULT_HOST_ALIASES)}
    ]
    _rebuild_maps()


def canonical_name(name: str) -> str | None:
    """Return the canonical name if `name` is a known alias/nickname, else None."""
    return _ALIAS_TO_CANON.get(_norm(name))


def canonical_type(canon: str) -> str:
    return _TYPE_BY_CANON.get(canon, "Person")


def is_pronoun(name: str) -> bool:
    return _norm(name) in {_norm(p) for p in PRONOUNS}


# clause/sentence tokens that mark an extracted "entity" as actually being a
# fragment of speech the extractor wrongly promoted to a node
# (e.g. "メールを読ませていただきます", "承知しました").
_FRAG_PUNCT = "。、，,！？!?「」（）()…"
_FRAG_VERB_END = (
    "ます", "ました", "ません", "でした", "したい", "したく", "なかった",
    "なる", "なった", "できる", "できた", "られる", "ている", "てる", "った",
    "だった", "しまう", "ましょう", "ください", "だろう", "でしょう",
)
_FRAG_PARTICLE = ("を", "へ", "から", "まで", "より")


def is_clause_fragment(name: str) -> bool:
    """Conservative heuristic: does this entity name read like a clause/sentence
    rather than a thing? Tuned to catch obvious multi-word clauses (punctuation,
    verb conjugations, case particles) while keeping plain nouns like 「漫画」「酒」.
    """
    n = (name or "").strip()
    if not n:
        return False
    if any(p in n for p in _FRAG_PUNCT):
        return True
    if len(n) >= 5 and any(n.endswith(s) for s in _FRAG_VERB_END):
        return True
    if len(n) >= 6 and any(p in n for p in _FRAG_PARTICLE):
        return True
    return False


def host_aliases() -> list[str]:
    for e in CANONICAL_ENTITIES:
        if e.get("is_host"):
            return e["aliases"]
    return []
