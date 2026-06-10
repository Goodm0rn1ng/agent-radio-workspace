"""SQLite-backed store for in-flight server state.

Replaces the in-process PENDING / KB_PENDING dicts so ingest interrupts and
KB-edit previews survive server restarts. Also keeps an append-only ingest
commit log (one row per pipeline stage) so partial failures across the
Neo4j / Chroma / checkpoint sqlite / index_version stores are visible after
the fact and can be reconciled.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time


class PendingStore:
    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pending_interrupts ("
            "thread_id TEXT PRIMARY KEY, label TEXT, kind TEXT, "
            "folder TEXT, items TEXT, updated_at REAL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS pending_kb_edits ("
            "edit_id TEXT PRIMARY KEY, conv_id TEXT, ops TEXT, "
            "created_at REAL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS ingest_commit_log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, thread_id TEXT, "
            "label TEXT, stage TEXT, status TEXT, detail TEXT, ts REAL)"
        )
        self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_commit_thread "
            "ON ingest_commit_log(thread_id, id)"
        )
        self._db.commit()

    # ── pending ingest interrupts ─────────────────────────────────
    def put_interrupt(self, entry: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO pending_interrupts"
                "(thread_id, label, kind, folder, items, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (entry["thread_id"], entry.get("label"), entry.get("kind"),
                 entry.get("folder"),
                 json.dumps(entry.get("items", []), ensure_ascii=False),
                 time.time()),
            )
            self._db.commit()

    def pop_interrupt(self, thread_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM pending_interrupts WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "DELETE FROM pending_interrupts WHERE thread_id=?",
                (thread_id,),
            )
            self._db.commit()
            return _row_to_interrupt(row)

    def get_interrupt(self, thread_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM pending_interrupts WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        return _row_to_interrupt(row) if row else None

    def list_interrupts(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM pending_interrupts ORDER BY updated_at"
            ).fetchall()
        return [_row_to_interrupt(r) for r in rows]

    # ── pending KB edits (await user confirm) ─────────────────────
    def put_kb_edit(self, edit_id: str, conv_id: str, ops: list[dict]) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO pending_kb_edits"
                "(edit_id, conv_id, ops, created_at) VALUES(?,?,?,?)",
                (edit_id, conv_id, json.dumps(ops, ensure_ascii=False),
                 time.time()),
            )
            self._db.commit()

    def pop_kb_edit(self, edit_id: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM pending_kb_edits WHERE edit_id=?",
                (edit_id,),
            ).fetchone()
            if row is None:
                return None
            self._db.execute(
                "DELETE FROM pending_kb_edits WHERE edit_id=?",
                (edit_id,),
            )
            self._db.commit()
            return {"edit_id": row["edit_id"], "conv_id": row["conv_id"],
                    "ops": json.loads(row["ops"])}

    # ── ingest commit log (partial-failure visibility) ────────────
    def log(self, thread_id: str, label: str | None, stage: str,
            status: str, detail: dict | None = None) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO ingest_commit_log"
                "(thread_id, label, stage, status, detail, ts) "
                "VALUES(?,?,?,?,?,?)",
                (thread_id, label, stage, status,
                 json.dumps(detail or {}, ensure_ascii=False, default=str),
                 time.time()),
            )
            self._db.commit()

    def thread_log(self, thread_id: str) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT stage, status, detail, ts FROM ingest_commit_log "
                "WHERE thread_id=? ORDER BY id",
                (thread_id,),
            ).fetchall()
        return [{"stage": r["stage"], "status": r["status"],
                 "detail": json.loads(r["detail"] or "{}"),
                 "ts": r["ts"]} for r in rows]

    def incomplete_threads(self, limit: int = 50) -> list[dict]:
        """Threads whose last log entry is not a terminal commit/cancel —
        i.e. ingestion that started but didn't reach a clean end state."""
        with self._lock:
            rows = self._db.execute(
                "SELECT thread_id, label, stage, status, ts FROM ingest_commit_log "
                "WHERE id IN ("
                "  SELECT MAX(id) FROM ingest_commit_log GROUP BY thread_id"
                ") AND status NOT IN ('committed','cancelled') "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]


def _row_to_interrupt(row) -> dict:
    return {
        "thread_id": row["thread_id"],
        "label": row["label"],
        "kind": row["kind"],
        "folder": row["folder"],
        "items": json.loads(row["items"] or "[]"),
    }
