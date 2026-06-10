"""HITL approval queue for newly discovered recurring segments.

New segments should not mutate ``segments_library.yaml`` until the user approves
them from Telegram.  This module keeps a tiny JSON-backed pending queue so the
pipeline can stay stateless between runs.
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from radio.segments_library import (
    append_new_segments_to_library,
    filter_library_by_series,
    load_segments_library,
    match_segment,
)


class PendingSegment(BaseModel):
    """One candidate recurring segment waiting for a human decision."""

    id: str
    status: str = "pending"
    created_at: str
    decided_at: str = ""
    program_series: str
    program_name: str
    air_date: str
    title_ja: str
    intro: str
    aliases: list[str] = []


class ApprovalStore:
    """Small JSON file store for pending segment approvals."""

    def __init__(self, path: Path):
        self.path = path

    def add_segments(
        self,
        *,
        program_series: str,
        program_name: str,
        air_date: str,
        segments: list[dict],
        library_path: Path | None = None,
    ) -> list[PendingSegment]:
        """Add new pending candidates, skipping library and pending duplicates."""
        if not program_series or not segments:
            return []

        records = self._load()
        library = (
            filter_library_by_series(load_segments_library(library_path), program_series)
            if library_path is not None
            else []
        )
        added: list[PendingSegment] = []
        added_ids: set[str] = set()
        for segment in segments:
            title_ja = normalize_segment_title(str(segment.get("title_ja") or ""))
            intro = str(segment.get("intro") or "").strip()
            if not title_ja or not intro:
                continue
            if match_segment(title_ja, library):
                logger.info(f"已入库环节跳过待审批：{title_ja}")
                continue
            existing = self._find_open_duplicate(records, program_series, title_ja)
            if existing:
                if existing.id not in added_ids:
                    added.append(existing)
                    added_ids.add(existing.id)
                continue
            pending = PendingSegment(
                id=secrets.token_hex(6),
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                program_series=program_series,
                program_name=program_name,
                air_date=air_date,
                title_ja=title_ja,
                intro=intro,
                aliases=[str(a) for a in (segment.get("aliases") or [])],
            )
            records.append(pending)
            added.append(pending)
            added_ids.add(pending.id)
            logger.info(f"新增待审批环节：{title_ja} ({pending.id})")

        if added:
            self._save(records)
        return added

    def list_pending(self, limit: int = 20) -> list[PendingSegment]:
        """Return newest pending records."""
        pending = [r for r in self._load() if r.status == "pending"]
        pending.sort(key=lambda r: r.created_at, reverse=True)
        return pending[:limit]

    def approve(self, segment_id: str, library_path: Path) -> tuple[PendingSegment, int, int]:
        """Approve a pending segment and append it to the library."""
        records = self._load()
        record = self._get(records, segment_id)
        if record.status != "pending":
            return record, 0, 1

        added, skipped = append_new_segments_to_library(
            library_path,
            record.program_series,
            [
                {
                    "title_ja": record.title_ja,
                    "intro": record.intro,
                    "aliases": record.aliases,
                }
            ],
        )
        record.status = "approved"
        record.decided_at = datetime.now(UTC).isoformat(timespec="seconds")
        self._save(records)
        logger.info(f"审批通过环节：{record.title_ja} ({record.id})")
        return record, added, skipped

    def skip(self, segment_id: str) -> PendingSegment:
        """Skip a pending segment without writing it to the library."""
        records = self._load()
        record = self._get(records, segment_id)
        if record.status == "pending":
            record.status = "skipped"
            record.decided_at = datetime.now(UTC).isoformat(timespec="seconds")
            self._save(records)
            logger.info(f"审批跳过环节：{record.title_ja} ({record.id})")
        return record

    def _load(self) -> list[PendingSegment]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"读取审批队列失败：{exc!r}")
            return []
        return [PendingSegment(**item) for item in (raw.get("segments") or [])]

    def _save(self, records: list[PendingSegment]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "segments": [r.model_dump() for r in records]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _find_open_duplicate(
        records: list[PendingSegment], program_series: str, title_ja: str
    ) -> PendingSegment | None:
        for record in records:
            if record.status != "pending":
                continue
            if record.program_series != program_series:
                continue
            if record.title_ja == title_ja:
                return record
            if record.title_ja in title_ja or title_ja in record.title_ja:
                return record
        return None

    @staticmethod
    def _get(records: list[PendingSegment], segment_id: str) -> PendingSegment:
        for record in records:
            if record.id == segment_id:
                return record
        raise KeyError(f"待审批环节不存在：{segment_id}")


def default_approval_store_path(logs_dir: Path) -> Path:
    """Keep approval state next to other runtime data under ``data/``."""
    return logs_dir.parent / "pending_segments.json"


def normalize_segment_title(title_ja: str) -> str:
    """Keep library candidates at the reusable segment-name level."""
    title = re.sub(r"\s+", " ", title_ja).strip()
    while True:
        cleaned = re.sub(r"\s*[（(][^（）()]{1,80}[）)]\s*$", "", title).strip()
        if cleaned == title:
            return title
        title = cleaned
