"""Graph hygiene: prune long-tail clause-fragment noise and merge
same-name-multiple-type duplicate entities.

The entity/relation ratio sits near 1 because ~74% of entities have degree ≤1
and ~half are type "Other" — most of those are clauses/sentences the extractor
wrongly promoted to nodes (e.g. "メールを読ませていただきます"). This pass removes
that obvious noise and folds fragmented duplicates (e.g. a program name split
across Program/Person/Project/Work) into one canonical node.

Dry-run by default (reports what it WOULD do); pass --apply to write. Writes are
irreversible (Agent/ is not a git repo) — review the dry-run first.

    python -m src.cleanup_graph              # report only
    python -m src.cleanup_graph --apply      # execute
"""
from __future__ import annotations

import argparse
from collections import defaultdict

from src import canonical
from src.mcp_layer.graph_store import GraphStore, entity_id

# when a name exists under several types, keep the most specific/structural one
TYPE_PRIORITY = ["Program", "Organization", "Group", "Person", "Listener",
                 "Project", "Work", "Character", "Segment", "Event", "Place",
                 "Joke", "Other"]


def _pick_type(types: set[str]) -> str:
    for t in TYPE_PRIORITY:
        if t in types:
            return t
    return sorted(types)[0]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    ap.add_argument("--max-noise-len", type=int, default=12,
                    help="also prune degree<=1 'Other' names at least this long (default 12)")
    args = ap.parse_args()

    with GraphStore() as g:
        R = g._read
        ent = R("MATCH (e:Entity) RETURN count(e) AS n", {})[0]["n"]
        rel = R("MATCH ()-[r:REL]->() RETURN count(r) AS n", {})[0]["n"]
        print(f"before:  entities={ent}  relations={rel}  ratio={rel/ent:.2f}")

        # 1) fragmentation — same name across more than one type
        rows = R("MATCH (e:Entity) RETURN e.eid AS eid, e.name AS name, e.type AS type", {})
        by_name: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            by_name[r["name"]].append(r)
        merges: list[tuple[str, str, str]] = []  # (old_eid, canon_eid, label)
        for name, nodes in by_name.items():
            types = {n["type"] for n in nodes}
            if len(nodes) > 1 and len(types) > 1:
                ct = _pick_type(types)
                canon_eid = next((n["eid"] for n in nodes if n["type"] == ct),
                                 entity_id(ct, name))
                for n in nodes:
                    if n["eid"] != canon_eid:
                        merges.append((n["eid"], canon_eid, f"{name} [{n['type']}→{ct}]"))

        # 2) noise — degree<=1 'Other' nodes that read like clauses, or are long
        deg = R("MATCH (e:Entity {type:'Other'}) OPTIONAL MATCH (e)-[r:REL]-() "
                "WITH e, count(r) AS d WHERE d <= 1 "
                "RETURN e.eid AS eid, e.name AS name", {})
        merge_olds = {m[0] for m in merges}
        noise = [(d["eid"], d["name"]) for d in deg
                 if d["eid"] not in merge_olds
                 and (canonical.is_clause_fragment(d["name"])
                      or len(d["name"]) >= args.max_noise_len)]

        print(f"\n[fragmentation] {len(merges)} duplicate node(s) → canonical type")
        for _o, _c, lbl in merges[:25]:
            print("   merge", lbl)
        if len(merges) > 25:
            print(f"   … +{len(merges)-25} more")

        print(f"\n[noise] {len(noise)} degree≤1 'Other' clause/long fragment(s) to prune")
        for _e, nm in noise[:30]:
            print("   prune", repr(nm))
        if len(noise) > 30:
            print(f"   … +{len(noise)-30} more")

        proj = ent - len(merges) - len(noise)
        print(f"\nprojected: entities≈{proj}  ratio≈{(rel-len(noise))/proj:.2f} (approx)")

        if not args.apply:
            print("\nDRY RUN — re-run with --apply to execute.")
            return

        for old, canon, _lbl in merges:
            g.merge_entity(old, canon)
        ids = [e for e, _ in noise]
        for i in range(0, len(ids), 200):
            g.detach_delete_entities(ids[i:i + 200])

        ent2 = R("MATCH (e:Entity) RETURN count(e) AS n", {})[0]["n"]
        rel2 = R("MATCH ()-[r:REL]->() RETURN count(r) AS n", {})[0]["n"]
        print(f"\nafter:   entities={ent2}  relations={rel2}  ratio={rel2/ent2:.2f}")


if __name__ == "__main__":
    main()
