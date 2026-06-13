"""「直播录制和切片」前端的后端路由（独立分块，挂在主服务 /clipper 下）。

提供：
- GET  /clipper                 → 页面
- GET  /clipper/api/programs    → 可用节目方案
- GET  /clipper/api/interests   → 个人感兴趣话题；POST 保存
- GET  /clipper/api/trends      → 近期涨得最快的前 X 个 B 站视频 + 数据 + 爆火因素
- POST /clipper/api/record      → 提交「下载/录制 → 处理入库 → 分析 → 推 Telegram」任务
- GET  /clipper/api/jobs        → 任务状态
"""
from __future__ import annotations

import json
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse
from pydantic import BaseModel

from clip.config import clip_config

_STATIC = Path(__file__).resolve().parents[1] / "static"            # Agent/clip/static
_INTERESTS = clip_config.abspath("./data/clipper_interests.json")   # Agent/clip/data

router = APIRouter(prefix="/clipper")


def _with_cost(kind: str, title: str, fn, *args):
    """在成本台账里把一次后台任务记为一条「产出」（LLM token + 全程墙钟耗时）。"""
    from src.llm import cost
    with cost.track(kind, title=title):
        fn(*args)


# ---- 简单缓存 + 任务表 ----
_trends_cache: dict = {"key": None, "ts": 0.0, "data": None}
_TRENDS_TTL = 300
_jobs: dict[str, dict] = {}


@router.get("")
@router.get("/")
def clipper_page():
    return FileResponse(_STATIC / "clipper.html", headers={"Cache-Control": "no-store"})


@router.get("/api/programs")
def list_programs():
    d = Path(__file__).resolve().parent / "programs"
    out = []
    for y in sorted(d.glob("*.yaml")):
        try:
            from clip.program_profile import load_profile
            p = load_profile(y.stem)
            out.append({"id": p.program_id, "display_name": p.display_name,
                        "performer": p.performer, "auto_telegram": p.auto_telegram})
        except Exception:  # noqa: BLE001
            out.append({"id": y.stem, "display_name": y.stem})
    return {"programs": out}


@router.get("/api/interests")
def get_interests():
    if _INTERESTS.exists():
        return json.loads(_INTERESTS.read_text(encoding="utf-8"))
    return {"topics": []}


class Interests(BaseModel):
    topics: list[str] = []


@router.post("/api/interests")
def set_interests(body: Interests):
    _INTERESTS.parent.mkdir(parents=True, exist_ok=True)
    _INTERESTS.write_text(json.dumps({"topics": body.topics}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return {"ok": True, "topics": body.topics}


def _load_trends_data(partitions: str = "", topk: int = 10, factors: bool = True,
                      *, force: bool = False) -> dict:
    # 聚焦「歌曲/虚拟主播/bangdream」：music 排行 + 关键词搜索（含个人兴趣话题）
    parts = partitions or clip_config.trends_partitions
    interests = (get_interests() or {}).get("topics", [])
    keywords = [k.strip() for k in clip_config.trends_keywords.split(",") if k.strip()] + interests
    key = (
        f"{parts}|{','.join(keywords)}|{topk}|{factors}|"
        f"min_age={clip_config.trends_min_age_hours}|pages={clip_config.trends_search_pages}"
    )
    now = time.time()
    if not force and _trends_cache["key"] == key and (now - _trends_cache["ts"]) < _TRENDS_TTL:
        return _trends_cache["data"]

    from clip.bilibili_source import BilibiliClient, fetch_trends
    from clip.trend_features import _now_ref, distill_features, score_item
    items = fetch_trends(partitions=parts, keywords=keywords, per_keyword=20)
    now_ref = _now_ref(items)
    # 不套用 6-48h 时间窗：综合热门/分区排行本身就是 curated 当下热点，且本机时钟为
    # 虚构未来日，时间窗会把全部条目误杀（这正是此前看板空白的原因）。只按动量排序。
    scored = sorted(items, key=lambda it: score_item(it, now_ref), reverse=True)
    kws = [k.lower() for k in keywords]

    def _match(it):
        blob = f"{it.title} {it.tname} {it.owner} {it.desc}".lower()
        return any(k in blob for k in kws)

    # 全站热点：综合热门为主（真正在火、跨分区），回退到整体动量榜
    broad = [it for it in scored if it.partition == "综合热门"][:topk] or scored[:topk]
    # 圈内热点：与 歌曲/虚拟主播/bangdream/声優 相关；可能为空（=当前圈内无出圈热点）
    niche = [it for it in scored if _match(it)][:topk]

    def _vid(it, scope):
        h = it.hours_since(now_ref)
        return {
            "bvid": it.bvid, "title": it.title, "scope": scope,
            "partition": it.tname or it.partition, "owner": it.owner,
            "view": it.view, "like": it.like, "coin": it.coin,
            "danmaku": it.danmaku, "reply": it.reply,
            "hours": round(h, 1), "momentum": round(it.view / h),
            "score": round(score_item(it, now_ref)),
            "url": f"https://www.bilibili.com/video/{it.bvid}",
        }
    broad_v = [_vid(it, "全站") for it in broad]
    niche_v = [_vid(it, "圈内") for it in niche]
    # videos（向后兼容 title_recommender）：圈内优先、其次全站，去重
    seen_v, videos = set(), []
    for v in niche_v + broad_v:
        if v["bvid"] not in seen_v:
            seen_v.add(v["bvid"])
            videos.append(v)

    factor_list = []
    if factors:
        # 爆火因素同时覆盖「圈内 + 出圈」，直接回答“是否错过了真正的热点”
        factor_items, fseen = [], set()
        for it in niche[:5] + broad[:6]:
            if it.bvid not in fseen:
                fseen.add(it.bvid)
                factor_items.append(it)
        if factor_items:
            try:
                from src.llm.client import LLMClient
                with BilibiliClient() as cli:
                    for it in factor_items:
                        cli.enrich(it)
                feats = distill_features(factor_items, LLMClient(),
                                         top_n=len(factor_items), prerank=False)
                factor_list = [{"topic": f.topic, "keywords": (f.keywords_zh + f.keywords_ja)[:6],
                                "hot_songs": f.hot_songs, "hook": f.hook} for f in feats]
            except Exception as e:  # noqa: BLE001
                factor_list = [{"topic": f"(爆火因素分析失败: {e})", "keywords": [], "hot_songs": [], "hook": ""}]

    data = {
        "broad": broad_v,
        "niche": niche_v,
        "videos": videos,
        "factors": factor_list,
        "generated_at": time.strftime("%H:%M:%S"),
    }
    _trends_cache.update(key=key, ts=now, data=data)
    return data


@router.get("/api/trends")
def trends(partitions: str = "", topk: int = 10, factors: bool = True, force: bool = False):
    return _load_trends_data(partitions=partitions, topk=topk, factors=factors, force=force)


class RecordReq(BaseModel):
    url: str
    program: str | None = None
    res: int | None = None
    telegram: bool = True


def _run_job(job_id: str, req: RecordReq):
    _jobs[job_id].update(status="running", stage="下载/处理")
    try:
        if req.res:
            clip_config.clip_video_res = req.res
        from clip.pipeline import pipeline_new
        run_dir = pipeline_new(req.url, profile_id=req.program,
                               dry_run=False, no_render=True, to_telegram=req.telegram)
        _jobs[job_id].update(status="done", stage="完成", run_dir=str(run_dir))
    except Exception as e:  # noqa: BLE001
        _jobs[job_id].update(status="error", stage="失败", error=str(e))


@router.post("/api/record")
def record(req: RecordReq):
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"id": job_id, "url": req.url, "program": req.program,
                     "status": "queued", "stage": "排队", "ts": time.time()}
    threading.Thread(target=_with_cost, args=("clip", f"录制处理 {req.url[:48]}", _run_job, job_id, req),
                     daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/jobs")
def jobs():
    return {"jobs": sorted(_jobs.values(), key=lambda j: j["ts"], reverse=True)}


# ───────── 手动指定时间轴切片 + 节目报告 ─────────
@router.get("/api/episodes")
def episodes(program: str):
    """列出某节目方案已归档的各期（供手动切片 / 报告选择）。"""
    from clip.program_profile import load_profile
    try:
        prof = load_profile(program)
        root = clip_config.abspath(prof.recordings_root) / prof.collection_id
    except Exception:  # noqa: BLE001
        return {"episodes": []}
    out = []
    if root.exists():
        for d in sorted(root.iterdir(), reverse=True):
            if d.is_dir() and not d.name.startswith("."):
                out.append({"episode_dir": str(d), "name": d.name,
                            "has_summary": (d / "05_summary.json").exists(),
                            "has_video": (d / "source.mp4").exists()})
    return {"episodes": out}


class SliceReq(BaseModel):
    program: str | None = None
    episode_dir: str
    start: float
    end: float
    llm_provider: str | None = None
    llm_model: str | None = None


class AssembleReq(BaseModel):
    job_id: str
    cues: list[dict]


def _llm_choice(req: SliceReq) -> tuple[str | None, str | None, bool]:
    provider = (req.llm_provider or "").strip().lower() or None
    model = (req.llm_model or "").strip() or None
    if provider and provider not in {"anthropic", "openai", "deepseek", "mimo"}:
        raise ValueError(f"不支持的 LLM provider：{provider}")
    return provider, model, bool(provider or model)


def _clip_sec(value, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            parts = [float(x) for x in value.strip().split(":")]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            if len(parts) == 2:
                return parts[0] * 60 + parts[1]
            return parts[0] if parts else default
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _section_range(section: dict) -> tuple[float, float] | None:
    parts = re.split(r"\s*[-–—]\s*", str(section.get("time_range") or ""), maxsplit=1)
    if len(parts) != 2:
        return None
    st, en = _clip_sec(parts[0]), _clip_sec(parts[1])
    return (st, en) if en > st else None


def _music_title(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    m = re.search(r"[「『《〈]([^」』》〉]{1,80})[」』》〉]", text)
    if m:
        return m.group(1).strip()
    return text.strip(" \t-—:：/／")


def _title_key(text: str) -> str:
    return re.sub(r"[\s　\"'`、。，．・!！?？:：;；/／\\\-\[\]（）()【】「」『』《》〈〉]+",
                  "", (text or "").lower())


def _song_artist_for_title(title: str, songs: list) -> str | None:
    key = _title_key(title)
    if not key:
        return None
    for song in songs:
        other = _title_key(getattr(song, "title", ""))
        if other and (key == other or key in other or other in key):
            return (getattr(song, "artist", None) or "").strip() or None
    return None


_SONG_START_MARKERS = (
    "聞いてください", "聴いてください", "歌っていき", "歌います", "歌わせて",
    "いきたいと思います", "いきましょう", "それでは", "じゃあ次",
)
_CHAT_LIKE_MARKERS = (
    "ありがとう", "ございます", "コメント", "じゃあ", "次", "歌います",
    "歌っていき", "歌わせて", "思います", "聞いてください", "聴いてください",
    "お疲れ", "よろしく", "ごめん", "待って", "えー", "はい",
)


def _seg_text(seg: dict) -> str:
    return (seg.get("ja") or seg.get("text") or "").strip()


def _seg_start(seg: dict) -> float:
    return _clip_sec(seg.get("start"))


def _seg_end(seg: dict) -> float:
    return _clip_sec(seg.get("end"), _seg_start(seg))


def _songish_line(text: str) -> bool:
    s = (text or "").strip()
    return len(s) >= 3 and not any(m in s for m in _CHAT_LIKE_MARKERS)


def _song_start_from_slice_asr(transcript: list[dict], start: float, end: float) -> float | None:
    rows = [
        seg for seg in sorted(transcript, key=_seg_start)
        if _seg_end(seg) > start and _seg_start(seg) < end
    ]
    near_end = min(end, start + 120.0)
    for i, seg in enumerate(rows):
        if _seg_start(seg) > near_end:
            break
        text = _seg_text(seg)
        if not any(m in text for m in _SONG_START_MARKERS):
            continue
        marker_end = _seg_end(seg)
        for nxt in rows[i + 1:]:
            nst = _seg_start(nxt)
            if nst < marker_end - 0.25:
                continue
            if nst - marker_end > 30.0:
                break
            next_text = _seg_text(nxt)
            if next_text and not any(m in next_text for m in _SONG_START_MARKERS):
                return max(start, nst)
        return max(start, marker_end)
    for seg in rows:
        if _seg_start(seg) > min(end, start + 60.0):
            break
        if _songish_line(_seg_text(seg)):
            return max(start, _seg_start(seg))
    return None


def _fallback_song_spans_from_summary(
    summary: dict,
    transcript: list[dict],
    songs: list,
    start: float,
    end: float,
) -> list:
    from clip.lyrics import SongSpan

    best: tuple[float, SongSpan] | None = None
    for section in summary.get("sections", []) or []:
        rng = _section_range(section)
        if not rng:
            continue
        sec_start, sec_end = rng
        overlap = min(end, sec_end) - max(start, sec_start)
        if overlap <= 0:
            continue
        title_blob = f"{section.get('title_ja') or section.get('title') or ''} {section.get('intro') or ''}"
        if "オープニング" in title_blob and "BGM" in title_blob:
            continue
        titles = [_music_title(x) for x in (section.get("music") or [])]
        titles = [x for x in titles if x]
        if not titles:
            continue
        progress = (max(start, sec_start) - sec_start) / max(sec_end - sec_start, 1.0)
        idx = max(0, min(len(titles) - 1, int(progress * len(titles))))
        song_start = _song_start_from_slice_asr(transcript, start, min(end, sec_end))
        if song_start is None:
            continue
        song_end = min(end, sec_end)
        if song_end - song_start < 8.0:
            continue
        title = titles[idx]
        span = SongSpan(song_start, song_end, title, artist=_song_artist_for_title(title, songs))
        if best is None or overlap > best[0]:
            best = (overlap, span)
    return [best[1]] if best else []


def _song_spans_from_setlist(songs: list, start: float, end: float) -> list:
    from clip.lyrics import SongSpan

    spans = []
    for s in songs:
        song_start = s.song_start if s.song_start is not None else s.start
        song_end = s.song_end if s.song_end is not None else s.end
        if song_end > start and song_start < end:
            spans.append(SongSpan(song_start, song_end, s.title, artist=s.artist))
    return spans


def _setlist_spans(
    D: Path,
    start: float,
    end: float,
    llm_provider: str | None = None,
    llm_model: str | None = None,
) -> list:
    """范围内的歌曲 → 歌唱区间（占位/已授权歌词）；其余谈话走二次精听。失败返回 []。"""
    from clip.setlist import extract_setlist
    from src.llm.client import LLMClient
    sp = D / "05_summary.json"
    if not sp.exists():
        return []
    try:
        transcript = []
        for fname in ("04_bilingual_segments.json", "03_ja_segments.json"):
            tp = D / fname
            if tp.exists():
                transcript = json.loads(tp.read_text("utf-8"))
                break
        summary = json.loads(sp.read_text("utf-8"))
        songs = []
        try:
            songs = extract_setlist(
                summary,
                LLMClient(provider=llm_provider, model=llm_model),
                transcript_segments=transcript,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] setlist primary failed, trying summary fallback: {e}")
        spans = _song_spans_from_setlist(songs, start, end)
        if not spans:
            spans = _fallback_song_spans_from_summary(summary, transcript, songs, start, end)
            if spans:
                labels = ", ".join(f"{s.title}@{s.start:.1f}-{s.end:.1f}" for s in spans)
                print(f"  setlist fallback: {labels}")
        return spans
    except Exception as e:  # noqa: BLE001
        print(f"  [warn] setlist spans failed: {e}")
        return []


def _slice_out_dir(D: Path, start: float, end: float, job_id: str) -> Path:
    from clip.youtube_source import safe_dirname
    return (clip_config.abspath("./data/clips")
            / f"{safe_dirname(D.name, 50)}_{int(start)}-{int(end)}s_{job_id}")


def _run_slice(job_id: str, req: SliceReq):
    _jobs[job_id].update(status="running", stage="切片+二次精听中")
    try:
        from clip.program_profile import load_profile
        from clip.render import render_segment
        prof = load_profile(req.program) if req.program else None
        D = Path(req.episode_dir)
        llm_provider, llm_model, force_llm = _llm_choice(req)
        out_dir = _slice_out_dir(D, req.start, req.end, job_id)
        final = render_segment(D / "source.mp4", req.start, req.end, out_dir, 0,
                               episode_dir=str(D), profile=prof, song_spans=[],
                               llm_provider=llm_provider, llm_model=llm_model,
                               force_llm_retranslate=force_llm,
                               slice_reprocess_song_spans=True)
        _jobs[job_id].update(status="done", stage="完成", clip_path=str(final),
                             size_mb=round(final.stat().st_size / 1048576, 1))
    except Exception as e:  # noqa: BLE001
        _jobs[job_id].update(status="error", stage="失败", error=str(e))


@router.post("/api/slice")
def slice_ep(req: SliceReq):
    job_id = uuid.uuid4().hex[:8]
    _jobs[job_id] = {"id": job_id, "kind": "slice", "status": "queued", "stage": "排队",
                     "ts": time.time(), "range": f"{int(req.start)}-{int(req.end)}s"}
    threading.Thread(target=_with_cost,
                     args=("clip", f"切片 {int(req.start)}-{int(req.end)}s", _run_slice, job_id, req),
                     daemon=True).start()
    return {"job_id": job_id}


# ---- 两段式切片：预览(切片+生成可编辑字幕) → 人工审核/修改 → 组装烧录 ----
def _cue_to_dict(c) -> dict:
    return {"start": round(c.start, 3), "end": round(c.end, 3),
            "ja": c.ja, "zh": c.zh, "song": c.ja.strip().startswith("♪")}


def _slice_title_recommendation(cues_data: list[dict],
                                llm_provider: str | None = None,
                                llm_model: str | None = None,
                                profile=None) -> dict:
    try:
        trend_data = _load_trends_data(topk=10, factors=True, force=False)
    except Exception as e:  # noqa: BLE001
        trend_data = {"videos": [], "factors": [{"topic": f"趋势数据获取失败：{e}"}]}
    llm = None
    try:
        from src.llm.client import LLMClient
        llm = LLMClient(provider=llm_provider, model=llm_model)
    except Exception:
        llm = None
    from clip.title_recommender import recommend_title
    return recommend_title(
        cues_data,
        trend_data,
        llm=llm,
        performer=getattr(profile, "performer", "") or "",
        program=getattr(profile, "display_name", "") or "",
    )


def _run_preview(job_id: str, req: SliceReq):
    _jobs[job_id].update(status="running", stage="切片+二次精听+补译中")
    try:
        from clip.aligner import normalize_cues_for_display
        from clip.program_profile import load_profile
        from clip.render import prepare_segment
        prof = load_profile(req.program) if req.program else None
        D = Path(req.episode_dir)
        llm_provider, llm_model, force_llm = _llm_choice(req)
        out_dir = _slice_out_dir(D, req.start, req.end, job_id)
        cut, cues = prepare_segment(D / "source.mp4", req.start, req.end, out_dir, 0,
                                    episode_dir=str(D), profile=prof, song_spans=[],
                                    llm_provider=llm_provider, llm_model=llm_model,
                                    force_llm_retranslate=force_llm,
                                    slice_reprocess_song_spans=True)
        accent = list(prof.accent_rgb((255, 255, 255))) if prof else [255, 255, 255]
        cues = normalize_cues_for_display(cues)
        cues_data = [_cue_to_dict(c) for c in cues]
        title_rec = _slice_title_recommendation(cues_data, llm_provider, llm_model, profile=prof)
        (out_dir / "cues.json").write_text(
            json.dumps(cues_data, ensure_ascii=False, indent=2), encoding="utf-8")
        llm_label = llm_provider or "default"
        if llm_model:
            llm_label += f"/{llm_model}"
        _jobs[job_id].update(status="done", stage=f"待审核 · LLM {llm_label}", cut_path=str(cut),
                             out_dir=str(out_dir), accent=accent, cues=cues_data,
                             title_recommendation=title_rec)
    except Exception as e:  # noqa: BLE001
        _jobs[job_id].update(status="error", stage="失败", error=str(e))


@router.post("/api/slice/preview")
def slice_preview(req: SliceReq):
    job_id = uuid.uuid4().hex[:8]
    llm_provider, llm_model, _ = _llm_choice(req)
    llm_label = llm_provider or "default"
    if llm_model:
        llm_label += f"/{llm_model}"
    _jobs[job_id] = {"id": job_id, "kind": "preview", "status": "queued",
                     "stage": f"排队 · LLM {llm_label}",
                     "ts": time.time(), "range": f"{int(req.start)}-{int(req.end)}s"}
    threading.Thread(target=_with_cost,
                     args=("clip", f"切片预览 {int(req.start)}-{int(req.end)}s", _run_preview, job_id, req),
                     daemon=True).start()
    return {"job_id": job_id}


@router.get("/api/slice/preview/{job_id}")
def slice_preview_status(job_id: str):
    j = _jobs.get(job_id)
    if not j:
        return {"error": "任务不存在"}
    return {"status": j.get("status"), "stage": j.get("stage"), "error": j.get("error"),
            "cues": j.get("cues", []), "has_final": bool(j.get("clip_path")),
            "size_mb": j.get("size_mb"), "title_recommendation": j.get("title_recommendation")}


@router.get("/api/slice/preview_video/{job_id}")
def slice_preview_video(job_id: str):
    j = _jobs.get(job_id)
    if not j or not j.get("cut_path") or not Path(j["cut_path"]).exists():
        return {"error": "尚未就绪或不存在"}
    return FileResponse(j["cut_path"], media_type="video/mp4")


def _run_assemble(job_id: str, cues_data: list[dict]):
    _jobs[job_id].update(status="running", stage="组装烧录中", error=None)
    try:
        from clip.aligner import Cue, normalize_cues_for_display
        from clip.render import assemble_segment
        job = _jobs[job_id]
        cut, out_dir = Path(job["cut_path"]), Path(job["out_dir"])
        accent = tuple(job.get("accent") or (255, 255, 255))
        cues = [Cue(float(c["start"]), float(c["end"]),
                    (c.get("ja") or "").strip(), (c.get("zh") or "").strip())
                for c in cues_data if (c.get("ja") or c.get("zh"))]
        cues = normalize_cues_for_display(cues)
        job["cues"] = [_cue_to_dict(c) for c in cues]
        final = assemble_segment(cut, cues, out_dir, 0, accent=accent)
        job.update(status="done", stage="完成", clip_path=str(final),
                   size_mb=round(final.stat().st_size / 1048576, 1))
    except Exception as e:  # noqa: BLE001
        _jobs[job_id].update(status="error", stage="失败", error=str(e))


@router.post("/api/slice/assemble")
def slice_assemble(req: AssembleReq):
    j = _jobs.get(req.job_id)
    if not j or not j.get("cut_path"):
        return {"error": "预览任务不存在或未就绪"}
    try:
        from clip.aligner import Cue, normalize_cues_for_display
        cues = [Cue(float(c["start"]), float(c["end"]),
                    (c.get("ja") or "").strip(), (c.get("zh") or "").strip())
                for c in req.cues if (c.get("ja") or c.get("zh"))]
        cues_data = [_cue_to_dict(c) for c in normalize_cues_for_display(cues)]
        Path(j["out_dir"], "cues.json").write_text(
            json.dumps(cues_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        cues_data = req.cues
    threading.Thread(target=_run_assemble, args=(req.job_id, cues_data), daemon=True).start()
    return {"job_id": req.job_id}


@router.get("/api/clip/{job_id}")
def download_clip(job_id: str):
    j = _jobs.get(job_id)
    if not j or not j.get("clip_path") or not Path(j["clip_path"]).exists():
        return {"error": "尚未就绪或不存在"}
    return FileResponse(j["clip_path"], filename=Path(j["clip_path"]).name, media_type="video/mp4")


@router.get("/api/report")
def report(episode_dir: str):
    from clip.report import build_report
    return build_report(episode_dir)


# ───────── 数据看板：B 站账号监测 + 全链路成本台账 ─────────
_account_cache: dict[str, dict] = {}
_ACCOUNT_TTL = 300


@router.get("/dashboard")
def databoard_page():
    return FileResponse(_STATIC / "databoard.html", headers={"Cache-Control": "no-store"})


@router.get("/api/account")
def monitor_account(account: str = "", limit: int = 20, force: bool = False):
    """监测某 B 站账号（主页 URL 或 UID）的稿件播放量。只读公开数据。"""
    account = (account or "").strip()
    if not account:
        return {"error": "请提供 B 站账号主页 URL 或 UID（例：space.bilibili.com/946974）"}
    key = f"{account}|{limit}"
    now = time.time()
    cached = _account_cache.get(key)
    if not force and cached and now - cached["ts"] < _ACCOUNT_TTL:
        return cached["data"]
    try:
        from clip.account_monitor import fetch_account
        data = fetch_account(account, limit=limit)
    except Exception as e:  # noqa: BLE001 — 风控/解析失败回前端文案
        return {"error": str(e)}
    _account_cache[key] = {"ts": now, "data": data}
    return data


@router.get("/api/cost")
def cost_board(n: int = 50):
    """全链路 LLM 成本台账：每条产出的 token / 耗时 / 估算 USD + 汇总。"""
    from src.llm import cost
    return {"outputs": cost.recent_outputs(n), "totals": cost.totals()}
