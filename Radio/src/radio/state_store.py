"""SQLite-backed runtime state for API jobs.

The store persists API-facing state, not execution.  The in-process
``JobManager`` still owns background tasks; SQLite gives the frontend a stable
place to read job/run/artifact history across API restarts.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class StateStore:
    """Small SQLite wrapper for job snapshots and artifact indexes."""

    def __init__(self, path: Path):
        self.path = path

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    profile_id TEXT,
                    collection_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS job_items (
                    job_id TEXT NOT NULL,
                    queue_index INTEGER NOT NULL,
                    run_id TEXT,
                    url TEXT NOT NULL,
                    title TEXT,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    work_dir TEXT,
                    error TEXT,
                    payload_json TEXT NOT NULL,
                    PRIMARY KEY (job_id, queue_index)
                );

                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    queue_index INTEGER,
                    status TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    source TEXT NOT NULL,
                    title TEXT,
                    air_date TEXT,
                    profile_id TEXT,
                    collection_id TEXT,
                    work_dir TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT,
                    job_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    label TEXT,
                    created_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    UNIQUE (run_id, job_id, kind, path)
                );

                CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at);
                CREATE INDEX IF NOT EXISTS idx_runs_job_id ON runs(job_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_job_id ON artifacts(job_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id);
                """
            )

    def upsert_job_payload(self, payload: dict[str, Any]) -> None:
        now = _now()
        job_id = str(payload["job_id"])
        with self._connect() as conn:
            self._upsert_job(conn, payload, updated_at=now)
            for item in payload.get("items") or []:
                self._upsert_item(conn, job_payload=payload, item=item)
                if item.get("run_id"):
                    self._upsert_run(conn, job_payload=payload, item=item)
            for result in payload.get("results") or []:
                self._upsert_result_artifacts(conn, job_id=job_id, result=result)

    def get_job_payload(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            return None
        return json.loads(str(row["payload_json"]))

    def list_job_payloads(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM jobs
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [json.loads(str(row["payload_json"])) for row in rows]

    def list_artifacts(
        self,
        *,
        job_id: str | None = None,
        run_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[str | int] = []
        if job_id:
            clauses.append("job_id = ?")
            params.append(job_id)
        if run_id:
            clauses.append("run_id = ?")
            params.append(run_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT id, run_id, job_id, kind, path, label, created_at, payload_json
                FROM artifacts
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [
            {
                "id": row["id"],
                "run_id": row["run_id"],
                "job_id": row["job_id"],
                "kind": row["kind"],
                "path": row["path"],
                "label": row["label"],
                "created_at": row["created_at"],
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in rows
        ]

    def mark_stale_jobs_failed(self) -> int:
        """Fail jobs that were active when the API server last stopped."""
        now = _now()
        changed = 0
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT payload_json
                FROM jobs
                WHERE status IN ('queued', 'waiting', 'running')
                """
            ).fetchall()
            for row in rows:
                payload = json.loads(str(row["payload_json"]))
                payload["status"] = "failed"
                payload["stage"] = "failed"
                payload["message"] = "server restarted before job completed"
                payload["error"] = "server restarted before job completed"
                payload["finished_at"] = now
                for item in payload.get("items") or []:
                    if item.get("status") in {"queued", "waiting", "running"}:
                        item["status"] = "failed"
                        item["stage"] = "failed"
                        item["message"] = "server restarted before item completed"
                        item["error"] = "server restarted before item completed"
                logs = list(payload.get("logs") or [])
                logs.append(f"{now}  failed: server restarted before job completed")
                payload["logs"] = logs[-80:]
                self._upsert_job(conn, payload, updated_at=now)
                for item in payload.get("items") or []:
                    self._upsert_item(conn, job_payload=payload, item=item)
                    if item.get("run_id"):
                        self._upsert_run(conn, job_payload=payload, item=item)
                changed += 1
        return changed

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _upsert_job(
        self,
        conn: sqlite3.Connection,
        payload: dict[str, Any],
        *,
        updated_at: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, kind, status, stage, profile_id, collection_id,
                created_at, updated_at, started_at, finished_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                kind = excluded.kind,
                status = excluded.status,
                stage = excluded.stage,
                profile_id = excluded.profile_id,
                collection_id = excluded.collection_id,
                updated_at = excluded.updated_at,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                payload_json = excluded.payload_json
            """,
            (
                payload["job_id"],
                payload.get("kind") or "",
                payload.get("status") or "",
                payload.get("stage") or "",
                payload.get("profile_id"),
                payload.get("collection_id"),
                payload.get("created_at") or updated_at,
                updated_at,
                payload.get("started_at"),
                payload.get("finished_at"),
                _json(payload),
            ),
        )

    def _upsert_item(
        self,
        conn: sqlite3.Connection,
        *,
        job_payload: dict[str, Any],
        item: dict[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO job_items (
                job_id, queue_index, run_id, url, title, status, stage,
                work_dir, error, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id, queue_index) DO UPDATE SET
                run_id = excluded.run_id,
                url = excluded.url,
                title = excluded.title,
                status = excluded.status,
                stage = excluded.stage,
                work_dir = excluded.work_dir,
                error = excluded.error,
                payload_json = excluded.payload_json
            """,
            (
                job_payload["job_id"],
                item.get("queue_index"),
                item.get("run_id"),
                item.get("url") or "",
                item.get("title"),
                item.get("status") or "",
                item.get("stage") or "",
                item.get("work_dir"),
                item.get("error"),
                _json(item),
            ),
        )

    def _upsert_run(
        self,
        conn: sqlite3.Connection,
        *,
        job_payload: dict[str, Any],
        item: dict[str, Any],
    ) -> None:
        status = item.get("status") or job_payload.get("status") or ""
        stage = item.get("stage") or job_payload.get("stage") or ""
        finished_at = (
            job_payload.get("finished_at")
            if status in {"succeeded", "failed", "canceled"}
            else None
        )
        payload = {
            "job_id": job_payload["job_id"],
            "kind": job_payload.get("kind"),
            "item": item,
        }
        conn.execute(
            """
            INSERT INTO runs (
                run_id, job_id, queue_index, status, stage, source, title,
                air_date, profile_id, collection_id, work_dir, started_at,
                finished_at, payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                status = excluded.status,
                stage = excluded.stage,
                title = excluded.title,
                profile_id = excluded.profile_id,
                collection_id = excluded.collection_id,
                work_dir = excluded.work_dir,
                started_at = excluded.started_at,
                finished_at = excluded.finished_at,
                payload_json = excluded.payload_json
            """,
            (
                item["run_id"],
                job_payload["job_id"],
                item.get("queue_index"),
                status,
                stage,
                job_payload.get("kind") or "unknown",
                item.get("title"),
                job_payload.get("air_date"),
                job_payload.get("profile_id"),
                job_payload.get("collection_id"),
                item.get("work_dir"),
                job_payload.get("started_at"),
                finished_at,
                _json(payload),
            ),
        )

    def _upsert_result_artifacts(
        self,
        conn: sqlite3.Connection,
        *,
        job_id: str,
        result: dict[str, Any],
    ) -> None:
        work_dir = result.get("work_dir")
        if not work_dir:
            return
        payload = dict(result)
        conn.execute(
            """
            INSERT INTO artifacts (run_id, job_id, kind, path, label, created_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, job_id, kind, path) DO UPDATE SET
                label = excluded.label,
                payload_json = excluded.payload_json
            """,
            (
                result.get("run_id"),
                job_id,
                "work_dir",
                str(work_dir),
                result.get("title") or "work directory",
                _now(),
                _json(payload),
            ),
        )


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
