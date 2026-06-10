"""Repair known ASR/entity normalization mistakes in the Neo4j graph.

Two passes:
  1. KNOWN_ENTITY_REDIRECTS — hand-curated ASR near-homophone fixes.
  2. same-name cross-type merge — collapse one name split across several types
     (type duplication / fragments) into a single canonical-type node, moving
     every edge (期数/时间戳/出处 preserved) via redirect_entity.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.mcp_layer.graph_store import GraphStore  # noqa: E402

# When a name exists as several types, this ranks which type the merged node
# keeps. Listener is handled first as a special case (it is load-bearing for the
# StatsAgent letter-writer count, and these groups are genuinely listeners). The
# program title resolves to Program. Everything else falls through this list;
# Other ranks last so the generic dumping type never wins over a specific one.
_TYPE_PRIORITY = [
    "Organization", "Project", "Work", "Segment", "Event", "Place",
    "Person", "Joke", "Character", "Service", "Program", "Other",
]


def canonical_type(name: str, types: list[str]) -> str:
    if "Listener" in types:
        return "Listener"
    if name == settings.program_name and "Program" in types:
        return "Program"
    for t in _TYPE_PRIORITY:
        if t in types:
            return t
    return types[0]


KNOWN_ENTITY_REDIRECTS = [
    {
        "old_name": "青鬼プロダクション",
        "old_type": "Organization",
        "new_name": "青二プロダクション",
        "new_type": "Organization",
        "aliases": ["青二", "青二プロ", "Aoni Production"],
    },
    {
        # ep30 guest 桜谷理子 calls the host ひなたん; it was split into its own
        # Person node and is phonetically confusable with 青木陽菜 (陽菜=ひな).
        # Pin it onto the host. (canonical.py now collapses it on future ingests.)
        "old_name": "ひなたん",
        "old_type": "Person",
        "new_name": "羊宮妃那",
        "new_type": "Person",
        "aliases": ["ひな", "ひなたん", "ヒナたん", "ひなぴ"],
    },
]


def merge_same_name_nodes(graph: GraphStore) -> int:
    """Collapse every same-name-across-types group into one canonical-type node.
    Returns the number of groups merged."""
    groups = graph.duplicate_name_groups()
    merged = 0
    for grp in groups:
        name, types = grp["name"], grp["types"]
        target = canonical_type(name, types)
        moved = False
        for t in types:
            if t == target:
                continue
            res = graph.redirect_entity(
                old_name=name, old_type=t, new_name=name, new_type=target,
            )
            if res["deleted"] or res["incoming"] or res["outgoing"]:
                moved = True
                print(
                    f"  {name} [{t}] -> [{target}]: "
                    f"incoming={res['incoming']} outgoing={res['outgoing']} "
                    f"deleted={res['deleted']}"
                )
        if moved:
            merged += 1
    return merged


def main():
    with GraphStore() as graph:
        print("== known ASR redirects ==")
        for repair in KNOWN_ENTITY_REDIRECTS:
            result = graph.redirect_entity(**repair)
            print(
                f"{result['old_eid']} -> {result['new_eid']}: "
                f"incoming={result['incoming']} outgoing={result['outgoing']} "
                f"deleted={result['deleted']}"
            )
        print("== same-name cross-type merge ==")
        n = merge_same_name_nodes(graph)
        print(f"merged {n} same-name groups")


if __name__ == "__main__":
    main()
