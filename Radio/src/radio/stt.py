"""Groq Whisper-large-v3 转写。

输入：一组音频切片文件路径（每片 ≤25MB），加上每片在完整音频中的起始秒偏移。
输出：合并后的 Segment 列表，时间戳已经按全局校正。
"""

from __future__ import annotations

import asyncio
import re
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


_PUNCT_RE = re.compile(r"[、。，,．.!！?？♪\s]+")


def _is_hallucination_phrase(text: str, phrases: list[str]) -> bool:
    """整段去掉标点后只剩已知幻听短语（或其重复拼接）→ True。"""
    norm = _PUNCT_RE.sub("", text)
    if not norm:
        return False
    residual = norm
    for p in sorted(phrases, key=len, reverse=True):
        residual = residual.replace(_PUNCT_RE.sub("", p), "")
    if residual == norm:  # 没有命中任何黑名单短语（防止超短文本被长度判定误杀）
        return False
    return len(residual) <= max(2, len(norm) // 10)


def _filter_segment(
    text: str,
    no_speech_prob: float | None,
    avg_logprob: float | None,
    compression_ratio: float | None,
    settings: Settings,
) -> str | None:
    """返回丢弃原因；None 表示保留。

    Whisper 在静音/音乐段会幻听出字幕式口癖或复读循环，verbose_json 的
    置信信号（no_speech_prob / avg_logprob / compression_ratio）专治此症。
    """
    stt = settings.stt
    if (
        no_speech_prob is not None
        and avg_logprob is not None
        and no_speech_prob > stt.filter_no_speech_max
        and avg_logprob < stt.filter_logprob_min
    ):
        return f"no_speech({no_speech_prob:.2f})+logprob({avg_logprob:.2f})"
    if compression_ratio is not None and compression_ratio > stt.filter_compression_max:
        return f"compression_ratio({compression_ratio:.2f})"
    if _is_hallucination_phrase(text, stt.hallucination_phrases):
        weak_conf = (no_speech_prob is not None and no_speech_prob > 0.2) or (
            avg_logprob is not None and avg_logprob < -0.45
        )
        if weak_conf or no_speech_prob is None:
            return "hallucination_phrase"
    return None


def _looks_like_prompt_echo(text: str, prompt: str | None) -> bool:
    """Whisper 偶尔会把 `、` 分隔的 prompt 词表当成转写内容复读。"""
    if not prompt:
        return False
    # 整段（去标点后）是 prompt 的连续子串、且横跨 ≥2 个 prompt 词 → 必为回声
    # （如「ラジオ、ライブ、安野希世乃、悠木碧」；单个词条不算——主播可能真的说出节目名）
    norm_text = _PUNCT_RE.sub("", text)
    if (
        len(norm_text) >= 6
        and norm_text in _PUNCT_RE.sub("", prompt)
        and len([c for c in text.split("、") if c.strip(" 、，,。")]) >= 2
        and not any(norm_text == _PUNCT_RE.sub("", term) for term in prompt.split("、"))
    ):
        return True
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
    dropped = 0
    for i, seg in enumerate(raw_segments):
        # groq sdk 返回的可能是 dict 或对象，统一取属性
        if isinstance(seg, dict):
            get = seg.get
        else:
            get = lambda k, d=None: getattr(seg, k, d)  # noqa: E731
        start = float(get("start"))
        end = float(get("end"))
        text = str(get("text") or "").strip()
        if not text:
            continue
        if _looks_like_prompt_echo(text, prompt):
            logger.warning(
                f"丢弃疑似 STT prompt 回声片段：{audio_path.name} "
                f"{offset_seconds + start:.1f}-{offset_seconds + end:.1f}s: {text[:120]}"
            )
            continue
        no_speech = get("no_speech_prob")
        logprob = get("avg_logprob")
        compression = get("compression_ratio")
        reason = _filter_segment(
            text,
            float(no_speech) if no_speech is not None else None,
            float(logprob) if logprob is not None else None,
            float(compression) if compression is not None else None,
            settings,
        )
        if reason:
            dropped += 1
            logger.info(
                f"丢弃疑似幻听段[{reason}]：{audio_path.name} "
                f"{offset_seconds + start:.1f}-{offset_seconds + end:.1f}s: {text[:80]}"
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

    logger.info(
        f"完成转写：{audio_path.name} → {len(segments)} 段"
        + (f"（过滤幻听 {dropped} 段）" if dropped else "")
    )
    return segments


def _collapse_repeats(flat: list[Segment]) -> list[Segment]:
    """跨段复读坍缩：同一文本连续出现 ≥3 次是 Whisper 在音乐/静音上的循环幻听
    （真实谈话几乎不会产生 3 个完全相同的连续段），保首段、并入时间范围。"""
    merged: list[Segment] = []
    run_start = 0
    for idx in range(1, len(flat) + 1):
        same = (
            idx < len(flat)
            and _PUNCT_RE.sub("", flat[idx].ja) == _PUNCT_RE.sub("", flat[run_start].ja)
        )
        if same:
            continue
        run = flat[run_start:idx]
        if len(run) >= 3:
            logger.info(
                f"坍缩复读幻听 x{len(run)}：{run[0].start:.1f}-{run[-1].end:.1f}s "
                f"「{run[0].ja[:50]}」"
            )
            merged.append(
                run[0].model_copy(update={"i": len(merged), "end": run[-1].end})
            )
        else:
            for seg in run:
                merged.append(seg.model_copy(update={"i": len(merged)}))
        run_start = idx
    return merged


async def _relisten_gaps(
    client: AsyncGroq,
    source_audio: Path,
    merged: list[Segment],
    settings: Settings,
    prompt: str | None,
) -> list[Segment]:
    """空洞重听：首轮转写漏听的兜底。两类空洞——

    1. 时间空洞：相邻段之间 ≥N 秒没有任何转写（解码窗被幻听吃掉后整窗跳过）。
    2. 窗口吞噬段：一个段横跨近整个解码窗却几乎没有文字（如 30s 只有「BanG Dream」），
       时间被junk段盖住所以不构成时间空洞，但内容同样丢了。

    把空洞单独切出来重转——短片没有前后音乐上下文，解码通常干净；
    仍是幻听的会被同一套过滤再次丢弃（音乐段的空洞属正常，重听后照样为空）。
    """
    stt = settings.stt
    gaps: list[tuple[float, float, Segment | None]] = []  # (start, end, 待替换的吞噬段)
    prev_end = 0.0
    for seg in merged:
        if seg.start - prev_end >= stt.gap_relisten_min_seconds:
            gaps.append((prev_end, seg.start, None))
        dur = seg.end - seg.start
        density = len(_PUNCT_RE.sub("", seg.ja)) / dur if dur > 0 else 99.0
        if dur >= stt.gap_relisten_min_seconds * 0.8 and density < 0.5:
            gaps.append((seg.start, seg.end, seg))
        prev_end = max(prev_end, seg.end)
    if not gaps:
        return merged
    gaps = gaps[:20]  # 成本上限
    logger.info(
        "空洞重听："
        + ", ".join(
            f"{a:.0f}-{b:.0f}s" + ("(低密度段)" if j else "") for a, b, j in gaps
        )
    )

    from radio.utils.ffmpeg import find_ffmpeg

    ffmpeg = find_ffmpeg()
    extra: list[Segment] = []
    replaced: set[int] = set()  # 被重听结果替换的吞噬段（按 id()）
    for a, b, junk in gaps:
        ss = max(0.0, a - 1.0)
        clip = source_audio.with_name(f"_gap_{int(ss)}{source_audio.suffix}")
        cmd = [
            ffmpeg, "-y", "-ss", f"{ss:.2f}", "-to", f"{b + 1.0:.2f}",
            "-i", str(source_audio), "-c", "copy", "-loglevel", "error", str(clip),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not clip.exists():
            logger.warning(f"空洞切片失败 {a:.0f}-{b:.0f}s：{stderr.decode()[:200]}")
            continue
        try:
            segs = await _transcribe_one(
                client, clip, ss, settings, start_index=0, prompt=prompt
            )
            # 只收完全落在空洞内的段，避免与两端既有段重复
            kept = [s for s in segs if s.start >= a - 0.5 and s.end <= b + 1.5]
            if junk is not None:
                # 重听结果须比吞噬段更充实才替换，否则保留原段
                junk_chars = len(_PUNCT_RE.sub("", junk.ja))
                new_chars = sum(len(_PUNCT_RE.sub("", s.ja)) for s in kept)
                if new_chars > junk_chars:
                    replaced.add(id(junk))
                    logger.info(
                        f"低密度段 {a:.0f}-{b:.0f}s 重听替换：{junk_chars}→{new_chars} 字符"
                        f"（{len(kept)} 段）"
                    )
                    extra.extend(kept)
            elif kept:
                logger.info(f"空洞 {a:.0f}-{b:.0f}s 重听找回 {len(kept)} 段")
                extra.extend(kept)
        except Exception as e:  # noqa: BLE001 — 重听失败不影响主结果
            logger.warning(f"空洞重听失败 {a:.0f}-{b:.0f}s：{e!r}")
        finally:
            clip.unlink(missing_ok=True)

    if not extra and not replaced:
        return merged
    base = [s for s in merged if id(s) not in replaced]
    combined = sorted([*base, *extra], key=lambda s: s.start)
    return [s.model_copy(update={"i": n}) for n, s in enumerate(combined)]


async def transcribe_segments(
    audio_segments: list[tuple[Path, float]],
    settings: Settings,
    program_display_name: str | None = None,
    source_audio: Path | None = None,
) -> list[Segment]:
    """并发转写所有切片。

    Args:
        audio_segments: [(切片路径, 切片在完整音频中的起始秒), ...]
        program_display_name: 节目标题，用于按当前 series 过滤 STT prompt 中的
            library 环节名和 radio 节目名（避免多节目场景下被无关词占满 prompt）
        source_audio: 完整音频路径；提供时对首轮转写留下的大空洞做二次重听（防漏听）
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

    # 合并：按切片顺序拼接
    flat: list[Segment] = []
    for segs in raw_results:
        assert not isinstance(segs, Exception)
        flat.extend(segs)

    merged = _collapse_repeats(flat)
    collapsed = len(flat) - len(merged)

    # 空洞重听（漏听兜底）；回填内容可能引入新的复读（歌词循环），再坍缩一遍
    if source_audio is not None and settings.stt.gap_relisten and merged:
        merged = _collapse_repeats(
            await _relisten_gaps(client, source_audio, merged, settings, prompt)
        )

    logger.info(
        f"全部转写完成：共 {len(merged)} 段"
        + (f"（坍缩复读 {collapsed} 段）" if collapsed else "")
    )
    return merged
