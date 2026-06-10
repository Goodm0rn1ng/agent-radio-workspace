"""Performance benchmark for the PRD 7.1 non-functional requirements.

Two targets, measured against the live local stack (Homebrew Neo4j via MCP +
local Chroma/e5):
  1. MCP 单次工具调用延迟 < 200ms  — per-call Neo4j-MCP round-trips.
  2. 混合检索（向量 + 图 2 跳 + 融合）总耗时 < 2s（不含 LLM 生成）
     — TwoStageRetriever.retrieve(), with question analysis (LLM) done OUTSIDE
       the timed section.

Run:  .venv/bin/python -m src.bench_perf
"""
from __future__ import annotations

import statistics
import sys
import time
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.agents.qa_agent import QAAgent  # noqa: E402
from src.build_summary_db import SUMMARY_COLLECTION  # noqa: E402
from src.llm.client import LLMClient  # noqa: E402
from src.mcp_layer.graph_store import GraphStore  # noqa: E402
from src.mcp_layer.vector_store import VectorStore  # noqa: E402
from src.retrieval.two_stage import TwoStageRetriever  # noqa: E402

QUESTIONS = [
    "节目名为什么叫こもれびじかん？",
    "羊宮妃那所属哪个事务所？",
    "第1期的来信人都有谁？",
    "她聊过哪些电影？",
    "羊宮妃那提到过的吉他相关的话题有哪些？",
    "嘉宾桜谷理子在第几期出现？",
]
MCP_TERMS = ["羊宮妃那", "青二プロダクション", "こもれびじかん", "桜谷理子"]


def _pct(xs: list[float], p: float) -> float:
    if not xs:
        return 0.0
    xs = sorted(xs)
    i = min(len(xs) - 1, int(round((p / 100) * (len(xs) - 1))))
    return xs[i]


def _report(name: str, ms: list[float], target_ms: float) -> None:
    ok = sum(1 for x in ms if x <= target_ms)
    print(f"\n[{name}]  n={len(ms)}  目标 ≤ {target_ms:.0f}ms")
    print(f"  mean={statistics.mean(ms):.1f}ms  p50={_pct(ms,50):.1f}ms  "
          f"p95={_pct(ms,95):.1f}ms  max={max(ms):.1f}ms")
    print(f"  达标(≤{target_ms:.0f}ms): {ok}/{len(ms)}  "
          f"{'✓ PASS' if ok == len(ms) else ('~ p95达标' if _pct(ms,95) <= target_ms else '✗ FAIL')}")


def bench_mcp(graph: GraphStore, reps: int = 20) -> None:
    # warm up (cold MCP / query plan cache)
    eids = []
    for t in MCP_TERMS:
        hits = graph.search_nodes(t)
        eids += [h["eid"] for h in hits[:2]]
    eids = eids[:4] or None

    search_ms, neigh_ms = [], []
    for i in range(reps):
        term = MCP_TERMS[i % len(MCP_TERMS)]
        t0 = time.perf_counter()
        graph.search_nodes(term)
        search_ms.append((time.perf_counter() - t0) * 1000)
        if eids:
            t0 = time.perf_counter()
            graph.neighbors(eids, hops=2)
            neigh_ms.append((time.perf_counter() - t0) * 1000)

    print("\n================ MCP 单次调用延迟 (PRD 7.1, 目标 <200ms) ================")
    _report("search_nodes (read_neo4j_cypher)", search_ms, 200)
    if neigh_ms:
        _report("neighbors 2-hop (read_neo4j_cypher)", neigh_ms, 200)


def bench_retrieval(qa: QAAgent, retriever: TwoStageRetriever) -> None:
    print("\n================ 混合检索耗时 (PRD 7.1, 目标 <2s, 不含 LLM) ================")
    # warm up the embedder + caches once (untimed)
    a = qa.analyze(QUESTIONS[0])
    retriever.retrieve(QUESTIONS[0], a["anchors"], search_query=a["search_queries"],
                       top_n=settings.qa_top_n)

    totals = []
    for q in QUESTIONS:
        a = qa.analyze(q)                      # LLM — OUTSIDE the timed section
        t0 = time.perf_counter()
        fused, dbg = retriever.retrieve(
            q, a["anchors"], search_query=a["search_queries"], top_n=settings.qa_top_n)
        dt = (time.perf_counter() - t0) * 1000
        totals.append(dt)
        print(f"  {dt:7.1f}ms  [{dbg.path:14}] graph={dbg.n_graph:>3} fused={len(fused):>3}  «{q}»")
    _report("hybrid retrieve() total", totals, 2000)


def main() -> None:
    print(f"provider={settings.llm_provider} model={settings.default_model} "
          f"embed={settings.vector_embedding_model}")
    with ExitStack() as stack:
        graph = stack.enter_context(GraphStore())
        chunks = stack.enter_context(VectorStore())
        summary = stack.enter_context(VectorStore(collection_name=SUMMARY_COLLECTION))
        qa = QAAgent(LLMClient())
        retriever = TwoStageRetriever(summary, chunks, graph)
        bench_mcp(graph)
        bench_retrieval(qa, retriever)
    print("\n完成。注：首次调用含模型/连接预热，已在测量前 warmup。")


if __name__ == "__main__":
    main()
