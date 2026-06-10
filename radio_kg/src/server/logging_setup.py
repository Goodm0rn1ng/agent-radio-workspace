"""Structured JSON logging with a per-request trace_id.

`trace_id` is stored in a ContextVar so any code path reached from a FastAPI
handler (QA pipeline, ingest step, KB edit) can log on the same trace without
threading the id through every call. Use `get_logger(__name__)` for module
loggers; the formatter merges `extra={...}` and the active trace_id.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from contextvars import ContextVar
from uuid import uuid4

_trace_id: ContextVar[str] = ContextVar("trace_id", default="-")
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": round(record.created, 3),
            "level": record.levelname,
            "logger": record.name,
            "trace_id": _trace_id.get(),
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in _RESERVED or k.startswith("_"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except TypeError:
                payload[k] = repr(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


_configured = False


def configure(level: str = "INFO") -> None:
    global _configured
    if _configured:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # silence noisy libs at INFO
    for name in ("httpx", "httpcore", "urllib3", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    configure()
    return logging.getLogger(name)


def new_trace_id() -> str:
    tid = uuid4().hex[:12]
    _trace_id.set(tid)
    return tid


def set_trace_id(tid: str) -> None:
    _trace_id.set(tid)


def get_trace_id() -> str:
    return _trace_id.get()


class TraceIdMiddleware:
    """ASGI middleware: stamps a trace_id per request and logs request/response."""

    def __init__(self, app):
        self.app = app
        self.log = get_logger("radio_kg.http")

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        incoming = headers.get(b"x-trace-id")
        tid = incoming.decode() if incoming else uuid4().hex[:12]
        token = _trace_id.set(tid)
        start = time.time()
        status_code = {"value": 0}

        async def _send(message):
            if message["type"] == "http.response.start":
                status_code["value"] = message.get("status", 0)
                raw = list(message.get("headers") or [])
                raw.append((b"x-trace-id", tid.encode()))
                message["headers"] = raw
            await send(message)

        path = scope.get("path", "")
        method = scope.get("method", scope["type"])
        try:
            await self.app(scope, receive, _send)
            self.log.info("request", extra={
                "method": method, "path": path,
                "status": status_code["value"],
                "dur_ms": round((time.time() - start) * 1000, 1),
            })
        except Exception:
            self.log.exception("request_failed", extra={
                "method": method, "path": path,
                "dur_ms": round((time.time() - start) * 1000, 1),
            })
            raise
        finally:
            _trace_id.reset(token)
