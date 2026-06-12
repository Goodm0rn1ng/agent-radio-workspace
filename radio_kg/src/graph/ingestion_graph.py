"""LangGraph ingestion state machine: parse -> index -> extract -> inspect -> sync.

High-risk inspection doubts and single-valued conflicts are no longer paused
for human approval: a second LLM pass (InspectorAgent.adjudicate /
adjudicate_conflicts) re-judges each one with the transcript excerpt + graph
history and the ruling is applied directly. An `auto_policy` still
short-circuits conflict adjudication for unattended batch ingestion.
"""
from __future__ import annotations

from dataclasses import dataclass

from langgraph.graph import END, START, StateGraph

from src import canonical
from src.agents.annotator_agent import AnnotatorAgent
from src.agents.doc_agent import build_chunks
from src.agents.extractor_agent import ExtractorAgent
from src.agents.inspector_agent import InspectorAgent
from src.agents.sync_agent import SyncAgent
from src.mcp_layer.vector_store import VectorStore
from src.schema.models import Chunk, Conflict, Triple
from src.schema.models import PipelineState


@dataclass
class Deps:
    extractor: ExtractorAgent
    inspector: InspectorAgent
    sync: SyncAgent
    vector: VectorStore
    annotator: "AnnotatorAgent | None" = None
    auto_policy: str | None = None  # confirm | overwrite | ignore | None


def build_ingestion_graph(deps: Deps, checkpointer):
    def parse_node(state: PipelineState) -> dict:
        chunks = build_chunks(state["episode_dir"])
        # Session Constants locked at episode init from folder metadata
        base = chunks[0].source if chunks else None
        label = base.episode_label if base else ""
        guest = ""
        if "ゲスト:" in label:
            guest = label.split("ゲスト:")[-1].split()[0]
        return {
            "chunks": [c.model_dump() for c in chunks],
            "host": canonical.HOST,
            "guest": guest,
        }

    def annotate_node(state: PipelineState) -> dict:
        if deps.annotator is None:
            return {}
        host, guest = state.get("host", canonical.HOST), state.get("guest", "")
        out, listeners = [], []
        for c in state["chunks"]:
            chunk = Chunk(**c)
            annotated, names = deps.annotator.annotate(chunk.text, host, guest)
            chunk.annotated_text = annotated
            out.append(chunk.model_dump())
            listeners += names
        return {"annotated_chunks": out, "listeners": sorted(set(listeners))}

    def index_node(state: PipelineState) -> dict:
        chunks = [Chunk(**c) for c in state["chunks"]]
        ids, docs, metas = [], [], []
        for c in chunks:
            ids.append(c.chunk_id)
            docs.append(c.retrieval_text or c.text)
            metas.append(
                {
                    "episode": c.source.episode,
                    "episode_label": c.source.episode_label,
                    "broadcast_date": c.source.broadcast_date,
                    "start_time": c.source.start_time,
                    "end_time": c.source.end_time,
                    "citation": c.source.citation(),
                }
            )
        deps.vector.add_chunks(ids, docs, metas)
        return {}

    def extract_node(state: PipelineState) -> dict:
        source = state.get("annotated_chunks") or state["chunks"]
        triples, dropped = [], []
        for c in source:
            ts, dp = deps.extractor.extract(Chunk(**c))
            triples += [t.model_dump() for t in ts]
            dropped += dp
        return {"triples": triples, "dropped": dropped}

    def inspect_node(state: PipelineState) -> dict:
        triples = [Triple(**t) for t in state["triples"]]
        checked, issues, dropped = [], [], []
        pending = []
        for result in deps.inspector.inspect_batch(triples):
            issues += [i.model_dump() for i in result.issues]
            if result.review_required:
                pending.append(result)
                continue
            if result.triple is not None:
                checked.append(result.triple.model_dump())

        if pending:
            # No human interrupt: a second LLM pass re-judges each doubt with
            # the full transcript excerpt + graph history and the ruling is
            # applied directly (审批队列废除 — 上下文终审后直接入库).
            rulings = deps.inspector.adjudicate(pending, state["chunks"])
            for result, (decision, final, reason) in zip(pending, rulings):
                issue = result.issues[-1]
                issue.severity = f"adjudicated_{decision}"
                issue.reason = reason or issue.reason
                issues.append(issue.model_dump())
                if decision == "drop":
                    dropped.append(
                        f"inspection_dropped: {issue.original_name} ({reason})"
                    )
                elif final is not None:
                    checked.append(final.model_dump())

        return {
            "inspected_triples": checked,
            "inspection_issues": issues,
            "dropped": dropped,
        }

    def sync_node(state: PipelineState) -> dict:
        triples = [Triple(**t) for t in state.get("inspected_triples", state["triples"])]
        pending: list[tuple[Conflict, Triple]] = []
        written = 0
        for t in triples:
            c = deps.sync.sync_triple(t)
            if c is None:
                written += 1
            else:
                pending.append((c, t))

        if not pending:
            return {"written": [f"{written} edges"]}

        if deps.auto_policy:
            for c, t in pending:
                deps.sync.resolve(c, deps.auto_policy, t)
            return {
                "written": [f"{written} edges, {len(pending)} auto-{deps.auto_policy}"],
                "conflicts": [c.model_dump() for c, _ in pending],
            }

        # No human interrupt: contextual LLM ruling per conflict
        # (confirm=保留历史线 / overwrite=覆盖旧值 / ignore=丢弃新值), applied directly.
        rulings = deps.inspector.adjudicate_conflicts(pending, state["chunks"])
        out_conflicts = []
        for (c, t), (decision, reason) in zip(pending, rulings):
            deps.sync.resolve(c, decision, t)
            d = c.model_dump()
            d["resolution"] = decision
            d["resolution_reason"] = reason
            out_conflicts.append(d)
        return {
            "written": [f"{written} edges, {len(pending)} adjudicated"],
            "conflicts": out_conflicts,
        }

    g = StateGraph(PipelineState)
    g.add_node("parse", parse_node)
    g.add_node("annotate", annotate_node)
    g.add_node("index", index_node)
    g.add_node("extract", extract_node)
    g.add_node("inspect", inspect_node)
    g.add_node("sync", sync_node)
    g.add_edge(START, "parse")
    g.add_edge("parse", "annotate")
    g.add_edge("annotate", "index")
    g.add_edge("index", "extract")
    g.add_edge("extract", "inspect")
    g.add_edge("inspect", "sync")
    g.add_edge("sync", END)
    return g.compile(checkpointer=checkpointer)
