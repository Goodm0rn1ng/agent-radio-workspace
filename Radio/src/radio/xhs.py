"""xiaohongshu-cli 集成：把 Telegram 头部消息保存为小红书私密笔记。

流程：
1. telegram_sender 在头部消息上挂「📕 保存到小红书」按钮，同时 enqueue 一条
   `XhsRecord`（status=pending）
2. 用户点按钮 → `transition_to_awaiting` 把该记录置为 awaiting_photo，并把同时
   只能有一条 awaiting 的不变量维持住（其它 awaiting 自动回退到 pending）
3. 用户向 bot 发送一张图片 → telegram_bot 下载后 `set_image` → `push_to_xiaohongshu`
4. CLI 成功 → mark_sent；失败 → mark_failed

设计上把固定占位图整个去掉，封面图来源完全来自 Telegram 上传。

首次需要：pipx install xiaohongshu-cli && xhs login --qrcode
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from loguru import logger
from pydantic import BaseModel

from radio.config import Settings
from radio.models import Summary
from radio.segments_library import extract_series_name


_DATE_SUFFIX_RE = re.compile(r"\s*\d{4}年\d{1,2}月\d{1,2}日(?:放送|配信)?\s*$")


def extract_xhs_title(program_name: str, max_chars: int) -> str:
    """从节目展示名抽取小红书标题：去掉末尾日期，按 max_chars 截断。

    例：
        "【アーカイブ】羊宮妃那のこもれびじかん #12 2025年6月22日放送"
            → "【アーカイブ】羊宮妃那のこもれびじかん #12"
    """
    title = _DATE_SUFFIX_RE.sub("", program_name).strip()
    if len(title) > max_chars:
        logger.warning(
            f"小红书标题超长（{len(title)} > {max_chars}），将截断："
            f"{title!r} → {title[:max_chars]!r}"
        )
        title = title[:max_chars]
    return title


def build_xhs_body(
    summary: Summary,
    program_name: str,
    max_chars: int,
) -> str:
    """渲染小红书正文：📻 节目名行 + 摘要 + 关键话题。

    话题标签（hashtag）不写进正文 —— 直接写 `#xxx` 在 XHS 不会变成可跳转话题；
    话题通过 CLI `--topic` 参数另行附加（见 push_to_xiaohongshu）。
    """
    header = f"📻 {program_name}\n\n"
    available = max_chars - len(header)

    topics_block = ""
    if summary.key_topics:
        topics_block = "\n\n🏷️ 关键话题\n" + "\n".join(
            f"• {t}" for t in summary.key_topics
        )

    body = (summary.summary or "").strip()
    if len(body) + len(topics_block) <= available:
        return header + body + topics_block

    if len(body) <= available:
        return header + body

    truncated = body[: max(0, available - 1)].rstrip() + "…"
    return header + truncated


class XhsRecord(BaseModel):
    id: str
    # pending → awaiting_photo → (sent | failed)；用户也可在 awaiting_photo 取消回 pending
    status: str = "pending"
    created_at: str
    decided_at: str = ""
    title: str
    body: str
    image_path: str = ""
    private: bool = True
    # 可跳转的小红书话题名（xhs CLI 只支持一个 --topic，故仅保留第一个）
    topic: str = ""
    error: str = ""
    # 原始头部消息的位置，用于回写按钮状态
    chat_id: int = 0
    message_id: int = 0


class XiaohongshuStore:
    """JSON-backed queue of pending Xiaohongshu posts (one per Telegram header)."""

    def __init__(self, path: Path):
        self.path = path

    def enqueue(
        self,
        *,
        title: str,
        body: str,
        private: bool,
        topic: str = "",
    ) -> XhsRecord:
        record = XhsRecord(
            id=secrets.token_hex(6),
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
            title=title,
            body=body,
            private=private,
            topic=topic,
        )
        records = self._load()
        records.append(record)
        self._save(records)
        logger.info(f"新增小红书待发笔记：{title!r} ({record.id})")
        return record

    def get(self, record_id: str) -> XhsRecord:
        for r in self._load():
            if r.id == record_id:
                return r
        raise KeyError(f"小红书待发笔记不存在：{record_id}")

    def transition_to_awaiting(self, record_id: str) -> XhsRecord:
        """Mark `record_id` as awaiting_photo. Any other awaiting record reverts to pending
        so there is always at most one awaiting upload at a time."""
        records = self._load()
        target: XhsRecord | None = None
        for r in records:
            if r.id == record_id:
                target = r
            elif r.status == "awaiting_photo":
                r.status = "pending"
                r.decided_at = ""
        if target is None:
            raise KeyError(f"小红书待发笔记不存在：{record_id}")
        target.status = "awaiting_photo"
        target.decided_at = datetime.now(UTC).isoformat(timespec="seconds")
        target.error = ""
        self._save(records)
        return target

    def revert_to_pending(self, record_id: str) -> XhsRecord:
        return self._update_status(record_id, status="pending", error="")

    def find_awaiting(self) -> XhsRecord | None:
        for r in self._load():
            if r.status == "awaiting_photo":
                return r
        return None

    def set_message(self, record_id: str, chat_id: int, message_id: int) -> XhsRecord:
        records = self._load()
        for r in records:
            if r.id == record_id:
                r.chat_id = chat_id
                r.message_id = message_id
                self._save(records)
                return r
        raise KeyError(f"小红书待发笔记不存在：{record_id}")

    def set_image(self, record_id: str, image_path: Path) -> XhsRecord:
        records = self._load()
        for r in records:
            if r.id == record_id:
                r.image_path = str(image_path)
                self._save(records)
                return r
        raise KeyError(f"小红书待发笔记不存在：{record_id}")

    def mark_sent(self, record_id: str) -> XhsRecord:
        return self._update_status(record_id, status="sent", error="")

    def mark_failed(self, record_id: str, error: str) -> XhsRecord:
        return self._update_status(record_id, status="failed", error=error[-500:])

    def _update_status(self, record_id: str, *, status: str, error: str) -> XhsRecord:
        records = self._load()
        for r in records:
            if r.id == record_id:
                r.status = status
                r.error = error
                r.decided_at = datetime.now(UTC).isoformat(timespec="seconds")
                self._save(records)
                return r
        raise KeyError(f"小红书待发笔记不存在：{record_id}")

    def _load(self) -> list[XhsRecord]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(f"读取小红书待发队列失败：{exc!r}")
            return []
        return [XhsRecord(**item) for item in (raw.get("records") or [])]

    def _save(self, records: list[XhsRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "records": [r.model_dump() for r in records]}
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def default_xhs_store_path(logs_dir: Path) -> Path:
    """Keep state next to other runtime data under ``data/``."""
    return logs_dir.parent / "pending_xhs.json"


def default_xhs_covers_dir(logs_dir: Path) -> Path:
    """Where Telegram-uploaded cover images are downloaded."""
    return logs_dir.parent / "xhs_covers"


def build_xhs_payload(
    settings: Settings,
    summary: Summary,
    program_name: str,
) -> tuple[str, str, str] | None:
    """构造（title, body, topic）。topic 为首选可跳转话题（xhs CLI 只支持一个）。

    若该 series 无配置 topics 则返回 None（不挂按钮）。
    """
    cfg = settings.xiaohongshu
    series = extract_series_name(program_name)
    topics = cfg.topics_by_series.get(series, [])
    if not topics:
        logger.debug(
            f"未为 series={series!r} 配置 xiaohongshu.topics_by_series，跳过小红书按钮"
        )
        return None
    title = extract_xhs_title(program_name, cfg.title_max_chars)
    body = build_xhs_body(summary, program_name, cfg.body_max_chars)
    topic = topics[0]
    if len(topics) > 1:
        logger.info(
            f"xiaohongshu-cli 仅支持单个 --topic，将使用 {topic!r}，"
            f"忽略其余 {len(topics) - 1} 个"
        )
    return title, body, topic


async def push_to_xiaohongshu(
    settings: Settings, record: XhsRecord
) -> tuple[bool, str]:
    """调 `xhs post ...`。返回 (ok, output_tail)。"""
    cfg = settings.xiaohongshu
    if not record.image_path:
        return False, "封面图未上传"
    image_path = Path(record.image_path)
    if not image_path.exists():
        return False, f"封面图不存在：{image_path}"

    cmd = [
        *cfg.cli_command,
        "post",
        "--title", record.title,
        "--body", record.body,
        "--images", str(image_path),
    ]
    if record.topic:
        cmd.extend(["--topic", record.topic])
    if record.private:
        cmd.append("--private")

    logger.info(f"调用 xiaohongshu-cli：{cmd[:2]} ... (--title {record.title!r})")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
    except FileNotFoundError:
        return False, f"未找到 CLI 可执行：{cfg.cli_command[0]}（pipx install xiaohongshu-cli？）"

    out = (stdout or b"").decode("utf-8", errors="replace")
    err = (stderr or b"").decode("utf-8", errors="replace")
    tail = (err or out).strip()
    tail = tail[-400:] if tail else ""

    if proc.returncode == 0:
        logger.info(f"小红书发布成功：{record.title!r}")
        return True, tail
    logger.warning(f"小红书发布失败（rc={proc.returncode}）：{tail!r}")
    return False, tail or f"xhs exit code {proc.returncode}"
