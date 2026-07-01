#!/usr/bin/env python3
"""Quantify the Agent workspace from real local artifacts.

The script is intentionally offline-first: by default it scans generated files
and historical benchmark outputs instead of calling LLMs, Neo4j, Chroma clients,
or the running FastAPI service. This makes the report suitable for a resume /
portfolio evidence pack and cheap enough to run after every iteration.

Run:
  .venv/bin/python scripts/quantify_project.py
  .venv/bin/python scripts/quantify_project.py --strict
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sqlite3
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


EPISODE_RE = re.compile(r"#\s*(\d+)")
TIME_RANGE_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?)\s*[-~〜ー]\s*"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?)"
)


@dataclass(frozen=True)
class Check:
    name: str
    value: float
    target: str
    ok: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "target": self.target,
            "ok": self.ok,
        }


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    idx = min(len(xs) - 1, int(round((pct / 100) * (len(xs) - 1))))
    return xs[idx]


def pct(part: float, total: float) -> float:
    return (part / total) if total else 0.0


def sec_from_ts(ts: str) -> float | None:
    parts = [p for p in ts.split(":") if p]
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def duration_from_time_range(raw: Any) -> float:
    if not isinstance(raw, str):
        return 0.0
    match = TIME_RANGE_RE.search(raw)
    if not match:
        return 0.0
    start = sec_from_ts(match.group("start"))
    end = sec_from_ts(match.group("end"))
    if start is None or end is None:
        return 0.0
    return max(0.0, end - start)


def episode_number(path: Path) -> int | None:
    match = EPISODE_RE.search(path.name)
    return int(match.group(1)) if match else None


def find_episode_dirs(recordings_dir: Path) -> list[Path]:
    if not recordings_dir.exists():
        return []
    markers = {"03_ja_segments.json", "04_bilingual_segments.json", "05_summary.json"}
    return sorted(
        p
        for p in recordings_dir.rglob("*")
        if p.is_dir() and any((p / marker).exists() for marker in markers)
    )


def collection_name(recordings_dir: Path, episode_dir: Path) -> str:
    try:
        rel = episode_dir.relative_to(recordings_dir)
    except ValueError:
        return "(outside)"
    return rel.parts[0] if rel.parts else "(root)"


def analyze_recordings(root: Path) -> dict[str, Any]:
    recordings_dir = root / "Radio" / "data" / "recordings"
    episode_dirs = find_episode_dirs(recordings_dir)
    by_collection: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "episode_dirs": 0,
            "complete_episode_dirs": 0,
            "segments": 0,
            "audio_hours": 0.0,
            "sections": 0,
            "songs": 0,
        }
    )
    complete_dirs = 0
    total_segments = 0
    bilingual_segments = 0
    translated_segments = 0
    total_ja_chars = 0
    total_zh_chars = 0
    total_audio_seconds = 0.0
    summary_files = 0
    total_sections = 0
    total_highlights = 0
    total_topics = 0
    recurring_sections = 0
    listener_mail_sections = 0
    music_mentions = 0
    missing: Counter[str] = Counter()
    unique_episode_keys: set[tuple[str, int]] = set()

    for ep_dir in episode_dirs:
        collection = collection_name(recordings_dir, ep_dir)
        files = {
            "03": ep_dir / "03_ja_segments.json",
            "04": ep_dir / "04_bilingual_segments.json",
            "05": ep_dir / "05_summary.json",
        }
        present = {key for key, path in files.items() if path.exists()}
        for key in files:
            if key not in present:
                missing[key] += 1
        complete = len(present) == 3
        complete_dirs += int(complete)

        ep_no = episode_number(ep_dir)
        if ep_no is not None:
            unique_episode_keys.add((collection, ep_no))

        segments = load_json(files["03"], [])
        if not isinstance(segments, list):
            segments = []
        bilingual = load_json(files["04"], [])
        if not isinstance(bilingual, list):
            bilingual = []

        seg_count = len(segments)
        total_segments += seg_count
        bilingual_segments += len(bilingual)
        translated_segments += sum(1 for item in bilingual if str(item.get("zh", "")).strip())
        total_ja_chars += sum(len(str(item.get("ja", ""))) for item in segments)
        total_zh_chars += sum(len(str(item.get("zh", ""))) for item in bilingual)

        starts = [to_float(item.get("start"), math.nan) for item in segments]
        ends = [to_float(item.get("end"), math.nan) for item in segments]
        starts = [x for x in starts if not math.isnan(x)]
        ends = [x for x in ends if not math.isnan(x)]
        duration_s = max(0.0, (max(ends) - min(starts))) if starts and ends else 0.0

        ep_sections_count = 0
        ep_music_mentions = 0
        summary = load_json(files["05"], {})
        if isinstance(summary, dict) and summary:
            summary_files += 1
            sections = summary.get("sections", [])
            if not isinstance(sections, list):
                sections = []
            ep_sections_count = len(sections)
            total_sections += ep_sections_count
            total_highlights += len(summary.get("highlights", []) or [])
            total_topics += len(summary.get("key_topics", []) or [])
            recurring_sections += sum(1 for s in sections if isinstance(s, dict) and s.get("is_recurring"))
            listener_mail_sections += sum(
                1
                for s in sections
                if isinstance(s, dict)
                and (
                    str(s.get("listener_mail_from", "")).strip()
                    or str(s.get("listener_mail", "")).strip()
                )
            )
            for section in sections:
                if not isinstance(section, dict):
                    continue
                music = section.get("music") or []
                if isinstance(music, list):
                    ep_music_mentions += len(music)
                if duration_s <= 0:
                    duration_s += duration_from_time_range(section.get("time_range"))

        music_mentions += ep_music_mentions
        total_audio_seconds += duration_s
        coll = by_collection[collection]
        coll["episode_dirs"] += 1
        coll["complete_episode_dirs"] += int(complete)
        coll["segments"] += seg_count
        coll["audio_hours"] += duration_s / 3600
        coll["sections"] += ep_sections_count
        coll["songs"] += ep_music_mentions

    for coll in by_collection.values():
        coll["audio_hours"] = round(coll["audio_hours"], 2)

    return {
        "recordings_dir": str(recordings_dir),
        "collections": len(by_collection),
        "episode_dirs": len(episode_dirs),
        "complete_episode_dirs": complete_dirs,
        "complete_rate": round(pct(complete_dirs, len(episode_dirs)), 4),
        "unique_episode_numbers_by_collection": len(unique_episode_keys),
        "missing_files": dict(missing),
        "audio_hours": round(total_audio_seconds / 3600, 2),
        "segments": total_segments,
        "bilingual_segments": bilingual_segments,
        "translated_segments": translated_segments,
        "translation_coverage": round(pct(translated_segments, bilingual_segments), 4),
        "ja_chars": total_ja_chars,
        "zh_chars": total_zh_chars,
        "summary_files": summary_files,
        "sections": total_sections,
        "highlights": total_highlights,
        "key_topics": total_topics,
        "recurring_sections": recurring_sections,
        "listener_mail_sections": listener_mail_sections,
        "music_mentions": music_mentions,
        "by_collection": dict(sorted(by_collection.items())),
    }


def analyze_radio_metrics(root: Path) -> dict[str, Any]:
    rows = iter_jsonl(root / "Radio" / "data" / "logs" / "metrics.jsonl")
    if not rows:
        return {"runs": 0}

    success_rows = [r for r in rows if r.get("success") is True]
    durations = [to_float(r.get("duration_s")) for r in rows if r.get("duration_s") is not None]
    success_durations = [
        to_float(r.get("duration_s")) for r in success_rows if r.get("duration_s") is not None
    ]
    by_source: dict[str, dict[str, Any]] = defaultdict(lambda: {"runs": 0, "success": 0})
    step_sums: Counter[str] = Counter()
    step_counts: Counter[str] = Counter()
    total_tokens = 0
    input_tokens = 0
    output_tokens = 0
    for row in rows:
        source = str(row.get("source") or "unknown")
        by_source[source]["runs"] += 1
        by_source[source]["success"] += int(row.get("success") is True)
        total_tokens += int(to_float(row.get("total_tokens")))
        input_tokens += int(to_float(row.get("input_tokens")))
        output_tokens += int(to_float(row.get("output_tokens")))
        for step, seconds in (row.get("step_durations") or {}).items():
            step_sums[step] += to_float(seconds)
            step_counts[step] += 1

    for value in by_source.values():
        value["success_rate"] = round(pct(value["success"], value["runs"]), 4)

    return {
        "runs": len(rows),
        "successful_runs": len(success_rows),
        "success_rate": round(pct(len(success_rows), len(rows)), 4),
        "segments_processed": sum(int(to_float(r.get("segments_count"))) for r in rows),
        "telegram_messages_sent": sum(int(to_float(r.get("telegram_messages_sent"))) for r in rows),
        "duration_s_mean": round(mean(durations) or 0.0, 2),
        "success_duration_s_mean": round(mean(success_durations) or 0.0, 2),
        "success_duration_s_p95": round(percentile(success_durations, 95) or 0.0, 2),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
        "by_source": dict(sorted(by_source.items())),
        "avg_step_seconds": {
            step: round(step_sums[step] / step_counts[step], 2)
            for step in sorted(step_sums)
            if step_counts[step]
        },
    }


def analyze_cost_ledger(root: Path) -> dict[str, Any]:
    rows = iter_jsonl(root / "radio_kg" / "data" / "cost_ledger.jsonl")
    if not rows:
        return {"records": 0}

    by_kind: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "records": 0,
            "llm_calls": 0,
            "total_tokens": 0,
            "cost_usd": 0.0,
            "wall_s": [],
            "llm_s": [],
        }
    )
    for row in rows:
        kind = str(row.get("kind") or "unknown")
        item = by_kind[kind]
        item["records"] += 1
        item["llm_calls"] += int(to_float(row.get("n_calls")))
        item["total_tokens"] += int(to_float(row.get("total_tokens")))
        item["cost_usd"] += to_float(row.get("cost_usd"))
        if row.get("wall_s") is not None:
            item["wall_s"].append(to_float(row.get("wall_s")))
        if row.get("llm_s") is not None:
            item["llm_s"].append(to_float(row.get("llm_s")))

    normalized: dict[str, dict[str, Any]] = {}
    for kind, item in sorted(by_kind.items()):
        records = item["records"]
        normalized[kind] = {
            "records": records,
            "llm_calls": item["llm_calls"],
            "total_tokens": item["total_tokens"],
            "cost_usd": round(item["cost_usd"], 6),
            "avg_cost_usd": round(item["cost_usd"] / records, 6) if records else 0.0,
            "avg_wall_s": round(mean(item["wall_s"]) or 0.0, 2),
            "p95_wall_s": round(percentile(item["wall_s"], 95) or 0.0, 2),
            "avg_llm_s": round(mean(item["llm_s"]) or 0.0, 2),
        }

    return {
        "records": len(rows),
        "total_cost_usd": round(sum(to_float(r.get("cost_usd")) for r in rows), 6),
        "total_tokens": sum(int(to_float(r.get("total_tokens"))) for r in rows),
        "by_kind": normalized,
    }


def analyze_chroma(root: Path) -> dict[str, Any]:
    db = root / "radio_kg" / "data" / "chroma" / "chroma.sqlite3"
    if not db.exists():
        return {"available": False}
    try:
        con = sqlite3.connect(db)
        rows = con.execute(
            """
            SELECT c.name, COUNT(e.id)
            FROM collections c
            LEFT JOIN segments s ON s.collection = c.id
            LEFT JOIN embeddings e ON e.segment_id = s.id
            GROUP BY c.name
            ORDER BY c.name
            """
        ).fetchall()
    except sqlite3.Error as exc:
        return {"available": False, "error": str(exc)}
    finally:
        try:
            con.close()
        except Exception:
            pass
    collections = {name: count for name, count in rows}
    return {
        "available": True,
        "collections": collections,
        "collection_count": len(collections),
        "embedding_records": sum(collections.values()),
    }


def latest_json(paths: list[Path]) -> tuple[Path | None, Any]:
    existing = [p for p in paths if p.exists()]
    if not existing:
        return None, None
    latest = max(existing, key=lambda p: p.stat().st_mtime)
    return latest, load_json(latest)


def flatten_scorecard(data: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    metrics: dict[str, Any] = {}
    graded = 0
    passed = 0
    for dimension, rows in (data.get("dimensions") or {}).items():
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            key = f"{dimension}/{name}"
            metrics[key] = row.get("value")
            if row.get("ok") is not None:
                graded += 1
                passed += int(row.get("ok") is True)
    summary = data.get("summary") or {}
    return {
        "ts": data.get("ts"),
        "graded": int(summary.get("graded") or graded),
        "passed": int(summary.get("passed") or passed),
        "pass_rate": round(pct(int(summary.get("passed") or passed), int(summary.get("graded") or graded)), 4),
        "metrics": metrics,
    }


def summarize_eval_rows(rows: Any) -> dict[str, Any]:
    if not isinstance(rows, list):
        return {}
    scored = [r for r in rows if isinstance(r, dict) and "relevance" in r]
    if not scored:
        return {"questions": len(rows), "scored": 0}
    return {
        "questions": len(rows),
        "scored": len(scored),
        "avg_relevance": round(mean([to_float(r.get("relevance")) for r in scored]) or 0.0, 4),
        "avg_completeness": round(mean([to_float(r.get("completeness")) for r in scored]) or 0.0, 4),
        "avg_grounding": round(mean([to_float(r.get("grounding")) for r in scored]) or 0.0, 4),
        "punt_count": sum(1 for r in scored if r.get("punt") is True),
        "avg_sources": round(mean([to_float(r.get("n_sources")) for r in scored]) or 0.0, 2),
        "routes": dict(Counter(str(r.get("route") or "unknown") for r in scored)),
        "categories": dict(Counter(str(r.get("cat") or "unknown") for r in scored)),
    }


def analyze_existing_evaluations(root: Path) -> dict[str, Any]:
    data_dir = root / "radio_kg" / "data"
    scorecard_path, scorecard = latest_json(list(data_dir.glob("scorecard_*.json")))
    eval20_path, eval20 = latest_json(list(data_dir.glob("eval_qa20*.json")))
    return {
        "latest_scorecard_path": str(scorecard_path) if scorecard_path else None,
        "latest_scorecard": flatten_scorecard(scorecard) if scorecard else {},
        "latest_eval_qa20_path": str(eval20_path) if eval20_path else None,
        "latest_eval_qa20": summarize_eval_rows(eval20) if eval20 else {},
    }


def analyze_clipper(root: Path) -> dict[str, Any]:
    clip_data = root / "clip" / "data"
    jobs = load_json(clip_data / "clip_jobs.json", {})
    if not isinstance(jobs, dict):
        jobs = {}
    item_count = 0
    item_duration_s = 0.0
    item_kinds: Counter[str] = Counter()
    for job in jobs.values():
        for item in job.get("items") or []:
            if not isinstance(item, dict):
                continue
            item_count += 1
            item_kinds[str(item.get("kind") or "unknown")] += 1
            item_duration_s += max(0.0, to_float(item.get("end")) - to_float(item.get("start")))

    plans = []
    for path in (clip_data / "clips").glob("*/plan.json"):
        plan = load_json(path, {})
        if isinstance(plan, dict):
            plans.append((path, plan))
    song_count = sum(len(plan.get("songs") or []) for _, plan in plans)
    candidate_clip_count = sum(len(plan.get("clips") or []) for _, plan in plans)
    ingest_ok = sum(1 for _, plan in plans if (plan.get("ingest") or {}).get("summary_ok") is True)
    ingest_error = sum(1 for _, plan in plans if (plan.get("ingest") or {}).get("error"))

    rendered_videos = list((clip_data / "clips").rglob("*.mp4")) + list(
        (root / "radio_kg" / "data" / "clips").rglob("*.mp4")
    )
    final_videos = [p for p in rendered_videos if p.name.endswith("_final.mp4")]
    rendered_bytes = sum(p.stat().st_size for p in rendered_videos if p.exists())

    program_profiles = [
        p
        for p in (root / "clip" / "clip" / "programs").glob("*.yaml")
        if not p.name.startswith("_")
    ]
    interests = load_json(clip_data / "clipper_interests.json", {})
    topics = interests.get("topics") if isinstance(interests, dict) else []

    return {
        "telegram_job_batches": len(jobs),
        "telegram_job_items": item_count,
        "telegram_item_duration_hours": round(item_duration_s / 3600, 2),
        "telegram_item_kinds": dict(item_kinds),
        "plan_files": len(plans),
        "songs_detected_in_plans": song_count,
        "candidate_clips_in_plans": candidate_clip_count,
        "plans_with_summary_ok": ingest_ok,
        "plans_with_ingest_error": ingest_error,
        "rendered_videos": len(rendered_videos),
        "final_videos": len(final_videos),
        "rendered_video_size_mb": round(rendered_bytes / 1024 / 1024, 2),
        "program_profiles": len(program_profiles),
        "interest_topics": len(topics or []),
    }


def analyze_config_assets(root: Path) -> dict[str, Any]:
    radio_profiles = [
        p
        for p in (root / "Radio" / "config" / "profiles").iterdir()
        if p.is_dir() and (p / "profile.yaml").exists()
    ] if (root / "Radio" / "config" / "profiles").exists() else []
    clip_profiles = [
        p
        for p in (root / "clip" / "clip" / "programs").glob("*.yaml")
        if not p.name.startswith("_")
    ]
    return {
        "radio_prompt_profiles": len(radio_profiles),
        "clip_program_profiles": len(clip_profiles),
        "launchd_plists": len(list((root / "scripts").glob("com.agent.*.plist"))),
        "server_pages": len(list((root / "radio_kg" / "src" / "server" / "static").glob("*.html"))),
    }


def build_checks(report: dict[str, Any]) -> list[Check]:
    recordings = report["recordings"]
    radio = report["radio_pipeline"]
    chroma = report["knowledge_store"].get("chroma", {})
    evaluations = report["evaluations"]
    scorecard = evaluations.get("latest_scorecard") or {}
    eval20 = evaluations.get("latest_eval_qa20") or {}

    return [
        Check(
            "episode_artifact_completion",
            recordings["complete_rate"],
            ">= 0.90",
            recordings["complete_rate"] >= 0.90,
        ),
        Check(
            "translation_coverage",
            recordings["translation_coverage"],
            ">= 0.95",
            recordings["translation_coverage"] >= 0.95,
        ),
        Check(
            "pipeline_success_rate",
            radio.get("success_rate", 0.0),
            ">= 0.80",
            radio.get("success_rate", 0.0) >= 0.80,
        ),
        Check(
            "vector_records",
            chroma.get("embedding_records", 0),
            ">= 1000",
            chroma.get("embedding_records", 0) >= 1000,
        ),
        Check(
            "scorecard_pass_rate",
            scorecard.get("pass_rate", 0.0),
            ">= 0.80",
            scorecard.get("pass_rate", 0.0) >= 0.80 if scorecard else False,
        ),
        Check(
            "qa20_grounding",
            eval20.get("avg_grounding", 0.0),
            ">= 0.80",
            eval20.get("avg_grounding", 0.0) >= 0.80 if eval20 else False,
        ),
    ]


def render_markdown(report: dict[str, Any], checks: list[Check]) -> str:
    rec = report["recordings"]
    radio = report["radio_pipeline"]
    store = report["knowledge_store"]
    evals = report["evaluations"]
    clip = report["clipper"]
    cost = report["cost_ledger"]
    scorecard = evals.get("latest_scorecard") or {}
    eval20 = evals.get("latest_eval_qa20") or {}

    lines = [
        "# Agent Project Quantitative Report",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "## Executive Summary",
        "",
        f"- Episode artifacts: {rec['complete_episode_dirs']}/{rec['episode_dirs']} complete "
        f"({rec['complete_rate']:.1%}); {rec['audio_hours']} audio hours scanned.",
        f"- Transcript scale: {rec['segments']:,} JA segments, {rec['translated_segments']:,} translated "
        f"segments ({rec['translation_coverage']:.1%} coverage), {rec['sections']:,} summary sections.",
        f"- Knowledge store: {store.get('chroma', {}).get('embedding_records', 0):,} Chroma embedding records "
        f"across {store.get('chroma', {}).get('collection_count', 0)} collections.",
        f"- Pipeline logs: {radio.get('successful_runs', 0)}/{radio.get('runs', 0)} successful runs "
        f"({radio.get('success_rate', 0):.1%}); {radio.get('segments_processed', 0):,} segments processed in logs.",
        f"- QA scorecard: {scorecard.get('passed', 0)}/{scorecard.get('graded', 0)} checks passed "
        f"({scorecard.get('pass_rate', 0):.1%}).",
        f"- QA20 latest: relevance {eval20.get('avg_relevance', 0):.2f}, completeness "
        f"{eval20.get('avg_completeness', 0):.2f}, grounding {eval20.get('avg_grounding', 0):.2f}.",
        f"- Clipper: {clip['plan_files']} plan files, {clip['songs_detected_in_plans']} detected songs, "
        f"{clip['final_videos']} rendered final videos.",
        f"- LLM cost ledger: ${cost.get('total_cost_usd', 0):.4f} tracked over "
        f"{cost.get('records', 0)} outputs.",
        "",
        "## Checks",
        "",
        "| Check | Value | Target | Result |",
        "|---|---:|---:|---|",
    ]
    for check in checks:
        lines.append(
            f"| {check.name} | {check.value:.4g} | {check.target} | "
            f"{'PASS' if check.ok else 'FAIL'} |"
        )

    lines.extend(
        [
            "",
            "## Collection Breakdown",
            "",
            "| Collection | Episodes | Complete | Audio Hours | Segments | Sections |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for name, row in rec["by_collection"].items():
        lines.append(
            f"| {name} | {row['episode_dirs']} | {row['complete_episode_dirs']} | "
            f"{row['audio_hours']:.2f} | {row['segments']:,} | {row['sections']:,} |"
        )

    lines.extend(
        [
            "",
            "## Chroma Collections",
            "",
            "| Collection | Records |",
            "|---|---:|",
        ]
    )
    for name, count in (store.get("chroma", {}).get("collections") or {}).items():
        lines.append(f"| {name} | {count:,} |")

    lines.extend(
        [
            "",
            "## Pipeline Source Breakdown",
            "",
            "| Source | Runs | Success | Success Rate |",
            "|---|---:|---:|---:|",
        ]
    )
    for source, row in (radio.get("by_source") or {}).items():
        lines.append(
            f"| {source} | {row['runs']} | {row['success']} | {row['success_rate']:.1%} |"
        )

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate quantitative metrics for the Agent project.")
    parser.add_argument("--root", default=".", help="workspace root, default: current directory")
    parser.add_argument(
        "--out-dir",
        default="radio_kg/data",
        help="directory for project_metrics_*.json/.md, relative to root by default",
    )
    parser.add_argument("--strict", action="store_true", help="exit non-zero when any check fails")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = root / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
    report: dict[str, Any] = {
        "generated_at": generated_at,
        "root": str(root),
        "recordings": analyze_recordings(root),
        "radio_pipeline": analyze_radio_metrics(root),
        "knowledge_store": {
            "chroma": analyze_chroma(root),
            "index_version": load_json(root / "radio_kg" / "data" / "index_version.json", {}),
        },
        "evaluations": analyze_existing_evaluations(root),
        "clipper": analyze_clipper(root),
        "cost_ledger": analyze_cost_ledger(root),
        "config_assets": analyze_config_assets(root),
    }
    checks = build_checks(report)
    report["checks"] = [check.as_dict() for check in checks]
    report["summary"] = {
        "checks_passed": sum(1 for check in checks if check.ok),
        "checks_total": len(checks),
        "strict_ok": all(check.ok for check in checks),
    }

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = out_dir / f"project_metrics_{stamp}.json"
    md_path = out_dir / f"project_metrics_{stamp}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(report, checks), encoding="utf-8")

    print(f"Quantitative report written:\n  JSON: {json_path}\n  MD:   {md_path}")
    print(
        f"Checks: {report['summary']['checks_passed']}/{report['summary']['checks_total']} "
        f"{'PASS' if report['summary']['strict_ok'] else 'with failures'}"
    )
    print(
        "Headline: "
        f"{report['recordings']['audio_hours']}h audio, "
        f"{report['recordings']['segments']:,} transcript segments, "
        f"{report['knowledge_store']['chroma'].get('embedding_records', 0):,} vector records, "
        f"{report['clipper']['songs_detected_in_plans']} detected songs."
    )
    if args.strict and not report["summary"]["strict_ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
