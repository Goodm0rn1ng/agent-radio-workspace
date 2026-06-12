"""Ingestion + QA backend (PRD 4.3 修订).

A long-lived FastAPI service holding one ingestion graph + live MCP stores +
checkpointer. Inspection doubts and sync conflicts are adjudicated in-graph by
a second contextual LLM pass and written directly — no human interrupts; the
/api/pending + /api/resume endpoints remain only to drain legacy suspended
threads from before the change.

Run:  .venv/bin/python -m uvicorn src.server.app:app --port 8000
"""
from __future__ import annotations

import json
import os
import queue
import sys
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from fastapi import FastAPI, HTTPException, Request, WebSocket
from fastapi.responses import (
    FileResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

from config.settings import settings
from src.agents.extractor_agent import ExtractorAgent
from src.agents.inspector_agent import InspectorAgent
from src.agents.sync_agent import SyncAgent
from src.agents.annotator_agent import AnnotatorAgent
from src.agents.qa_agent import QAAgent
from src.agents.stats_agent import StatsAgent
from src.agents.mail_analytics import MailAnalytics
from src.agents.memory_agent import MemoryAgent
from src.agents.persona_agent import PersonaAgent
from src.server.conv_store import ConversationStore
from src.server.pending_store import PendingStore
from src.server.rwlock import ReadWriteLock
from src.server.logging_setup import (
    TraceIdMiddleware, configure as configure_logging, get_logger, get_trace_id,
)
from src.build_summary_db import SUMMARY_COLLECTION, summary_records_for_folder
from src.build_persona import INSIGHTS_COLLECTION, MAIL_COLLECTION
from src.graph.ingestion_graph import Deps, build_ingestion_graph
from src.graph.qa_graph import QADeps, build_qa_graph
from src.graph.qa_graph2 import QADeps2, build_two_stage_qa_graph
from src.ingest import select_folders
from src import index_version
from src.source_data import iter_collections
from src.agents.doc_agent import parse_folder_metadata
from src.llm.client import LLMClient
from src.mcp_layer.graph_store import GraphStore
from src.mcp_layer.vector_store import VectorStore
from src.retrieval.retrievers import GraphRetriever, VectorRetriever
from src.retrieval.two_stage import TwoStageRetriever

STATIC = Path(__file__).resolve().parent / "static"
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
RADIO_ROOT = WORKSPACE_ROOT / "Radio"
RADIO_DAEMON_PID = RADIO_ROOT / "data" / "logs" / "radio-daemon.pid"

configure_logging()
log = get_logger("radio_kg.server")

# in-process singletons populated at startup
_STACK = ExitStack()
# Many concurrent readers (QA) coexist; a writer (ingest step / KB apply)
# excludes both. Writer-priority avoids starving the rarer ingest path.
_RW = ReadWriteLock()
GRAPH = None
QA_GRAPH = None
QA2_GRAPH = None
STATS_AGENT: StatsAgent | None = None
QA_AGENT: QAAgent | None = None
MEMORY_AGENT: MemoryAgent | None = None
PERSONA_AGENT: PersonaAgent | None = None
GRAPH_STORE: GraphStore | None = None
CHUNK_STORE: VectorStore | None = None
SUMMARY_STORE: VectorStore | None = None
CONVS: ConversationStore | None = None
PENDING_STORE: PendingStore | None = None
SERVER_STARTED_AT: float = 0.0
RADIO_APP = None
RADIO_STARTUP = None
RADIO_LEGACY_API_ROOTS = {
    "artifacts",
    "collections",
    "credentials",
    "health",
    "jobs",
    "knowledge",
    "live-jobs",
    "metrics",
    "playlists",
    "profiles",
    "radiko-jobs",
    "scheduler",
    "video-jobs",
}
app = FastAPI(title="radio_kg 审批看板")
app.add_middleware(TraceIdMiddleware)


def _mount_radio_app() -> None:
    """Mount the producer console as /radio inside the radio_kg app."""
    global RADIO_APP, RADIO_STARTUP
    config_path = RADIO_ROOT / "config" / "config.yaml"
    if not config_path.exists():
        return

    os.environ.setdefault("RADIO_CONFIG", str(config_path))
    os.environ.setdefault("RADIO_PROJECT_ROOT", str(RADIO_ROOT))
    os.environ.setdefault("RADIO_PROFILES_DIR", str(RADIO_ROOT / "config" / "profiles"))
    os.environ.setdefault("RADIO_KG_AUTO_INGEST", "1")
    os.environ.setdefault("RADIO_KG_AUTO_INGEST_URL", "http://127.0.0.1:8000/api/ingest")

    from radio import api as radio_api

    RADIO_APP = radio_api.app
    RADIO_STARTUP = radio_api._startup
    app.mount("/radio", RADIO_APP, name="radio")


_mount_radio_app()


@app.on_event("startup")
async def _startup():
    global GRAPH, QA_GRAPH, QA2_GRAPH, STATS_AGENT, QA_AGENT, MEMORY_AGENT, PERSONA_AGENT, GRAPH_STORE, CHUNK_STORE, SUMMARY_STORE, CONVS, PENDING_STORE, SERVER_STARTED_AT
    SERVER_STARTED_AT = time.time()
    log.info("startup_begin")
    llm = LLMClient()
    GRAPH_STORE = _STACK.enter_context(GraphStore())
    vector = CHUNK_STORE = _STACK.enter_context(VectorStore())
    SUMMARY_STORE = _STACK.enter_context(VectorStore(collection_name=SUMMARY_COLLECTION))
    ckpt = _STACK.enter_context(
        SqliteSaver.from_conn_string(str(settings.abspath(settings.checkpoint_db)))
    )
    QA_AGENT = QAAgent(llm)
    deps = Deps(
        extractor=ExtractorAgent(llm, GRAPH_STORE),
        inspector=InspectorAgent(llm, GRAPH_STORE),
        sync=SyncAgent(GRAPH_STORE),
        vector=vector,
        annotator=AnnotatorAgent(llm),
        auto_policy=None,            # surface every interrupt to the dashboard
    )
    GRAPH = build_ingestion_graph(deps, ckpt)
    QA_GRAPH = build_qa_graph(QADeps(
        qa=QAAgent(llm),
        graph_retriever=GraphRetriever(GRAPH_STORE),
        vector_retriever=VectorRetriever(vector),
    ))
    QA2_GRAPH = build_two_stage_qa_graph(QADeps2(
        qa=QAAgent(llm),
        two_stage=TwoStageRetriever(SUMMARY_STORE, vector, GRAPH_STORE),
        top_n=settings.qa_top_n,
    ))
    STATS_AGENT = StatsAgent(llm, GRAPH_STORE, vector=vector,
                             mail_analytics=MailAnalytics(llm))
    MEMORY_AGENT = MemoryAgent(llm, GRAPH_STORE)
    insights_store = _STACK.enter_context(VectorStore(collection_name=INSIGHTS_COLLECTION))
    mail_store = _STACK.enter_context(VectorStore(collection_name=MAIL_COLLECTION))
    PERSONA_AGENT = PersonaAgent(llm, mail_store=mail_store, insights_store=insights_store)
    CONVS = ConversationStore(str(settings.abspath(settings.conversations_db)))
    pending_db_path = settings.abspath(settings.conversations_db).parent / "pending.sqlite"
    PENDING_STORE = PendingStore(str(pending_db_path))
    if RADIO_APP is not None and RADIO_STARTUP is not None:
        if getattr(RADIO_APP.state, "manager", None) is None:
            await RADIO_STARTUP()
    incomplete = PENDING_STORE.incomplete_threads()
    log.info("startup_done", extra={
        "pending_interrupts": len(PENDING_STORE.list_interrupts()),
        "incomplete_ingests": len(incomplete),
    })


@app.on_event("shutdown")
def _shutdown():
    _STACK.close()


# ── helpers ───────────────────────────────────────────────────────
def _step(payload, thread_id: str, label: str, folder: str | None = None) -> dict:
    """Drive a LangGraph ingestion step under an exclusive (writer) lock.

    Each stage transition is logged to PENDING_STORE.ingest_commit_log so that
    a partial failure across Neo4j / Chroma / index_version is visible after
    the fact rather than vanishing with the process.
    """
    config = {"configurable": {"thread_id": thread_id}}
    PENDING_STORE.log(thread_id, label, "graph_invoke", "started",
                      {"folder": folder, "trace_id": get_trace_id()})
    try:
        with _RW.writer():
            result = GRAPH.invoke(payload, config)
    except Exception as e:  # noqa: BLE001
        PENDING_STORE.log(thread_id, label, "graph_invoke", "failed",
                          {"error": str(e)[:500]})
        log.exception("ingest_failed", extra={"thread_id": thread_id, "label": label})
        raise
    if "__interrupt__" in result:
        value = result["__interrupt__"][0].value
        kind = "conflicts" if "conflicts" in value else "inspection_issues"
        entry = {
            "thread_id": thread_id,
            "label": label,
            "kind": kind,
            "items": value[kind],
            "folder": folder,
        }
        PENDING_STORE.put_interrupt(entry)
        PENDING_STORE.log(thread_id, label, "graph_invoke", "interrupted",
                          {"kind": kind, "n_items": len(value.get(kind, []))})
        log.info("ingest_interrupted", extra={
            "thread_id": thread_id, "label": label, "kind": kind,
        })
        return {"status": "interrupted", **entry}
    PENDING_STORE.pop_interrupt(thread_id)
    PENDING_STORE.log(thread_id, label, "graph_invoke", "ok",
                      {"written": len(result.get("written", []))})
    try:
        indexed_summary_sections = _index_summary_folder(folder)
        PENDING_STORE.log(thread_id, label, "index_summary", "ok",
                          {"sections": indexed_summary_sections})
    except Exception as e:  # noqa: BLE001
        PENDING_STORE.log(thread_id, label, "index_summary", "failed",
                          {"error": str(e)[:500]})
        log.exception("index_summary_failed", extra={"thread_id": thread_id})
        raise
    # auto incremental: graph + chunk + summary all just advanced together for
    # this episode, so re-stamp the unified version/fingerprint. Keeps the three
    # indices provably in sync (no "graph new, vectors old").
    try:
        _restamp_indices()
        PENDING_STORE.log(thread_id, label, "restamp_indices", "ok", {})
    except Exception as e:  # noqa: BLE001
        PENDING_STORE.log(thread_id, label, "restamp_indices", "failed",
                          {"error": str(e)[:500]})
    PENDING_STORE.log(thread_id, label, "ingest", "committed", {})
    log.info("ingest_committed", extra={
        "thread_id": thread_id, "label": label,
        "written": len(result.get("written", [])),
    })
    return {
        "status": "completed",
        "thread_id": thread_id,
        "written": result.get("written", []),
        "indexed_summary_sections": indexed_summary_sections,
        "conflicts": result.get("conflicts", []),
        "inspection_issues": len(result.get("inspection_issues", [])),
        "dropped": len(result.get("dropped", [])),
        "index_version": index_version.get(index_version.GRAPH).get("version"),
    }


def _restamp_indices() -> None:
    try:
        index_version.stamp(index_version.GRAPH, GRAPH_STORE.ingested_labels())
        if SUMMARY_STORE is not None:
            index_version.stamp(index_version.SUMMARY, SUMMARY_STORE.distinct_labels())
        if CHUNK_STORE is not None:
            index_version.stamp(index_version.CHUNK, CHUNK_STORE.distinct_labels())
    except Exception:
        pass


def _index_summary_folder(folder: str | None) -> int:
    if not folder or SUMMARY_STORE is None:
        return 0
    ids, docs, metas = summary_records_for_folder(Path(folder), settings.program_name)
    SUMMARY_STORE.add_chunks(ids, docs, metas)
    return len(ids)


# ── API ───────────────────────────────────────────────────────────
class IngestReq(BaseModel):
    episode: int | None = None
    dir: str | None = None


class ResumeReq(BaseModel):
    thread_id: str
    decisions: list[str]


@app.get("/api/episodes")
def episodes():
    data_dir = settings.abspath(settings.radio_data_dir)
    groups = iter_collections(data_dir, require_segments=True)
    done_eps = set(GRAPH_STORE.ingested_episodes())
    done_labels = set(GRAPH_STORE.ingested_labels())

    def is_ingested(meta) -> bool:
        if meta.episode is not None:
            return meta.episode in done_eps
        return meta.episode_label in done_labels

    collections = []
    total = ingested = 0
    for name in sorted(groups):
        eps = []
        for folder in groups[name]:
            meta = parse_folder_metadata(str(folder), settings.program_name)
            done = is_ingested(meta)
            total += 1
            ingested += int(done)
            eps.append({
                "episode": meta.episode,
                "label": folder.name,
                "program": meta.program,
                "broadcast_date": meta.broadcast_date,
                "dir": str(folder),
                "ingested": done,
            })
        collections.append({"name": name, "episodes": eps})
    return {
        "collections": collections,
        "stats": GRAPH_STORE.stats(),
        "total": total,
        "ingested": ingested,
    }


@app.get("/api/pending")
def pending():
    return {"pending": PENDING_STORE.list_interrupts()}


def _daemon_status() -> dict:
    """Liveness of the Radio APScheduler daemon (separate process)."""
    if not RADIO_DAEMON_PID.exists():
        return {"ok": False, "reason": "pid file missing", "path": str(RADIO_DAEMON_PID)}
    try:
        pid = int(RADIO_DAEMON_PID.read_text().strip())
    except (OSError, ValueError) as e:
        return {"ok": False, "reason": f"unreadable pid: {e}"}
    try:
        os.kill(pid, 0)
        return {"ok": True, "pid": pid}
    except ProcessLookupError:
        return {"ok": False, "reason": "process not running", "pid": pid}
    except PermissionError:
        # process exists but isn't owned by us → alive enough for health
        return {"ok": True, "pid": pid, "note": "not owned"}


@app.get("/api/health")
def health():
    """Per-component liveness probe.

    Components: Neo4j (via graph MCP), three vector collections (chunk, summary,
    conversations sqlite), the persistent pending store, and the Radio scheduler
    daemon. Overall `status` = "ok" iff every component reports ok; otherwise
    "degraded" so an external watchdog can page or restart.
    """
    components: dict[str, dict] = {}
    components["graph"] = GRAPH_STORE.ping() if GRAPH_STORE else {"ok": False, "reason": "not initialized"}
    components["chunk_vector"] = CHUNK_STORE.ping() if CHUNK_STORE else {"ok": False, "reason": "not initialized"}
    components["summary_vector"] = SUMMARY_STORE.ping() if SUMMARY_STORE else {"ok": False, "reason": "not initialized"}
    try:
        n_conv = len(CONVS.list()) if CONVS else -1
        components["conversations"] = {"ok": CONVS is not None, "n": n_conv}
    except Exception as e:  # noqa: BLE001
        components["conversations"] = {"ok": False, "error": str(e)[:200]}
    try:
        components["pending_store"] = {
            "ok": PENDING_STORE is not None,
            "pending_interrupts": len(PENDING_STORE.list_interrupts()) if PENDING_STORE else 0,
            "incomplete_ingests": len(PENDING_STORE.incomplete_threads()) if PENDING_STORE else 0,
        }
    except Exception as e:  # noqa: BLE001
        components["pending_store"] = {"ok": False, "error": str(e)[:200]}
    components["scheduler_daemon"] = _daemon_status()
    overall = "ok" if all(c.get("ok") for c in components.values()) else "degraded"
    return {
        "status": overall,
        "uptime_sec": round(time.time() - SERVER_STARTED_AT, 1),
        "trace_id": get_trace_id(),
        "components": components,
    }


@app.get("/api/ingest_log/{thread_id}")
def ingest_log(thread_id: str):
    """Per-thread commit log — see which stages succeeded/failed for an ingest."""
    return {"thread_id": thread_id,
            "entries": PENDING_STORE.thread_log(thread_id)}


@app.get("/api/index_status")
def index_status():
    """Unified index versions + drift: are graph / chunk / summary indices in
    sync, or has one advanced past the others?"""
    graph_labels = GRAPH_STORE.ingested_labels() if GRAPH_STORE else []
    chunk_labels = CHUNK_STORE.distinct_labels() if CHUNK_STORE else set()
    summary_labels = SUMMARY_STORE.distinct_labels() if SUMMARY_STORE else set()
    return index_version.status(graph_labels=graph_labels,
                                chunk_labels=chunk_labels,
                                summary_labels=summary_labels)


@app.post("/api/ingest")
def ingest(req: IngestReq):
    folders = select_folders(req.episode, False, req.dir)
    folder = folders[0]
    label = Path(folder).name
    # fresh thread per run: prior completed checkpoints + additive state reducers
    # would otherwise accumulate chunks/triples across re-ingests.
    thread_id = f"{label}::{uuid4().hex[:8]}"
    return _step({"episode_dir": folder}, thread_id, label, folder)


@app.post("/api/resume")
def resume(req: ResumeReq):
    entry = PENDING_STORE.get_interrupt(req.thread_id)
    if entry is None:
        raise HTTPException(404, "no pending interrupt for this thread")
    return _step(
        Command(resume=req.decisions),
        req.thread_id,
        entry.get("label", req.thread_id),
        entry.get("folder"),
    )


class AskReq(BaseModel):
    question: str
    mode: str = "qa"   # "qa" (factual RAG) | "mail" (host replies to it as お便り)


def _pack(passages):
    return [{"text": p.text, "citation": p.citation, "origin": p.origin}
            for p in (passages or [])]


# answer memo for repeated first-turn questions. Keyed on the normalized
# question + the index registry's mtime token, so any ingest (which re-stamps
# the registry) naturally invalidates every cached answer.
_ANSWER_CACHE: OrderedDict[tuple, dict] = OrderedDict()
_ANSWER_CACHE_CAP = 128
# overlaps the routing LLM call with the analyze LLM call (both ~3s, serial
# before; the analyze result is simply discarded on stats/dossier routes)
_QA_PREFETCH = ThreadPoolExecutor(max_workers=2, thread_name_prefix="qa-prefetch")


def _index_token() -> int:
    try:
        return index_version.REGISTRY_PATH.stat().st_mtime_ns
    except OSError:
        return 0


def _run_qa(question: str, history: list[dict] | None = None,
            progress=None) -> dict:
    """Cached wrapper around _run_qa_uncached (first-turn questions only —
    follow-ups depend on conversation history)."""
    if history:
        return _run_qa_uncached(question, history, progress)
    key = (" ".join(question.split()), _index_token())
    hit = _ANSWER_CACHE.get(key)
    if hit is not None:
        _ANSWER_CACHE.move_to_end(key)
        log.info("qa_cache_hit", extra={"q_len": len(question)})
        return dict(hit)
    result = _run_qa_uncached(question, None, progress)
    if result.get("answer"):
        _ANSWER_CACHE[key] = dict(result)
        while len(_ANSWER_CACHE) > _ANSWER_CACHE_CAP:
            _ANSWER_CACHE.popitem(last=False)
    return result


def _run_qa_uncached(question: str, history: list[dict] | None = None,
                     progress=None) -> dict:
    """Shared QA: route stats vs two-stage retrieval. `history` enables multi-turn.

    `progress` is an optional callable(text) used by the streaming endpoint to
    surface live stage updates; it is a no-op for the plain POST path.

    Caller must hold _RW.reader() (read lock around MCP-touching steps).
    """
    emit = progress or (lambda _text: None)
    standalone = QA_AGENT.contextualize(history, question) if history else question
    emit("规划检索策略…")
    analysis_future = _QA_PREFETCH.submit(QA_AGENT.analyze, standalone)
    route = STATS_AGENT.route(standalone)
    if route["kind"] == "dossier":
        emit(f"调取「{route['name']}」的全部记录…")
        d = STATS_AGENT.dossier(route["name"], standalone)
        if d is not None:
            return {"question": question, "standalone": standalone, "answer": d["answer"],
                    "anchors": [], "intent": "实体档案", "search_query": "", "search_queries": [],
                    "graph_hits": d["n_records"], "vector_hits": 0,
                    "debug": {"names": d["names"], "episodes": d["n_episodes"]},
                    "sources": d["sources"], "mode": "dossier"}
        # name did not resolve in the graph → fall back to normal retrieval
    if route["kind"] == "stats":
        emit("统计聚合中…")
        r = STATS_AGENT.answer(standalone)
        if not r.get("fallback"):
            srcs = r.get("sources", [])
            return {"question": question, "standalone": standalone, "answer": r["answer"],
                    "anchors": [], "intent": "统计聚合", "search_query": "", "search_queries": [],
                    "graph_hits": 0, "vector_hits": len(srcs), "debug": {}, "sources": srcs,
                    "mode": "stats", "tool": r.get("tool")}
        # no safe stats tool matched → fall back to normal retrieval
    emit("检索资料中…")
    try:
        analysis = analysis_future.result()
    except Exception:  # noqa: BLE001 — analyze 失败由图内节点重试/兜底
        analysis = {}
    # stream the two-stage graph node-by-node so the streaming endpoint can
    # report retrieval vs. generation progress; accumulating per-node updates
    # reconstructs the same final state as .invoke().
    result: dict = {}
    for upd in QA2_GRAPH.stream({"question": standalone, "history": history or [],
                                 **analysis}):
        for node, out in upd.items():
            if out:
                result.update(out)
            if node == "retrieve2":
                emit(f"已召回 {len(result.get('fused', []))} 条资料，生成并校验答案…")
    return {
        "question": question,
        "standalone": standalone,
        "answer": result.get("answer", ""),
        "anchors": result.get("anchors", []),
        "intent": result.get("intent", ""),
        "search_query": result.get("search_query", ""),
        "search_queries": result.get("search_queries", []),
        "graph_hits": result.get("debug", {}).get("n_graph", 0),
        "vector_hits": len(result.get("fused", [])),
        "debug": result.get("debug", {}),
        "sources": _pack(result.get("fused", [])),
        "mode": "two_stage",
    }


@app.post("/api/ask")
def ask(req: AskReq):
    log.info("ask", extra={"mode": req.mode, "q_len": len(req.question or "")})
    with _RW.reader():
        return _run_qa(req.question)


@app.post("/api/ask2")
def ask2(req: AskReq):
    """Two-stage (coarse summary route -> fine window) QA."""
    log.info("ask2", extra={"q_len": len(req.question or "")})
    with _RW.reader():
        result = QA2_GRAPH.invoke({"question": req.question})
    fused = result.get("fused", [])
    return {
        "question": req.question,
        "answer": result.get("answer", ""),
        "anchors": result.get("anchors", []),
        "search_query": result.get("search_query", ""),
        "search_queries": result.get("search_queries", []),
        "debug": result.get("debug", {}),
        "sources": [{"text": p.text, "citation": p.citation, "origin": p.origin}
                    for p in fused],
    }


# ── conversations (multi-turn chat, SQLite-backed) ─────────────────
@app.get("/api/conversations")
def list_conversations():
    return {"conversations": CONVS.list()}


@app.post("/api/conversations")
def create_conversation():
    return CONVS.create()


@app.get("/api/conversations/{cid}")
def get_conversation(cid: str):
    c = CONVS.get(cid)
    if c is None:
        raise HTTPException(404, "no such conversation")
    return c


@app.delete("/api/conversations/{cid}")
def delete_conversation(cid: str):
    if not CONVS.delete(cid):
        raise HTTPException(404, "no such conversation")
    return {"ok": True}


@app.post("/api/conversations/{cid}/ask")
def ask_in_conversation(cid: str, req: AskReq):
    if not CONVS.exists(cid):
        raise HTTPException(404, "no such conversation")
    CONVS.add_message(cid, "user", req.question, auto_title=True)

    # explicit "记住：/订正：…" messages are knowledge-base edits: parse and preview,
    # do NOT write the graph until the user confirms.
    if MEMORY_AGENT.is_kb_update(req.question):
        with _RW.reader():
            ops = MEMORY_AGENT.parse(req.question)
        if not ops:
            content = "未能从这条订正中解析出明确的事实（主语—关系—对象）。可换种说法，例如「订正：羊宮妃那 所属 青二プロダクション」。"
            CONVS.add_message(cid, "assistant", content, {"kind": "text"})
            return {"kind": "text", "answer": content, "conversation": CONVS.get(cid)}
        edit_id = uuid4().hex[:12]
        PENDING_STORE.put_kb_edit(edit_id, cid, ops)
        lines = [MemoryAgent.preview_line(o) for o in ops]
        content = "请确认以下知识库变更（以你所说为准，原广播事实将保留为历史线）：\n" + "\n".join(lines)
        CONVS.add_message(cid, "assistant", content,
                          {"kind": "kb_preview", "edit_id": edit_id, "ops": ops})
        return {"kind": "kb_preview", "edit_id": edit_id, "ops": ops,
                "answer": content, "conversation": CONVS.get(cid)}

    history = CONVS.history(cid)[:-1]  # exclude the just-added current question

    # 来信模式: treat the message as listener mail, reply in the host's on-air
    # style — bilingual (ZH default + JA), toggled in the reply card.
    if req.mode == "mail":
        with _RW.reader():
            resp = PERSONA_AGENT.reply_mail(req.question, history)
        meta = {"kind": "mail", "sources": resp["sources"], "mode": "mail",
                "answer_ja": resp["answer_ja"]}
        CONVS.add_message(cid, "assistant", resp["answer_zh"], meta)
        return {"kind": "mail", "conversation": CONVS.get(cid),
                "answer": resp["answer_zh"], **meta}

    with _RW.reader():
        resp = _run_qa(req.question, history)
    meta = {"kind": "qa", "sources": resp["sources"], "anchors": resp.get("anchors", []),
            "search_queries": resp.get("search_queries", []),
            "graph_hits": resp.get("graph_hits", 0), "vector_hits": resp.get("vector_hits", 0),
            "mode": resp.get("mode", ""), "standalone": resp.get("standalone", req.question)}
    summary = CONVS.add_message(cid, "assistant", resp["answer"], meta)
    return {"kind": "qa", "conversation": summary, "answer": resp["answer"], **meta}


class KbConfirmReq(BaseModel):
    edit_id: str
    confirm: bool = True


@app.post("/api/conversations/{cid}/kb_confirm")
def kb_confirm(cid: str, req: KbConfirmReq):
    entry = PENDING_STORE.pop_kb_edit(req.edit_id)
    if entry is None or entry["conv_id"] != cid:
        raise HTTPException(404, "no such pending knowledge edit")
    if not req.confirm:
        content = "已取消，未写入知识库。"
        CONVS.add_message(cid, "assistant", content, {"kind": "text"})
        log.info("kb_cancelled", extra={"edit_id": req.edit_id, "conv_id": cid})
        return {"status": "cancelled", "answer": content, "conversation": CONVS.get(cid)}
    with _RW.writer():
        results = MEMORY_AGENT.apply(entry["ops"])
    _ANSWER_CACHE.clear()   # KB edits change answers without re-stamping indices
    log.info("kb_applied", extra={"edit_id": req.edit_id, "conv_id": cid,
                                  "n_ops": len(entry["ops"])})
    lines = [f"{MemoryAgent.preview_line(o)} — {o['status']}" for o in results]
    content = "已写入知识库（用户订正）：\n" + "\n".join(lines)
    CONVS.add_message(cid, "assistant", content, {"kind": "kb_applied", "results": results})
    return {"status": "applied", "results": results, "answer": content, "conversation": CONVS.get(cid)}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False)}\n\n"


@app.get("/api/conversations/{cid}/ask_stream")
def ask_in_conversation_stream(cid: str, q: str, mode: str = "qa"):
    """SSE variant of /ask: emits staged progress events while the answer is
    being routed, retrieved and generated, then a final `done` event carrying
    the new assistant message id. The answer itself is persisted and re-fetched
    by the client (the verified/structured pipeline produces text only after
    generation+verification, so we stream stages, not raw tokens)."""
    if not CONVS.exists(cid):
        raise HTTPException(404, "no such conversation")

    def gen():
        try:
            CONVS.add_message(cid, "user", q, auto_title=True)

            if MEMORY_AGENT.is_kb_update(q):
                yield _sse({"type": "stage", "text": "解析订正…"})
                with _RW.reader():
                    ops = MEMORY_AGENT.parse(q)
                if not ops:
                    content = ("未能从这条订正中解析出明确的事实（主语—关系—对象）。"
                               "可换种说法，例如「订正：羊宮妃那 所属 青二プロダクション」。")
                    s = CONVS.add_message(cid, "assistant", content, {"kind": "text"})
                    yield _sse({"type": "done", "message_id": s.get("last_message_id")})
                    return
                edit_id = uuid4().hex[:12]
                PENDING_STORE.put_kb_edit(edit_id, cid, ops)
                lines = [MemoryAgent.preview_line(o) for o in ops]
                content = ("请确认以下知识库变更（以你所说为准，原广播事实将保留为历史线）：\n"
                           + "\n".join(lines))
                s = CONVS.add_message(cid, "assistant", content,
                                      {"kind": "kb_preview", "edit_id": edit_id, "ops": ops})
                yield _sse({"type": "done", "message_id": s.get("last_message_id")})
                return

            history = CONVS.history(cid)[:-1]

            if mode == "mail":
                yield _sse({"type": "stage", "text": "正在读你的来信…"})
                with _RW.reader():
                    yield _sse({"type": "stage", "text": "组织回信中…"})
                    resp = PERSONA_AGENT.reply_mail(q, history)
                meta = {"kind": "mail", "sources": resp["sources"], "mode": "mail",
                        "answer_ja": resp["answer_ja"]}
                s = CONVS.add_message(cid, "assistant", resp["answer_zh"], meta)
                yield _sse({"type": "done", "message_id": s.get("last_message_id")})
                return

            # _run_qa is one blocking call with several internal stages. Run it
            # on a worker thread and stream its progress-callback texts through a
            # queue so the client sees each stage live (not all at the end).
            events: queue.Queue = queue.Queue()
            box: dict = {}

            def _worker():
                try:
                    with _RW.reader():
                        box["resp"] = _run_qa(
                            q, history, progress=lambda t: events.put(("stage", t)))
                except Exception as e:  # noqa: BLE001
                    box["error"] = str(e)
                    log.exception("ask_stream qa worker failed")
                finally:
                    events.put(("end", None))

            worker = threading.Thread(target=_worker, daemon=True)
            worker.start()
            while True:
                kind, payload = events.get()
                if kind == "stage":
                    yield _sse({"type": "stage", "text": payload})
                else:
                    break
            worker.join()
            if "error" in box:
                yield _sse({"type": "error", "detail": box["error"]})
                return
            resp = box["resp"]
            meta = {"kind": "qa", "sources": resp["sources"], "anchors": resp.get("anchors", []),
                    "search_queries": resp.get("search_queries", []),
                    "graph_hits": resp.get("graph_hits", 0),
                    "vector_hits": resp.get("vector_hits", 0),
                    "mode": resp.get("mode", ""),
                    "standalone": resp.get("standalone", q)}
            s = CONVS.add_message(cid, "assistant", resp["answer"], meta)
            yield _sse({"type": "done", "message_id": s.get("last_message_id")})
        except Exception as e:  # noqa: BLE001 - surface any failure to the client
            log.exception("ask_stream failed")
            yield _sse({"type": "error", "detail": str(e)})

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


class FeedbackReq(BaseModel):
    message_id: int
    value: str | None = None  # "up" | "down" | None (clear)


@app.post("/api/conversations/{cid}/feedback")
def set_feedback(cid: str, req: FeedbackReq):
    if req.value not in (None, "up", "down"):
        raise HTTPException(400, "value must be 'up', 'down' or null")
    if not CONVS.set_feedback(cid, req.message_id, req.value):
        raise HTTPException(404, "no such assistant message")
    log.info("feedback", extra={"conv_id": cid, "message_id": req.message_id,
                                "value": req.value or "clear"})
    return {"ok": True}


def _is_legacy_radio_api_path(radio_path: str) -> bool:
    root = radio_path.split("/", 1)[0]
    return root in RADIO_LEGACY_API_ROOTS


@app.api_route(
    "/api/{radio_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    include_in_schema=False,
)
async def legacy_radio_api(request: Request, radio_path: str):
    if RADIO_APP is None or not _is_legacy_radio_api_path(radio_path):
        raise HTTPException(404, "Not Found")
    target = f"/radio/api/{radio_path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return RedirectResponse(target, status_code=307)


@app.websocket("/api/jobs/ws")
async def legacy_radio_jobs_ws(websocket: WebSocket) -> None:
    if RADIO_APP is None:
        await websocket.close(code=1011)
        return
    scope = dict(websocket.scope)
    scope["app"] = RADIO_APP
    scope["root_path"] = ""
    await RADIO_APP(scope, websocket.receive, websocket.send)


@app.get("/")
def index():
    # 主页：三个入口（对话 / Radio 录制 / 直播录制和切片）
    return FileResponse(STATIC / "home.html", headers={"Cache-Control": "no-store"})


@app.get("/chat")
def chat_page():
    return FileResponse(STATIC / "chat.html", headers={"Cache-Control": "no-store"})


@app.get("/dashboard")
def dashboard():
    return FileResponse(STATIC / "index.html")


@app.get("/ask")
def ask_page():
    return FileResponse(STATIC / "ask.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")

# 「直播录制和切片」独立项目 Agent/clip/ 的路由（/clipper）。clip 装在工作区 venv，
# 缺失不影响主服务。
try:
    from clip.server_routes import router as _clipper_router
    app.include_router(_clipper_router)
except Exception as _e:  # noqa: BLE001 — clip 路由缺失不影响主服务
    log.warning("clip 路由未挂载：%s", _e)


@app.get("/assets/app.js", include_in_schema=False)
def legacy_root_radio_app_js():
    return PlainTextResponse(
        'window.location.replace("/?radio_kg_refresh=" + Date.now());\n',
        media_type="text/javascript",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/assets/{asset_path:path}", include_in_schema=False)
def legacy_root_radio_assets(asset_path: str):
    return RedirectResponse(f"/radio/assets/{asset_path}", status_code=307)
