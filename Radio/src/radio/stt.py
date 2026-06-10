"""Groq Whisper-large-v3 转写。

输入：一组音频切片文件路径（每片 ≤25MB），加上每片在完整音频中的起始秒偏移。
输出：合并后的 Segment 列表，时间戳已经按全局校正。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from groq import AsyncGroq
from loguru import logger

from radio.config import Settings
from radio.models import Segment
from radio.segments_library import extract_series_name, load_segments_library
from radio.terminology import load_terminology
from radio.utils.retry import async_retry

# Whisper API prompt 上限是 224 tokens（OpenAI / Groq 都一样）。
# 日文一个 token ≈ 1-2 字符，留 10% 余量。
_STT_PROMPT_MAX_CHARS = 200


def build_stt_prompt(
    settings: Settings,
    program_display_name: str | None = None,
) -> str | None:
    """动态构造 Whisper prompt（按优先级分批塞，超限按优先级丢）。

    优先级（高 → 低）：
      P0  base prompt           用户在 config.yaml / profile 里写的兜底词
      P1  当前 series 的 radio    当前节目名
      P2  当前 series 的 library  当前节目的常驻环节标题

    设计目的：
    - 多节目场景下只注入"当前节目"的环节名，避免被无关节目占满 prompt
    - 不再把全局角色名 / 歌曲名 / 台词清单注入 STT，避免 Whisper 把 prompt 当作音频内容复读
    - 把"被丢掉的词"打到日志，便于调优

    Args:
        program_display_name: 节目标题（如「MyGO!!!!!の「迷子集会」#178」），
            用于按 series_name 过滤 segments_library 和 radio 条目。
            None 时回退到不过滤行为。
    """
    series_name = extract_series_name(program_display_name) if program_display_name else None

    # 收集各优先级分桶
    buckets: list[tuple[str, list[str]]] = []  # [(priority_label, [terms])]

    radio_terms: list[str] = []
    try:
        terms_data = load_terminology(settings.translation.terminology_path)
        for term in terms_data.get("terms", []):
            cat = term.get("category", "")
            ja = str(term.get("ja", "")).strip()
            if not ja:
                continue
            if cat == "radio":
                # 按当前 series 过滤，避免把其他节目名注入本期 STT。
                if series_name:
                    if ja in series_name or series_name in ja:
                        radio_terms.insert(0, ja)
                else:
                    radio_terms.append(ja)
    except Exception as e:
        logger.warning(f"STT prompt 注入 terminology 失败：{e!r}")

    if settings.stt.prompt:
        buckets.append(("P0 base", [settings.stt.prompt]))
    buckets.append(("P1 radio", radio_terms))

    # P2: 当前 series 的 library 环节
    library_titles: list[str] = []
    try:
        library = load_segments_library(settings.summary.segments_library_path)
        for entry in library:
            if series_name:
                if not entry.program_ja:
                    continue
                if not (entry.program_ja in series_name or series_name in entry.program_ja):
                    continue
            library_titles.append(entry.title_ja)
    except Exception as e:
        logger.warning(f"STT prompt 注入 segments_library 失败：{e!r}")
    buckets.append(("P2 current-library", library_titles))

    # 按优先级填，去重，超限按"整词"丢（不撕一半的词）
    sep = "、"
    selected: list[str] = []
    seen: set[str] = set()
    used_len = 0
    dropped: list[tuple[str, str]] = []  # [(priority_label, term)]

    for label, terms in buckets:
        for term in terms:
            term = term.strip()
            if not term or term in seen:
                continue
            # 计算加入后总长度（含分隔符）
            new_len = used_len + len(term) + (len(sep) if selected else 0)
            if new_len <= _STT_PROMPT_MAX_CHARS:
                selected.append(term)
                seen.add(term)
                used_len = new_len
            else:
                dropped.append((label, term))

    if not selected:
        return None

    prompt = sep.join(selected)
    if dropped:
        # 用 INFO 而非 WARNING——超限是常态（说明库丰富了），但要让用户能看到
        # 哪些词被丢了，便于决定是否要扩 library / 拆 program / 改优先级
        dropped_summary = ", ".join(f"[{p}]{t}" for p, t in dropped[:8])
        more = f"…等 {len(dropped) - 8} 个" if len(dropped) > 8 else ""
        logger.info(
            f"STT prompt 因 224 token 上限丢弃 {len(dropped)} 个词：{dropped_summary}{more}"
        )
    return prompt


def _looks_like_prompt_echo(text: str, prompt: str | None) -> bool:
    """Whisper 偶尔会把 `、` 分隔的 prompt 词表当成转写内容复读。"""
    if not prompt:
        return False
    chunks = [chunk.strip(" 、，,。") for chunk in text.split("、")]
    chunks = [chunk for chunk in chunks if chunk]
    if len(chunks) < 5:
        return False

    prompt_terms = {
        term.strip(" 、，,。")
        for term in prompt.split("、")
        if term.strip(" 、，,。")
    }
    if len(prompt_terms) < 5:
        return False

    matches = sum(1 for chunk in chunks if chunk in prompt_terms)
    return matches >= 5 and matches / len(chunks) >= 0.6


@async_retry(attempts=3, base_delay=2.0, backoff=2.0)
async def _transcribe_one(
    client: AsyncGroq,
    audio_path: Path,
    offset_seconds: float,
    settings: Settings,
    start_index: int,
    prompt: str | None = None,
) -> list[Segment]:
    """转写单个切片，返回带全局时间偏移的 Segment 列表。"""
    logger.info(f"提交转写：{audio_path.name}（偏移 {offset_seconds:.0f}s）")

    with audio_path.open("rb") as f:
        resp = await client.audio.transcriptions.create(
            file=(audio_path.name, f.read()),
            model=settings.stt.model,
            language=settings.stt.language,
            prompt=prompt or settings.stt.prompt or None,
            response_format="verbose_json",
            timestamp_granularities=["segment"],
        )

    raw_segments = getattr(resp, "segments", None) or []
    segments: list[Segment] = []
    for i, seg in enumerate(raw_segments):
        # groq sdk 返回的可能是 dict 或对象，统一取属性
        if isinstance(seg, dict):
            start = float(seg["start"])
            end = float(seg["end"])
            text = seg["text"].strip()
        else:
            start = float(seg.start)
            end = float(seg.end)
            text = seg.text.strip()
        if not text:
            continue
        if _looks_like_prompt_echo(text, prompt):
            logger.warning(
                f"丢弃疑似 STT prompt 回声片段：{audio_path.name} "
                f"{offset_seconds + start:.1f}-{offset_seconds + end:.1f}s: {text[:120]}"
            )
            continue
        segments.append(
            Segment(
                i=start_index + i,
                start=offset_seconds + start,
                end=offset_seconds + end,
                ja=text,
            )
        )

    logger.info(f"完成转写：{audio_path.name} → {len(segments)} 段")
    return segments


async def transcribe_segments(
    audio_segments: list[tuple[Path, float]],
    settings: Settings,
    program_display_name: str | None = None,
) -> list[Segment]:
    """并发转写所有切片。

    Args:
        audio_segments: [(切片路径, 切片在完整音频中的起始秒), ...]
        program_display_name: 节目标题，用于按当前 series 过滤 STT prompt 中的
            library 环节名和 radio 节目名（避免多节目场景下被无关词占满 prompt）
    """
    client = AsyncGroq(api_key=settings.secrets.groq_api_key.get_secret_value())
    semaphore = asyncio.Semaphore(settings.runtime.stt_concurrency)

    # 动态构造 prompt（按优先级 + series 过滤；详见 build_stt_prompt 文档）
    prompt = build_stt_prompt(settings, program_display_name=program_display_name)
    if prompt:
        logger.info(f"STT prompt（{len(prompt)}/{_STT_PROMPT_MAX_CHARS} 字符）：{prompt[:80]}…")

    # 先估算每片的 start_index 范围。Whisper 每片段数不可预知，
    # 所以先用 path 顺序拿回结果，再在合并阶段重新编号。
    async def _run(path: Path, offset: float) -> list[Segment]:
        async with semaphore:
            return await _transcribe_one(
                client, path, offset, settings, start_index=0, prompt=prompt
            )

    tasks = [_run(path, offset) for path, offset in audio_segments]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    errors = [result for result in raw_results if isinstance(result, Exception)]
    if errors:
        logger.error(
            f"转写失败：{len(errors)} / {len(raw_results)} 个切片失败，"
            f"首个错误：{type(errors[0]).__name__}: {errors[0]}"
        )
        raise errors[0]

    # 合并：按切片顺序拼接，重新编全局 i
    merged: list[Segment] = []
    for segs in raw_results:
        assert not isinstance(segs, Exception)
        for seg in segs:
            merged.append(seg.model_copy(update={"i": len(merged)}))

    logger.info(f"全部转写完成：共 {len(merged)} 段")
    return merged
