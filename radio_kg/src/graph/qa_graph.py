"""LangGraph Q&A state machine implementing GraphRAG hybrid retrieval (PRD 4.1).

         ┌─> graph_retrieve ─┐
analyze ─┤                   ├─> fuse ─> generate
         └─> vector_retrieve ┘
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from config.settings import settings
from src.agents.qa_agent import QAAgent
from src.retrieval.fusion import build_context, expand_citation_refs, fuse
from src.retrieval.retrievers import GraphRetriever, Passage, VectorRetriever


class QAState(TypedDict, total=False):
    question: str
    anchors: list
    intent: str
    search_query: str
    search_queries: list
    graph_hits: list      # list[Passage]
    vector_hits: list     # list[Passage]
    fused: list           # list[Passage]
    context: str
    answer: str


@dataclass
class QADeps:
    qa: QAAgent
    graph_retriever: GraphRetriever
    vector_retriever: VectorRetriever
    hops: int = 2
    vector_k: int = settings.qa_vector_k
    top_n: int = settings.qa_top_n


def build_qa_graph(deps: QADeps):
    def analyze(state: QAState) -> dict:
        return deps.qa.analyze(state["question"])

    def graph_retrieve(state: QAState) -> dict:
        query = " ".join(state.get("search_queries") or [state.get("search_query") or state["question"]])
        return {
            "graph_hits": deps.graph_retriever.retrieve(
                state["anchors"], hops=deps.hops, query=query
            )
        }

    def vector_retrieve(state: QAState) -> dict:
        return {
            "vector_hits": deps.vector_retriever.retrieve(
                state.get("search_queries") or [state.get("search_query") or state["question"]],
                k=deps.vector_k,
            )
        }

    def fuse_node(state: QAState) -> dict:
        passages: list[Passage] = fuse(
            state.get("vector_hits", []), state.get("graph_hits", []), top_n=deps.top_n
        )
        return {"fused": passages, "context": build_context(passages)}

    def generate(state: QAState) -> dict:
        answer = deps.qa.answer(state["question"], state["context"])
        return {"answer": expand_citation_refs(answer, state.get("fused", []))}

    g = StateGraph(QAState)
    g.add_node("analyze", analyze)
    g.add_node("graph_retrieve", graph_retrieve)
    g.add_node("vector_retrieve", vector_retrieve)
    g.add_node("fuse", fuse_node)
    g.add_node("generate", generate)
    g.add_edge(START, "analyze")
    g.add_edge("analyze", "graph_retrieve")
    g.add_edge("analyze", "vector_retrieve")
    g.add_edge("graph_retrieve", "fuse")
    g.add_edge("vector_retrieve", "fuse")
    g.add_edge("fuse", "generate")
    g.add_edge("generate", END)
    return g.compile()
