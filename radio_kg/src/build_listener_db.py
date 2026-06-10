"""Persist letter-writers (来信者) as :Entity{type:'Listener'} nodes with a
`投稿` relation to the program, sourced per episode. Deterministic — read from
each episode's 05_summary.json `listener_mail_from` (no LLM).

Enables the StatsAgent to answer aggregation questions like "how many distinct
letter-writers" / "who wrote the most".

Run:  .venv/bin/python -m src.build_listener_db
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.mcp_layer.graph_store import GraphStore  # noqa: E402
from src.source_data import episode_number, iter_episode_folders  # noqa: E402

_SPLIT = re.compile(r"[,，、/]+")


def _names(field: str) -> list[str]:
    return [n.strip() for n in _SPLIT.split(field or "") if n.strip()]


def build():
    data_dir = settings.abspath(settings.radio_data_dir)
    program = settings.program_name
    rows = []  # (listener, episode)
    for folder in iter_episode_folders(data_dir, archives_only=True, require_summary=True):
        fp = folder / "05_summary.json"
        episode = episode_number(folder)
        if episode is None:
            continue
        data = json.loads(fp.read_text(encoding="utf-8"))
        for sec in data.get("sections", []):
            for name in _names(sec.get("listener_mail_from", "")):
                rows.append((name, episode))

    with GraphStore() as g:
        prog_eid = g.merge_node(program, "Program")
        absorbed = set()
        for name, episode in rows:
            lid = g.merge_node(name, "Listener")
            # absorb any pre-existing same-name node of a different type (e.g. the
            # Extractor's Person) into this Listener node so the writer never
            # leaves a same-name duplicate behind.
            if name not in absorbed:
                absorbed.add(name)
                for hit in g.search_nodes(name):
                    if hit["name"] == name and hit["type"] != "Listener":
                        g.redirect_entity(
                            old_name=name, old_type=hit["type"],
                            new_name=name, new_type="Listener",
                        )
            g.merge_directed_relationship(
                lid, "投稿", prog_eid,
                start_epoch=episode, program=program, episode=episode,
                episode_label=f"#{episode}", broadcast_date="",
                start_time=None, end_time=None, source_type="document",
                file_name="05_summary.json", page=None, segment=None,
                confidence=1.0, citation=f"《{program}》第{episode}期 お便り",
            )
        n_listeners = g.count_by_type("Listener")
        n_posts = g.count_relation("投稿")
        print(f"listeners merged: distinct={n_listeners}, mail-edges={n_posts}")


if __name__ == "__main__":
    build()
