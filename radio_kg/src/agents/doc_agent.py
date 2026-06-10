"""DocAgent: parse a source folder into timestamped, metadata-bound chunks.

Source layout (per episode folder):
  03_ja_segments.json / 04_bilingual_segments.json : [{i,start,end,ja,zh}]
  05_summary.json                                  : structured summary

Folder name carries provenance, e.g.
  2026-05-16_【アーカイブ】羊宮妃那のこもれびじかん #1 2025年4月6日放送
  2026-05-22_【アーカイブ】ゲスト：桜谷理子 羊宮妃那のこもれびじかん #30 ...
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from config.settings import settings
from src.agents.transcript_normalizer import normalize_transcript_text
from src.schema.models import Chunk, SourceRef, _sec_to_ts

# group segments into windows of at least this duration / char budget
WINDOW_SECONDS = 90.0
WINDOW_CHARS = 700

_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_EP_RE = re.compile(r"#(\d+)")
_TYPE_RE = re.compile(r"【([^】]+)】")
_GUEST_RE = re.compile(r"ゲスト：([^\s　]+)")
_LEAD_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}_")
_AIR_DATE_RE = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日(?:\([^)]*\))?\s*放送?")


def derive_program(folder_name: str) -> str:
    """Best-effort program title from a folder name, by stripping the date
    prefix, 【type】, ゲスト, #episode and trailing air-date tokens. Empty if
    nothing meaningful remains (caller falls back to the configured default)."""
    text = _LEAD_DATE_RE.sub("", folder_name)
    text = _TYPE_RE.sub("", text)
    text = _GUEST_RE.sub("", text)
    text = _EP_RE.sub("", text)
    text = _AIR_DATE_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip(" 　-_")


def _id_part(text: str) -> str:
    text = re.sub(r"\s+", "-", text.strip())
    text = re.sub(r"[^0-9A-Za-z_\-\u3040-\u30ff\u3400-\u9fff]+", "-", text)
    return text.strip("-") or "source"


def parse_folder_metadata(folder: str, program: str) -> SourceRef:
    name = Path(folder).name
    ep = _EP_RE.search(name)
    type_m = _TYPE_RE.search(name)
    guest = _GUEST_RE.search(name)
    date_m = _DATE_RE.search(name)

    broadcast_date = ""
    if date_m:
        y, mo, d = (int(x) for x in date_m.groups())
        broadcast_date = f"{y:04d}-{mo:02d}-{d:02d}"

    label_parts = []
    if ep:
        label_parts.append(f"#{ep.group(1)}")
    if type_m:
        label_parts.append(type_m.group(1))
    if guest:
        label_parts.append(f"ゲスト:{guest.group(1)}")

    return SourceRef(
        source_type="audio",
        program=derive_program(name) or program,
        episode=int(ep.group(1)) if ep else None,
        episode_label=" ".join(label_parts) or name,
        broadcast_date=broadcast_date,
    )


def _load_segments(folder: Path) -> list[dict]:
    for fname in ("04_bilingual_segments.json", "03_ja_segments.json"):
        p = folder / fname
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"no segments json in {folder}")


def build_chunks(folder: str, program: str | None = None) -> list[Chunk]:
    program = program or settings.program_name
    fpath = Path(folder)
    base = parse_folder_metadata(folder, program)
    segments = _load_segments(fpath)

    chunks: list[Chunk] = []
    buf: list[dict] = []
    buf_chars = 0
    idx = 0

    def flush():
        nonlocal buf, buf_chars, idx
        if not buf:
            return
        start = buf[0]["start"]
        end = buf[-1]["end"]
        lines = []
        retrieval_lines = []
        for s in buf:
            if not s.get("ja"):
                continue
            ts = _sec_to_ts(s["start"])
            ja = normalize_transcript_text(s["ja"])
            zh = (s.get("zh") or "").strip()
            lines.append(f"[{ts}] {ja}")
            retrieval_lines.append(f"[{ts}] JA: {ja}")
            if zh:
                retrieval_lines.append(f"[{ts}] ZH: {zh}")
        src = base.model_copy(update={"start_time": start, "end_time": end})
        chunks.append(
            Chunk(
                chunk_id=f"ep{base.episode}-{_id_part(base.episode_label)}-{idx:03d}",
                text="\n".join(lines),
                retrieval_text="\n".join(retrieval_lines),
                source=src,
            )
        )
        idx += 1
        buf, buf_chars = [], 0

    for seg in segments:
        buf.append(seg)
        buf_chars += len(seg.get("ja", ""))
        span = seg["end"] - buf[0]["start"]
        if span >= WINDOW_SECONDS or buf_chars >= WINDOW_CHARS:
            flush()
    flush()
    return chunks
