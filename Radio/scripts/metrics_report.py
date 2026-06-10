"""聚合 data/logs/metrics.jsonl，输出周/月度运行报表。

用法：
    uv run python scripts/metrics_report.py                  # 全部历史
    uv run python scripts/metrics_report.py --since 7d       # 最近 7 天
    uv run python scripts/metrics_report.py --since 2026-05  # 2026-05 月份
    uv run python scripts/metrics_report.py --jsonl path/...
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path


def parse_since(spec: str | None) -> datetime | None:
    """支持 '7d' / '30d' / '2026-05' / '2026-05-15' / None。"""
    if not spec:
        return None
    m = re.fullmatch(r"(\d+)d", spec)
    if m:
        return datetime.now(timezone.utc) - timedelta(days=int(m.group(1)))
    if re.fullmatch(r"\d{4}-\d{2}", spec):
        return datetime.fromisoformat(spec + "-01T00:00:00+00:00")
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", spec):
        return datetime.fromisoformat(spec + "T00:00:00+00:00")
    raise SystemExit(f"无法解析 --since: {spec}")


def load_metrics(path: Path, since: datetime | None) -> list[dict]:
    if not path.exists():
        raise SystemExit(f"metrics 文件不存在：{path}")
    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since is not None:
                started = row.get("started_at", "")
                try:
                    ts = datetime.fromisoformat(started)
                except ValueError:
                    continue
                if ts < since:
                    continue
            rows.append(row)
    return rows


def fmt_seconds(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    return f"{s / 60:.1f}min"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    k = (len(sorted_values) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)


def report(rows: list[dict]) -> None:
    if not rows:
        print("📭 没有匹配的 metrics 记录")
        return

    total = len(rows)
    success = sum(1 for r in rows if r.get("success"))
    failed = total - success

    durations = [r.get("duration_s", 0.0) for r in rows if r.get("duration_s")]
    segments = [r.get("segments_count", 0) for r in rows]
    sections = [r.get("sections_count", 0) for r in rows]
    library_hits = [r.get("library_hits", 0) for r in rows]
    library_added = [r.get("library_added", 0) for r in rows]

    sources = Counter(r.get("source", "?") for r in rows)
    programs = Counter(r.get("program_name", "?") for r in rows)

    print("📊 Radio-Oshikatsu 运行报表")
    print(f"   时间范围首/末：{rows[0].get('started_at', '?')}  →  {rows[-1].get('started_at', '?')}")
    print()
    print(f"运行次数：{total}  (✅ {success}  ❌ {failed})")
    print()
    print("耗时分布")
    if durations:
        print(f"  平均   : {fmt_seconds(statistics.mean(durations))}")
        print(f"  中位数 : {fmt_seconds(statistics.median(durations))}")
        print(f"  P95    : {fmt_seconds(percentile(durations, 0.95))}")
        print(f"  最长   : {fmt_seconds(max(durations))}")
    print()
    print("内容指标（仅成功运行）")
    success_rows = [r for r in rows if r.get("success")]
    if success_rows:
        seg_s = [r.get("segments_count", 0) for r in success_rows]
        sec_s = [r.get("sections_count", 0) for r in success_rows]
        hit_s = [r.get("library_hits", 0) for r in success_rows]
        add_s = [r.get("library_added", 0) for r in success_rows]
        print(f"  平均 transcript 段数 : {statistics.mean(seg_s):.0f}")
        print(f"  平均 sections 数     : {statistics.mean(sec_s):.1f}")
        print(f"  累计 library 命中    : {sum(hit_s)}")
        print(f"  累计 library 自动入库: {sum(add_s)}")
        # 命中率
        total_sec = sum(sec_s)
        if total_sec:
            hit_rate = sum(hit_s) / total_sec * 100
            print(f"  常驻环节命中率       : {hit_rate:.1f}%")
    print()
    print(f"按入口分类（source）：")
    for src, n in sources.most_common():
        print(f"  - {src}: {n}")
    print()
    print(f"节目分布：")
    for prog, n in programs.most_common(10):
        print(f"  - {prog}: {n}")

    if failed:
        print()
        print("最近失败：")
        for r in rows[-failed:][-5:]:
            if not r.get("success"):
                errs = r.get("errors") or []
                err = errs[0] if errs else "(no error message)"
                print(f"  ✗ {r.get('started_at', '?')} | {r.get('program_name', '?')}: {err[:100]}")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="聚合 metrics.jsonl 出报表")
    p.add_argument(
        "--jsonl",
        type=Path,
        default=Path("data/logs/metrics.jsonl"),
        help="metrics 文件路径",
    )
    p.add_argument(
        "--since",
        default=None,
        help="起始时间：'7d' / '30d' / '2026-05' / '2026-05-15'，默认全部",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    since = parse_since(args.since)
    rows = load_metrics(args.jsonl, since)
    report(rows)
    return 0


if __name__ == "__main__":
    sys.exit(main())
