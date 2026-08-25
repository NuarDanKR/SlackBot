"""OpenAI(GPT) 어댑터. SDK는 lazy import."""
from __future__ import annotations

import os
from collections.abc import Sequence

from ..base import LLMResponse, Message, ModelSpec


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.getenv("OPENAI_API_KEY")
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import openai  # lazy
            except ImportError as e:  # pragma: no cover
                raise RuntimeError("openai SDK 미설치: pip install openai") from e
            self._client = openai.OpenAI(api_key=self._api_key)
        return self._client

    def complete(
        self,
        spec: ModelSpec,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        client = self._get_client()
        payload = [{"role": m.role, "content": m.content} for m in messages]
        resp = client.chat.completions.create(
            model=spec.model,
            messages=payload,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = resp.choices[0].message.content or ""
        in_tok = resp.usage.prompt_tokens
        out_tok = resp.usage.completion_tokens
        return LLMResponse(
            text=text,
            model=spec.model,
            provider=self.name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=spec.cost(in_tok, out_tok),
            raw=resp,
        )
