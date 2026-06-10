"""Semantic layer over the Neo4j MCP server.

SECURITY BOUNDARY (PRD 7.3): the LLM never authors Cypher. Only the fixed,
parameterized templates in this module produce Cypher; all variable data flows
through bound `$params`. Relationships use a single generic type `:REL` with the
semantic label carried in a property, so relation names can never be injected
into the query string. CDC versioning lives on the edge via start/end_epoch.
"""
from __future__ import annotations

import re
from typing import Optional

from config.settings import settings
from src.mcp_layer.client import McpStdioClient


def _norm(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip()).lower()


_HONORIFICS = ("さん", "ちゃん", "くん", "君", "さま", "様", "氏", "先生")


def _strip_honorific(name: str) -> str:
    n = _norm(name)
    for h in _HONORIFICS:
        if n.endswith(h):
            return n[: -len(h)]
    return n


def entity_id(etype: str, name: str) -> str:
    return f"{etype}:{_norm(name)}"


class GraphStore:
    def __init__(self):
        self._mcp = McpStdioClient(
            command=settings.mcp_neo4j_command,
            args=settings.mcp_neo4j_args.split(),
            env={
                "NEO4J_URI": settings.neo4j_uri,
                "NEO4J_USERNAME": settings.neo4j_username,
                "NEO4J_PASSWORD": settings.neo4j_password,
            },
        )

    def __enter__(self):
        self._mcp.start()
        self.init_schema()
        return self

    def __exit__(self, *exc):
        self._mcp.close()

    def ping(self) -> dict:
        """Cheap liveness probe for /api/health."""
        try:
            res = self._read("RETURN 1 AS ok", {})
            ok = bool(res) and (res[0].get("ok") == 1)
            return {"ok": ok, "alive": self._mcp.is_alive()}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "alive": self._mcp.is_alive(), "error": str(e)[:200]}

    # ── low-level (only place Cypher is constructed) ──────────────
    def _read(self, query: str, params: dict) -> list:
        res = self._mcp.call_tool("read_neo4j_cypher", {"query": query, "params": params})
        if res is None:
            return []
        return res if isinstance(res, list) else [res]

    def _write(self, query: str, params: dict) -> list:
        res = self._mcp.call_tool("write_neo4j_cypher", {"query": query, "params": params})
        if res is None:
            return []
        return res if isinstance(res, list) else [res]

    # ── predefined safe tools ─────────────────────────────────────
    def init_schema(self):
        # The MCP write tool rejects schema DDL; the uniqueness constraint is
        # provisioned out-of-band (see README). MERGE keys on eid regardless.
        return

    def search_nodes(self, term: str, limit: int = 10) -> list[dict]:
        """Entity linking lookup by name or alias substring (case-insensitive)."""
        q = (
            "MATCH (e:Entity) "
            "WHERE toLower(e.name) CONTAINS $t "
            "OR any(a IN e.aliases WHERE toLower(a) CONTAINS $t) "
            "RETURN e.eid AS eid, e.name AS name, e.type AS type, "
            "e.aliases AS aliases LIMIT $limit"
        )
        return self._read(q, {"t": _norm(term), "limit": limit})

    def neighbors(self, eids: list[str], hops: int = 2, limit: int = 60) -> list[dict]:
        """Read-only multi-hop expansion around anchor entities for GraphRAG.

        Returns each edge on bounded paths as a sourced triple. `hops` is a
        code-controlled literal (clamped 1..3), never user/LLM text, so the
        variable-length pattern stays injection-safe."""
        if not eids:
            return []
        hops = max(1, min(int(hops), 3))
        q = (
            f"MATCH path = (a:Entity)-[:REL*1..{hops}]-(b:Entity) "
            "WHERE a.eid IN $eids "
            "UNWIND relationships(path) AS r "
            "WITH DISTINCT r, startNode(r) AS s, endNode(r) AS o "
            "RETURN s.name AS subject, r.relation AS relation, o.name AS object, "
            "r.episode AS episode, r.start_time AS start_time, "
            "r.end_time AS end_time, r.citation AS citation, "
            "r.end_epoch AS end_epoch, r.confidence AS confidence "
            "LIMIT $limit"
        )
        return self._read(q, {"eids": eids, "limit": limit})

    def merge_node(self, name: str, etype: str, aliases: Optional[list[str]] = None) -> str:
        eid = entity_id(etype, name)
        self._write(
            "MERGE (e:Entity {eid: $eid}) "
            "ON CREATE SET e.name = $name, e.type = $type "
            "SET e.aliases = [a IN coalesce(e.aliases, []) WHERE NOT a IN $aliases] + $aliases",
            {"eid": eid, "name": name, "type": etype, "aliases": aliases or []},
        )
        return eid

    def get_active_relationship(self, subject_eid: str, relation: str) -> list[dict]:
        """Current (un-expired) edges of a semantic relation from subject."""
        q = (
            "MATCH (s:Entity {eid: $sid})-[r:REL {relation: $rel}]->(o:Entity) "
            "WHERE r.end_epoch IS NULL "
            "RETURN o.eid AS object_eid, o.name AS object_name, "
            "r.start_epoch AS start_epoch"
        )
        return self._read(q, {"sid": subject_eid, "rel": relation})

    def relationship_object_counts(
        self,
        subject_eid: str,
        relation_terms: Optional[list[str]] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Historical object frequency for InspectorAgent fact checking."""
        q = (
            "MATCH (s:Entity {eid: $sid})-[r:REL]->(o:Entity) "
            "WHERE $terms = [] OR any(t IN $terms WHERE r.relation CONTAINS t) "
            "RETURN o.eid AS object_eid, o.name AS object_name, o.type AS object_type, "
            "count(r) AS mentions, collect(DISTINCT r.citation)[0..3] AS citations "
            "ORDER BY mentions DESC LIMIT $limit"
        )
        return self._read(q, {"sid": subject_eid, "terms": relation_terms or [], "limit": limit})

    def expire_relationship(self, subject_eid: str, relation: str, object_eid: str, end_epoch: int):
        q = (
            "MATCH (s:Entity {eid: $sid})-[r:REL {relation: $rel}]->(o:Entity {eid: $oid}) "
            "WHERE r.end_epoch IS NULL SET r.end_epoch = $end"
        )
        self._write(q, {"sid": subject_eid, "rel": relation, "oid": object_eid, "end": end_epoch})

    def delete_relationship(self, subject_eid: str, relation: str, object_eid: str):
        q = (
            "MATCH (s:Entity {eid: $sid})-[r:REL {relation: $rel}]->(o:Entity {eid: $oid}) "
            "DELETE r"
        )
        self._write(q, {"sid": subject_eid, "rel": relation, "oid": object_eid})

    def redirect_entity(
        self,
        *,
        old_name: str,
        old_type: str,
        new_name: str,
        new_type: str,
        aliases: Optional[list[str]] = None,
    ) -> dict:
        """Merge a known bad entity into its canonical replacement.

        Used for post-hoc InspectorAgent repairs: all incoming/outgoing REL edges
        are moved to the canonical node while preserving edge properties.
        """
        old_eid = entity_id(old_type, old_name)
        new_eid = self.merge_node(new_name, new_type, aliases or [])
        if old_eid == new_eid:
            return {"old_eid": old_eid, "new_eid": new_eid, "incoming": 0, "outgoing": 0, "deleted": 0}

        def count_from(rows: list, key: str) -> int:
            if not rows:
                return 0
            row = rows[0]
            if key in row:
                return int(row[key] or 0)
            for value in row.values():
                if isinstance(value, bool):
                    return int(value)
                if isinstance(value, int):
                    return value
            return 0

        incoming = 0
        outgoing = 0
        for has_epoch in (True, False):
            epoch_predicate = "IS NOT NULL" if has_epoch else "IS NULL"
            merge_props = "{relation: r.relation, start_epoch: r.start_epoch}" if has_epoch else "{relation: r.relation}"
            rows = self._write(
                "MATCH (s:Entity)-[r:REL]->(old:Entity {eid: $old_eid}), "
                "(new:Entity {eid: $new_eid}) "
                f"WHERE r.start_epoch {epoch_predicate} "
                f"MERGE (s)-[nr:REL {merge_props}]->(new) "
                "SET nr += properties(r) "
                "WITH r, nr DELETE r "
                "RETURN count(nr) AS moved",
                {"old_eid": old_eid, "new_eid": new_eid},
            )
            incoming += count_from(rows, "moved")
            rows = self._write(
                "MATCH (old:Entity {eid: $old_eid})-[r:REL]->(o:Entity), "
                "(new:Entity {eid: $new_eid}) "
                f"WHERE r.start_epoch {epoch_predicate} "
                f"MERGE (new)-[nr:REL {merge_props}]->(o) "
                "SET nr += properties(r) "
                "WITH r, nr DELETE r "
                "RETURN count(nr) AS moved",
                {"old_eid": old_eid, "new_eid": new_eid},
            )
            outgoing += count_from(rows, "moved")

        rows = self._write(
            "MATCH (old:Entity {eid: $old_eid}) DETACH DELETE old "
            "RETURN count(old) AS deleted",
            {"old_eid": old_eid},
        )
        deleted = count_from(rows, "deleted")
        return {
            "old_eid": old_eid,
            "new_eid": new_eid,
            "incoming": incoming,
            "outgoing": outgoing,
            "deleted": deleted,
        }

    def merge_directed_relationship(
        self,
        subject_eid: str,
        relation: str,
        object_eid: str,
        *,
        start_epoch: Optional[int],
        program: str,
        episode: Optional[int],
        episode_label: str,
        broadcast_date: str,
        start_time: Optional[float],
        end_time: Optional[float],
        source_type: str,
        file_name: str,
        page: Optional[int],
        segment: Optional[int],
        confidence: float,
        citation: str,
    ):
        """Create a temporal, sourced edge. The relation label is data, not
        part of the query structure (single :REL type)."""
        q = (
            "MATCH (s:Entity {eid: $sid}), (o:Entity {eid: $oid}) "
            "MERGE (s)-[r:REL {relation: $rel, start_epoch: $start_epoch}]->(o) "
            "SET r.program = $program, r.episode = $episode, "
            "r.episode_label = $episode_label, r.broadcast_date = $broadcast_date, "
            "r.start_time = $start_time, r.end_time = $end_time, "
            "r.source_type = $source_type, r.file_name = $file_name, "
            "r.page = $page, r.segment = $segment, "
            "r.confidence = $confidence, r.citation = $citation"
        )
        self._write(
            q,
            {
                "sid": subject_eid,
                "oid": object_eid,
                "rel": relation,
                "start_epoch": start_epoch,
                "program": program,
                "episode": episode,
                "episode_label": episode_label,
                "broadcast_date": broadcast_date,
                "start_time": start_time,
                "end_time": end_time,
                "source_type": source_type,
                "file_name": file_name,
                "page": page,
                "segment": segment,
                "confidence": confidence,
                "citation": citation,
            },
        )

    # ── offline maintenance (graph hygiene scripts only) ─────────────
    def merge_entity(self, old_eid: str, canon_eid: str) -> None:
        """Fold one duplicate node into a canonical one: rewire every edge from
        `old_eid` onto `canon_eid` (preserving edge properties), then delete the
        emptied node. Used to collapse same-name-different-type fragments."""
        if old_eid == canon_eid:
            return
        # outgoing edges (skip ones that would become canon→canon self-loops)
        self._write(
            "MATCH (b:Entity {eid:$old})-[r:REL]->(x:Entity), (a:Entity {eid:$canon}) "
            "WHERE x.eid <> $canon "
            "MERGE (a)-[nr:REL {relation:r.relation, start_epoch:r.start_epoch}]->(x) "
            "SET nr += properties(r) DELETE r",
            {"old": old_eid, "canon": canon_eid})
        # incoming edges
        self._write(
            "MATCH (x:Entity)-[r:REL]->(b:Entity {eid:$old}), (a:Entity {eid:$canon}) "
            "WHERE x.eid <> $canon "
            "MERGE (x)-[nr:REL {relation:r.relation, start_epoch:r.start_epoch}]->(a) "
            "SET nr += properties(r) DELETE r",
            {"old": old_eid, "canon": canon_eid})
        self._write("MATCH (b:Entity {eid:$old}) DETACH DELETE b", {"old": old_eid})

    def detach_delete_entities(self, eids: list[str]) -> None:
        """Remove entities and their edges (long-tail noise pruning)."""
        if not eids:
            return
        self._write(
            "MATCH (e:Entity) WHERE e.eid IN $eids DETACH DELETE e", {"eids": eids})

    # ── read-only aggregation tools (for StatsAgent) ──────────────
    def count_by_type(self, etype: str) -> int:
        rows = self._read(
            "MATCH (e:Entity {type: $t}) RETURN count(e) AS n", {"t": etype})
        return rows[0]["n"] if rows else 0

    def count_relation(self, relation: str) -> int:
        rows = self._read(
            "MATCH ()-[r:REL {relation: $rel}]->() RETURN count(r) AS n",
            {"rel": relation})
        return rows[0]["n"] if rows else 0

    def type_distribution(self, limit: int = 20) -> list[dict]:
        return self._read(
            "MATCH (e:Entity) RETURN e.type AS type, count(e) AS n "
            "ORDER BY n DESC LIMIT $limit", {"limit": limit})

    def top_subjects_by_relation(self, relation: str, limit: int = 10) -> list[dict]:
        """Subjects ranked by how many `relation` edges they have (e.g. who
        posted the most mail)."""
        return self._read(
            "MATCH (s:Entity)-[r:REL {relation: $rel}]->() "
            "RETURN s.name AS name, s.type AS type, count(r) AS cnt "
            "ORDER BY cnt DESC, name LIMIT $limit",
            {"rel": relation, "limit": limit})

    def relation_per_episode(self, relation: str) -> list[dict]:
        return self._read(
            "MATCH ()-[r:REL {relation: $rel}]->() "
            "RETURN r.episode AS episode, count(r) AS n ORDER BY episode",
            {"rel": relation})

    def episode_broadcast_date(self, episode: int, program_hint: str = "") -> list[dict]:
        """Broadcast date(s) for an episode number, read from edge metadata
        (`broadcast_date`, derived from the folder name at ingest). Episode
        numbers repeat across programs, so results are grouped by program /
        label; an optional program substring narrows the match."""
        rows = self._read(
            "MATCH ()-[r:REL]->() WHERE r.episode = $ep "
            "AND r.broadcast_date IS NOT NULL AND r.broadcast_date <> '' "
            "RETURN DISTINCT r.program AS program, r.episode_label AS label, "
            "r.broadcast_date AS broadcast_date ORDER BY broadcast_date",
            {"ep": episode})
        if program_hint:
            h = program_hint.strip()
            narrowed = [r for r in rows
                        if h in (r.get("program") or "") or h in (r.get("label") or "")]
            return narrowed or rows
        return rows

    def resolve_entities(self, name: str, limit: int = 20) -> list[dict]:
        """Entity-link a name to graph nodes. Prefers exact (normalized) name
        matches; falls back to substring/alias hits. Same name may resolve to
        several nodes (e.g. a Person and a Listener) — all are returned so a
        dossier can union their edges."""
        hits = self.search_nodes(name, limit=limit)
        if not hits:
            return []
        # Match ignoring trailing honorifics, so a sender stored as "にららさん"
        # is found whether the user types "にらら" or "にららさん", and Person /
        # Listener nodes for the same referent are unioned.
        target = _strip_honorific(name)
        same = [h for h in hits if _strip_honorific(h.get("name", "")) == target]
        return same or hits

    def entity_records(self, eids: list[str], limit: int = 4000) -> list[dict]:
        """EVERY edge touching the given entities, in both directions, with full
        provenance — the complete, untruncated trace for an entity dossier
        (all of a sender's mail, everything a person did)."""
        if not eids:
            return []
        q = (
            "MATCH (s:Entity)-[r:REL]->(o:Entity) "
            "WHERE s.eid IN $eids OR o.eid IN $eids "
            "RETURN s.name AS subject, r.relation AS relation, o.name AS object, "
            "r.episode AS episode, r.episode_label AS episode_label, "
            "r.start_time AS start_time, r.end_time AS end_time, "
            "r.citation AS citation, r.source_type AS source_type, "
            "r.end_epoch AS end_epoch "
            "ORDER BY r.episode, r.start_time LIMIT $limit"
        )
        return self._read(q, {"eids": eids, "limit": limit})

    def list_by_type(self, etype: str, limit: int = 1000) -> list[dict]:
        return self._read(
            "MATCH (e:Entity {type: $t}) RETURN e.name AS name "
            "ORDER BY name LIMIT $limit", {"t": etype, "limit": limit})

    def ingested_episodes(self) -> list[int]:
        rows = self._read(
            "MATCH ()-[r:REL]->() WHERE r.episode IS NOT NULL "
            "RETURN DISTINCT r.episode AS ep ORDER BY ep",
            {},
        )
        return [r["ep"] for r in rows if r.get("ep") is not None]

    def ingested_labels(self) -> list[str]:
        """Distinct episode_label values — lets the console mark un-numbered
        episodes (live recordings without a #N) as already ingested."""
        rows = self._read(
            "MATCH ()-[r:REL]->() WHERE r.episode_label IS NOT NULL "
            "RETURN DISTINCT r.episode_label AS lbl",
            {},
        )
        return [r["lbl"] for r in rows if r.get("lbl")]

    def duplicate_name_groups(self) -> list[dict]:
        """Names that exist as more than one node (same name split across
        types / fragments). Drives the post-hoc same-name merge in repair_graph."""
        return self._read(
            "MATCH (e:Entity) "
            "WITH e.name AS name, collect(DISTINCT e.type) AS types, count(*) AS c "
            "WHERE c > 1 "
            "RETURN name, types, c AS count ORDER BY c DESC, name",
            {},
        )

    def stats(self) -> dict:
        nodes = self._read("MATCH (e:Entity) RETURN count(e) AS n", {})
        rels = self._read("MATCH ()-[r:REL]->() RETURN count(r) AS n", {})
        n = nodes[0]["n"] if nodes else 0
        r = rels[0]["n"] if rels else 0
        return {"entities": n, "relations": r}
