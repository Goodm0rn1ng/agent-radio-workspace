"""Build the Summary DB (Stage-1 coarse router) from each episode's
05_summary.json. Each summary section becomes one dense, low-token document
carrying [episode + time_range] metadata, so Stage-1 can match the gist and
hand Stage-2 precise clues. No LLM — summaries are pre-made.

Run:  .venv/bin/python -m src.build_summary_db
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import settings  # noqa: E402
from src.agents.doc_agent import parse_folder_metadata  # noqa: E402
from src.mcp_layer.vector_store import VectorStore  # noqa: E402
from src.source_data import episode_number, iter_episode_folders  # noqa: E402

SUMMARY_COLLECTION = "radio_summaries"
_TR_RE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})")


def _to_sec(ts: str) -> int:
    h, m, s = (int(x) for x in ts.split(":"))
    return h * 3600 + m * 60 + s


def _parse_range(tr: str) -> tuple[int, int]:
    found = _TR_RE.findall(tr or "")
    if not found:
        return 0, 0
    start = _to_sec(":".join(found[0]))
    end = _to_sec(":".join(found[1])) if len(found) > 1 else start
    return start, end


def _sec_to_ts(sec: int) -> str:
    return f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"


def _join_items(items) -> str:
    if not items:
        return ""
    if isinstance(items, list):
        return "；".join(str(x) for x in items if x)
    return str(items)


def _id_part(text: str) -> str:
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"[^0-9A-Za-z_\-\u3040-\u30ff\u3400-\u9fff]+", "-", text)
    return text.strip("-") or "source"


def _source_folders(data_dir: Path) -> list[Path]:
    return iter_episode_folders(data_dir, require_summary=True)


def summary_records_for_folder(folder: Path, program: str) -> tuple[list[str], list[str], list[dict]]:
    ids, docs, metas = [], [], []

    episode = episode_number(folder)
    if episode is None:
        return ids, docs, metas
    source = parse_folder_metadata(str(folder), program)
    program = source.program  # per-folder program (collections differ)
    fp = folder / "05_summary.json"
    if not fp.exists():
        return ids, docs, metas
    data = json.loads(fp.read_text(encoding="utf-8"))
    for i, sec in enumerate(data.get("sections", [])):
        start, end = _parse_range(sec.get("time_range", ""))
        title = sec.get("title") or sec.get("title_ja") or ""
        content = sec.get("content") or ""
        intro = sec.get("intro") or ""
        listener_mail = sec.get("listener_mail") or ""
        reactions = _join_items(sec.get("member_reactions"))
        notes = _join_items(sec.get("notes"))
        topics = "、".join(data.get("key_topics", []))
        # dense routing document: Chinese summary fields first; they catch
        # Chinese questions whose terms never appear literally in Japanese.
        doc = "\n".join(
            part for part in [
                title,
                intro,
                content,
                f"来信：{listener_mail}" if listener_mail else "",
                f"主持反应：{reactions}" if reactions else "",
                f"备注：{notes}" if notes else "",
                f"关键话题：{topics}" if topics else "",
            ] if part
        ).strip()
        citation = (f"《{program}》第{episode}期 "
                    f"{_sec_to_ts(start)}-{_sec_to_ts(end)}")
        ids.append(f"sum-ep{episode}-{_id_part(source.episode_label)}-{i:02d}")
        docs.append(doc)
        metas.append({
            "episode": episode,
            "episode_label": source.episode_label,
            "section_title": title,
            "start_sec": start,
            "end_sec": end,
            "broadcast_date": sec.get("time_range", ""),
            "citation": citation,
        })
    return ids, docs, metas


def build():
    data_dir = settings.abspath(settings.radio_data_dir)
    program = settings.program_name
    ids, docs, metas = [], [], []

    for folder in _source_folders(data_dir):
        folder_ids, folder_docs, folder_metas = summary_records_for_folder(folder, program)
        ids.extend(folder_ids)
        docs.extend(folder_docs)
        metas.extend(folder_metas)

    with VectorStore(collection_name=SUMMARY_COLLECTION) as v:
        v.reset_collection()
        # add in batches to bound embedding memory
        B = 64
        for j in range(0, len(ids), B):
            v.add_chunks(ids[j:j + B], docs[j:j + B], metas[j:j + B])
        print(f"summary DB built: collection={SUMMARY_COLLECTION} count={v.count()}")
        from src import index_version as iv
        print("stamped:", iv.stamp(iv.SUMMARY, v.distinct_labels()))


if __name__ == "__main__":
    build()
