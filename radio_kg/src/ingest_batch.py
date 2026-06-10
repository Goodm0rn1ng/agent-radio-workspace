"""Unattended batch ingestion of every not-yet-ingested episode folder.

Unlike `python -m src.ingest --all` (which only sees `#N アーカイブ` folders),
this discovers ALL collections via `iter_collections` — numbered + un-numbered
lives, every program — and ingests whatever the graph hasn't seen yet. Per
folder it updates graph + chunk vectors (the pipeline's index node) AND the
summary vector DB, so retrieval indices don't drift behind the graph.

Run:
  .venv/bin/python -m src.ingest_batch                 # all not-yet-ingested
  .venv/bin/python -m src.ingest_batch --limit 3       # first N (smoke)
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from config.settings import settings  # noqa: E402
from src.agents.annotator_agent import AnnotatorAgent  # noqa: E402
from src.agents.doc_agent import parse_folder_metadata  # noqa: E402
from src.agents.extractor_agent import ExtractorAgent  # noqa: E402
from src.agents.inspector_agent import InspectorAgent  # noqa: E402
from src.agents.sync_agent import SyncAgent  # noqa: E402
from src.build_summary_db import SUMMARY_COLLECTION, summary_records_for_folder  # noqa: E402
from src.graph.ingestion_graph import Deps, build_ingestion_graph  # noqa: E402
from src.llm.client import LLMClient  # noqa: E402
from src.mcp_layer.graph_store import GraphStore  # noqa: E402
from src.mcp_layer.vector_store import VectorStore  # noqa: E402
from src.source_data import iter_collections  # noqa: E402


def discover_new(graph: GraphStore) -> list[Path]:
    data_dir = settings.abspath(settings.radio_data_dir)
    groups = iter_collections(data_dir, require_segments=True)
    done_eps = set(graph.ingested_episodes())
    done_lbl = set(graph.ingested_labels())

    def ingested(meta) -> bool:
        if meta.episode is not None:
            return meta.episode in done_eps
        return meta.episode_label in done_lbl

    new: list[Path] = []
    for name in sorted(groups):
        for folder in groups[name]:
            meta = parse_folder_metadata(str(folder), settings.program_name)
            if not ingested(meta):
                new.append(folder)
    return new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="only the first N new folders (smoke test)")
    ap.add_argument("--auto", default="confirm", choices=["confirm", "overwrite", "ignore"])
    args = ap.parse_args()

    ckpt_path = str(settings.abspath(settings.checkpoint_db))
    Path(ckpt_path).parent.mkdir(parents=True, exist_ok=True)
    llm = LLMClient()

    with GraphStore() as graph_store, VectorStore() as vector_store, \
            VectorStore(collection_name=SUMMARY_COLLECTION) as summary_store, \
            SqliteSaver.from_conn_string(ckpt_path) as ckpt:
        folders = discover_new(graph_store)
        if args.limit:
            folders = folders[: args.limit]
        print(f"discovered {len(folders)} not-yet-ingested folders", flush=True)

        deps = Deps(
            extractor=ExtractorAgent(llm, graph_store),
            inspector=InspectorAgent(llm, graph_store),
            sync=SyncAgent(graph_store),
            vector=vector_store,
            annotator=AnnotatorAgent(llm),
            auto_policy=args.auto,
        )
        graph = build_ingestion_graph(deps, ckpt)

        ok = failed = 0
        for i, folder in enumerate(folders, 1):
            label = folder.name
            thread_id = f"{label}::{uuid4().hex[:8]}"
            print(f"\n[{i}/{len(folders)}] === {label} ===", flush=True)
            try:
                config = {"configurable": {"thread_id": thread_id}}
                result = graph.invoke({"episode_dir": str(folder)}, config)
                # auto_policy resolves interrupts in-graph, so no resume loop needed;
                # guard anyway in case a payload slips through.
                while "__interrupt__" in result:
                    result = graph.invoke(Command(resume=["confirm"]), config)
                ids, docs, metas = summary_records_for_folder(folder, settings.program_name)
                summary_store.add_chunks(ids, docs, metas)
                print(f"  written: {result.get('written', [])}  "
                      f"summary_sections: {len(ids)}  "
                      f"dropped: {len(result.get('dropped', []))}", flush=True)
                ok += 1
            except Exception as e:
                failed += 1
                print(f"  FAILED: {e}\n{traceback.format_exc()}", flush=True)

        print(f"\n=== batch done: {ok} ok, {failed} failed ===", flush=True)
        print("graph stats:", graph_store.stats(), flush=True)

        # stamp unified version + fingerprint so drift vs the graph is visible
        from src import index_version as iv
        iv.stamp(iv.GRAPH, graph_store.ingested_labels())
        iv.stamp(iv.CHUNK, vector_store.distinct_labels())
        iv.stamp(iv.SUMMARY, summary_store.distinct_labels())
        print("index versions:", iv.status(
            graph_labels=graph_store.ingested_labels(),
            chunk_labels=vector_store.distinct_labels(),
            summary_labels=summary_store.distinct_labels()), flush=True)


if __name__ == "__main__":
    main()
