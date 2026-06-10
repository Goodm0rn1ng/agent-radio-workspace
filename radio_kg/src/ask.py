"""CLI entrypoint for GraphRAG Q&A.

Examples:
  python -m src.ask "羊宮妃那はどの事務所に所属している？"
  python -m src.ask --show-context "番組のタイトル候補は何だった？"
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.agents.qa_agent import QAAgent  # noqa: E402
from src.graph.qa_graph import QADeps, build_qa_graph  # noqa: E402
from src.llm.client import LLMClient  # noqa: E402
from src.mcp_layer.graph_store import GraphStore  # noqa: E402
from src.mcp_layer.vector_store import VectorStore  # noqa: E402
from src.retrieval.retrievers import GraphRetriever, VectorRetriever  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question")
    ap.add_argument("--provider", help="override LLM provider")
    ap.add_argument("--hops", type=int, default=2)
    ap.add_argument("--two-stage", action="store_true", help="coarse summary route -> fine window")
    ap.add_argument("--show-context", action="store_true")
    args = ap.parse_args()

    llm = LLMClient(provider=args.provider)

    # route: statistics/aggregation questions -> StatsAgent tool
    from src.agents.stats_agent import StatsAgent
    with GraphStore() as gs:
        stats = StatsAgent(llm, gs)
        if stats.is_stats(args.question):
            r = stats.answer(args.question)
            print(f"\n❓ {args.question}\n   路由: 统计聚合 (tool={r.get('tool')})")
            print(f"\n📊 {r['answer']}\n")
            return

    if args.two_stage:
        from src.build_summary_db import SUMMARY_COLLECTION
        from src.graph.qa_graph2 import QADeps2, build_two_stage_qa_graph
        from src.retrieval.two_stage import TwoStageRetriever
        with GraphStore() as graph_store, VectorStore() as chunk_store, \
                VectorStore(collection_name=SUMMARY_COLLECTION) as summary_store:
            two = TwoStageRetriever(summary_store, chunk_store, graph_store, hops=args.hops)
            graph = build_two_stage_qa_graph(QADeps2(qa=QAAgent(llm), two_stage=two))
            result = graph.invoke({"question": args.question})
        dbg = result.get("debug", {})
        print(f"\n❓ {args.question}")
        print(f"   锚点: {result.get('anchors')}  检索式: {result.get('search_query','')}")
        if result.get("search_queries"):
            print(f"   扩展检索式: {' | '.join(result.get('search_queries', []))}")
        print(f"   两段式: path={dbg.get('path')} 摘要线索={len(dbg.get('summary_clues',[]))} "
              f"(best dist={dbg.get('best_summary_distance')}) 窗口切片={dbg.get('n_window')} "
              f"图谱={dbg.get('n_graph')} 兜底直检={dbg.get('n_fallback')} → 融合 {len(result.get('fused',[]))}")
        if args.show_context:
            print("\n--- 融合上下文 ---\n" + result.get("context", ""))
        print(f"\n💬 {result.get('answer', '')}\n")
        return

    with GraphStore() as graph_store, VectorStore() as vector_store:
        deps = QADeps(
            qa=QAAgent(llm),
            graph_retriever=GraphRetriever(graph_store),
            vector_retriever=VectorRetriever(vector_store),
            hops=args.hops,
        )
        graph = build_qa_graph(deps)
        result = graph.invoke({"question": args.question})

    print(f"\n❓ {args.question}")
    if result.get("anchors"):
        print(f"   锚点: {result['anchors']}  意图: {result.get('intent','')}")
    if result.get("search_query") and result.get("search_query") != args.question:
        print(f"   向量检索式: {result['search_query']}")
    if result.get("search_queries"):
        print(f"   扩展检索式: {' | '.join(result.get('search_queries', []))}")
    print(f"   检索: 图谱 {len(result.get('graph_hits', []))} 条 / "
          f"向量 {len(result.get('vector_hits', []))} 条 → 融合 {len(result.get('fused', []))} 条")
    if args.show_context:
        print("\n--- 融合上下文 ---")
        print(result.get("context", ""))
    print(f"\n💬 {result.get('answer', '')}\n")


if __name__ == "__main__":
    main()
