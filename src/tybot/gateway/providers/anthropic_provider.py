"""Anthropic(Claude) 어댑터. SDK는 lazy import."""
from __future__ import annotations

import os
from collections.abc import Sequence

from ..base import LLMResponse, Message, ModelSpec


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import anthropic  # lazy
            except ImportError as e:  # pragma: no cover
                raise RuntimeError(
                    "anthropic SDK 미설치: pip install anthropic"
                ) from e
            self._client = anthropic.Anthropic(api_key=self._api_key)
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
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        resp = client.messages.create(
            model=spec.model,
            system=system or None,
            messages=turns,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        text = "".join(getattr(b, "text", "") for b in resp.content)
        in_tok = resp.usage.input_tokens
        out_tok = resp.usage.output_tokens
        return LLMResponse(
            text=text,
            model=spec.model,
            provider=self.name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=spec.cost(in_tok, out_tok),
            raw=resp,
        )
