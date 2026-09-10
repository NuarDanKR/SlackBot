"""Anthropic(Claude) 어댑터. SDK는 lazy import."""
from __future__ import annotations

import os
from collections.abc import Sequence

from ..base import LLMResponse, Message, ModelSpec, ToolCall, ToolSpec


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
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse:
        client = self._get_client()
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns = [
            {"role": m.role, "content": m.content}
            for m in messages
            if m.role in ("user", "assistant")
        ]
        # `system` 은 **콘텐츠 블록 배열**이고, 없으면 **키를 아예 빼야 한다.**
        #
        # 2026-09-08 실측, 두 번 걸렸다. 둘 다 같은 400 문구로 나타나서 첫 수정 뒤에도
        # 같은 오류를 보게 됐다 — `system: Input should be a valid array`.
        #
        # | 보낸 것 | 결과 |
        # |---|---|
        # | 문자열 | sonnet·haiku 는 받고 **opus-5 는 거부** |
        # | `None` | SDK 가 `"system": null` 로 실어 보내고 거부당한다 |
        # | 배열 | OK |
        # | 키 없음 | OK (SDK 기본값이 `Omit` 이다) |
        #
        # 겉으로는 **모델을 바꾸면 답이 안 나오는** 것으로 보였다. 마스터는
        # sonnet/haiku 라 문자열이 통했고, 전문 봇만 opus 로 지정돼 있어 라우팅은
        # 맞는데 전문가만 조용히 폴백했다.
        #
        # 배열이 표준 형태고, 나중에 프롬프트 캐시(`cache_control`)를 붙일 자리도
        # 여기다 — 문자열로는 못 붙인다.
        request: dict = {
            "model": spec.model,
            "messages": turns,
            "max_tokens": max_tokens,
        }
        if system:
            request["system"] = [{"type": "text", "text": system}]
        # `temperature` 를 받지 않는 모델이 있다 — Opus 5·4.8·4.7, Sonnet 5 는
        # 샘플링 파라미터를 제거했고 보내면 `temperature is deprecated for this
        # model` 로 400 이다. **어느 모델이 무엇을 받는지는 레지스트리가 안다** —
        # 여기에 모델 이름을 박으면 새 모델이 늘 때마다 썩는다.
        if spec.supports_sampling:
            request["temperature"] = temperature
        # 도구가 없으면 **키를 아예 빼야 한다.** 빈 배열을 보내면 거부하는 모델이
        # 있고, 그 실패는 `system` 때와 같은 모양으로 나타난다.
        if tools:
            request["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.input_schema,
                }
                for t in tools
            ]
        resp = client.messages.create(**request)
        # `text` 블록만 모은다. thinking 블록은 `.text` 가 없고, tool_use 는 아래에서
        # 따로 꺼낸다 — 여기서 섞으면 도구 인자가 답변 본문에 붙는다.
        text = "".join(
            getattr(b, "text", "") for b in resp.content
            if getattr(b, "type", "") == "text"
        )
        calls = tuple(
            ToolCall(
                id=str(getattr(b, "id", "")),
                name=str(getattr(b, "name", "")),
                input=dict(getattr(b, "input", {}) or {}),
            )
            for b in resp.content
            if getattr(b, "type", "") == "tool_use"
        )
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
            tool_calls=calls,
            stop_reason=str(getattr(resp, "stop_reason", "") or ""),
        )
