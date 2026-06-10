"""Synchronous bridge over the async MCP Python SDK.

The official `mcp` client is asyncio-based; the ingestion pipeline (LangGraph
nodes) is synchronous. This runs a dedicated event loop in a background thread
and exposes a blocking `call_tool`, keeping a single long-lived stdio session
per MCP server (no per-call process spawn).

If the stdio child dies (event loop exits or session goes None), the next
`call_tool` transparently rebuilds the loop+thread+session before retrying.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any, Optional

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class McpStdioClient:
    def __init__(self, command: str, args: list[str], env: Optional[dict] = None):
        self._params = StdioServerParameters(command=command, args=args, env=env)
        self._restart_lock = threading.Lock()
        self._reset_state()

    def _reset_state(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._session: Optional[ClientSession] = None
        self._ready = threading.Event()
        self._stop_evt: Optional[asyncio.Event] = None
        self._started = False
        self._dead = False
        self._exit_reason: Optional[BaseException] = None

    def _run_loop(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        except BaseException as e:  # noqa: BLE001
            self._exit_reason = e
        finally:
            self._dead = True
            self._session = None
            self._ready.set()  # unblock any start() waiters
            try:
                self._loop.close()
            except Exception:
                pass

    async def _serve(self):
        self._stop_evt = asyncio.Event()
        async with stdio_client(self._params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._session = session
                self._ready.set()
                await self._stop_evt.wait()

    def start(self) -> "McpStdioClient":
        if self._started and not self._dead and self._session is not None:
            return self
        with self._restart_lock:
            if self._started and not self._dead and self._session is not None:
                return self
            if self._dead or self._session is None:
                self._reset_state()
            self._thread.start()
            if not self._ready.wait(timeout=60) or self._session is None:
                reason = self._exit_reason or "no session"
                raise RuntimeError(
                    f"MCP server did not start: {self._params.command} ({reason})"
                )
            self._started = True
        return self

    def is_alive(self) -> bool:
        return self._started and not self._dead and self._session is not None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()

    def call_tool(self, name: str, args: dict, timeout: float = 300) -> Any:
        if not self.is_alive():
            self.start()
        try:
            fut = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(name, args), self._loop
            )
            result = fut.result(timeout=timeout)
        except (RuntimeError, AttributeError) as e:
            # event loop closed / session vanished mid-call → reconnect once
            if self.is_alive():
                raise
            self._mark_dead()
            self.start()
            fut = asyncio.run_coroutine_threadsafe(
                self._session.call_tool(name, args), self._loop
            )
            result = fut.result(timeout=timeout)
            _ = e
        return self._unwrap(result)

    def _mark_dead(self) -> None:
        self._dead = True
        self._session = None
        self._started = False

    @staticmethod
    def _unwrap(result: Any) -> Any:
        """Flatten MCP CallToolResult content into plain Python."""
        if getattr(result, "isError", False):
            texts = [getattr(c, "text", str(c)) for c in result.content]
            raise RuntimeError("MCP tool error: " + " ".join(texts))
        out = []
        for c in getattr(result, "content", []) or []:
            text = getattr(c, "text", None)
            if text is None:
                out.append(c)
                continue
            try:
                out.append(json.loads(text))
            except (json.JSONDecodeError, TypeError):
                out.append(text)
        if len(out) == 1:
            return out[0]
        return out

    def close(self):
        if self._started and self._stop_evt is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_evt.set)
            except RuntimeError:
                pass
            self._thread.join(timeout=10)
            self._started = False
            self._session = None
            self._dead = True
