"""SQLite-backed conversation store for the chat UI.

Replaces the in-process dict so chat history survives server restarts. Two
tables: conversations (id/title/timestamps) and messages (role/content + a JSON
`meta` blob carrying sources, anchors, kb-preview payloads, etc.).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from uuid import uuid4


class ConversationStore:
    def __init__(self, db_path: str):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS conversations ("
            "id TEXT PRIMARY KEY, title TEXT, created_at REAL, updated_at REAL)"
        )
        self._db.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, conv_id TEXT, role TEXT, "
            "content TEXT, meta TEXT, ts REAL, "
            "FOREIGN KEY(conv_id) REFERENCES conversations(id) ON DELETE CASCADE)"
        )
        self._db.execute("CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, id)")
        self._db.commit()

    def _summary_row(self, conv_id: str) -> dict:
        c = self._db.execute("SELECT * FROM conversations WHERE id=?", (conv_id,)).fetchone()
        n = self._db.execute("SELECT count(*) AS n FROM messages WHERE conv_id=?", (conv_id,)).fetchone()["n"]
        return {"id": c["id"], "title": c["title"], "updated_at": c["updated_at"], "message_count": n}

    def list(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT id FROM conversations ORDER BY updated_at DESC"
            ).fetchall()
            return [self._summary_row(r["id"]) for r in rows]

    def create(self) -> dict:
        cid = uuid4().hex[:12]
        now = time.time()
        with self._lock:
            self._db.execute(
                "INSERT INTO conversations(id, title, created_at, updated_at) VALUES(?,?,?,?)",
                (cid, "新对话", now, now),
            )
            self._db.commit()
            return self._summary_row(cid)

    def exists(self, cid: str) -> bool:
        with self._lock:
            return self._db.execute(
                "SELECT 1 FROM conversations WHERE id=?", (cid,)
            ).fetchone() is not None

    def get(self, cid: str) -> dict | None:
        with self._lock:
            c = self._db.execute("SELECT * FROM conversations WHERE id=?", (cid,)).fetchone()
            if c is None:
                return None
            rows = self._db.execute(
                "SELECT id, role, content, meta, ts FROM messages WHERE conv_id=? ORDER BY id", (cid,)
            ).fetchall()
            msgs = []
            for r in rows:
                m = {"id": r["id"], "role": r["role"], "content": r["content"], "ts": r["ts"]}
                if r["meta"]:
                    m.update(json.loads(r["meta"]))
                msgs.append(m)
            return {"id": c["id"], "title": c["title"], "created_at": c["created_at"],
                    "updated_at": c["updated_at"], "messages": msgs}

    def delete(self, cid: str) -> bool:
        with self._lock:
            cur = self._db.execute("DELETE FROM conversations WHERE id=?", (cid,))
            self._db.execute("DELETE FROM messages WHERE conv_id=?", (cid,))
            self._db.commit()
            return cur.rowcount > 0

    def history(self, cid: str) -> list[dict]:
        """Plain [{role, content}] turns for the LLM (no meta)."""
        c = self.get(cid)
        return [{"role": m["role"], "content": m["content"]} for m in c["messages"]] if c else []

    def add_message(self, cid: str, role: str, content: str, meta: dict | None = None,
                    auto_title: bool = False) -> dict:
        now = time.time()
        with self._lock:
            ins = self._db.execute(
                "INSERT INTO messages(conv_id, role, content, meta, ts) VALUES(?,?,?,?,?)",
                (cid, role, content, json.dumps(meta or {}, ensure_ascii=False), now),
            )
            mid = ins.lastrowid
            if auto_title:
                cur = self._db.execute("SELECT title FROM conversations WHERE id=?", (cid,)).fetchone()
                if cur and cur["title"] == "新对话":
                    title = content[:30] + ("…" if len(content) > 30 else "")
                    self._db.execute("UPDATE conversations SET title=? WHERE id=?", (title, cid))
            self._db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (now, cid))
            self._db.commit()
            summary = self._summary_row(cid)
            summary["last_message_id"] = mid
            return summary

    def set_feedback(self, cid: str, msg_id: int, value: str | None) -> bool:
        """Persist 👍/👎 onto an assistant message's meta blob. value None clears it."""
        with self._lock:
            row = self._db.execute(
                "SELECT meta FROM messages WHERE id=? AND conv_id=? AND role='assistant'",
                (msg_id, cid),
            ).fetchone()
            if row is None:
                return False
            meta = json.loads(row["meta"]) if row["meta"] else {}
            if value:
                meta["feedback"] = value
            else:
                meta.pop("feedback", None)
            self._db.execute("UPDATE messages SET meta=? WHERE id=?",
                             (json.dumps(meta, ensure_ascii=False), msg_id))
            self._db.commit()
            return True
