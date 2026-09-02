"""Anthropic(Claude) 어댑터. SDK는 lazy import."""
from __future__ import annotations

import os
from collections.abc import Sequence

from ..base import LLMResponse, Message, ModelSpec


def _resolve_key() -> str | None:
    """키를 DB 에서 먼저 찾고, 없으면 환경변수로 되돌아간다.

    `.env` 는 평문이라 서버에 들어갈 수 있는 사람이면 누구나 읽는다. DB 에는
    암호화해 넣고 암호화 키는 DB 밖 파일에 둔다.

    **콘솔을 안 쓰는 설치에서도 그대로 떠야 하므로** 환경변수 경로를 남긴다.
    콘솔 모듈이 없거나 DB 가 없으면 조용히 환경변수를 쓴다.
    """
    try:
        from ...console.llm_secret_store import resolve_key

        return resolve_key("anthropic")
    except Exception:  # noqa: BLE001 - 키 조회가 답변 경로를 끊으면 안 된다
        return os.getenv("ANTHROPIC_API_KEY")


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or _resolve_key()
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
