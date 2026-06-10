"""节目处理方案 + 归档方案 的加载器。

每个节目一个 YAML（`clip/programs/<id>.yaml`），含 processing（处理口径）
与 archiving（归档口径）两段。clipper Branch B 按 profile 处理并归档某 VTuber 的直播。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_PROGRAMS_DIR = Path(__file__).resolve().parent / "programs"


@dataclass
class ProgramProfile:
    program_id: str
    display_name: str
    raw: dict
    performer: str = ""
    band: str = ""
    accent_color: str = ""        # 应援色 hex（如 "#4477CC"），用于字幕样式

    def accent_rgb(self, default=(255, 255, 255)) -> tuple[int, int, int]:
        h = (self.accent_color or "").lstrip("#")
        if len(h) == 6:
            return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
        return default

    # processing
    language: str = "ja"
    translate_to: str = "zh"
    translation_prompt_path: Path | None = None
    summary_style: str = ""
    viral_focus: str = ""
    host_canonical: str = ""
    host_type: str = "Person"
    host_aliases: list = field(default_factory=list)
    name_corrections: dict = field(default_factory=dict)
    terminology: dict = field(default_factory=dict)
    members: list = field(default_factory=list)

    # archiving
    collection_id: str = ""
    recordings_root: str = "../Radio/data/recordings"
    episode_dir_template: str = "{date}_{label}"
    kg_program_name: str = ""
    auto_policy: str = "confirm"
    keep_source_video: bool = True
    auto_telegram: bool = False     # 上传/处理后自动推送 Telegram 切片菜单

    @property
    def member_glossary(self) -> str:
        """成员名册一行串，供 LLM 总结/二次创作做上下文。"""
        return "；".join(
            f"{m.get('name')}({m.get('yomi')},{m.get('role')})" for m in self.members
        )


def load_profile(program_id: str) -> ProgramProfile:
    path = program_id if program_id.endswith(".yaml") else f"{program_id}.yaml"
    fpath = Path(path) if Path(path).is_absolute() else _PROGRAMS_DIR / path
    if not fpath.exists():
        avail = ", ".join(p.stem for p in _PROGRAMS_DIR.glob("*.yaml")) or "(无)"
        raise FileNotFoundError(f"找不到节目方案 {fpath}；现有：{avail}")
    data = yaml.safe_load(fpath.read_text(encoding="utf-8"))
    proc = data.get("processing", {})
    arch = data.get("archiving", {})
    return ProgramProfile(
        program_id=data["program_id"],
        display_name=data.get("display_name", data["program_id"]),
        raw=data,
        performer=data.get("performer", ""),
        band=data.get("band", ""),
        accent_color=data.get("accent_color", ""),
        language=proc.get("language", "ja"),
        translate_to=proc.get("translate_to", "zh"),
        translation_prompt_path=_resolve_program_path(
            proc.get("translation_prompt_path"), fpath.parent
        ),
        summary_style=proc.get("summary_style", ""),
        viral_focus=proc.get("viral_focus", ""),
        host_canonical=(proc.get("host") or {}).get("canonical", ""),
        host_type=(proc.get("host") or {}).get("type", "Person"),
        host_aliases=(proc.get("host") or {}).get("aliases", []) or [],
        name_corrections=proc.get("name_corrections", {}) or {},
        terminology=proc.get("terminology", {}) or {},
        members=proc.get("members", []) or [],
        collection_id=arch.get("collection_id", data["program_id"]),
        recordings_root=arch.get("recordings_root", "../Radio/data/recordings"),
        episode_dir_template=arch.get("episode_dir_template", "{date}_{label}"),
        kg_program_name=arch.get("kg_program_name", data.get("display_name", "")),
        auto_policy=arch.get("auto_policy", "confirm"),
        keep_source_video=arch.get("keep_source_video", True),
        auto_telegram=bool(arch.get("auto_telegram", False)),
    )


def _resolve_program_path(value: str | None, base_dir: Path) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else base_dir / path
