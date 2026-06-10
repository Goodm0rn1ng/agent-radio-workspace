"""总结模块：双语 transcript → 结构化 Summary（含 sections / highlights）。"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from loguru import logger

from radio.config import Settings
from radio.models import ProgramSection, Segment, Summary
from radio.segments_library import (
    SegmentEntry,
    extract_series_name,
    filter_library_by_series,
    format_library_for_prompt,
    load_segments_library,
    match_segment,
)
from radio.terminology import (
    apply_summary_corrections,
    format_terminology_for_prompt,
    load_post_corrections,
)
from radio.utils.metrics import TokenUsage
from radio.utils.retry import async_retry

DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "summarize.txt"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
TokenCallback = Callable[[str, TokenUsage], None]


@dataclass(frozen=True)
class LLMTextResponse:
    text: str
    usage: TokenUsage


class ModelOutputTruncatedError(RuntimeError):
    """模型返回的结构化 JSON 不完整，通常是输出 token 上限导致。"""


SUMMARY_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "title_ja": {"type": "string"},
                    "intro": {"type": "string"},
                    "is_recurring": {"type": "boolean"},
                    "time_range": {"type": "string"},
                    "content": {"type": "string"},
                    "listener_mail_from": {"type": "string"},
                    "listener_mail_ja": {"type": "string"},
                    "listener_mail": {"type": "string"},
                    "member_reactions": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "music": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                    "notes": {
                        "type": "array",
                        "items": {"type": "string"},
                    },
                },
                "required": [
                    "title_ja",
                    "intro",
                    "is_recurring",
                    "time_range",
                    "content",
                    "listener_mail_from",
                    "listener_mail",
                    "member_reactions",
                    "music",
                    "notes",
                ],
            },
        },
        "key_topics": {
            "type": "array",
            "items": {"type": "string"},
        },
        "highlights": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string"},
                    "reason": {"type": "string"},
                    "quote": {"type": "string"},
                },
                "required": ["timestamp", "reason", "quote"],
            },
        },
    },
    "required": ["summary", "sections", "key_topics"],
}


def _format_seconds(s: float) -> str:
    """秒数 → HH:MM:SS"""
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _build_transcript_text(segments: list[Segment]) -> str:
    """为 prompt 拼一个可读的双语 transcript。"""
    lines = []
    for seg in segments:
        ts = _format_seconds(seg.start)
        lines.append(f"[{ts}] {seg.ja} / {seg.zh}")
    return "\n".join(lines)


def _build_summary_prompt(
    template: str,
    segments: list[Segment],
    settings: Settings,
    library: list[SegmentEntry],
    recent_history_text: str = "（暂无往期记录）",
) -> str:
    terminology = format_terminology_for_prompt(settings.translation.terminology_path)
    library_text = format_library_for_prompt(library)
    transcript_text = _build_transcript_text(segments)
    return (
        template.replace("{max_summary_chars}", str(settings.summary.max_summary_chars))
        .replace(
            "{target_highlight_count}", str(settings.summary.target_highlight_count)
        )
        .replace("{terminology}", terminology)
        .replace("{segments_library}", library_text)
        .replace("{recent_history}", recent_history_text)
        .replace("{transcript}", transcript_text)
    )


def _strip_json_fence(raw: str) -> str:
    """模型偶尔会用 ```json 包裹响应，剥掉。"""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _extract_json_object(raw: str) -> str:
    """从模型响应中提取第一个顶层 JSON object。"""
    text = _strip_json_fence(raw)
    start = text.find("{")
    if start < 0:
        return text

    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start=start):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return text[start:]


def _repair_inner_quotes(raw: str) -> str:
    """启发式修复：把 JSON 字符串值内未转义的 ASCII 双引号替换成中文双引号"。

    策略：
    - 用栈跟踪字符串内/外状态
    - 在字符串内遇到 `"` 时，向后看一两个字符：如果接下来是 `,`、`}`、`]`、`:`、空白
      （即合法的字符串结束符），认为它是字符串结束；否则视为字符串内未转义的引号，替换成 `"`
    - 这是 best-effort：不是严格的 JSON 修复器，但对"LLM 在 summary 文本里塞引号"这种
      最常见情况有效

    只在 json.loads 已失败时调用。
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    in_string = False
    escaped = False
    while i < n:
        ch = raw[i]
        if not in_string:
            out.append(ch)
            if ch == '"':
                in_string = True
            i += 1
            continue

        # 字符串内
        if escaped:
            out.append(ch)
            escaped = False
            i += 1
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            i += 1
            continue
        if ch == '"':
            # 决定这个引号是结束还是内嵌
            j = i + 1
            while j < n and raw[j] in " \t":
                j += 1
            nxt = raw[j] if j < n else ""
            if nxt in (",", "}", "]", ":", "\n", "\r", ""):
                # 字符串结束
                out.append(ch)
                in_string = False
                i += 1
            else:
                # 内嵌引号：替换为中文双引号
                out.append("”")  # ”
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _dump_raw_response(raw: str, settings: Settings, label: str) -> Path:
    """把 LLM 原始响应写到磁盘，便于排查。"""
    logs_dir = settings.runtime.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)
    path = logs_dir / f"summarize_raw_{label}.json"
    path.write_text(raw, encoding="utf-8")
    return path


def _parse_summary_json(raw_extracted: str, settings: Settings) -> dict:
    """两步解析：先严格 json.loads；失败则启发式修复再 loads。两步都失败则 dump + 抛出。"""
    try:
        return json.loads(raw_extracted)
    except json.JSONDecodeError as e:
        if _looks_truncated_json(raw_extracted, e):
            ts = int(__import__("time").time())
            path_raw = _dump_raw_response(raw_extracted, settings, f"truncated_{ts}")
            raise ModelOutputTruncatedError(
                "模型返回的 summary JSON 被截断，已触发重试；"
                f"原始响应已 dump：{path_raw}"
            ) from e
        logger.warning(
            f"严格 JSON 解析失败（{e.msg} at char {e.pos}）→ 尝试启发式修复内嵌引号"
        )
        repaired = _repair_inner_quotes(raw_extracted)
        try:
            parsed = json.loads(repaired)
            logger.info("启发式修复成功")
            return parsed
        except json.JSONDecodeError as e2:
            ts = int(__import__("time").time())
            path_raw = _dump_raw_response(raw_extracted, settings, f"failed_{ts}_raw")
            path_repaired = _dump_raw_response(
                repaired, settings, f"failed_{ts}_repaired"
            )
            logger.error(
                f"启发式修复也失败：{e2.msg} at char {e2.pos}。"
                f"已 dump：{path_raw}、{path_repaired}"
            )
            raise


def _summary_max_output_tokens(settings: Settings) -> int:
    return int(getattr(settings.summary, "max_output_tokens", 32768) or 32768)


def _looks_truncated_json(raw: str, err: json.JSONDecodeError) -> bool:
    stripped = raw.rstrip()
    if not stripped:
        return False
    if stripped.endswith("}"):
        return False
    return err.msg in {
        "Expecting ',' delimiter",
        "Expecting value",
        "Unterminated string starting at",
    }


async def _summarize_with_anthropic(prompt: str, settings: Settings) -> LLMTextResponse:
    client = AsyncAnthropic(
        api_key=settings.secrets.anthropic_api_key.get_secret_value()
    )
    logger.info(f"调用 Claude 总结：{settings.summary.model}（prompt {len(prompt)} 字符）")
    resp = await client.messages.create(
        model=settings.summary.model,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return LLMTextResponse(
        text="".join(block.text for block in resp.content if hasattr(block, "text")),
        usage=_usage_from_anthropic_response(resp),
    )


async def _summarize_with_gemini(prompt: str, settings: Settings) -> LLMTextResponse:
    if settings.secrets.gemini_api_key is None:
        raise RuntimeError("summary.provider=gemini 需要在 .env 中配置 GEMINI_API_KEY")

    model = settings.summary.model.removeprefix("models/")
    url = GEMINI_API_URL.format(model=model)
    body = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": _summary_max_output_tokens(settings),
            "responseMimeType": "application/json",
            "responseSchema": SUMMARY_RESPONSE_SCHEMA,
        },
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": settings.secrets.gemini_api_key.get_secret_value(),
    }
    logger.info(f"调用 Gemini 总结：{settings.summary.model}（prompt {len(prompt)} 字符）")

    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(url, headers=headers, json=body)
        if resp.is_error:
            raise RuntimeError(
                f"Gemini API 调用失败：HTTP {resp.status_code}，model={settings.summary.model}，"
                f"body={resp.text[:500]}"
            )
    data = resp.json()
    text = _extract_gemini_text(data, settings)
    return LLMTextResponse(
        text=text,
        usage=_usage_from_gemini_response(data),
    )


def _extract_gemini_text(data: dict, settings: Settings) -> str:
    candidate = data["candidates"][0]
    finish_reason = candidate.get("finishReason")
    if finish_reason == "MAX_TOKENS":
        raise ModelOutputTruncatedError(
            "Gemini summary 输出达到 maxOutputTokens 上限，"
            f"当前上限为 {_summary_max_output_tokens(settings)}；已触发重试"
        )
    parts = candidate["content"]["parts"]
    return "".join(part.get("text", "") for part in parts)


def _usage_from_anthropic_response(resp) -> TokenUsage:
    usage = getattr(resp, "usage", None)
    return TokenUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


def _usage_from_gemini_response(data: dict) -> TokenUsage:
    usage = data.get("usageMetadata") or {}
    return TokenUsage(
        input_tokens=int(usage.get("promptTokenCount") or 0),
        output_tokens=int(usage.get("candidatesTokenCount") or 0),
        total_tokens=int(usage.get("totalTokenCount") or 0),
    )


async def _call_summary_model(
    prompt: str,
    settings: Settings,
    token_callback: TokenCallback | None = None,
) -> str:
    provider = settings.summary.provider.lower()
    if provider == "anthropic":
        response = await _summarize_with_anthropic(prompt, settings)
        label = "summary.anthropic"
    elif provider == "gemini":
        response = await _summarize_with_gemini(prompt, settings)
        label = "summary.gemini"
    else:
        raise ValueError(f"不支持的 summary.provider：{settings.summary.provider}")

    if token_callback is not None:
        token_callback(label, response.usage)
    return response.text


def _apply_segments_library(
    sections: list[ProgramSection],
    library: list[SegmentEntry],
) -> tuple[list[ProgramSection], int, int]:
    """对每个 section，按 title_ja 在 library 中查找匹配。

    命中：覆盖 intro 为 library 标准版，is_recurring=True。
    未命中：保持 LLM 输出，is_recurring=False。

    返回：(更新后 sections, 命中次数, 未命中次数)
    """
    if not library:
        return sections, 0, len(sections)

    updated: list[ProgramSection] = []
    matched = 0
    unmatched = 0
    for section in sections:
        entry = match_segment(section.title_ja, library)
        if entry is not None:
            updated.append(
                section.model_copy(
                    update={
                        "title_ja": entry.title_ja,  # 统一用库里的规范原文
                        "intro": entry.intro,
                        "is_recurring": True,
                    }
                )
            )
            matched += 1
            logger.info(
                f"环节匹配命中：'{section.title_ja}' → library['{entry.title_ja}']"
            )
        else:
            updated.append(section.model_copy(update={"is_recurring": False}))
            unmatched += 1
            if section.title_ja:
                logger.debug(f"环节未命中库：'{section.title_ja}'")
    return updated, matched, unmatched


def dump_summary_to_disk(
    summary: Summary,
    out_dir: Path,
    program_name: str,
    air_date: str,
) -> Path:
    """把 Summary 序列化为 JSON 落盘，方便事后审计/复盘/搜索。"""
    safe = "".join("_" if ch in '/\\:*?"<>|' else ch for ch in program_name).strip()[:80]
    path = out_dir / f"summary_{safe or 'untitled'}_{air_date}.json"
    path.write_text(
        json.dumps(summary.model_dump(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"Summary 已落盘：{path}")
    return path


@async_retry(attempts=3, base_delay=2.0)
async def summarize(
    segments: list[Segment],
    settings: Settings,
    *,
    program_name: str = "",
    air_date: str = "",
    token_callback: TokenCallback | None = None,
) -> Summary:
    """对完整 transcript 生成摘要 + 关键话题 + 高光时刻 + 分段复盘。

    Args:
        segments: 双语 transcript 列表
        settings: 全局配置
        program_name: 节目完整标题（用于按 series 拉历史摘要注入 prompt）
        air_date: 本期播出日期（用于 history 过滤"在此之前"的条目）
    """
    template = settings.summary.prompt_path.read_text(encoding="utf-8")
    full_library = load_segments_library(settings.summary.segments_library_path)

    # 关键：按当前节目 series 过滤 library，避免跨节目业界通用术语（如「ふつおた
    # のコーナー」「オープニング」「エンディング」）被其他节目下登记的同名条目
    # 误匹配。后续 prompt 注入和 _apply_segments_library 都基于 series_library。
    series_name = extract_series_name(program_name) if program_name else ""
    if series_name:
        series_library = filter_library_by_series(full_library, series_name)
        logger.info(
            f"library 按 series 过滤：{len(full_library)} → {len(series_library)} "
            f"条（series='{series_name}'）"
        )
    else:
        series_library = full_library
        logger.warning(
            "未提供 program_name；library 全量参与匹配（可能跨节目误命中）"
        )

    library = series_library  # 后面所有匹配 / prompt 注入都用 series-filtered

    # 注入"往期回忆"：同节目历史按相关度 + 时间衰减排序。
    from radio.history import format_history_for_prompt, load_relevant_history
    history_path = settings.runtime.logs_dir.parent / "history_context.jsonl"
    if program_name:
        recent = load_relevant_history(
            history_path,
            program_name,
            query_text=_build_transcript_text(segments),
            limit=settings.summary.history_recent_n,
            air_date_before=air_date or None,
        )
        history_text = format_history_for_prompt(recent)
        if recent:
            logger.info(
                f"history 注入 {len(recent)} 期往期回忆（相关度 + 时间衰减排序）"
            )
    else:
        history_text = "（未提供 program_name，跳过往期回忆）"

    prompt = _build_summary_prompt(
        template, segments, settings, library, recent_history_text=history_text
    )

    raw = _extract_json_object(
        await _call_summary_model(prompt, settings, token_callback)
    )
    parsed = _parse_summary_json(raw, settings)
    summary = Summary(**parsed)

    # 1. 术语库译后修正（对所有中文字段做 str.replace）
    summary = apply_summary_corrections(
        summary,
        load_post_corrections(settings.translation.terminology_path),
    )
    # 2. 常驻环节库匹配（覆盖 intro + 标记 is_recurring）
    new_sections, matched, unmatched = _apply_segments_library(summary.sections, library)
    summary = summary.model_copy(update={"sections": new_sections})

    logger.info(
        f"总结完成：{len(summary.summary)} 字摘要 + "
        f"{len(summary.sections)} 个 sections（命中库 {matched}，未命中 {unmatched}） + "
        f"{len(summary.key_topics)} 个话题 + "
        f"{len(summary.highlights)} 个高光"
    )
    return summary
