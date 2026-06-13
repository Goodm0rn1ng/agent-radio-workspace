"""Two-stage QA state machine (coarse summary route -> fine window fetch).

analyze -> retrieve2 (summary route + window fetch + graph + fallback + fuse) -> generate
The multi-step retrieval lives inside one node to keep the flow deterministic
and easy to test (no fragile conditional edges).

Recall hardening (borrowed from mature RAG designs):
- enumeration/aggregation questions ("哪些/全部/清单/时间轴/最常…") retrieve
  wide from the start — top-k routing structurally under-recalls them;
- corrective retry (CRAG-style): an abstained answer triggers one broadened
  re-retrieval + regeneration before giving up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from config.settings import settings
from src.agents.qa_agent import QAAgent
from src.retrieval.fusion import build_context, expand_citation_refs
from src.retrieval.two_stage import TwoStageRetriever

# 枚举/跨期聚合意图：需要广召回而非 top-k 精召回
_ENUM_RE = re.compile(
    r"哪些|都有|都聊|都说|都推荐|全部|完整|清单|列出|列举|时间轴|時間軸|演变|演變"
    r"|多少次|几次|幾次|前\s*\d|最常|排名|总共|總共|一共|历次|歷次|どんな|すべて|全て|一覧"
)

_ABSTAIN_MARK = "資料からは確認できません"


def _is_abstain(answer: str) -> bool:
    return not answer or _ABSTAIN_MARK in answer or "资料からは確認できません" in answer


class QA2State(TypedDict, total=False):
    question: str
    history: list
    anchors: list
    intent: str
    search_query: str
    search_queries: list
    intent_enum: bool
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
    def _queries(state: QA2State) -> list:
        return state.get("search_queries") or [state.get("search_query", "")]

    def _retrieve(state: QA2State, wide: bool):
        return deps.two_stage.retrieve(
            state["question"], state.get("anchors", []), _queries(state),
            top_n=deps.top_n, wide=wide)

    def _generate(state: QA2State) -> dict:
        """One generation pass over state['fused']; returns {answer, debug}."""
        dbg = dict(state.get("debug", {}))
        if settings.qa_structured_answer:
            # fact -> source_id -> citation, with post-hoc verification so the
            # model cannot cite a passage that wasn't retrieved or state an
            # unsupported claim. Enumeration questions get an exhaustiveness
            # directive so distinct items are not collapsed into one fact.
            r = deps.qa.answer_structured(
                state["question"], state.get("fused", []), state.get("history"),
                enumerate_mode=state.get("intent_enum", False))
            dbg["facts_kept"] = len(r.get("facts", []))
            dbg["facts_dropped"] = r.get("dropped", 0)
            return {"answer": r["answer"], "debug": dbg}
        answer = deps.qa.answer(state["question"], state["context"], state.get("history"))
        return {"answer": expand_citation_refs(answer, state.get("fused", [])), "debug": dbg}

    def analyze(state: QA2State) -> dict:
        # precomputed upstream (run in parallel with routing) — skip the LLM call
        if state.get("search_queries"):
            return {"intent_enum": bool(_ENUM_RE.search(state.get("question", "")))}
        out = deps.qa.analyze(state["question"])
        out["intent_enum"] = bool(_ENUM_RE.search(state.get("question", "")))
        return out

    def retrieve2(state: QA2State) -> dict:
        # enumeration / cross-episode aggregation under-recalls with top-k
        # routing — widen the recall budget from the start for those.
        wide = state.get("intent_enum", False)
        fused, dbg = _retrieve(state, wide=wide)
        d = dict(dbg.__dict__)
        d["wide"] = wide
        return {"fused": fused, "context": build_context(fused), "debug": d}

    def generate(state: QA2State) -> dict:
        out = _generate(state)
        # corrective retry (CRAG-style): an abstained answer triggers one
        # broadened re-retrieval + regeneration before giving up.
        if _is_abstain(out["answer"]) and not state.get("debug", {}).get("wide"):
            fused, dbg = _retrieve(state, wide=True)
            d = dict(dbg.__dict__)
            d["wide"] = True
            d["corrective_retry"] = True
            retry = _generate({**state, "fused": fused,
                               "context": build_context(fused), "debug": d})
            if not _is_abstain(retry["answer"]):
                return retry
        return out

    g = StateGraph(QA2State)
    g.add_node("analyze", analyze)
    g.add_node("retrieve2", retrieve2)
    g.add_node("generate", generate)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "retrieve2")
    g.add_edge("retrieve2", "generate")
    g.add_edge("generate", END)
    return g.compile()
