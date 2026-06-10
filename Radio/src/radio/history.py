"""轻量级历史摘要库：每期 Summary 关键短评写一行 JSONL 累积。

用途：调 Gemini 总结新一期时，把同节目的最近 N 期 key_topics + highlights
作为「往期回忆」上下文喂给 LLM，让本期总结能感知到"上次提过 X，本期是否提及"。

数据结构（每行 JSON）：
{
  "timestamp": "2026-05-16T12:34:56+00:00",     # 写入时刻
  "program_series": "MyGO!!!!!の「迷子集会」",   # extract_series_name 结果
  "program_name": "MyGO!!!!!の「迷子集会」#178",  # 完整标题（带期数）
  "air_date": "2026-05-13",
  "key_topics": ["...", "..."],                   # 1-2 句一条，最多 6 条
  "highlight_quotes": ["...", "..."]              # 高光的 quote 摘要，最多 5 条
}

文件位置：`data/history_context.jsonl`（追加写）。
"""

from __future__ import annotations

import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger

from radio.models import Summary
from radio.segments_library import extract_series_name


def append_history_entry(
    jsonl_path: Path,
    program_name: str,
    air_date: str,
    summary: Summary,
) -> None:
    """把本期 Summary 简短抽取后 append 到 history JSONL。"""
    series = extract_series_name(program_name) or program_name
    entry = {
        "timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "program_series": series,
        "program_name": program_name,
        "air_date": air_date,
        "key_topics": [_clean(t) for t in (summary.key_topics or [])][:6],
        "highlight_quotes": [
            _clean(h.quote or h.reason)
            for h in (summary.highlights or [])
        ][:5],
    }
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    logger.info(
        f"history append → {jsonl_path.name}: {series} / {air_date} "
        f"({len(entry['key_topics'])} topics, "
        f"{len(entry['highlight_quotes'])} quotes)"
    )


def load_recent_history(
    jsonl_path: Path,
    program_name: str,
    *,
    limit: int = 5,
    air_date_before: str | None = None,
) -> list[dict]:
    """读最近 limit 期同节目的历史条目。

    Args:
        jsonl_path: history_context.jsonl 路径
        program_name: 当前节目完整标题（用 extract_series_name 过滤）
        limit: 最多返回几条（默认 5）
        air_date_before: 只看在该日期之前的历史（避免把当期自己也拉进来）。
            None 表示不过滤。
    """
    if not jsonl_path.exists():
        return []
    target_series = extract_series_name(program_name) or program_name

    rows: list[dict] = []
    try:
        with jsonl_path.open("r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                except json.JSONDecodeError:
                    continue
                if row.get("program_series") != target_series:
                    continue
                if air_date_before and row.get("air_date", "") >= air_date_before:
                    continue
                rows.append(row)
    except Exception as e:
        logger.warning(f"load_recent_history 读 jsonl 失败：{e!r}")
        return []

    # 取最近 N 条（按 air_date 排序）
    rows.sort(key=lambda r: r.get("air_date", ""), reverse=True)
    return rows[:limit]


def load_relevant_history(
    jsonl_path: Path,
    program_name: str,
    *,
    query_text: str,
    limit: int = 5,
    air_date_before: str | None = None,
) -> list[dict]:
    """Load history with lexical relevance plus time decay.

    This is the local, dependency-free version of the PRD's time-weighted RAG.
    It keeps the same JSONL storage and can later be swapped for embeddings.
    """
    rows = load_recent_history(
        jsonl_path,
        program_name,
        limit=200,
        air_date_before=air_date_before,
    )
    if not rows:
        return []

    query_terms = _terms(query_text)
    has_recall_trigger = _has_recall_trigger(query_text)
    scored: list[tuple[float, dict]] = []
    for row in rows:
        entry_text = " ".join(
            [
                str(row.get("program_name") or ""),
                " ".join(row.get("key_topics") or []),
                " ".join(row.get("highlight_quotes") or []),
            ]
        )
        entry_terms = _terms(entry_text)
        overlap = len(query_terms & entry_terms)
        lexical = overlap / math.sqrt(max(len(entry_terms), 1))
        recency = _recency_score(row.get("air_date", ""), air_date_before)

        # When the current transcript contains explicit recall words, relevance
        # should dominate. Otherwise, keep recent episodes useful as context.
        score = lexical * 3.0 + recency if has_recall_trigger else lexical * 1.5 + recency
        row = {**row, "rag_score": round(score, 4)}
        scored.append((score, row))

    scored.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in scored[:limit]]


def format_history_for_prompt(entries: list[dict]) -> str:
    """把 history 条目压成 LLM prompt 用的短文本。"""
    if not entries:
        return "（暂无往期记录）"

    lines: list[str] = []
    for e in entries:
        air = e.get("air_date", "?")
        name = e.get("program_name", "?")
        lines.append(f"━ {air}  {name}")
        topics = e.get("key_topics") or []
        if topics:
            lines.append("  关键话题：")
            for t in topics:
                lines.append(f"    · {t}")
        quotes = e.get("highlight_quotes") or []
        if quotes:
            lines.append("  高光摘录：")
            for q in quotes:
                lines.append(f"    · {q}")
    return "\n".join(lines)


def _clean(text: str) -> str:
    """把字符串里的换行、多空格压平，方便单行存储/展示。"""
    if not text:
        return ""
    return " ".join(text.split())


def _has_recall_trigger(text: str) -> bool:
    triggers = (
        "上次",
        "之前",
        "以前",
        "前回",
        "この前",
        "前に",
        "前の",
        "先週",
        "前期",
        "上期",
    )
    return any(trigger in text for trigger in triggers)


def _terms(text: str) -> set[str]:
    normalized = re.sub(r"\s+", "", text.lower())
    terms = set(re.findall(r"[a-z0-9]{2,}", normalized))
    cjk = re.sub(r"[^\u3040-\u30ff\u3400-\u9fff]", "", normalized)
    terms.update(cjk[i : i + 2] for i in range(max(len(cjk) - 1, 0)))
    return terms


def _recency_score(air_date: str, before: str | None) -> float:
    if not air_date or not before:
        return 0.5
    try:
        current = datetime.fromisoformat(before).date()
        past = datetime.fromisoformat(air_date).date()
    except ValueError:
        return 0.5
    days = max((current - past).days, 0)
    return 1.0 / (1.0 + days / 30.0)
