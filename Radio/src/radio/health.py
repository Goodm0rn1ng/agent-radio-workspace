"""Radiko 录制前健康检查。

策略：录制开始前 N 分钟（默认 5）发一次 HTTP 探测：
1. radiko.jp 总站可达
2. 目标 station_id 的当前节目 API 能正常返回 XML（说明 station 在播）
3. 当前在播节目 ft 跟我们预期的开播时间一致（time-shift 跑过去节目不检查这点）

失败行为：
- 发 Telegram 告警（含 station / 失败原因）
- 决定权交给 caller：可以选择"继续尝试录制"（万一只是 API 抖动）
  或"放弃录制"

实现做成纯函数 + Telegram 通知 helper，不依赖调度器（main_radiko 启动时
sync 跑一次 health check 后再开始录制即可）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from xml.etree import ElementTree as ET

import httpx
from loguru import logger

# Radiko 节目表 API（今日节目，含 ft/to/title 列表）
_RADIKO_TODAY_URL = "https://radiko.jp/v3/program/station/date/{date}/{station_id}.xml"
# 日本时区（节目表的 ft/to 都是 JST）
_JST = timezone(timedelta(hours=9))


@dataclass(frozen=True)
class HealthCheckResult:
    ok: bool
    station_id: str
    current_program_title: str = ""
    current_ft: str = ""
    current_to: str = ""
    detail: str = ""


async def probe_radiko_station(
    station_id: str,
    *,
    timeout_s: float = 10.0,
) -> HealthCheckResult:
    """HTTP 探测 station 当前节目状态。

    流程：
    1. GET 今日节目表 XML（包含全天 prog 列表）
    2. 找出当前 JST 时刻落在 [ft, to) 区间的 prog
    3. 返回节目标题 + 时段
    """
    now_jst = datetime.now(_JST)
    date_str = now_jst.strftime("%Y%m%d")
    # Radiko 节目表"今日"覆盖到次日凌晨 5 点；凌晨 5 点前要查昨天
    if now_jst.hour < 5:
        date_str = (now_jst - timedelta(days=1)).strftime("%Y%m%d")

    url = _RADIKO_TODAY_URL.format(date=date_str, station_id=station_id)
    logger.info(f"health check → GET {url}")
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=timeout_s) as cli:
            r = await cli.get(url)
            r.raise_for_status()
    except httpx.HTTPStatusError as e:
        return HealthCheckResult(
            ok=False,
            station_id=station_id,
            detail=f"HTTP {e.response.status_code}（电台 ID 可能拼错）",
        )
    except (httpx.RequestError, httpx.TimeoutException) as e:
        return HealthCheckResult(
            ok=False, station_id=station_id, detail=f"网络异常：{e!r}"
        )

    body = r.text or ""
    if not body.strip().startswith("<?xml"):
        return HealthCheckResult(
            ok=False,
            station_id=station_id,
            detail=f"响应非 XML：{body[:120]}",
        )

    # 解析当前正在播的 prog
    try:
        root = ET.fromstring(body)
        progs = root.findall(".//prog")
        if not progs:
            return HealthCheckResult(
                ok=False,
                station_id=station_id,
                detail="节目表为空（电台可能停播）",
            )
        now_tag = now_jst.strftime("%Y%m%d%H%M%S")
        for prog in progs:
            ft = prog.get("ft", "")
            to = prog.get("to", "")
            if ft and to and ft <= now_tag < to:
                title_el = prog.find("title")
                title = (title_el.text or "").strip() if title_el is not None else ""
                return HealthCheckResult(
                    ok=True,
                    station_id=station_id,
                    current_program_title=title,
                    current_ft=ft,
                    current_to=to,
                )
        # 没匹配上：当前是 station 节目间隙（停播窗口）
        return HealthCheckResult(
            ok=False,
            station_id=station_id,
            detail=f"当前 JST {now_tag} 不在任何节目时段内",
        )
    except ET.ParseError as e:
        return HealthCheckResult(
            ok=False, station_id=station_id, detail=f"XML 解析失败：{e!r}"
        )


async def health_check_before_record(
    station_id: str,
    *,
    pre_record_minutes: int = 5,
    do_wait: bool = False,
) -> HealthCheckResult:
    """录制前健康检查入口。

    - do_wait=False：立即 probe 一次（适合脚本即时启动场景）
    - do_wait=True：sleep pre_record_minutes 后再 probe（适合调度器场景，
      但当前 v1 没有调度器，等 M3 用上）

    返回检查结果，调用方决定后续走向。
    """
    if do_wait and pre_record_minutes > 0:
        logger.info(
            f"录制前 {pre_record_minutes} 分钟健康检查窗口：先等待…"
        )
        await asyncio.sleep(pre_record_minutes * 60)
    return await probe_radiko_station(station_id)


async def notify_health_failure(
    settings,
    result: HealthCheckResult,
    *,
    program_name: str = "",
) -> None:
    """健康检查失败时发 Telegram 告警。"""
    # 延迟导入，避免循环依赖
    from telegram import Bot
    from telegram.constants import ParseMode

    def _escape(text: str) -> str:
        chars = r"_*[]()~`>#+-=|{}.!"
        return "".join(("\\" + ch if ch in chars else ch) for ch in (text or ""))

    text = (
        f"⚠️ *Radiko 录制前健康检查失败*\n"
        f"节目：{_escape(program_name) or '(unknown)'}\n"
        f"电台：`{_escape(result.station_id)}`\n"
        f"原因：{_escape(result.detail)}\n"
        f"\n确认 Radiko 站点是否正常、station\\_id 是否拼对。"
    )
    bot = Bot(token=settings.secrets.telegram_bot_token.get_secret_value())
    try:
        await bot.send_message(
            chat_id=settings.secrets.telegram_chat_id,
            text=text,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.info("已发送 health 失败告警到 Telegram")
    except Exception as e:
        logger.error(f"health 告警 Telegram 发送失败：{e!r}")
