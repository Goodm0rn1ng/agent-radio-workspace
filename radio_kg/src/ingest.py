"""CLI entrypoint for incremental ingestion.

Examples:
  python -m src.ingest --episode 1                 # ingest episode #1 (アーカイブ)
  python -m src.ingest --all --auto confirm        # batch, auto-resolve conflicts
  python -m src.ingest --dir "../hina_radio/<...>"  # a specific folder
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from langgraph.checkpoint.sqlite import SqliteSaver  # noqa: E402
from langgraph.types import Command  # noqa: E402

from config.settings import settings  # noqa: E402
from src.agents.annotator_agent import AnnotatorAgent  # noqa: E402
from src.agents.extractor_agent import ExtractorAgent  # noqa: E402
from src.agents.inspector_agent import InspectorAgent  # noqa: E402
from src.agents.sync_agent import SyncAgent  # noqa: E402
from src.graph.ingestion_graph import Deps, build_ingestion_graph  # noqa: E402
from src.llm.client import LLMClient  # noqa: E402
from src.mcp_layer.graph_store import GraphStore  # noqa: E402
from src.mcp_layer.vector_store import VectorStore  # noqa: E402
from src.source_data import episode_number, iter_episode_folders  # noqa: E402


def select_folders(episode: int | None, want_all: bool, explicit: str | None) -> list[str]:
    if explicit:
        return [explicit]
    data_dir = settings.abspath(settings.radio_data_dir)
    archives = iter_episode_folders(
        data_dir,
        archives_only=True,
        require_segments=True,
    )
    if want_all:
        return [str(p) for p in archives]
    if episode is not None:
        for p in archives:
            if episode_number(p) == episode:
                return [str(p)]
        raise SystemExit(f"episode #{episode} (アーカイブ) not found in {data_dir}")
    raise SystemExit("specify --episode N, --all, or --dir PATH")


def save_pending(label: str, conflicts: list[dict]):
    if not conflicts:
        return
    out = settings.abspath(settings.pending_dir) / f"{label}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(conflicts, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"  pending conflicts written: {out}")


def prompt_decisions(conflicts: list[dict]) -> list[str]:
    decisions = []
    print(f"\n  !! {len(conflicts)} 知识冲突需要确认:")
    for c in conflicts:
        print(f"    关系「{c['relation']}」 {c['subject_name']}: "
              f"已有 -> {c['existing_object']}  |  新提取 -> {c['new_object']}")
        ans = input("    [c]onfirm 保留历史线 / [o]verwrite 覆盖 / [i]gnore 忽略 (默认 c): ").strip().lower()
        decisions.append({"c": "confirm", "o": "overwrite", "i": "ignore"}.get(ans, "confirm"))
    return decisions


def prompt_inspection_decisions(issues: list[dict]) -> list[str]:
    decisions = []
    print(f"\n  !! {len(issues)} 条审核高风险事实需要确认:")
    for issue in issues:
        print(f"    关系「{issue['relation']}」: "
              f"{issue['original_name']} -> 建议修正为 {issue['suggested_name']}")
        print(f"    原因: {issue.get('reason', '')}")
        ans = input("    [a]ccept 采用纠偏 / [k]eep 保留原文 / [i]gnore 忽略 (默认 a): ").strip().lower()
        decisions.append({
            "a": "accept_correction",
            "k": "keep_original",
            "i": "ignore",
        }.get(ans, "accept_correction"))
    return decisions


def ingest_one(graph, folder: str, auto_policy: str | None):
    label = Path(folder).name
    config = {"configurable": {"thread_id": label}}
    print(f"\n=== ingest: {label} ===")
    result = graph.invoke({"episode_dir": folder}, config)

    while "__interrupt__" in result:
        payload = result["__interrupt__"][0].value
        if "inspection_issues" in payload:
            decisions = prompt_inspection_decisions(payload["inspection_issues"])
        elif "conflicts" in payload:
            decisions = prompt_decisions(payload["conflicts"])
        else:
            raise RuntimeError(f"unknown interrupt payload: {payload}")
        result = graph.invoke(Command(resume=decisions), config)

    print(f"  inspection issues: {len(result.get('inspection_issues', []))}")
    print(f"  dropped (ambiguous): {len(result.get('dropped', []))}")
    print(f"  result: {result.get('written', [])}")
    save_pending(label, result.get("conflicts", []))
    save_pending(f"{label}.inspection", result.get("inspection_issues", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", type=int)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dir")
    ap.add_argument("--auto", choices=["confirm", "overwrite", "ignore"])
    ap.add_argument("--provider", help="override LLM provider")
    args = ap.parse_args()

    folders = select_folders(args.episode, args.all, args.dir)
    llm = LLMClient(provider=args.provider)
    ckpt_path = str(settings.abspath(settings.checkpoint_db))
    Path(ckpt_path).parent.mkdir(parents=True, exist_ok=True)

    with GraphStore() as graph_store, VectorStore() as vector_store, \
            SqliteSaver.from_conn_string(ckpt_path) as ckpt:
        deps = Deps(
            extractor=ExtractorAgent(llm, graph_store),
            inspector=InspectorAgent(llm, graph_store),
            sync=SyncAgent(graph_store),
            vector=vector_store,
            annotator=AnnotatorAgent(llm),
            auto_policy=args.auto,
        )
        graph = build_ingestion_graph(deps, ckpt)
        for folder in folders:
            ingest_one(graph, folder, args.auto)

        print("\n=== graph stats ===", graph_store.stats())


if __name__ == "__main__":
    main()
