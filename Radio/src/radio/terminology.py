"""术语库加载与译后专有名词修正。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from radio.models import Segment, Summary


def load_terminology(path: Path) -> dict:
    """读取 YAML 术语库。文件不存在时返回空库，方便普通项目继续运行。"""
    if not path.exists():
        logger.warning(f"术语库不存在，跳过：{path}")
        return {"terms": [], "post_corrections": {}}

    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return {
        "terms": raw.get("terms") or [],
        "post_corrections": raw.get("post_corrections") or {},
    }


def format_terminology_for_prompt(path: Path) -> str:
    """把术语库压成适合放进 LLM prompt 的短清单。"""
    data = load_terminology(path)
    terms = data["terms"]
    if not terms:
        return "（未配置术语库）"

    lines = []
    for term in terms:
        ja = term.get("ja", "")
        zh = term.get("zh", "")
        category = term.get("category", "term")
        aliases = term.get("aliases") or []
        note = term.get("note", "")
        alias_text = f"；别名：{', '.join(str(a) for a in aliases)}" if aliases else ""
        note_text = f"；注意：{note}" if note else ""
        lines.append(f"- [{category}] {ja} => {zh}{alias_text}{note_text}")
    return "\n".join(lines)


def load_post_corrections(path: Path) -> dict[str, str]:
    """读取译后修正常用错译/繁简/别名映射。"""
    raw = load_terminology(path)["post_corrections"]
    return {str(k): str(v) for k, v in raw.items()}


def apply_terminology_corrections(
    segments: list[Segment],
    corrections: dict[str, str],
) -> list[Segment]:
    """对中文译文执行术语库中的机械修正。"""
    if not corrections:
        return segments

    out: list[Segment] = []
    changed = 0
    for seg in segments:
        zh = seg.zh
        for wrong, right in corrections.items():
            if wrong in zh:
                zh = zh.replace(wrong, right)
        if zh != seg.zh:
            changed += 1
        out.append(seg.model_copy(update={"zh": zh}))

    logger.info(f"应用术语库译后修正：{len(corrections)} 条规则，命中 {changed} 段")
    return out


def apply_text_corrections(text: str, corrections: dict[str, str]) -> str:
    """对普通文本执行同一套术语修正。"""
    for wrong, right in corrections.items():
        text = text.replace(wrong, right)
    return text


def _apply_corrections_to_value(value: Any, corrections: dict[str, str]) -> Any:
    if isinstance(value, str):
        return apply_text_corrections(value, corrections)
    if isinstance(value, list):
        return [_apply_corrections_to_value(item, corrections) for item in value]
    if isinstance(value, dict):
        return {
            key: _apply_corrections_to_value(item, corrections)
            for key, item in value.items()
        }
    return value


def apply_summary_corrections(
    summary: Summary,
    corrections: dict[str, str],
) -> Summary:
    """对摘要对象中的所有中文文本执行术语修正。"""
    if not corrections:
        return summary

    corrected = _apply_corrections_to_value(summary.model_dump(), corrections)
    return Summary(**corrected)
