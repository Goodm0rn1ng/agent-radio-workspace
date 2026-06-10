"""SyncAgent: CDC incremental update into the graph.

- Nodes are MERGEd (idempotent).
- Multi-valued relations (言及する, 好き, ...) are simply merged as sourced edges.
- Single-valued relations (担当/負責人/役職 ...) are versioned: when a NEW object
  contradicts the current active object, that's a knowledge change. It is NOT
  auto-applied — it becomes a Conflict surfaced to the approval flow (PRD 4.3).
  Resolution:
    confirm   -> expire old edge (end_epoch=episode), add new (start_epoch=episode)
    overwrite -> delete old edge, add new
    ignore    -> keep old, drop new
"""
from __future__ import annotations

import re

from src.mcp_layer.graph_store import GraphStore, entity_id
from src.schema.models import Conflict, SourceRef, Triple

# relations that should hold a single current value (object change = a change event)
SINGLE_VALUED = re.compile(r"担当|負責|责任|負担|役職|リーダー|代表|主担|担任|负责")

_DATE_RE = re.compile(r"(\d{4})[-年/](\d{1,2})[-月/](\d{1,2})")


def epoch_for(src) -> int:
    """Non-null CDC epoch for an edge. Numbered episodes use the episode number;
    un-numbered lives (no #N) derive a stable epoch from their broadcast date
    (YYYYMMDD) so Neo4j can MERGE on a non-null start_epoch. Falls back to 0."""
    if getattr(src, "episode", None) is not None:
        return src.episode
    for text in (getattr(src, "broadcast_date", "") or "",
                 getattr(src, "episode_label", "") or ""):
        m = _DATE_RE.search(text)
        if m:
            y, mo, d = (int(x) for x in m.groups())
            return y * 10000 + mo * 100 + d
    return 0


class SyncAgent:
    def __init__(self, graph: GraphStore):
        self.graph = graph

    def is_single_valued(self, relation: str) -> bool:
        return bool(SINGLE_VALUED.search(relation))

    def _ensure_nodes(self, t: Triple) -> tuple[str, str]:
        s = self.graph.merge_node(t.subject.name, t.subject.type, t.subject.aliases)
        o = self.graph.merge_node(t.object.name, t.object.type, t.object.aliases)
        return s, o

    def _write_edge(self, sid: str, relation: str, oid: str, t: Triple):
        src = t.source
        self.graph.merge_directed_relationship(
            sid, relation, oid,
            start_epoch=epoch_for(src),
            program=src.program, episode=src.episode,
            episode_label=src.episode_label, broadcast_date=src.broadcast_date,
            start_time=src.start_time, end_time=src.end_time,
            source_type=src.source_type, file_name=src.file_name,
            page=src.page, segment=src.segment,
            confidence=t.confidence, citation=src.citation(),
        )

    def sync_triple(self, t: Triple) -> Conflict | None:
        """Apply one triple. Returns a Conflict if it needs human resolution."""
        sid, oid = self._ensure_nodes(t)

        if self.is_single_valued(t.relation):
            active = self.graph.get_active_relationship(sid, t.relation)
            other = [a for a in active if a["object_eid"] != oid]
            if other:
                return Conflict(
                    relation=t.relation,
                    subject_name=t.subject.name,
                    existing_object=other[0]["object_name"],
                    new_object=t.object.name,
                    new_source=t.source,
                )
        self._write_edge(sid, t.relation, oid, t)
        return None

    def resolve(self, conflict: Conflict, decision: str, new_triple: Triple):
        sid = entity_id(new_triple.subject.type, new_triple.subject.name)
        oid = entity_id(new_triple.object.type, new_triple.object.name)
        episode = new_triple.source.episode or 0

        if decision == "ignore":
            return
        if decision == "confirm":
            # close the history line on the old edge, open a new one
            for a in self.graph.get_active_relationship(sid, conflict.relation):
                if a["object_eid"] != oid:
                    self.graph.expire_relationship(
                        sid, conflict.relation, a["object_eid"], episode
                    )
            self._write_edge(sid, conflict.relation, oid, new_triple)
        elif decision == "overwrite":
            # discard the old line entirely (no history kept)
            for a in self.graph.get_active_relationship(sid, conflict.relation):
                if a["object_eid"] != oid:
                    self.graph.delete_relationship(
                        sid, conflict.relation, a["object_eid"]
                    )
            self._write_edge(sid, conflict.relation, oid, new_triple)
