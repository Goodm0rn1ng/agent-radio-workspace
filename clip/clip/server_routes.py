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


@router.get("/programs")
def programs_page():
    return FileResponse(_STATIC / "programs.html", headers={"Cache-Control": "no-store"})


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


@router.get("/api/programs/{program_id}")
def program_detail(program_id: str):
    """节目方案完整明细（原始 yaml 文本 + 自带提示词全文），供前端编辑。"""
    from fastapi import HTTPException

    from clip.program_profile import profile_detail
    try:
        return profile_detail(program_id)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class ProgramSave(BaseModel):
    yaml_text: str
    summary_prompt: str | None = None
    translation_prompt: str | None = None


@router.put("/api/programs/{program_id}")
def program_save(program_id: str, body: ProgramSave):
    """保存编辑后的节目方案（yaml 逐字写回，保留注释）。"""
    from fastapi import HTTPException

    from clip.program_profile import save_profile
    try:
        return save_profile(
            program_id,
            body.yaml_text,
            summary_prompt_text=body.summary_prompt,
            translation_prompt_text=body.translation_prompt,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


class ProgramDraft(BaseModel):
    description: str
    channel_url: str | None = None


@router.post("/api/programs/draft")
def program_draft(body: ProgramDraft):
    """信息收集 agent：自然语言描述 → 抓取 ACG/声优资料站 → LLM 合成方案草稿（不落盘）。"""
    from fastapi import HTTPException

    from clip.profile_research import research_profile
    if not (body.description or "").strip():
        raise HTTPException(status_code=400, detail="请填写节目描述")
    try:
        res = research_profile(body.description, channel_url=body.channel_url)
    except Exception as e:  # noqa: BLE001 - 调研失败回前端而非 500 静默
        raise HTTPException(status_code=502, detail=f"调研失败：{e}") from e
    return {"program_id": res.program_id, "yaml_text": res.yaml_text,
            "sources": res.sources, "notes": res.notes}


class DryRunReq(BaseModel):
    program: str | None = None
    episode_dir: str
    summary_prompt: str | None = None          # 草稿提示词全文；空＝用方案/全局默认
    summary_style: str | None = None
    style_exemplar: str | None = None          # few-shot 范例总结
    max_summary_chars: int | None = None
    target_highlight_count: int | None = None


@router.post("/api/programs/dryrun")
def program_dryrun(req: DryRunReq):
    """用（可能未保存的）草稿设置，在选定样片上实跑一次总结，返回真实 Summary。

    summary_* 字段缺省＝沿用该方案/全局默认（用于「改前」对照）；提供＝草稿覆盖（「改后」）。
    无副作用：临时提示词写 temp、auto_append_new_segments=False。
    """
    import asyncio
    import shutil
    import tempfile
    from fastapi import HTTPException

    ep = Path(req.episode_dir)
    bil = ep / "04_bilingual_segments.json"
    if not bil.exists():
        raise HTTPException(status_code=400, detail="该期缺少 04_bilingual_segments.json，无法试运行")

    from radio.config import load_settings
    from radio.models import Segment
    from radio.summarize import summarize
    from clip.kb_ingest import _RADIO_CONFIG, _settings_with_profile_translation

    tmpd = Path(tempfile.mkdtemp(prefix="_dryrun_"))
    try:
        settings = load_settings(_RADIO_CONFIG)
        program_name = ""
        if req.program:
            try:
                from clip.program_profile import load_profile
                prof = load_profile(req.program)
                settings = _settings_with_profile_translation(settings, prof, tmpd)
                program_name = prof.kg_program_name or prof.display_name
            except Exception:  # noqa: BLE001 — 方案加载失败则用全局默认
                pass
        su: dict = {"auto_append_new_segments": False}
        if req.summary_prompt and req.summary_prompt.strip():
            p = tmpd / "_dryrun_summarize.txt"
            p.write_text(req.summary_prompt, encoding="utf-8")
            su["prompt_path"] = p
        if req.summary_style is not None:
            su["summary_style"] = req.summary_style
        if req.style_exemplar is not None:
            su["style_exemplar"] = req.style_exemplar
        if req.max_summary_chars:
            su["max_summary_chars"] = req.max_summary_chars
        if req.target_highlight_count is not None:
            su["target_highlight_count"] = req.target_highlight_count
        settings = settings.model_copy(update={"summary": settings.summary.model_copy(update=su)})

        segs = [Segment(**s) for s in json.loads(bil.read_text("utf-8"))]
        try:
            res = asyncio.run(summarize(segs, settings, program_name=program_name))
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=f"试运行失败：{e}") from e
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    d = res.model_dump()
    return {"model": settings.summary.model,
            "summary": d.get("summary", ""),
            "sections": [{"title_ja": s.get("title_ja", ""), "time_range": s.get("time_range", ""),
                          "content": s.get("content", ""), "music": s.get("music", [])}
                         for s in d.get("sections", [])],
            "key_topics": d.get("key_topics", [])}


class CorrectionReq(BaseModel):
    section: str                      # name_corrections | terminology
    wrong: str
    right: str


@router.post("/api/programs/{program_id}/correction")
def program_correction(program_id: str, body: CorrectionReq):
    """纠错回流：把「错→对」一键写进方案的 name_corrections / terminology。"""
    from fastapi import HTTPException

    from clip.program_profile import add_mapping
    try:
        return add_mapping(program_id, body.section, body.wrong, body.right)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/api/episode_summary")
def episode_summary(episode_dir: str):
    """把某期的 05_summary.json 渲染成可读文本，供 few-shot「范例总结」使用。"""
    p = Path(episode_dir) / "05_summary.json"
    if not p.exists():
        return {"error": "该期没有 05_summary.json"}
    try:
        d = json.loads(p.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": f"解析失败：{e}"}
    out = [f"【摘要】{d.get('summary', '')}", ""]
    for s in d.get("sections", []) or []:
        head = (s.get("title_ja") or "").strip()
        tr = (s.get("time_range") or "").strip()
        out.append(f"■ {head}{('  ['+tr+']') if tr else ''}")
        if s.get("content"):
            out.append(s["content"])
        if s.get("music"):
            out.append("🎵 " + " / ".join(s["music"]))
        out.append("")
    if d.get("key_topics"):
        out.append("【关键点】" + "；".join(d["key_topics"]))
    return {"text": "\n".join(out).strip()}


@router.get("/api/summary_schema")
def summary_schema():
    """产物结构静态预览用：Summary（05_summary.json）的字段骨架 + 说明 + 示例。

    与下游契约一致（schema 不随方案变化，只有内容/长度/侧重变）。
    """
    return {
        "title": "05_summary.json（结构化摘要）",
        "fields": [
            {"name": "summary", "type": "string", "note": "整场摘要，受方案 max_summary_chars 限制"},
            {"name": "sections[]", "type": "array", "note": "按时间顺序的分段复盘", "children": [
                {"name": "title_ja", "type": "string", "note": "环节/曲目日语原文标题"},
                {"name": "time_range", "type": "string", "note": "HH:MM:SS-HH:MM:SS（切片依据）"},
                {"name": "content", "type": "string", "note": "本段具体内容 80-180 字"},
                {"name": "music[]", "type": "array", "note": "本段日文原曲名（setlist）"},
                {"name": "member_reactions[]", "type": "array", "note": "成员/本人发言"},
                {"name": "listener_mail_from / listener_mail", "type": "string", "note": "スパチャ/评论署名与翻译"},
                {"name": "notes[]", "type": "array", "note": "梗/术语/告知/译注"},
            ]},
            {"name": "key_topics[]", "type": "array", "note": "3-6 个关键点，每条一句"},
            {"name": "highlights[]", "type": "array", "note": "受方案 target_highlight_count 控制（直播多为 0）"},
        ],
    }


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
        # 转写/摘要是否真的产出（plan.json 里 ingest.summary_ok）。失败不再伪装成「完成」。
        ingest = _plan_ingest(run_dir)
        if ingest.get("summary_ok") is False:
            _jobs[job_id].update(status="error", stage="转写/摘要失败：本场无总结、未入库",
                                 run_dir=str(run_dir), error=ingest.get("error", "未产出 05_summary.json"))
        else:
            _jobs[job_id].update(status="done", stage="完成", run_dir=str(run_dir))
    except Exception as e:  # noqa: BLE001
        _jobs[job_id].update(status="error", stage="失败", error=str(e))


def _plan_ingest(run_dir) -> dict:
    """读 run_dir/plan.json 的 ingest 段，判断本场是否真的产出了总结。"""
    try:
        plan = json.loads((Path(run_dir) / "plan.json").read_text("utf-8"))
        return plan.get("ingest") or {}
    except Exception:  # noqa: BLE001
        return {}


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
    # 只回轻量字段（不含庞大的 cues 数组）；kind/range 供前端识别可重新审核的切片预览任务。
    keep = ("id", "kind", "status", "stage", "error", "url", "range", "ts", "size_mb")
    out = [{k: j.get(k) for k in keep} for j in _jobs.values()]
    return {"jobs": sorted(out, key=lambda j: j.get("ts", 0), reverse=True)}


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


def _clips_root() -> Path:
    return clip_config.abspath("./data/clips")


@router.get("/api/slice/recoverable")
def slice_recoverable():
    """磁盘上已切好但内存任务已丢失（刷新/重启）的预览：可重新打开审核卡。

    一个可恢复预览＝某 clips 子目录同时有 cues.json 和切片视频 clip_00.mp4。
    """
    root = _clips_root()
    out = []
    if root.exists():
        for d in sorted(root.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0,
                        reverse=True):
            cues_f, cut_f = d / "cues.json", d / "clip_00.mp4"
            if not (d.is_dir() and cues_f.exists() and cut_f.exists()):
                continue
            try:
                n = len(json.loads(cues_f.read_text("utf-8")))
            except Exception:  # noqa: BLE001
                continue
            m = re.search(r"_(\d+)-(\d+)s_([0-9a-f]+)$", d.name)
            out.append({"dir": str(d), "name": d.name[:60],
                        "range": f"{m.group(1)}-{m.group(2)}s" if m else "",
                        "n_cues": n, "mtime": int(cut_f.stat().st_mtime),
                        "assembled": (d / "clip_00_final.mp4").exists()})
    return {"items": out[:30]}


class RecoverReq(BaseModel):
    dir: str
    program: str | None = None


@router.post("/api/slice/recover")
def slice_recover(req: RecoverReq):
    """从磁盘目录重建内存预览任务，返回 job_id，前端据此打开审核卡。"""
    d = Path(req.dir).resolve()
    root = _clips_root().resolve()
    if root not in d.parents or not d.is_dir():
        return {"error": "目录非法"}
    cues_f, cut_f = d / "cues.json", d / "clip_00.mp4"
    if not (cues_f.exists() and cut_f.exists()):
        return {"error": "该目录缺少 cues.json 或切片视频"}
    try:
        cues = json.loads(cues_f.read_text("utf-8"))
    except Exception as e:  # noqa: BLE001
        return {"error": f"cues.json 解析失败：{e}"}
    accent = [255, 255, 255]
    if req.program:
        try:
            from clip.program_profile import load_profile
            accent = list(load_profile(req.program).accent_rgb((255, 255, 255)))
        except Exception:  # noqa: BLE001
            pass
    m = re.search(r"_(\d+)-(\d+)s_([0-9a-f]+)$", d.name)
    job_id = m.group(3) if m else uuid.uuid4().hex[:8]
    _jobs[job_id] = {"id": job_id, "kind": "preview", "status": "done",
                     "stage": "待审核（已从磁盘恢复）", "ts": time.time(),
                     "range": f"{m.group(1)}-{m.group(2)}s" if m else "",
                     "cut_path": str(cut_f), "out_dir": str(d), "accent": accent,
                     "cues": cues, "title_recommendation": None}
    return {"job_id": job_id}


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
