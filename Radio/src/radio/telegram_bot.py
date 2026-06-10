"""Telegram bot polling handlers for HITL approvals."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from radio.approval import ApprovalStore, default_approval_store_path
from radio.config import Settings
from radio.telegram_sender import _md_escape
from radio.xhs import (
    XiaohongshuStore,
    default_xhs_covers_dir,
    default_xhs_store_path,
    push_to_xiaohongshu,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup


def build_bot_application(settings: Settings) -> Application:
    """Build a python-telegram-bot Application with approval callbacks."""
    app = (
        Application.builder()
        .token(settings.secrets.telegram_bot_token.get_secret_value())
        .build()
    )
    app.bot_data["settings"] = settings
    app.add_handler(CallbackQueryHandler(_handle_segment_callback, pattern=r"^seg:"))
    app.add_handler(CallbackQueryHandler(_handle_xhs_callback, pattern=r"^xhs:"))
    app.add_handler(MessageHandler(filters.PHOTO, _handle_xhs_photo))
    app.add_handler(CommandHandler("status", _handle_status))
    app.add_handler(CommandHandler("pending", _handle_pending))
    # 可选：clip 切片回调（点击 Telegram 菜单按钮即切片）。clip 与本包同装在工作区
    # venv（Agent/pyproject.toml）；缺失时静默跳过，不影响录制 bot。
    try:
        from clip.telegram_clip import register_clip_handlers
        register_clip_handlers(app)
    except Exception as _e:  # noqa: BLE001
        logger.info(f"clip 切片回调未注册（可忽略）：{_e}")
    return app


async def _handle_segment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed_chat(settings, query.message.chat_id if query.message else None):
        await query.answer("未授权", show_alert=True)
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("审批请求格式不正确", show_alert=True)
        await query.edit_message_text("审批请求格式不正确。")
        return

    _, action, segment_id = parts
    store = ApprovalStore(default_approval_store_path(settings.runtime.logs_dir))
    try:
        if action == "approve":
            record, added, skipped = store.approve(
                segment_id,
                settings.summary.segments_library_path,
            )
            result = "已入库" if added else "已确认（库中已有，未重复写入）"
            if skipped and not added:
                result = "已确认（重复项，未重复写入）"
        elif action == "skip":
            record = store.skip(segment_id)
            result = "已跳过"
        else:
            await query.answer("未知审批动作", show_alert=True)
            await query.edit_message_text("未知审批动作。")
            return
    except KeyError:
        await query.answer("记录不存在或已清理", show_alert=True)
        await query.edit_message_text("这条待审批记录不存在，可能已被清理。")
        return

    await query.answer(result, show_alert=False)
    text = "\n".join(
        [
            f"*{_md_escape(result)}*",
            f"节目：{_md_escape(record.program_series)}",
            f"日期：{_md_escape(record.air_date)}",
            f"JP：*{_md_escape(record.title_ja)}*",
        ]
    )
    try:
        await query.edit_message_text(text=text, parse_mode=ParseMode.MARKDOWN_V2)
    except TelegramError as exc:
        logger.warning(f"Telegram 审批反馈编辑失败，改发新消息：{exc!r}")
        if query.message:
            await query.message.reply_text(
                "\n".join(
                    [
                        result,
                        f"节目：{record.program_series}",
                        f"日期：{record.air_date}",
                        f"JP：{record.title_ja}",
                    ]
                )
            )


def _xhs_save_keyboard(record_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("📕 保存到小红书", callback_data=f"xhs:save:{record_id}")]]
    )


def _xhs_cancel_keyboard(record_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ 取消上传", callback_data=f"xhs:cancel:{record_id}")]]
    )


async def _handle_xhs_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    query = update.callback_query
    if not query:
        return
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed_chat(settings, query.message.chat_id if query.message else None):
        await query.answer("未授权", show_alert=True)
        return

    parts = (query.data or "").split(":")
    if len(parts) != 3:
        await query.answer("未知操作", show_alert=True)
        return
    _, action, record_id = parts

    store = XiaohongshuStore(default_xhs_store_path(settings.runtime.logs_dir))

    if action == "save":
        try:
            record = store.transition_to_awaiting(record_id)
        except KeyError:
            await query.answer("记录已不存在", show_alert=True)
            return
        await query.answer("请上传封面图片")
        try:
            await query.edit_message_reply_markup(
                reply_markup=_xhs_cancel_keyboard(record_id)
            )
        except TelegramError as exc:
            logger.warning(f"Telegram 切换为取消按钮失败：{exc!r}")
        if query.message:
            await query.message.reply_text(
                f"📷 请直接发送一张图片作为「{record.title}」的小红书封面。"
                f"\n（同一时间仅追踪一条待上传记录；点其它推文的「📕 保存到小红书」会接管。）"
            )
        return

    if action == "cancel":
        try:
            store.revert_to_pending(record_id)
        except KeyError:
            await query.answer("记录已不存在", show_alert=True)
            return
        await query.answer("已取消")
        try:
            await query.edit_message_reply_markup(
                reply_markup=_xhs_save_keyboard(record_id)
            )
        except TelegramError as exc:
            logger.warning(f"Telegram 切换回保存按钮失败：{exc!r}")
        return

    await query.answer("未知操作", show_alert=True)


async def _handle_xhs_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    """User uploaded a photo — if some XHS record is awaiting, use it as cover and publish."""
    if not update.message or not update.message.photo:
        return
    settings: Settings = context.application.bot_data["settings"]
    if not _is_allowed_chat(settings, update.message.chat_id):
        return

    store = XiaohongshuStore(default_xhs_store_path(settings.runtime.logs_dir))
    record = store.find_awaiting()
    if record is None:
        return  # No pending XHS request — ignore the photo silently.

    covers_dir = default_xhs_covers_dir(settings.runtime.logs_dir)
    covers_dir.mkdir(parents=True, exist_ok=True)
    target = covers_dir / f"{record.id}.jpg"

    photo = update.message.photo[-1]  # largest available size
    try:
        tg_file = await context.bot.get_file(photo.file_id)
        await tg_file.download_to_drive(custom_path=str(target))
    except TelegramError as exc:
        logger.warning(f"下载 Telegram 封面图失败：{exc!r}")
        await update.message.reply_text(f"❌ 封面图下载失败：{exc}")
        return

    record = store.set_image(record.id, target)
    await update.message.reply_text(
        f"📷 已收到封面，正在发布到小红书：{record.title}"
    )

    ok, tail = await push_to_xiaohongshu(settings, record)
    if ok:
        store.mark_sent(record.id)
        await _xhs_update_original_markup(context, record, reply_markup=None)
        await update.message.reply_text(
            f"📕 已保存到小红书私密笔记：{record.title}"
        )
    else:
        store.mark_failed(record.id, tail)
        await _xhs_update_original_markup(
            context, record, reply_markup=_xhs_save_keyboard(record.id)
        )
        await update.message.reply_text(
            f"❌ 小红书发布失败：{tail[-300:] or '未知错误'}\n"
            f"可在原推文消息上再点「📕 保存到小红书」重试。"
        )


async def _xhs_update_original_markup(
    context: ContextTypes.DEFAULT_TYPE,
    record,
    *,
    reply_markup: InlineKeyboardMarkup | None,
) -> None:
    """Edit the original header message's keyboard. Best-effort."""
    if not record.chat_id or not record.message_id:
        return
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=record.chat_id,
            message_id=record.message_id,
            reply_markup=reply_markup,
        )
    except TelegramError as exc:
        logger.warning(f"回写原消息按钮失败：{exc!r}")


async def _handle_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not update.message or not _is_allowed_chat(settings, update.message.chat_id):
        return

    metrics = _latest_metric(settings.runtime.logs_dir / "metrics.jsonl")
    pending_count = len(
        ApprovalStore(default_approval_store_path(settings.runtime.logs_dir)).list_pending(100)
    )
    if not metrics:
        text = f"暂无运行记录。\n待审批环节：{pending_count}"
    else:
        status = "成功" if metrics.get("success") else "失败"
        errors = metrics.get("errors") or []
        error_text = f"\n最近错误：{errors[-1]}" if errors else ""
        text = (
            f"最近运行：{status}\n"
            f"节目：{metrics.get('program_name', '?')}\n"
            f"日期：{metrics.get('air_date', '?')}\n"
            f"耗时：{metrics.get('duration_s', 0):.1f}s\n"
            f"sections：{metrics.get('sections_count', 0)}\n"
            f"library 命中：{metrics.get('library_hits', 0)}\n"
            f"待审批环节：{pending_count}"
            f"{error_text}"
        )
    await update.message.reply_text(text)


async def _handle_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.application.bot_data["settings"]
    if not update.message or not _is_allowed_chat(settings, update.message.chat_id):
        return

    pending = ApprovalStore(default_approval_store_path(settings.runtime.logs_dir)).list_pending(10)
    if not pending:
        await update.message.reply_text("当前没有待审批环节。")
        return

    lines = ["待审批环节："]
    for item in pending:
        lines.append(f"- {item.air_date} {item.title_ja} ({item.id})")
    await update.message.reply_text("\n".join(lines))


def _latest_metric(path: Path) -> dict | None:
    if not path.exists():
        return None
    latest = None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                latest = json.loads(line)
            except json.JSONDecodeError:
                continue
    return latest


def _is_allowed_chat(settings: Settings, chat_id: int | str | None) -> bool:
    if chat_id is None:
        return False
    return str(chat_id) == str(settings.secrets.telegram_chat_id)


def run_bot(settings: Settings) -> None:
    """Run polling until interrupted."""
    logger.info("启动 Telegram HITL bot polling")
    build_bot_application(settings).run_polling()
