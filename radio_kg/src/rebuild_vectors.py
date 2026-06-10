"""Rebuild the Chroma vector collection from timestamped radio chunks only.

This does not run extraction or graph sync. It is safe for swapping embedding
models because it writes to the configured vector collection.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.agents.doc_agent import build_chunks  # noqa: E402
from src.mcp_layer.vector_store import VectorStore  # noqa: E402
from src.source_data import iter_episode_folders, select_episode  # noqa: E402


def _source_folders() -> list[Path]:
    data_dir = settings.abspath(settings.radio_data_dir)
    return iter_episode_folders(data_dir, require_segments=True)


def select_folders(episode: int | None, explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit)]
    archives = _source_folders()
    if episode is None:
        return archives
    found = select_episode(archives, episode)
    if found:
        return found
    raise SystemExit(f"episode #{episode} not found in {settings.abspath(settings.radio_data_dir)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--episode", type=int, help="rebuild only one archive episode")
    ap.add_argument("--dir", help="rebuild from a specific source folder")
    ap.add_argument("--no-reset", action="store_true", help="append/upsert instead of recreating")
    args = ap.parse_args()

    folders = select_folders(args.episode, args.dir)
    print(f"embedding model: {settings.vector_embedding_model}")
    print(f"collection: {settings.effective_vector_collection}")
    print(f"folders: {len(folders)}")

    total = 0
    with VectorStore() as vector:
        if not args.no_reset:
            print("resetting collection...")
            vector.reset_collection()
        for folder in folders:
            chunks = build_chunks(str(folder))
            ids = [c.chunk_id for c in chunks]
            docs = [c.retrieval_text or c.text for c in chunks]
            metas = [
                {
                    "episode": c.source.episode,
                    "episode_label": c.source.episode_label,
                    "broadcast_date": c.source.broadcast_date,
                    "start_time": c.source.start_time,
                    "end_time": c.source.end_time,
                    "citation": c.source.citation(),
                }
                for c in chunks
            ]
            vector.add_chunks(ids, docs, metas)
            total += len(chunks)
            print(f"  {folder.name}: {len(chunks)} chunks")

        print(f"done: indexed {total} chunks; collection count={vector.count()}")


if __name__ == "__main__":
    main()
