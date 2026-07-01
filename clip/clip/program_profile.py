"""节目处理方案 + 归档方案 的加载器。

每个节目一个 YAML（`clip/programs/<id>.yaml`），含 processing（处理口径）
与 archiving（归档口径）两段。clipper Branch B 按 profile 处理并归档某 VTuber 的直播。
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

_PROGRAMS_DIR = Path(__file__).resolve().parent / "programs"
# 节目 id 仅允许小写字母/数字/下划线/连字符，避免路径穿越（与 Radio profiles 同款约束）。
PROGRAM_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,48}$")


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
    stt_prompt: str = ""          # 覆盖全局 STT prompt（注入本节目人名/术语，防异节目人名幻听）
    translation_prompt_path: Path | None = None
    summary_style: str = ""
    summary_exemplar: str = ""    # few-shot 范例总结（注入 {style_exemplar} 槽）
    # 节目专属总结：整段提示词模板（覆盖全局 summarize.txt）+ 长度/highlight 数参数。
    # 都为空时回退全局默认；summary_style 仍会注入全局模板的 {summary_style} 槽。
    summary_prompt_path: Path | None = None
    summary_max_chars: int | None = None
    summary_highlight_count: int | None = None
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
        stt_prompt=proc.get("stt_prompt", ""),
        translation_prompt_path=_resolve_program_path(
            proc.get("translation_prompt_path"), fpath.parent
        ),
        summary_style=proc.get("summary_style", ""),
        summary_exemplar=proc.get("summary_exemplar", ""),
        summary_prompt_path=_resolve_program_path(
            proc.get("summary_prompt_path"), fpath.parent
        ),
        summary_max_chars=(proc.get("summary") or {}).get("max_summary_chars"),
        summary_highlight_count=(proc.get("summary") or {}).get("target_highlight_count"),
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


def _validate_program_id(program_id: str) -> None:
    if not PROGRAM_ID_RE.match(program_id or ""):
        raise ValueError(f"非法 program_id：{program_id!r}（仅小写字母/数字/_/-，2-49 位）")


def read_prompt_text(path: Path | None) -> str:
    """读取某个提示词文件全文；路径为空或不存在时返回空串。"""
    if path is not None and Path(path).exists():
        return Path(path).read_text(encoding="utf-8")
    return ""


def profile_detail(program_id: str, programs_dir: Path = _PROGRAMS_DIR) -> dict:
    """前端编辑用：返回原始 yaml 文本 + 解析后的关键字段 + 自带提示词全文。"""
    _validate_program_id(program_id)
    fpath = programs_dir / f"{program_id}.yaml"
    if not fpath.exists():
        raise FileNotFoundError(f"节目方案不存在：{program_id}")
    p = load_profile(program_id)
    return {
        "id": p.program_id,
        "display_name": p.display_name,
        "yaml_text": fpath.read_text(encoding="utf-8"),
        "summary_prompt": read_prompt_text(p.summary_prompt_path),
        "summary_prompt_is_custom": p.summary_prompt_path is not None,
        "translation_prompt": read_prompt_text(p.translation_prompt_path),
        "summary_style": p.summary_style,
        "summary_exemplar": p.summary_exemplar,
        "summary_max_chars": p.summary_max_chars,
        "summary_highlight_count": p.summary_highlight_count,
    }


def save_profile(
    program_id: str,
    yaml_text: str,
    *,
    summary_prompt_text: str | None = None,
    translation_prompt_text: str | None = None,
    programs_dir: Path = _PROGRAMS_DIR,
) -> dict:
    """原子写回节目方案。yaml_text 逐字保存（保留注释）；可选附带提示词文本。

    校验：id 合法、yaml 可解析为 dict、program_id 一致；自带 summary 提示词必须含
    {transcript} 占位符（否则总结无从注入逐字稿）。
    """
    _validate_program_id(program_id)
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        raise ValueError(f"YAML 解析失败：{e}") from e
    if not isinstance(data, dict):
        raise ValueError("方案内容必须是 YAML 映射（dict）")
    if data.get("program_id") != program_id:
        raise ValueError(
            f"yaml 内 program_id={data.get('program_id')!r} 与目标 {program_id!r} 不一致"
        )

    proc = data.get("processing", {}) or {}
    # 提示词文本若提供，写入 yaml 引用的相对路径（缺省路径＝<id>_summarize/_translate.txt）。
    if summary_prompt_text is not None:
        if "{transcript}" not in summary_prompt_text:
            raise ValueError("总结提示词必须包含 {transcript} 占位符")
        rel = proc.get("summary_prompt_path") or f"{program_id}_summarize.txt"
        # `_` 前缀＝多方案共享模板（如 _live_summarize.txt），不随单个方案保存被覆盖。
        if not Path(rel).name.startswith("_"):
            _atomic_write(programs_dir / rel, summary_prompt_text.rstrip() + "\n")
    if translation_prompt_text is not None:
        rel = proc.get("translation_prompt_path") or f"{program_id}_translate.txt"
        _atomic_write(programs_dir / rel, translation_prompt_text.rstrip() + "\n")

    _atomic_write(programs_dir / f"{program_id}.yaml", yaml_text.rstrip() + "\n")
    return {"id": program_id, "saved": True}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


_YAML_NEEDS_QUOTE = re.compile(r"""[:#\[\]{}&*!|>'"%@`,]|^\s|\s$|^[?\-]""")


def _yaml_quote(s: str) -> str:
    if s == "" or _YAML_NEEDS_QUOTE.search(s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _strip_key(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def _yaml_upsert_mapping(text: str, section: str, key: str, value: str) -> str:
    """在 processing.<section> 映射块内插入/更新 `key: value`，保留其余注释与字段。"""
    lines = text.split("\n")
    hi = next((i for i, l in enumerate(lines)
               if re.match(rf"^(\s*){re.escape(section)}:\s*$", l)), -1)
    qk, qv = _yaml_quote(key), _yaml_quote(value)
    if hi < 0:                                   # 块不存在：在 processing: 下新建
        pi = next((i for i, l in enumerate(lines) if re.match(r"^processing:\s*$", l)), -1)
        entry = f"  {section}:\n    {qk}: {qv}"
        lines.insert(pi + 1 if pi >= 0 else len(lines), entry)
        return "\n".join(lines)
    base = len(lines[hi]) - len(lines[hi].lstrip())
    ind = base + 2
    end, dup, last = hi + 1, -1, hi
    while end < len(lines):
        l = lines[end]
        if l.strip() == "" or l.lstrip().startswith("#"):
            end += 1
            continue
        cur = len(l) - len(l.lstrip())
        if cur < ind:
            break
        if cur == ind:
            last = end
            m = re.match(r"^\s*(.+?):", l)
            if m and _strip_key(m.group(1)) == key:
                dup = end
        end += 1
    newline = " " * ind + f"{qk}: {qv}"
    if dup >= 0:
        lines[dup] = newline
    else:
        lines.insert(last + 1, newline)
    return "\n".join(lines)


def add_mapping(program_id: str, section: str, key: str, value: str,
                programs_dir: Path = _PROGRAMS_DIR) -> dict:
    """纠错回流：把「错→对」写进方案的 name_corrections / terminology（保留注释、原子写）。"""
    _validate_program_id(program_id)
    if section not in ("name_corrections", "terminology"):
        raise ValueError("section 仅支持 name_corrections / terminology")
    key, value = (key or "").strip(), (value or "").strip()
    if not key or not value:
        raise ValueError("错/对 都不能为空")
    fpath = programs_dir / f"{program_id}.yaml"
    if not fpath.exists():
        raise FileNotFoundError(f"节目方案不存在：{program_id}")
    new_text = _yaml_upsert_mapping(fpath.read_text(encoding="utf-8"), section, key, value)
    data = yaml.safe_load(new_text)               # 写入后必须仍是合法 YAML 且键值生效
    if not isinstance(data, dict):
        raise ValueError("写入后 YAML 非法")
    got = (data.get("processing", {}) or {}).get(section, {}) or {}
    if got.get(key) != value:
        raise ValueError("写入后校验失败：未命中期望键值")
    _atomic_write(fpath, new_text.rstrip() + "\n")
    return {"id": program_id, "section": section, "key": key, "value": value}
