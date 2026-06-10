"""Telegram Bot 推送：分块摘要消息（头部 / 分段复盘 / 高光时刻）+ 双语 transcript 附件。"""

from __future__ import annotations

from pathlib import Path

from loguru import logger
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from radio.approval import PendingSegment
from radio.config import Settings
from radio.models import ProgramSection, Summary
from radio.utils.retry import async_retry
from radio.xhs import (
    XiaohongshuStore,
    build_xhs_payload,
    default_xhs_store_path,
)

TELEGRAM_MAX = 4096
SAFE_LIMIT = 3900  # 给 MarkdownV2 转义留余量


def _md_escape(text: str) -> str:
    """Telegram MarkdownV2 必须转义的字符。"""
    chars = r"_*[]()~`>#+-=|{}.!"
    out = []
    for ch in text:
        if ch in chars:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _build_header_message(
    program_name: str,
    summary: Summary,
    air_date: str,
) -> str:
    """节目标题 + 总体摘要 + 关键话题。"""
    lines = [
        f"📻 *{_md_escape(program_name)}* — {_md_escape(air_date)}",
        "",
        _md_escape(summary.summary),
    ]
    if summary.key_topics:
        lines.append("")
        lines.append("*🏷️ 关键话题*")
        for topic in summary.key_topics:
            lines.append(f"• {_md_escape(topic)}")
    return "\n".join(lines)


def _render_section(section: ProgramSection, index: int) -> str:
    """渲染单个 section（MarkdownV2）。

    v0.2.1 起渲染规则：
    - 只保留环节日语原标题 (title_ja)，不渲染中文标题
    - 只保留来信署名 (listener_mail_from)，不渲染日语/中文全文
    - 保留：intro / content / member_reactions / music / notes
    """
    tag = "⭐常驻" if section.is_recurring else "🆕新环节"
    title_ja = (section.title_ja or "").strip()

    lines = [
        f"*{index}\\.* `{_md_escape(section.time_range)}` _{_md_escape(tag)}_",
    ]
    if title_ja:
        lines.append(f"  JP: *{_md_escape(title_ja)}*")
    if section.intro:
        for ln in section.intro.splitlines():
            ln = ln.strip()
            if ln:
                lines.append(f"  介绍：{_md_escape(ln)}")
    if section.content:
        lines.append(f"  内容：{_md_escape(section.content)}")
    if section.listener_mail_from:
        lines.append(f"  来信：{_md_escape(section.listener_mail_from)}")
    if section.listener_mail:
        lines.append(f"  来信内容：{_md_escape(section.listener_mail)}")
    if section.member_reactions:
        for reaction in section.member_reactions[:4]:
            lines.append(f"  · {_md_escape(reaction)}")
    if section.music:
        lines.append(f"  选曲：{_md_escape('、'.join(section.music))}")
    if section.notes:
        for note in section.notes[:3]:
            lines.append(f"  📝 {_md_escape(note)}")
    return "\n".join(lines)


def _build_section_messages(sections: list[ProgramSection]) -> list[str]:
    """把分段复盘拆成若干条不超过 4096 字符的消息。"""
    if not sections:
        return []

    chunks: list[str] = []
    current: list[str] = ["*🧭 分段复盘*"]
    current_len = len(current[0])

    for i, section in enumerate(sections, start=1):
        rendered = _render_section(section, i)
        # +2 是 "\n\n" 分隔符
        if current_len + len(rendered) + 2 > SAFE_LIMIT and len(current) > 1:
            chunks.append("\n\n".join(current))
            current = [f"*🧭 分段复盘（续 {len(chunks) + 1}）*", rendered]
            current_len = len(current[0]) + len(rendered) + 2
        else:
            current.append(rendered)
            current_len += len(rendered) + 2

    if len(current) > 1:
        chunks.append("\n\n".join(current))
    return chunks


def build_summary_messages(
    program_name: str,
    summary: Summary,
    air_date: str,
) -> list[str]:
    """返回要发送的所有 Telegram 消息（按发送顺序）。

    v0.2.1 起不再追加「高光时刻」消息——highlights 仍在 Summary 落盘 JSON 中
    供事后使用，但不推送 Telegram。
    """
    messages = [_build_header_message(program_name, summary, air_date)]
    messages.extend(_build_section_messages(summary.sections))
    return [m if len(m) <= TELEGRAM_MAX else m[: TELEGRAM_MAX - 20] + "\n…（已截断）" for m in messages]


async def notify_pipeline_failure(
    settings: Settings,
    *,
    program_name: str,
    air_date: str,
    error_message: str,
) -> None:
    """Pipeline 抛错时发一条简短 Telegram 通知。失败本身要不影响主流程。"""
    bot = Bot(token=settings.secrets.telegram_bot_token.get_secret_value())
    chat_id = settings.secrets.telegram_chat_id

    # 截断过长错误消息，并转义 MarkdownV2
    err_short = error_message[:500]
    text = (
        f"❌ *Pipeline 失败*\n"
        f"节目：{_md_escape(program_name)}\n"
        f"日期：{_md_escape(air_date)}\n"
        f"错误：`{_md_escape(err_short)}`\n"
        f"\n详细日志见 `data/logs/radio_*.log` 与 `data/logs/metrics.jsonl`。"
    )
    if len(text) > TELEGRAM_MAX:
        text = text[: TELEGRAM_MAX - 20] + "\n…（已截断）"

    await bot.send_message(
        chat_id=chat_id, text=text, parse_mode=ParseMode.MARKDOWN_V2
    )
    logger.info(f"已发送 Telegram 失败通知到 chat_id={chat_id}")


@async_retry(attempts=3, base_delay=2.0)
async def send_to_telegram(
    settings: Settings,
    summary: Summary,
    transcript_path: Path,
    program_name: str,
    air_date: str,
    pending_segments: list[PendingSegment] | None = None,
) -> None:
    """分块发送摘要 + transcript 附件。"""
    bot = Bot(token=settings.secrets.telegram_bot_token.get_secret_value())
    chat_id = settings.secrets.telegram_chat_id

    messages = build_summary_messages(program_name, summary, air_date)
    logger.info(f"发送 Telegram 摘要：{len(messages)} 条消息 → chat_id={chat_id}")

    xhs = _maybe_enqueue_xhs(settings, summary, program_name)
    xhs_keyboard = xhs[0] if xhs else None
    xhs_record_id = xhs[1] if xhs else None
    for i, message in enumerate(messages, start=1):
        logger.info(f"  [{i}/{len(messages)}] {len(message)} 字符")
        reply_markup = xhs_keyboard if i == 1 else None
        sent = await bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=ParseMode.MARKDOWN_V2,
            reply_markup=reply_markup,
        )
        if i == 1 and xhs_record_id and sent is not None:
            XiaohongshuStore(
                default_xhs_store_path(settings.runtime.logs_dir)
            ).set_message(xhs_record_id, sent.chat_id, sent.message_id)

    for pending in pending_segments or []:
        await _send_pending_segment_approval(bot, chat_id, pending)

    logger.info(f"发送 transcript 附件：{transcript_path.name}")
    with transcript_path.open("rb") as f:
        await bot.send_document(
            chat_id=chat_id,
            document=f,
            filename=transcript_path.name,
            caption=f"📄 {program_name} 双语逐字稿",
        )


def _maybe_enqueue_xhs(
    settings: Settings,
    summary: Summary,
    program_name: str,
) -> tuple[InlineKeyboardMarkup, str] | None:
    """If xiaohongshu is enabled and topics are configured, enqueue a record and
    return (keyboard, record_id). Otherwise None."""
    cfg = settings.xiaohongshu
    if not cfg.enabled:
        return None
    payload = build_xhs_payload(settings, summary, program_name)
    if payload is None:
        return None
    title, body, topic = payload
    store = XiaohongshuStore(default_xhs_store_path(settings.runtime.logs_dir))
    record = store.enqueue(
        title=title,
        body=body,
        private=cfg.private,
        topic=topic,
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📕 保存到小红书",
                    callback_data=f"xhs:save:{record.id}",
                )
            ]
        ]
    )
    return keyboard, record.id


async def _send_pending_segment_approval(
    bot: Bot,
    chat_id: str,
    pending: PendingSegment,
) -> None:
    """Send one approval card for a newly discovered segment."""
    text = "\n".join(
        [
            "🧩 *新环节待审批*",
            f"节目：{_md_escape(pending.program_series)}",
            f"日期：{_md_escape(pending.air_date)}",
            f"JP：*{_md_escape(pending.title_ja)}*",
            "",
            _md_escape(pending.intro[:900]),
        ]
    )
    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "👍 入库",
                    callback_data=f"seg:approve:{pending.id}",
                ),
                InlineKeyboardButton(
                    "❌ 跳过",
                    callback_data=f"seg:skip:{pending.id}",
                ),
            ]
        ]
    )
    await bot.send_message(
        chat_id=chat_id,
        text=text if len(text) <= TELEGRAM_MAX else text[: TELEGRAM_MAX - 20] + "\n…（已截断）",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=keyboard,
    )
