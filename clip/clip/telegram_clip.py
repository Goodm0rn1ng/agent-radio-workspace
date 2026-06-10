"""Telegram 联动：新直播处理完后，把「可能爆火片段 + 本场歌枠区块」推送到 Telegram，
每项带一个按钮；用户点击 → 自动切该段并把成片发回。

- 复用 Radio 的 bot token / chat（同一个 token 只能有一个轮询消费者，故回调处理器
  既可注册进 Radio 既有 bot（register_clip_handlers），也可独立运行 run_clipper_bot）。
- 任务映射落盘 data/clip_jobs.json，使推送进程与 bot 进程跨进程共享。
- 切片复用 render.render_segment（谈话中日字幕；歌唱段按 clipper 歌词三档策略处理）。
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from clip.config import clip_config

_JOBS_PATH = clip_config.abspath("./data/clip_jobs.json")          # Agent/clip/data
_RADIO_CONFIG = Path(__file__).resolve().parents[2] / "Radio" / "config" / "config.yaml"


def _radio_secrets() -> tuple[str, str]:
    from radio.config import load_settings
    s = load_settings(_RADIO_CONFIG)
    return (s.secrets.telegram_bot_token.get_secret_value(), str(s.secrets.telegram_chat_id))


# ---------------- 任务映射存储 ----------------
def _load_jobs() -> dict:
    if _JOBS_PATH.exists():
        return json.loads(_JOBS_PATH.read_text(encoding="utf-8"))
    return {}


def _save_jobs(d: dict) -> None:
    _JOBS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _JOBS_PATH.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")


def save_job(items: list[dict], program_id: str, episode_dir: str, accent_hex: str) -> str:
    run_id = uuid.uuid4().hex[:8]
    jobs = _load_jobs()
    jobs[run_id] = {"program_id": program_id, "episode_dir": episode_dir,
                    "accent": accent_hex, "ts": time.time(), "items": items}
    _save_jobs(jobs)
    return run_id


def get_job_item(run_id: str, idx: int) -> tuple[dict, dict] | None:
    job = _load_jobs().get(run_id)
    if not job or not (0 <= idx < len(job["items"])):
        return None
    return job, job["items"][idx]


# ---------------- 文案 + 键盘 ----------------
def _fmt_ts(sec: float) -> str:
    sec = int(sec)
    return f"{sec//60:02d}:{sec%60:02d}"


def _get_field(obj, name: str, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def build_items(viral_clips: list, songs: list) -> list[dict]:
    """统一成可切片条目：爆火片段 + 歌枠区块。"""
    items: list[dict] = []
    for c in viral_clips:
        items.append({"kind": "viral", "title": c.title or "片段",
                      "start": float(c.start), "end": float(c.end),
                      "reason": getattr(c, "reason", ""), "score": getattr(c, "score", 0)})
    for s in songs:
        start = _as_float(_get_field(s, "start"))
        end = _as_float(_get_field(s, "end"), start)
        clip_start = _as_float(_get_field(s, "clip_start"), start)
        clip_end = _as_float(_get_field(s, "clip_end"), end)
        song_start = _as_float(_get_field(s, "song_start"), start)
        song_end = _as_float(_get_field(s, "song_end"), end)
        if clip_end <= clip_start:
            clip_start, clip_end = start, end
        if song_end <= song_start:
            song_start, song_end = start, end
        items.append({
            "kind": "song",
            "title": _get_field(s, "title", "歌曲"),
            "origin": _get_field(s, "origin", ""),
            "start": clip_start,
            "end": clip_end,
            "clip_start": clip_start,
            "clip_end": clip_end,
            "song_start": song_start,
            "song_end": song_end,
            "lyrics_file": _get_field(s, "lyrics_file", None),
            "artist": _get_field(s, "artist", None),
            "confidence": _get_field(s, "confidence", 0.0),
        })
    return items


def build_message(display_name: str, items: list[dict]) -> str:
    lines = [f"🎬 *{display_name}* 新直播已处理入库。可切片项："]
    virals = [it for it in items if it["kind"] == "viral"]
    songs = [it for it in items if it["kind"] == "song"]
    if virals:
        lines.append("\n🔥 可能爆火片段：")
        for it in virals:
            lines.append(f"  • [{_fmt_ts(it['start'])}-{_fmt_ts(it['end'])}] {it['title']}"
                         f"（{it.get('score',0):.2f}）")
    if songs:
        lines.append("\n🎵 本场歌枠区块：")
        for it in songs:
            song_range = ""
            if ("song_start" in it and "song_end" in it
                    and (abs(it["song_start"] - it["start"]) > 0.5
                         or abs(it["song_end"] - it["end"]) > 0.5)):
                song_range = f"（演唱 {_fmt_ts(it['song_start'])}-{_fmt_ts(it['song_end'])}）"
            origin = f"｜{it.get('origin')}" if it.get("origin") else ""
            lines.append(f"  • [{_fmt_ts(it['start'])}-{_fmt_ts(it['end'])}] {it['title']}{origin}{song_range}")
    lines.append("\n👇 点下方按钮自动切片（歌唱段按歌词策略嵌入字幕，谈话段中日字幕）")
    return "\n".join(lines)


def build_keyboard(run_id: str, items: list[dict]):
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    rows = []
    for i, it in enumerate(items):
        tag = "🔥" if it["kind"] == "viral" else "🎵"
        label = f"{tag} 切 [{_fmt_ts(it['start'])}] {it['title'][:14]}"
        rows.append([InlineKeyboardButton(label, callback_data=f"clip:{run_id}:{i}")])
    return InlineKeyboardMarkup(rows)


# ---------------- 推送 ----------------
async def push_clip_menu(display_name: str, items: list[dict], run_id: str) -> None:
    token, chat_id = _radio_secrets()
    from telegram import Bot
    bot = Bot(token=token)
    await bot.send_message(chat_id=chat_id, text=build_message(display_name, items),
                           parse_mode="Markdown", reply_markup=build_keyboard(run_id, items))


def push_clip_menu_sync(profile, viral_clips: list, songs: list, episode_dir: str) -> str:
    items = build_items(viral_clips, songs)
    run_id = save_job(items, profile.program_id if profile else "",
                      episode_dir, getattr(profile, "accent_color", "") or "")
    asyncio.run(push_clip_menu(getattr(profile, "display_name", "直播"), items, run_id))
    print(f"  已推送 Telegram 切片菜单（run_id={run_id}，{len(items)} 项）")
    return run_id


# ---------------- 回调：点击即切片 ----------------
_TG_MAX_BYTES = 50 * 1024 * 1024       # Telegram bot 发送上限 50MB


def _render_job_item(job: dict, item: dict, idx: int) -> Path:
    """完整性优先：按识别出的真实区间切片，**不限长、不压缩**。"""
    from clip.lyrics import SongSpan
    from clip.program_profile import load_profile
    from clip.render import render_segment
    from clip.youtube_source import safe_dirname

    profile = None
    if job.get("program_id"):
        try:
            profile = load_profile(job["program_id"])
        except Exception:  # noqa: BLE001
            profile = None
    episode_dir = job["episode_dir"]
    video = Path(episode_dir) / "source.mp4"
    start, end = float(item["start"]), float(item["end"])
    spans = []
    if item["kind"] == "song":
        song_start = _as_float(item.get("song_start"), start)
        song_end = _as_float(item.get("song_end"), end)
        span_start = max(start, song_start)
        span_end = min(end, song_end)
        if span_end > span_start:
            spans = [SongSpan(
                span_start,
                span_end,
                item["title"],
                lyrics_file=item.get("lyrics_file") or None,
                artist=item.get("artist") or None,
            )]
    out_dir = clip_config.abspath("./data/clips") / f"{safe_dirname(item['title'], 60)}_{int(time.time())}"
    return render_segment(video, start, end, out_dir, 0,
                          episode_dir=episode_dir, profile=profile, song_spans=spans)


async def _handle_clip_callback(update, context) -> None:
    query = update.callback_query
    await query.answer("开始切片…")
    try:
        _, run_id, idx_s = query.data.split(":")
        idx = int(idx_s)
    except ValueError:
        return
    found = get_job_item(run_id, idx)
    if not found:
        await query.message.reply_text("⚠️ 该切片任务已过期或不存在。")
        return
    job, item = found
    await query.message.reply_text(
        f"✂️ 正在切片：{item['title']}（{_fmt_ts(item['start'])}-{_fmt_ts(item['end'])}），约需 1-3 分钟…")
    try:
        final = await asyncio.to_thread(_render_job_item, job, item, idx)
    except Exception as e:  # noqa: BLE001
        await query.message.reply_text(f"❌ 切片失败：{e}")
        return
    size_mb = final.stat().st_size / 1048576

    # 完整性优先：超 50MB 不截断/不压缩，直接说明原因 + 本地路径
    if final.stat().st_size > _TG_MAX_BYTES:
        await query.message.reply_text(
            f"📁 已切出完整片段：{item['title']}\n"
            f"⚠️ 成片 {size_mb:.0f}MB，超过 Telegram 50MB 发送上限，未发送。\n"
            f"已保存本地：{final}")
        return
    try:
        with open(final, "rb") as f:
            await context.bot.send_video(
                chat_id=query.message.chat_id, video=f, caption=f"✅ {item['title']}",
                read_timeout=300, write_timeout=300, connect_timeout=60, pool_timeout=60)
    except Exception as e:  # noqa: BLE001 — 网络等原因发送失败：说明原因 + 本地路径，不静默
        await query.message.reply_text(
            f"📁 已切出完整片段：{item['title']}（{size_mb:.0f}MB）\n"
            f"⚠️ 发送失败：{e}\n已保存本地：{final}")


def register_clip_handlers(app) -> None:
    """把切片回调注册进既有 bot Application（与 Radio 共用同一 token）。"""
    from telegram.ext import CallbackQueryHandler
    app.add_handler(CallbackQueryHandler(_handle_clip_callback, pattern=r"^clip:"))


def run_clipper_bot() -> None:
    """独立运行切片 bot（若未接入 Radio 既有 bot 时使用）。"""
    from telegram.ext import ApplicationBuilder
    token, _ = _radio_secrets()
    app = ApplicationBuilder().token(token).build()
    register_clip_handlers(app)
    print("clipper telegram bot polling…")
    app.run_polling()


if __name__ == "__main__":
    run_clipper_bot()
