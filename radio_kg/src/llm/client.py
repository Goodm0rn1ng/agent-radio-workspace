"""Pluggable LLM client supporting anthropic / openai / deepseek / mimo.

Exposes a single `complete_json` used by the extraction agents so the rest of
the pipeline never depends on a concrete provider SDK.
"""
from __future__ import annotations

import json
from typing import Any

from config.settings import settings


class LLMError(RuntimeError):
    pass


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    return text.strip()


class LLMClient:
    def __init__(self, provider: str | None = None, model: str | None = None):
        self.provider = provider or settings.llm_provider
        self.model = model or settings.default_model
        self._client = self._build_client()

    def _build_client(self):
        if self.provider == "anthropic":
            from anthropic import Anthropic

            if not settings.anthropic_api_key:
                raise LLMError("ANTHROPIC_API_KEY not set")
            return Anthropic(api_key=settings.anthropic_api_key)
        if self.provider in ("openai", "deepseek", "mimo"):
            from openai import OpenAI

            if self.provider == "deepseek":
                if not settings.deepseek_api_key:
                    raise LLMError("DEEPSEEK_API_KEY not set")
                return OpenAI(
                    api_key=settings.deepseek_api_key,
                    base_url="https://api.deepseek.com",
                )
            if self.provider == "mimo":
                if not settings.mimo_api_key:
                    raise LLMError("MIMO_API_KEY not set")
                return OpenAI(
                    api_key=settings.mimo_api_key,
                    base_url=settings.mimo_base_url,
                )
            if not settings.openai_api_key:
                raise LLMError("OPENAI_API_KEY not set")
            return OpenAI(api_key=settings.openai_api_key)
        raise LLMError(f"unknown provider: {self.provider}")

    def complete_json(
        self, system: str, user: str, max_tokens: int = 4096
    ) -> Any:
        """Return parsed JSON from the model. Raises LLMError on bad output."""
        raw = self._complete_text(system, user, max_tokens, json_mode=True)
        try:
            return json.loads(_strip_code_fence(raw))
        except json.JSONDecodeError as e:
            raise LLMError(f"model did not return valid JSON: {e}\n---\n{raw[:800]}")

    def _complete_text(self, system: str, user: str, max_tokens: int, json_mode: bool = False) -> str:
        if self.provider == "anthropic":
            resp = self._client.messages.create(
                model=self.model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in resp.content if b.type == "text")
        # openai / deepseek / mimo (chat completions, OpenAI-compatible)
        kwargs = {}
        if json_mode:
            # OpenAI-compatible APIs require the literal token 'json' in the prompt
            kwargs["response_format"] = {"type": "json_object"}
            if "json" not in (system + user).lower():
                system = system + "\n(respond in json)"
        token_param = "max_completion_tokens" if self.provider == "mimo" else "max_tokens"
        kwargs[token_param] = max_tokens
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            **kwargs,
        )
        return resp.choices[0].message.content or ""
