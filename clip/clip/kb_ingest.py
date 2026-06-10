"""Branch B：把下载的直播自动「总结入库」，且**不经过人工审查**。

两步：
1. 复用 Radio `run_pipeline` 做 STT/翻译/摘要，产出 03/04/05 JSON（与现有入库格式一致）。
   —— 关闭 Telegram 推送与自动 handoff，避免触发会走审查的 /api/ingest。
2. 复用 radio_kg 入库图，以 auto_policy="confirm" 运行：冲突/高风险在图内自动解决，
   不触发 interrupt 审查（与 ingest_batch.py 同款）。

不改动 Radio 录制流程，也不改动 radio_kg 既有入库逻辑；只是作为库调用并设好开关。
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

import yaml

from config.settings import settings as kg_settings
from clip.youtube_source import LiveMeta

_RADIO_ROOT = Path(__file__).resolve().parents[2] / "Radio"        # Agent/Radio
_RADIO_CONFIG = _RADIO_ROOT / "config" / "config.yaml"


def _safe_name(text: str, limit: int = 80) -> str:
    cleaned = re.sub(r'[/\\:*?"<>|]', "_", text).strip()
    return (cleaned or "live")[:limit]


async def _noop(*_a, **_k):  # 替换 Telegram 推送，避免硬依赖与外发
    return None


def _settings_with_profile_translation(settings, profile, work_dir: Path):
    """Apply clip program translation prompt/terminology overrides in memory."""
    if profile is None:
        return settings

    merged_corrections = dict(getattr(settings, "name_corrections", {}) or {})
    merged_corrections.update(getattr(profile, "name_corrections", {}) or {})

    translation_updates = {}
    prompt_path = getattr(profile, "translation_prompt_path", None)
    if prompt_path is not None:
        translation_updates["prompt_path"] = prompt_path
    terminology_path = _write_profile_terminology(settings, profile, work_dir)
    if terminology_path is not None:
        translation_updates["terminology_path"] = terminology_path

    updates = {
        "name_corrections": merged_corrections,
        "translation": settings.translation.model_copy(update=translation_updates),
    }
    # 节目自带 STT prompt：替换全局 prompt（防止其他节目的人名被 Whisper 在音乐段幻听出来）
    stt_prompt = getattr(profile, "stt_prompt", "") or ""
    if stt_prompt.strip():
        updates["stt"] = settings.stt.model_copy(update={"prompt": stt_prompt.strip()})

    return settings.model_copy(update=updates)


def _write_profile_terminology(settings, profile, work_dir: Path) -> Path | None:
    """Merge Radio terminology with clip program terms for this one run."""
    terms = []
    post_corrections = {}
    try:
        from radio.terminology import load_terminology

        base = load_terminology(settings.translation.terminology_path)
        terms.extend(base.get("terms") or [])
        post_corrections.update(base.get("post_corrections") or {})
    except Exception:  # noqa: BLE001 - profile terms can still stand alone
        pass

    terms.extend(_profile_terms_for_prompt(profile))
    for wrong, right in (getattr(profile, "name_corrections", {}) or {}).items():
        if wrong and right:
            post_corrections[str(wrong)] = str(right)
    for src, dst in (getattr(profile, "terminology", {}) or {}).items():
        if src and dst:
            post_corrections[str(src)] = str(dst)

    if not terms and not post_corrections:
        return None

    work_dir.mkdir(parents=True, exist_ok=True)
    path = work_dir / "_clip_profile_terminology.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "description": f"Runtime terminology for {profile.program_id}",
                "terms": terms,
                "post_corrections": post_corrections,
            },
            allow_unicode=True,
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _profile_terms_for_prompt(profile) -> list[dict]:
    out: list[dict] = []
    for src, dst in (getattr(profile, "terminology", {}) or {}).items():
        if src and dst:
            out.append(
                {
                    "category": "program_term",
                    "ja": str(src),
                    "zh": str(dst),
                    "note": "峰月律 / 夢限大みゅーたいぷ节目方案术语。",
                }
            )
    for wrong, right in (getattr(profile, "name_corrections", {}) or {}).items():
        if wrong and right:
            out.append(
                {
                    "category": "asr_correction",
                    "ja": str(wrong),
                    "zh": str(right),
                    "note": "ASR 或旧译可能出现的同音/近音错写；翻译时结合上下文修正。",
                }
            )
    for member in getattr(profile, "members", []) or []:
        name = member.get("name")
        if not name:
            continue
        aliases = [member.get("yomi")] if member.get("yomi") else []
        out.append(
            {
                "category": "member",
                "ja": name,
                "zh": name,
                "aliases": aliases,
                "note": f"{getattr(profile, 'display_name', '')} 成员；{member.get('role', '')}。",
            }
        )
    return out


def _transcribe_and_summarize(live: LiveMeta, work_dir: Path,
                              profile=None) -> Path:
    """复用 Radio run_pipeline 产出 03/04/05；返回 episode 工作目录。

    profile 提供时：把节目处理方案的专名词典并入 Radio settings（仅内存，不改其配置文件），
    并用方案里的节目名作 display_name。
    """
    _ensure_radio_on_path()
    import radio.pipeline as rp
    from radio.config import load_settings

    # 关闭外发副作用：Telegram 推送 + radio_kg 自动 handoff（那条会走审查）。
    rp.send_to_telegram = _noop
    rp.notify_pipeline_failure = _noop
    os.environ["RADIO_KG_AUTO_INGEST"] = "0"
    os.environ.pop("RADIO_KG_AUTO_INGEST_URL", None)

    settings = load_settings(_RADIO_CONFIG)
    display_name = live.title
    if profile is not None:
        settings = _settings_with_profile_translation(settings, profile, work_dir)
        display_name = profile.kg_program_name or profile.display_name

    work_dir.mkdir(parents=True, exist_ok=True)
    # 源是视频（常为 AV1）→ Radio 的音频分段器无法把视频流塞进 m4a。先抽纯音频供 STT，
    # 视频原样保留在归档目录供切片二创。
    audio_path = _extract_audio(Path(live.video_path), work_dir)
    asyncio.run(rp.run_pipeline(
        audio_path=audio_path,
        settings=settings,
        display_name=display_name,
        source="clipper_youtube",
        work_dir=work_dir,
    ))
    return work_dir


def _extract_audio(video_path: Path, work_dir: Path) -> Path:
    """从（可能是 AV1 的）视频抽出 aac 音频，供 STT 用。"""
    from clip.ffmpeg_util import ffmpeg_bin, run
    if video_path.suffix.lower() in (".m4a", ".mp3", ".wav", ".aac"):
        return video_path
    out = work_dir / "audio.m4a"
    run([ffmpeg_bin(), "-y", "-i", str(video_path), "-vn", "-c:a", "aac", "-b:a", "128k", str(out)])
    return out


def ingest_folder_auto(folder: str, auto_policy: str = "confirm", profile=None) -> dict:
    """以 auto_policy 运行 radio_kg 入库图（无审查）。返回前后图谱规模。

    profile 提供 host 时：临时把 KG 主持人身份切到该节目（第一人称/昵称归一到正确的人），
    入库后立即还原默认主持人，避免污染其它节目的图谱。"""
    from langgraph.checkpoint.sqlite import SqliteSaver
    from src import canonical
    from src.agents.annotator_agent import AnnotatorAgent
    from src.agents.extractor_agent import ExtractorAgent
    from src.agents.inspector_agent import InspectorAgent
    from src.agents.sync_agent import SyncAgent
    from src.graph.ingestion_graph import Deps, build_ingestion_graph
    from src.ingest import ingest_one
    from src.llm.client import LLMClient
    from src.mcp_layer.graph_store import GraphStore
    from src.mcp_layer.vector_store import VectorStore

    host_overridden = bool(profile and getattr(profile, "host_canonical", ""))
    if host_overridden:
        canonical.set_host(profile.host_canonical, profile.host_aliases, profile.host_type)
        print(f"  KG 主持人身份 → {profile.host_canonical}（别名 {len(profile.host_aliases)} 个）")

    llm = LLMClient()
    ckpt_path = str(kg_settings.abspath(kg_settings.checkpoint_db))
    Path(ckpt_path).parent.mkdir(parents=True, exist_ok=True)
    try:
        with GraphStore() as graph_store, VectorStore() as vector_store, \
                SqliteSaver.from_conn_string(ckpt_path) as ckpt:
            before = graph_store.stats()
            deps = Deps(
                extractor=ExtractorAgent(llm, graph_store),
                inspector=InspectorAgent(llm, graph_store),
                sync=SyncAgent(graph_store),
                vector=vector_store,
                annotator=AnnotatorAgent(llm),
                auto_policy=auto_policy,          # ← 关键：无审查
            )
            graph = build_ingestion_graph(deps, ckpt)
            ingest_one(graph, folder, auto_policy)
            after = graph_store.stats()
    finally:
        if host_overridden:
            canonical.reset_host()
    return {"auto_policy": auto_policy, "before": before, "after": after}


def summarize_and_ingest(live: LiveMeta, profile=None) -> dict:
    # 归档方案：视频已下载到归档 episode 目录（= 视频所在目录），03/04/05 也产在此。
    work_dir = Path(live.video_path).parent
    auto_policy = getattr(profile, "auto_policy", "confirm") if profile else "confirm"
    _transcribe_and_summarize(live, work_dir, profile=profile)
    info = ingest_folder_auto(str(work_dir), auto_policy=auto_policy, profile=profile)
    info["episode_dir"] = str(work_dir)
    print(f"  自动入库完成（auto_policy={auto_policy}，无审查）：{info['before']} → {info['after']}")
    return info
