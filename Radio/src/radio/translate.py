"""Deepseek API 批量翻译：日文 segments → 中文 zh 字段。

策略（见 ADR 0003）：每批 N 段，强约束 JSON 输出，段数校验；失败降级单段翻译。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from anthropic import AsyncAnthropic
from loguru import logger

from radio.config import Settings
from radio.models import Segment
from radio.terminology import format_terminology_for_prompt
from radio.utils.metrics import TokenUsage
from radio.utils.retry import async_retry

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_PROMPT_PATH = Path(__file__).parent / "prompts" / "translate.txt"
TokenCallback = Callable[[str, TokenUsage], None]


@dataclass(frozen=True)
class LLMTextResponse:
    text: str
    usage: TokenUsage


def _load_prompt_template(path: Path | None = None) -> str:
    return (path or DEFAULT_PROMPT_PATH).read_text(encoding="utf-8")


async def _call_deepseek(
    client: httpx.AsyncClient,
    settings: Settings,
    prompt: str,
    temperature: float = 0.2,
) -> LLMTextResponse:
    """调一次 Deepseek chat completion，返回纯文本响应。"""
    api_key = settings.secrets.deepseek_api_key.get_secret_value()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.translation.model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
        "response_format": {"type": "json_object"},
    }
    resp = await client.post(
        DEEPSEEK_API_URL, headers=headers, json=body, timeout=120.0
    )
    resp.raise_for_status()
    data = resp.json()
    return LLMTextResponse(
        text=data["choices"][0]["message"]["content"],
        usage=_usage_from_openai_response(data),
    )


async def _call_anthropic_translation(
    settings: Settings,
    prompt: str,
    temperature: float = 0.2,
) -> LLMTextResponse:
    """调 Claude 做精细翻译，返回纯文本响应。"""
    client = AsyncAnthropic(
        api_key=settings.secrets.anthropic_api_key.get_secret_value()
    )
    resp = await client.messages.create(
        model=settings.translation.fine_model,
        max_tokens=4096,
        temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    return LLMTextResponse(
        text="".join(block.text for block in resp.content if hasattr(block, "text")),
        usage=_usage_from_anthropic_response(resp),
    )


def _usage_from_openai_response(data: dict) -> TokenUsage:
    usage = data.get("usage") or {}
    return TokenUsage(
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
    )


def _usage_from_anthropic_response(resp) -> TokenUsage:
    usage = getattr(resp, "usage", None)
    return TokenUsage(
        input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
    )


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return text.strip()


def _parse_translation(raw: str, expected_count: int) -> dict[int, str]:
    """解析 LLM 的 JSON 响应，返回 {i: zh}。段数不符则抛出（触发上层重试）。

    LLM 偶尔漏 `zh` 或 `i` 字段，按 ValueError 抛出（被 @async_retry 捕获）。
    """
    parsed = json.loads(_strip_json_fence(raw))
    segments = parsed.get("segments", [])
    if len(segments) != expected_count:
        raise ValueError(
            f"翻译段数不匹配：输入 {expected_count}，输出 {len(segments)}"
        )
    out: dict[int, str] = {}
    for s in segments:
        if "i" not in s or "zh" not in s:
            raise ValueError(
                f"翻译段缺字段：{s}（期望 'i' + 'zh'）"
            )
        out[int(s["i"])] = s["zh"] or ""
    return out


@async_retry(attempts=2, base_delay=1.0, exceptions=(ValueError, json.JSONDecodeError))
async def _translate_batch(
    client: httpx.AsyncClient,
    batch: list[Segment],
    settings: Settings,
    template: str,
    token_callback: TokenCallback | None = None,
) -> dict[int, str]:
    """翻译一批，校验段数。带一次自动重试。"""
    input_json = json.dumps(
        [{"i": s.i, "ja": s.ja} for s in batch],
        ensure_ascii=False,
    )
    prompt = template.replace("{input_json}", input_json)
    response = await _call_deepseek(client, settings, prompt, temperature=0.2)
    if token_callback is not None:
        token_callback("translation.deepseek", response.usage)
    return _parse_translation(response.text, expected_count=len(batch))


@async_retry(attempts=2, base_delay=1.0, exceptions=(ValueError, json.JSONDecodeError))
async def _translate_batch_anthropic(
    batch: list[Segment],
    settings: Settings,
    template: str,
    token_callback: TokenCallback | None = None,
) -> dict[int, str]:
    """用 Claude Haiku 翻译一批，校验段数。"""
    input_json = json.dumps(
        [{"i": s.i, "ja": s.ja} for s in batch],
        ensure_ascii=False,
    )
    prompt = template.replace("{input_json}", input_json)
    response = await _call_anthropic_translation(settings, prompt, temperature=0.2)
    if token_callback is not None:
        token_callback("translation.anthropic", response.usage)
    return _parse_translation(response.text, expected_count=len(batch))


def _extract_single_zh(parsed: dict) -> str:
    """从 LLM 单段响应里抽 zh；缺字段返回空串而不是 KeyError。"""
    segments = parsed.get("segments") or []
    if not segments:
        return ""
    return segments[0].get("zh", "") or ""


async def _translate_single(
    client: httpx.AsyncClient,
    segment: Segment,
    settings: Settings,
    template: str,
    token_callback: TokenCallback | None = None,
) -> str:
    """降级路径：单段单翻。校验 1 段 = 1 段必通过。"""
    input_json = json.dumps([{"i": segment.i, "ja": segment.ja}], ensure_ascii=False)
    prompt = template.replace("{input_json}", input_json)
    response = await _call_deepseek(client, settings, prompt, temperature=0.0)
    if token_callback is not None:
        token_callback("translation.deepseek", response.usage)
    return _extract_single_zh(json.loads(_strip_json_fence(response.text)))


async def _translate_single_anthropic(
    segment: Segment,
    settings: Settings,
    template: str,
    token_callback: TokenCallback | None = None,
) -> str:
    """Claude Haiku 单段降级翻译。"""
    input_json = json.dumps([{"i": segment.i, "ja": segment.ja}], ensure_ascii=False)
    prompt = template.replace("{input_json}", input_json)
    response = await _call_anthropic_translation(settings, prompt, temperature=0.0)
    if token_callback is not None:
        token_callback("translation.anthropic", response.usage)
    return _extract_single_zh(json.loads(_strip_json_fence(response.text)))


async def translate_segments(
    segments: list[Segment],
    settings: Settings,
    *,
    fine: bool = False,
    token_callback: TokenCallback | None = None,
) -> list[Segment]:
    """批量翻译所有 segment，回填 zh 字段。原顺序不变。"""
    terminology = format_terminology_for_prompt(settings.translation.terminology_path)
    template = _load_prompt_template(settings.translation.prompt_path).replace(
        "{terminology}", terminology
    )
    batch_size = settings.translation.batch_size
    provider = settings.translation.fine_provider if fine else settings.translation.provider
    provider = provider.lower()

    translated: dict[int, str] = {}
    async with httpx.AsyncClient() as client:
        for offset in range(0, len(segments), batch_size):
            batch = segments[offset : offset + batch_size]
            logger.info(
                f"翻译批 {offset // batch_size + 1}：第 {offset + 1}-{offset + len(batch)} 段"
            )
            try:
                if provider == "deepseek":
                    result = await _translate_batch(
                        client, batch, settings, template, token_callback
                    )
                elif provider == "anthropic":
                    result = await _translate_batch_anthropic(
                        batch, settings, template, token_callback
                    )
                else:
                    raise ValueError(f"不支持的 translation provider：{provider}")
                translated.update(result)
            except (ValueError, json.JSONDecodeError) as e:
                logger.warning(f"批量翻译失败 → 降级为单段翻译：{e}")
                for seg in batch:
                    try:
                        if provider == "deepseek":
                            zh = await _translate_single(
                                client, seg, settings, template, token_callback
                            )
                        elif provider == "anthropic":
                            zh = await _translate_single_anthropic(
                                seg, settings, template, token_callback
                            )
                        else:
                            raise ValueError(f"不支持的 translation provider：{provider}")
                        translated[seg.i] = zh
                    except Exception as inner:
                        logger.error(f"单段翻译也失败（段 {seg.i}）：{inner}")
                        translated[seg.i] = "[翻译失败]"
                    await asyncio.sleep(0.1)

    # 回填 zh 字段
    out: list[Segment] = []
    for seg in segments:
        zh = translated.get(seg.i, "[翻译缺失]")
        out.append(seg.model_copy(update={"zh": zh}))

    logger.info(f"翻译完成：{len(out)} 段")
    return out
