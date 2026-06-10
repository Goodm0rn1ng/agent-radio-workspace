"""编排两条分支：pipeline_past（Branch A）/ pipeline_new（Branch B）。

dry_run：只跑选材，产出 plan.json，不依赖 ffmpeg/whisperx。
no_render：切片但不做字幕对齐/烧录。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from clip.bilibili_source import BilibiliClient, fetch_trends
from clip.config import clip_config
from clip.models import MatchedClip
from clip.trend_features import distill_features, rank_items
from src.llm.client import LLMClient


def _new_run_dir(tag: str) -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    d = clip_config.abspath(clip_config.clip_output_dir) / f"{tag}_{ts}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_plan(run_dir: Path, branch: str, clips: list[MatchedClip], extra: dict | None = None) -> Path:
    plan = {
        "branch": branch,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "clips": [c.to_dict() for c in clips],
        **(extra or {}),
    }
    path = run_dir / "plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nplan.json -> {path}  （{len(clips)} 个候选片段）")
    return path


def _collect_trends(llm: LLMClient) -> list:
    print("== [1] 抓取 B 站分区热榜 ==")
    items = fetch_trends()
    top = rank_items(items)[: clip_config.clip_topk * 2]
    print(f"== 增强 top {len(top)} 条（tags/热评）==")
    with BilibiliClient() as cli:
        for it in top:
            cli.enrich(it)
    print("== 提炼爆款特征 ==")
    feats = distill_features(items, llm, top_n=clip_config.clip_topk * 2)
    for f in feats:
        print(f"  · {f.topic}  | ja={f.keywords_ja} songs={f.hot_songs}")
    return feats


def pipeline_past(dry_run: bool = False, no_render: bool = False) -> Path:
    from clip.matcher import match_trends  # 局部导入，避免无谓加载向量栈
    llm = LLMClient()
    run_dir = _new_run_dir("past")
    feats = _collect_trends(llm)
    print("== [2] 向量匹配过往素材 ==")
    clips = match_trends(feats, llm)
    for c in clips:
        flag = "⚠无媒体" if c.media_missing else "✓"
        print(f"  {flag} [{c.score:.2f}] {c.episode_label} {c.start:.0f}-{c.end:.0f}s · {c.title}")
    _write_plan(run_dir, "past", clips)
    if not dry_run:
        _render_clips(clips, run_dir, no_render)
    return run_dir


def pipeline_new(url: str, profile_id: str | None = None,
                 dry_run: bool = False, no_render: bool = False,
                 to_telegram: bool = False) -> Path:
    from clip.viral_analyzer import analyze_live
    from clip.youtube_source import (archive_episode_dir, download_live,
                                            probe_meta)
    llm = LLMClient()
    run_dir = _new_run_dir("new")

    profile = None
    if profile_id:
        from clip.program_profile import load_profile
        profile = load_profile(profile_id)
        print(f"== 节目方案：{profile.display_name}（{profile.performer}）==")
        if getattr(profile, "auto_telegram", False):
            to_telegram = True   # 方案声明上传后自动推 Telegram

    print("== [B-1] yt-dlp 下载直播（视频+音频）==")
    if profile is not None:
        # 归档方案：直接落到 RADIO_DATA_DIR/<collection>/<date>_<title>/
        meta = probe_meta(url)
        rec_root = clip_config.abspath(profile.recordings_root)
        dest = archive_episode_dir(rec_root, profile.collection_id, meta)
        print(f"  归档目录：{dest}")
    else:
        dest = run_dir / "source"
    live = download_live(url, dest)

    print("== [B-2] 抓取 B 站热点 + 爆火潜力分析 ==")
    clips = []
    try:
        feats = _collect_trends(llm)
        clips = analyze_live(live, feats, llm, profile=profile)
        for c in clips:
            print(f"  [{c.score:.2f}] {c.start:.0f}-{c.end:.0f}s · {c.title} ← {c.matched_signal}")
    except Exception as e:  # noqa: BLE001 — 选材失败不应阻断核心的处理+归档
        print(f"  [warn] B 站热点/爆火分析失败（不影响处理+归档）：{e}")

    ingest_info: dict = {}
    if not dry_run:
        print("== [B-3] 自动总结入库（无审查）==")
        try:
            from clip.kb_ingest import summarize_and_ingest
            ingest_info = summarize_and_ingest(live, profile=profile)
        except Exception as e:  # noqa: BLE001 — 入库失败不阻断剪辑分支
            print(f"  [warn] 自动入库失败（不影响剪辑）：{e}")
            ingest_info = {"error": str(e)}

    # 本场歌枠区块/曲目（仅曲名，无歌词）
    episode_dir = live.video_path.parent if live.video_path else None
    songs = []
    if not dry_run and episode_dir and (episode_dir / "05_summary.json").exists():
        try:
            import json as _json

            from clip.setlist import extract_setlist
            transcript = []
            for fname in ("04_bilingual_segments.json", "03_ja_segments.json"):
                p = episode_dir / fname
                if p.exists():
                    transcript = _json.loads(p.read_text("utf-8"))
                    break
            songs = extract_setlist(
                _json.loads((episode_dir / "05_summary.json").read_text("utf-8")),
                llm,
                transcript_segments=transcript,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 取曲清单失败：{e}")

    _write_plan(run_dir, "new", clips, extra={
        "source_url": url,
        "program": profile.program_id if profile else None,
        "archive_dir": str(episode_dir) if episode_dir else None,
        "songs": [s.to_dict() for s in songs],
        "ingest": ingest_info,
    })

    if to_telegram and not dry_run and episode_dir:
        # 推送「爆火片段 + 歌枠区块」菜单到 Telegram，点击即切片（不在本地自动渲染）
        print("== [B-4] 推送 Telegram 切片菜单 ==")
        try:
            from clip.telegram_clip import push_clip_menu_sync
            push_clip_menu_sync(profile, clips, songs, str(episode_dir))
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] Telegram 推送失败：{e}")
    elif not dry_run:
        accent = profile.accent_rgb((255, 255, 255)) if profile else (255, 255, 255)
        _render_clips(clips, run_dir, no_render, accent=accent)
    return run_dir


def _render_clips(clips: list[MatchedClip], run_dir: Path, no_render: bool,
                  accent=(255, 255, 255)) -> None:
    """切片 →（可选）字幕对齐 + 烧录。重依赖在此惰性导入。"""
    from clip.slicer import slice_clip

    out_dir = run_dir / "clips"
    out_dir.mkdir(exist_ok=True)
    for i, c in enumerate(clips):
        if c.media_missing or not c.media_path:
            print(f"  [skip] 片段 {i+1} 无源媒体，跳过切片（保留在 plan.json）")
            continue
        if (c.end - c.start) > 600:
            print(f"  [skip] 片段 {i+1} 时长 {c.end-c.start:.0f}s 过长（>10min，疑似整场无章节），跳过渲染")
            continue
        try:
            cut = slice_clip(c, out_dir, i)
            print(f"  切片 -> {cut.name}")
            if no_render:
                continue
            from clip.aligner import make_subtitles
            from clip.packager import package
            srt = make_subtitles(c, cut, out_dir, i)
            final = package(cut, srt, out_dir, i, accent=accent)
            print(f"  成片 -> {final.name}")
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 片段 {i+1} 渲染失败：{e}")
