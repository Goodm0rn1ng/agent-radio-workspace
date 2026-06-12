"""Two-stage QA state machine (coarse summary route -> fine window fetch).

analyze -> retrieve2 (summary route + window fetch + graph + fallback + fuse) -> generate
The multi-step retrieval lives inside one node to keep the flow deterministic
and easy to test (no fragile conditional edges).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from config.settings import settings
from src.agents.qa_agent import QAAgent
from src.retrieval.fusion import build_context, expand_citation_refs
from src.retrieval.two_stage import TwoStageRetriever


class QA2State(TypedDict, total=False):
    question: str
    history: list
    anchors: list
    intent: str
    search_query: str
    search_queries: list
    fused: list
    context: str
    answer: str
    debug: dict


@dataclass
class QADeps2:
    qa: QAAgent
    two_stage: TwoStageRetriever
    top_n: int = settings.qa_top_n


def build_two_stage_qa_graph(deps: QADeps2):
    def analyze(state: QA2State) -> dict:
        # precomputed upstream (run in parallel with routing) — skip the LLM call
        if state.get("search_queries"):
            return {}
        return deps.qa.analyze(state["question"])

    def retrieve2(state: QA2State) -> dict:
        fused, dbg = deps.two_stage.retrieve(
            state["question"], state.get("anchors", []),
            state.get("search_queries") or [state.get("search_query", "")],
            top_n=deps.top_n)
        return {"fused": fused, "context": build_context(fused), "debug": dbg.__dict__}

    def generate(state: QA2State) -> dict:
        if settings.qa_structured_answer:
            # fact -> source_id -> citation, with post-hoc verification so the
            # model cannot cite a passage that wasn't retrieved or state an
            # unsupported claim.
            r = deps.qa.answer_structured(
                state["question"], state.get("fused", []), state.get("history"))
            dbg = dict(state.get("debug", {}))
            dbg["facts_kept"] = len(r.get("facts", []))
            dbg["facts_dropped"] = r.get("dropped", 0)
            return {"answer": r["answer"], "debug": dbg}
        answer = deps.qa.answer(state["question"], state["context"], state.get("history"))
        return {"answer": expand_citation_refs(answer, state.get("fused", []))}

    g = StateGraph(QA2State)
    g.add_node("analyze", analyze)
    g.add_node("retrieve2", retrieve2)
    g.add_node("generate", generate)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "retrieve2")
    g.add_edge("retrieve2", "generate")
    g.add_edge("generate", END)
    return g.compile()
