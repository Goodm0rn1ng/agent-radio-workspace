"""Unified version + build fingerprint for every store, with drift detection.

The graph, the chunk-vector index, and the summary-vector index must describe
the *same* set of episodes. When ingestion updates one but not the others you
get "graph new, vectors old" (or the reverse) and retrieval silently misses or
mis-cites. This module stamps each store with:

  - version     : monotonic counter, bumped on every (re)build of that store
  - fingerprint : sha1 over the sorted set of episode_labels the store covers
  - n_labels / updated_at

`status()` compares each store's fingerprint to the graph's and flags drift, so
the server / a健康check can surface "summary index is stale, re-index needed".

The registry is a small JSON file under data/.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from config.settings import settings

REGISTRY_PATH = settings.abspath("data") / "index_version.json"

# logical stores tracked. graph is the source of truth for "what should exist".
GRAPH = "graph"
CHUNK = "chunk_vectors"
SUMMARY = "summary_vectors"
PERSONA = "persona"          # mail/insights/profile distilled artifacts


def fingerprint(labels) -> str:
    items = sorted({str(x) for x in labels if x})
    h = hashlib.sha1("\n".join(items).encode("utf-8")).hexdigest()
    return h[:16]


def _load() -> dict:
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(reg: dict) -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(reg, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def stamp(store: str, labels) -> dict:
    """Record a fresh build of `store` covering `labels`. Bumps the version and
    recomputes the fingerprint. Returns the new entry."""
    reg = _load()
    labels = sorted({str(x) for x in labels if x})
    prev = reg.get(store, {})
    entry = {
        "version": int(prev.get("version", 0)) + 1,
        "fingerprint": fingerprint(labels),
        "n_labels": len(labels),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    reg[store] = entry
    _save(reg)
    return entry


def get(store: str) -> dict:
    return _load().get(store, {})


def status(graph_labels=None, chunk_labels=None, summary_labels=None) -> dict:
    """Report each store's version/fingerprint, plus set-based drift vs the
    graph: which labels the graph has but the index is missing (truly stale),
    which the index has but the graph doesn't (extra). 'in_sync' means the
    index covers everything the graph knows about.

    Note: chunk vectors can carry MORE labels than the graph (every folder has
    chunks; some folders extract no triples). What we care about is `missing` —
    labels the graph has that the index lacks. Those need re-indexing."""
    reg = _load()
    out = {"stores": reg, "drift": {}, "live_fingerprint": {}}
    sets: dict[str, set] = {}
    if graph_labels is not None:
        sets[GRAPH] = {str(x) for x in graph_labels if x}
        out["live_fingerprint"][GRAPH] = fingerprint(sets[GRAPH])
    if chunk_labels is not None:
        sets[CHUNK] = {str(x) for x in chunk_labels if x}
        out["live_fingerprint"][CHUNK] = fingerprint(sets[CHUNK])
    if summary_labels is not None:
        sets[SUMMARY] = {str(x) for x in summary_labels if x}
        out["live_fingerprint"][SUMMARY] = fingerprint(sets[SUMMARY])
    base = sets.get(GRAPH)
    if base is not None:
        for store in (CHUNK, SUMMARY):
            if store not in sets:
                continue
            missing = sorted(base - sets[store])
            extra = sorted(sets[store] - base)
            out["drift"][store] = {
                "status": "in_sync" if not missing else "STALE",
                "missing_from_index": missing[:20],
                "missing_count": len(missing),
                "extra_in_index": extra[:20],
                "extra_count": len(extra),
            }
        persona_fp = reg.get(PERSONA, {}).get("fingerprint")
        if persona_fp is not None:
            out["drift"][PERSONA] = {
                "status": "in_sync" if persona_fp == out["live_fingerprint"][GRAPH] else "STALE",
                "stamped_fingerprint": persona_fp,
            }
    return out
