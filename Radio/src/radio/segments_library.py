"""常驻广播环节知识库：加载、匹配、prompt 格式化、自动追加。

数据文件：`config/segments_library.yaml`
用途：
1. 在 summarize prompt 中告诉 LLM 节目已有的常驻环节及其官方介绍
2. LLM 输出后按 title_ja 匹配，命中则覆盖 intro 为 library 标准版（一致性保证）
3. 自动追加：LLM 发现的新环节（is_recurring=False）若不在 library 中，
   按 program 系列归类后追加到 YAML；下次跑同期节目就能命中
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from loguru import logger
from pydantic import BaseModel


class SegmentEntry(BaseModel):
    """一个常驻环节条目。"""

    id: str
    program_ja: str
    title_ja: str
    aliases: list[str] = []
    intro: str

    @property
    def all_titles(self) -> list[str]:
        return [self.title_ja, *self.aliases]


def load_segments_library(path: Path) -> list[SegmentEntry]:
    """读取 YAML，扁平化为所有环节条目的列表。文件缺失时返回空列表（不阻断 pipeline）。"""
    if not path.exists():
        logger.warning(f"常驻环节库不存在，跳过：{path}")
        return []

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    out: list[SegmentEntry] = []
    for program in raw.get("programs", []) or []:
        program_ja = str(program.get("program_ja") or "")
        for seg in program.get("recurring_segments", []) or []:
            entry = SegmentEntry(
                id=str(seg.get("id") or ""),
                program_ja=program_ja,
                title_ja=str(seg.get("title_ja") or ""),
                aliases=[str(a) for a in (seg.get("aliases") or [])],
                intro=str(seg.get("intro") or "").strip(),
            )
            if not entry.title_ja:
                continue
            out.append(entry)
    logger.info(f"加载常驻环节库：{len(out)} 条")
    return out


def match_segment(title_ja: str, library: list[SegmentEntry]) -> SegmentEntry | None:
    """按 title_ja + aliases 找匹配条目。

    匹配策略（按优先级）：
    1. 精确等于 title_ja 或 aliases 之一
    2. library 中某条目的 title_ja 是输入的子串（处理 LLM 在标题里加修饰的情况）
    3. 输入是 library 中某条目的子串（处理 LLM 截断或简写）
    """
    if not title_ja or not library:
        return None

    title_ja = title_ja.strip()
    # 第 1 优先级：精确匹配
    for entry in library:
        if title_ja == entry.title_ja:
            return entry
        if title_ja in entry.aliases:
            return entry
    # 第 2 / 3 优先级：子串匹配
    for entry in library:
        for known in entry.all_titles:
            if not known:
                continue
            if known in title_ja or title_ja in known:
                return entry
    return None


def filter_library_by_series(
    library: list[SegmentEntry],
    series_name: str,
) -> list[SegmentEntry]:
    """只保留属于当前 series 的 library 条目。

    跨节目的"业界通用术语"（如「ふつおたのコーナー」「オープニング」「エンディング」
    日本广播都用）必须按系列隔离，否则在跑节目 B 时会误匹配节目 A 下登记的同名环节。
    series_name 双向 substring 匹配 program_ja。
    """
    if not series_name:
        return library
    out: list[SegmentEntry] = []
    for entry in library:
        pja = (entry.program_ja or "").strip()
        if not pja:
            continue
        if pja == series_name or pja in series_name or series_name in pja:
            out.append(entry)
    return out


def _slugify(text: str) -> str:
    """把日文/中文标题压成稳定的 snake_case 拉丁 id。

    不要求人类可读——这是 program/segment 的内部标识符。
    """
    # 取 hash 前缀保证短且稳定；保留少量人类可读片段
    import hashlib

    pieces = re.findall(r"[A-Za-z0-9]+", text)
    safe = "_".join(p.lower() for p in pieces)[:32]
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:6]
    return f"{safe}_{digest}" if safe else f"auto_{digest}"


def extract_series_name(program_display_name: str) -> str:
    """从 'MyGO!!!!!の「迷子集会」#178' 抽出系列名 'MyGO!!!!!の「迷子集会」'。

    去掉末尾的 '#数字' / '第N回' / '第N弾' 等期数标识。
    """
    name = program_display_name.strip()
    name = re.sub(r"^(?:【[^】]{1,40}】\s*)+", "", name).strip()
    date_suffix = r"(?:\s+\d{4}年\d{1,2}月\d{1,2}日(?:放送|配信)?)?"
    name = re.sub(rf"\s*#\s*\d+{date_suffix}\s*$", "", name)  # #178 / #2 2025年4月13日放送
    name = re.sub(rf"\s*第\s*\d+\s*[回弾期话話]{date_suffix}\s*$", "", name)  # 第3弾
    name = re.sub(rf"\s*Vol\.?\s*\d+{date_suffix}\s*$", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+\d{4}年\d{1,2}月\d{1,2}日(?:放送|配信)?\s*$", "", name)
    return name.strip()


def append_new_segments_to_library(
    library_path: Path,
    series_name: str,
    new_segments: list[dict],
) -> tuple[int, int]:
    """把新发现的环节追加到 library YAML。

    Args:
        library_path: segments_library.yaml 路径
        series_name: 这期节目所属的系列名（如 'MyGO!!!!!の「迷子集会」'）
        new_segments: list of dicts with keys: title_ja, intro, (optional) aliases

    Returns:
        (added_count, skipped_count) — 实际追加的条数、因去重跳过的条数

    去重策略：
    - 在同 program 下，title_ja 精确等于已有条目或其 aliases 之一 → 跳过
    - title_ja 是已有条目 title_ja 的子串、或反向子串 → 跳过（避免变种重复入库）
    """
    if not series_name or not new_segments:
        return 0, 0

    # 读取（不存在则新建空结构）
    if library_path.exists():
        with library_path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {"version": 1, "programs": []}

    programs = data.setdefault("programs", [])

    # 找匹配的 program（系列名 substring 双向匹配）
    target = None
    for p in programs:
        pja = (p.get("program_ja") or "").strip()
        if pja and (pja == series_name or pja in series_name or series_name in pja):
            target = p
            break

    if target is None:
        target = {
            "program_id": _slugify(series_name),
            "program_ja": series_name,
            "recurring_segments": [],
        }
        programs.append(target)
        logger.info(f"library 新建 program 节点：{series_name}")

    existing = target.setdefault("recurring_segments", [])
    # 收集已有标题（含 aliases）做 dedup
    known_titles: list[str] = []
    for s in existing:
        title = (s.get("title_ja") or "").strip()
        if title:
            known_titles.append(title)
        for a in s.get("aliases") or []:
            a = str(a).strip()
            if a:
                known_titles.append(a)

    def _is_dup(candidate: str) -> bool:
        candidate = candidate.strip()
        for known in known_titles:
            if candidate == known:
                return True
            # 子串双向匹配：候选包含已知或已知包含候选
            if known and (known in candidate or candidate in known):
                return True
        return False

    added = 0
    skipped = 0
    for seg in new_segments:
        title_ja = (seg.get("title_ja") or "").strip()
        intro = (seg.get("intro") or "").strip()
        if not title_ja or not intro:
            skipped += 1
            continue
        if _is_dup(title_ja):
            skipped += 1
            continue
        existing.append(
            {
                "id": _slugify(title_ja),
                "title_ja": title_ja,
                "aliases": list(seg.get("aliases") or []),
                "intro": intro,
            }
        )
        known_titles.append(title_ja)
        added += 1
        logger.info(f"library 追加新环节：{title_ja}（program={target.get('program_ja')}）")

    if added > 0:
        # 写回（注释会丢失，但元数据节点保留）
        with library_path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(
                data,
                f,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
                width=120,
            )
    return added, skipped


def format_library_for_prompt(library: list[SegmentEntry]) -> str:
    """把环节库压成 LLM prompt 里可读的列表。"""
    if not library:
        return "（暂无已登记的常驻环节，所有环节都视为新发现）"

    lines: list[str] = []
    by_program: dict[str, list[SegmentEntry]] = {}
    for entry in library:
        by_program.setdefault(entry.program_ja, []).append(entry)

    for program_ja, entries in by_program.items():
        lines.append(f"节目：{program_ja}")
        for entry in entries:
            alias_text = (
                f"（别名：{', '.join(entry.aliases)}）" if entry.aliases else ""
            )
            lines.append(f"  - 环节：{entry.title_ja} {alias_text}")
            for ln in entry.intro.splitlines():
                ln = ln.strip()
                if ln:
                    lines.append(f"    介绍：{ln}")
    return "\n".join(lines)
