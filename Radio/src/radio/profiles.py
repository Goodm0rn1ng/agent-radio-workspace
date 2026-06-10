"""Prompt profile loading and per-run settings overrides."""

from __future__ import annotations

import os
import re
from pathlib import Path

import yaml
from pydantic import BaseModel

from radio.config import Settings

def _radio_project_root() -> Path | None:
    explicit = os.environ.get("RADIO_PROJECT_ROOT")
    if explicit:
        return Path(explicit)
    config_path = os.environ.get("RADIO_CONFIG")
    if config_path:
        return Path(config_path).resolve().parent.parent
    return None


def _default_profiles_dir() -> Path:
    explicit = os.environ.get("RADIO_PROFILES_DIR")
    if explicit:
        return Path(explicit)
    project_root = _radio_project_root()
    if project_root is not None:
        return project_root / "config" / "profiles"
    return Path("config/profiles")


DEFAULT_PROFILES_DIR = _default_profiles_dir()
PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,48}$")


class PromptProfile(BaseModel):
    id: str
    name: str
    description: str = ""
    terminology_path: Path | None = None
    translation_prompt_path: Path | None = None
    summary_prompt_path: Path | None = None
    segments_library_path: Path | None = None
    stt_prompt: str | None = None


def list_prompt_profiles(profiles_dir: Path = DEFAULT_PROFILES_DIR) -> list[PromptProfile]:
    """List profiles from ``config/profiles/*/profile.yaml``."""
    if not profiles_dir.exists():
        return []
    profiles: list[PromptProfile] = []
    for path in sorted(profiles_dir.glob("*/profile.yaml")):
        try:
            profiles.append(load_prompt_profile(path.parent.name, profiles_dir))
        except Exception:
            continue
    return profiles


def load_prompt_profile(
    profile_id: str,
    profiles_dir: Path = DEFAULT_PROFILES_DIR,
) -> PromptProfile:
    """Load one prompt profile by id."""
    _validate_profile_id(profile_id)
    path = profiles_dir / profile_id / "profile.yaml"
    if not path.exists():
        raise FileNotFoundError(f"Prompt profile 不存在：{profile_id}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw.setdefault("id", profile_id)
    profile = PromptProfile(**raw)
    return _resolve_profile_paths(profile, path.parent)


def save_prompt_profile(
    *,
    profile_id: str,
    name: str,
    description: str = "",
    translation_prompt: str,
    summary_prompt: str,
    terminology_path: Path | None = None,
    segments_library_path: Path | None = None,
    stt_prompt: str | None = None,
    profiles_dir: Path = DEFAULT_PROFILES_DIR,
) -> PromptProfile:
    """Create or replace a user-managed prompt profile."""
    _validate_profile_id(profile_id)
    if not translation_prompt.strip() or not summary_prompt.strip():
        raise ValueError("translation_prompt 和 summary_prompt 不能为空")
    if "{input_json}" not in translation_prompt:
        raise ValueError("translation_prompt 必须包含 {input_json}")
    if "{transcript}" not in summary_prompt:
        raise ValueError("summary_prompt 必须包含 {transcript}")

    profile_dir = profiles_dir / profile_id
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "translate.txt").write_text(translation_prompt.strip() + "\n", encoding="utf-8")
    (profile_dir / "summarize.txt").write_text(summary_prompt.strip() + "\n", encoding="utf-8")

    payload = {
        "id": profile_id,
        "name": name,
        "description": description,
        "translation_prompt_path": "translate.txt",
        "summary_prompt_path": "summarize.txt",
    }
    if terminology_path is not None:
        payload["terminology_path"] = str(terminology_path)
    if segments_library_path is not None:
        payload["segments_library_path"] = str(segments_library_path)
    if stt_prompt:
        payload["stt_prompt"] = stt_prompt

    (profile_dir / "profile.yaml").write_text(
        yaml.safe_dump(payload, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    return load_prompt_profile(profile_id, profiles_dir)


def apply_prompt_profile(settings: Settings, profile: PromptProfile) -> Settings:
    """Return a settings copy with profile-specific prompt paths applied."""
    translation_updates = {}
    if profile.terminology_path is not None:
        translation_updates["terminology_path"] = profile.terminology_path
    if profile.translation_prompt_path is not None:
        translation_updates["prompt_path"] = profile.translation_prompt_path

    summary_updates = {}
    if profile.summary_prompt_path is not None:
        summary_updates["prompt_path"] = profile.summary_prompt_path
    if profile.segments_library_path is not None:
        summary_updates["segments_library_path"] = profile.segments_library_path

    stt_updates = {}
    if profile.stt_prompt is not None:
        stt_updates["prompt"] = profile.stt_prompt

    return settings.model_copy(
        update={
            "translation": settings.translation.model_copy(update=translation_updates),
            "summary": settings.summary.model_copy(update=summary_updates),
            "stt": settings.stt.model_copy(update=stt_updates),
        }
    )


def _resolve_profile_paths(profile: PromptProfile, profile_dir: Path) -> PromptProfile:
    updates = {}
    project_root = _radio_project_root()
    for field in (
        "terminology_path",
        "translation_prompt_path",
        "summary_prompt_path",
        "segments_library_path",
    ):
        value = getattr(profile, field)
        if value is None or value.is_absolute():
            continue
        if len(value.parts) == 1:
            updates[field] = profile_dir / value
        elif project_root is not None:
            updates[field] = project_root / value
        else:
            updates[field] = value
    return profile.model_copy(update=updates)


def _validate_profile_id(profile_id: str) -> None:
    if not PROFILE_ID_RE.match(profile_id):
        raise ValueError("profile_id 只能使用小写字母、数字、下划线和连字符，长度 2-49")
