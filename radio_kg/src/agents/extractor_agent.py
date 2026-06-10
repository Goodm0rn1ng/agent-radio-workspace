"""ExtractorAgent: chunk text -> [subject]-(relation)->[object] triples,
bound to the chunk's provenance, with entity-linking against the existing graph.

Disambiguation (PRD 3.2): every extracted entity is looked up via the graph
MCP `search_nodes` tool; a match adopts the canonical name/id so nicknames and
abbreviations collapse onto one node. Unresolvable pronouns are dropped (PRD 7.2).
"""
from __future__ import annotations

from src import canonical
from src.llm.client import LLMClient, LLMError
from src.mcp_layer.graph_store import GraphStore, entity_id
from src.schema.models import Chunk, Entity, SourceRef, Triple

ENTITY_TYPES = [
    "Person",        # 人物（声优、嘉宾、被提及者）
    "Listener",      # 来信者/听众投稿者（与 build_listener_db 共用，避免同名分裂）
    "Project",       # 企划
    "Segment",       # 节目环节/コーナー
    "Joke",          # 生草梗 / inside joke
    "Work",          # 作品（动画、歌曲、电影、节目）
    "Place",         # 地点
    "Organization",  # 组织/公司
    "Event",         # 事件
    "Other",
]
_VALID_TYPES = set(ENTITY_TYPES)

SYSTEM = """あなたはラジオ番組の書き起こしから知識グラフ用の三つ組を抽出する専門家です。
日本語のトーク内容から [主体]-(関係)->[客体] の三つ組を抽出してください。

# 話者と一人称の解決（重要・タグに従う）
本文は話者ごとに <Host_Section> / <Guest_Section name="…"> / <Listener_Section name="…"> で囲まれています。一人称・二人称は**囲んでいるタグで決定**し、推測しないこと。
- <Host_Section> 内の一人称（私／僕／わたし／自分 等）は「{host}」に解決する。
- <Guest_Section name="G"> 内の一人称は そのゲスト「G」に解決する。
- <Listener_Section name="L"> 内の一人称・二人称は、その投稿者「L」を指す。L が "不明" の場合は、その人物に依存する関係を作らない（破棄）。Host が投稿に応答した内容は Host_Section 側にある。
- タグ名・セクション見出し自体は entity にしない。{guest_note}
- 「{host}」の愛称・略称（例: {aliases}）は entity 名を「{host}」に正規化する。
- 囲みタグで話者を特定できない裸の代名詞（あの人／それ 等）はそのまま entity 名にしない。

# 一般ルール
- entity の type は次から選ぶ: {types}
- relation は短い日本語の述語にする（例: 主持する, 出演する, 好き, 担当する, 飼っている, 言及する）。
- 文脈で特定できない曖昧な指示語は無視し、三つ組を作らない（ゴミデータ防止）。
- 本文から読み取れる事実のみを抽出する。憶測しない。
- 各 entity に既知の別名・愛称があれば aliases に入れる。confidence は 0.0〜1.0。

必ず次の JSON のみを出力する:
{{"triples": [{{"subject": {{"name": "...", "type": "...", "aliases": ["..."]}},
  "relation": "...",
  "object": {{"name": "...", "type": "...", "aliases": ["..."]}},
  "confidence": 0.9}}]}}
三つ組が無ければ {{"triples": []}} を返す。"""


class ExtractorAgent:
    def __init__(self, llm: LLMClient, graph: GraphStore):
        self.llm = llm
        self.graph = graph

    def extract(self, chunk: Chunk) -> tuple[list[Triple], list[str]]:
        """Return (resolved triples, dropped notes)."""
        guest_note = ""
        label = chunk.source.episode_label or ""
        if "ゲスト" in label:
            guest = label.split("ゲスト:")[-1].split()[0] if "ゲスト:" in label else ""
            guest_note = (
                f"この回にはゲスト「{guest}」がいます。ゲストの発言の一人称はゲスト本人を指し、"
                f"「{canonical.HOST}」に結びつけないこと。" if guest else
                "この回にはゲストがいます。ゲスト発言の一人称をパーソナリティに結びつけないこと。"
            )
        system = SYSTEM.format(
            types=", ".join(ENTITY_TYPES),
            host=canonical.HOST,
            aliases="、".join(canonical.host_aliases()),
            guest_note=guest_note,
        )
        body = chunk.annotated_text or chunk.text
        try:
            data = self.llm.complete_json(system, body)
        except LLMError:
            return [], [f"{chunk.chunk_id}: extraction_failed"]

        triples: list[Triple] = []
        dropped: list[str] = []
        for raw in data.get("triples", []):
            t = self._build_triple(raw, chunk.source)
            if t is None:
                dropped.append(f"{chunk.chunk_id}: {raw}")
                continue
            triples.append(t)
        return triples, dropped

    def _build_triple(self, raw: dict, source: SourceRef) -> Triple | None:
        try:
            subj = raw["subject"]
            obj = raw["object"]
            relation = (raw.get("relation") or "").strip()
            if not relation or not subj.get("name") or not obj.get("name"):
                return None
            # bare pronoun the LLM failed to resolve -> too ambiguous, drop (PRD 6.2)
            if canonical.is_pronoun(subj["name"]) or canonical.is_pronoun(obj["name"]):
                return None
            # a clause/sentence the LLM wrongly promoted to an entity (the main
            # source of degree-1 "Other" long-tail noise) -> drop the triple
            if canonical.is_clause_fragment(subj["name"]) or canonical.is_clause_fragment(obj["name"]):
                return None
            s_ent = self._resolve(subj)
            o_ent = self._resolve(obj)
            return Triple(
                subject=s_ent,
                relation=relation,
                object=o_ent,
                confidence=float(raw.get("confidence", 0.8)),
                source=source,
                subject_id=self._existing_id(s_ent),
                object_id=self._existing_id(o_ent),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def _resolve(self, ent: dict) -> Entity:
        """Entity linking: canonicalize known nicknames, then prefer an existing
        canonical node if name/alias matches."""
        name = ent["name"].strip()
        etype = ent.get("type") or "Other"
        aliases = [a for a in ent.get("aliases", []) if a]
        # deterministic nickname -> canonical (e.g. 羊宮 / ひな -> 羊宮妃那)
        canon = canonical.canonical_name(name)
        if canon and canon != name:
            aliases = list(dict.fromkeys(aliases + [name]))
            name, etype = canon, canonical.canonical_type(canon)
        # adopt an existing node's type so the same name never splits across types
        for hit in self.graph.search_nodes(name):
            if hit["name"] == name or name in (hit.get("aliases") or []):
                return Entity(name=hit["name"], type=hit["type"], aliases=aliases)
        # creating a fresh node: keep the LLM out of inventing types off-whitelist
        if etype not in _VALID_TYPES:
            etype = "Other"
        return Entity(name=name, type=etype, aliases=aliases)

    def _existing_id(self, ent: Entity) -> str | None:
        eid = entity_id(ent.type, ent.name)
        hits = self.graph.search_nodes(ent.name)
        for h in hits:
            if h["eid"] == eid:
                return eid
        return None
