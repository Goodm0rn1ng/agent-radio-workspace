"""双语 transcript 拼装 + 名词修正字典应用。"""

from __future__ import annotations

from pathlib import Path

from loguru import logger

from radio.models import Segment


def _format_seconds(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def apply_name_corrections(
    segments: list[Segment],
    corrections: dict[str, str],
) -> list[Segment]:
    """对日文和中文都做 str.replace 替换。corrections 为空则原样返回。"""
    if not corrections:
        return segments
    out: list[Segment] = []
    for seg in segments:
        ja, zh = seg.ja, seg.zh
        for wrong, right in corrections.items():
            ja = ja.replace(wrong, right)
            zh = zh.replace(wrong, right)
        out.append(seg.model_copy(update={"ja": ja, "zh": zh}))
    logger.info(f"应用名词修正字典（{len(corrections)} 条）")
    return out


def write_bilingual_txt(
    segments: list[Segment],
    out_path: Path,
    program_name: str,
) -> Path:
    """写出双语 transcript 文件，返回写入路径。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# {program_name}",
        f"# 共 {len(segments)} 段，"
        f"总时长约 {_format_seconds(segments[-1].end) if segments else '00:00:00'}",
        "",
    ]
    for seg in segments:
        ts = _format_seconds(seg.start)
        lines.append(f"[{ts}]")
        lines.append(f"  JP: {seg.ja}")
        lines.append(f"  CN: {seg.zh}")
        lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info(f"双语 transcript 已写入：{out_path}")
    return out_path
