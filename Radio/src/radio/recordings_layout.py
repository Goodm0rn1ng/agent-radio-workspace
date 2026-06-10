"""recordings/ 目录布局规则：按 segments_library 的 program_id 分包。

目录结构：
    data/recordings/
      <program_id>/
        <YYYY-MM-DD>_<safe_title>/
          audio.m4a
          bilingual.txt
          summary_*.json

resolve_program_subdir(program_name, settings) 给定节目展示名（如
"MyGO!!!!!の「迷子集会」#178"），尝试匹配 segments_library 中已登记的 program；
匹配上 → 返回该 program_id；匹配不上 → 返回 fallback bucket（_video / _manual / _other）。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import yaml
from loguru import logger

from radio.segments_library import extract_series_name

# Fallback bucket 名（前缀 _ 提示不是真正的 program_id）
_BUCKET_VIDEO = "_video"
_BUCKET_MANUAL = "_manual"
_BUCKET_OTHER = "_other"


def _series_to_stable_id(series_name: str) -> str:
    """从系列名生成稳定子目录名。

    优先策略：抽 [A-Za-z0-9]+ 拼接 + sha1 前缀确保稳定。
    "羊宮妃那のこもれびじかん" → "_p_<hash>" （全日文，无拉丁字符）
    "MyGO!!!!!の「迷子集会」"   → "mygo_<hash>"
    保证同一 series_name 永远映射到同一目录名。
    """
    if not series_name:
        return _BUCKET_OTHER
    pieces = re.findall(r"[A-Za-z0-9]+", series_name)
    digest = hashlib.sha1(series_name.encode("utf-8")).hexdigest()[:6]
    safe = "_".join(p.lower() for p in pieces)[:24]
    return f"{safe}_{digest}" if safe else f"_p_{digest}"


def resolve_program_subdir(
    program_name: str,
    library_path: Path,
    source: str = "unknown",
) -> str:
    """根据节目展示名 + source 决定 recordings 下的子目录名。

    Args:
        program_name: 用户传入的 --title 或 settings.program.name，如
            "MyGO!!!!!の「迷子集会」#178"。
        library_path: segments_library.yaml 路径。
        source: pipeline 入口类型（"radiko" / "video" / "oneshot" / ...），
            匹配不上 library 时按此 fallback 到 _video / _manual / _other。

    Returns:
        子目录名，如 "mygo_meigo_shukai" 或 "_video" 等。
    """
    series_name = extract_series_name(program_name)

    if library_path.exists() and series_name:
        try:
            with library_path.open("r", encoding="utf-8") as f:
                raw = yaml.safe_load(f) or {}
            for program in raw.get("programs", []) or []:
                pja = (program.get("program_ja") or "").strip()
                if not pja:
                    continue
                # 双向 substring 匹配（与 segments_library 命中规则一致）
                if pja == series_name or pja in series_name or series_name in pja:
                    pid = (program.get("program_id") or "").strip()
                    if pid:
                        return pid
        except Exception as e:
            logger.warning(f"resolve_program_subdir 读 library 失败：{e!r}")

    # library 没命中但有系列名 → 用稳定 hash 子目录（同一节目永远同目录）
    # 注：第一次跑某节目时 library 里还没节点；跑完会自动入库（创建 program_id）。
    # 但用户可以手动 rename segments_library.yaml 里的 program_id 为可读名，
    # 之后再用 resolve_program_subdir 会命中 library 路径。
    if series_name:
        return _series_to_stable_id(series_name)

    # 没系列名（极端情况）→ 按入口类型 fallback
    if source in ("video",):
        return _BUCKET_VIDEO
    if source in ("oneshot", "manual"):
        return _BUCKET_MANUAL
    return _BUCKET_OTHER


def safe_episode_dir_name(program_name: str, air_date: str) -> str:
    """生成单期工作目录名："2026-05-11_羊宮妃那の「こもれびじかん」#4"。

    替换 / \\ : * ? " < > | 这些文件系统非法字符为 _。
    """
    safe = "".join(
        "_" if ch in '/\\:*?"<>|' else ch
        for ch in (program_name or "untitled")
    ).strip()
    # 截断 90 字符防止某些文件系统路径名上限
    safe = safe[:90].rstrip()
    return f"{air_date}_{safe}" if safe else air_date


def build_work_dir(
    base: Path,
    program_name: str,
    air_date: str,
    library_path: Path,
    source: str,
    collection_id: str | None = None,
) -> Path:
    """完整路径：<base>/<program_id>/<YYYY-MM-DD>_<safe_title>/"""
    subdir = safe_collection_dir_name(collection_id) if collection_id else resolve_program_subdir(
        program_name, library_path, source
    )
    episode = safe_episode_dir_name(program_name, air_date)
    return base / subdir / episode


def safe_collection_dir_name(collection_id: str | None) -> str:
    """Normalize a user-selected collection id into one folder name."""
    value = (collection_id or "").strip().lower()
    value = re.sub(r"[^\w-]+", "_", value, flags=re.UNICODE)
    value = re.sub(r"_+", "_", value).strip("_.-")
    if not value:
        return _BUCKET_OTHER
    return value[:64]
