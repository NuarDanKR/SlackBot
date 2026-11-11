"""Google Gemini Developer API 어댑터.

Vision worker가 DB에서 복호화한 명시적 API 키를 주입한다. 이 모듈은 환경변수나
일반 TYBot 키를 찾지 않고, 다른 provider/model로 폴백하지 않는다.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence

from ..base import LLMResponse, Message, ModelSpec, ProviderTimeout, ToolSpec

_MODEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def _parts(content: str | list[dict]) -> list[dict]:
    if isinstance(content, str):
        return [{"text": content}]
    parts: list[dict] = []
    for block in content:
        if block.get("type") == "text" and isinstance(block.get("text"), str):
            parts.append({"text": block["text"]})
            continue
        if block.get("type") != "image":
            continue
        source = block.get("source") or {}
        media = source.get("media_type")
        data = source.get("data")
        if isinstance(media, str) and isinstance(data, str) and media and data:
            parts.append({"inline_data": {"mime_type": media, "data": data}})
    return parts


class GoogleProvider:
    name = "google"

    def __init__(self, api_key: str, *, opener=urllib.request.urlopen) -> None:
        if not (api_key or "").strip():
            raise ValueError("google_api_key_required")
        self._api_key = api_key.strip()
        self._opener = opener

    def complete(
        self,
        spec: ModelSpec,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: Sequence[ToolSpec] = (),
        timeout_seconds: float | None = None,
    ) -> LLMResponse:
        if tools:
            raise ValueError("google_tools_unsupported")
        if spec.provider != self.name or not _MODEL.fullmatch(spec.model):
            raise ValueError("google_model_invalid")
        system = "\n\n".join(
            message.content
            for message in messages
            if message.role == "system" and isinstance(message.content, str)
        )
        contents = []
        for message in messages:
            if message.role == "system":
                continue
            parts = _parts(message.content)
            if parts:
                contents.append({
                    "role": "model" if message.role == "assistant" else "user",
                    "parts": parts,
                })
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": temperature,
                "responseMimeType": "application/json",
            },
        }
        if system:
            payload["system_instruction"] = {"parts": [{"text": system}]}
        request = urllib.request.Request(
            _ENDPOINT.format(model=spec.model),
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": self._api_key,
            },
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8"))
        except TimeoutError as exc:
            raise ProviderTimeout("google request timed out") from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise ProviderTimeout("google request timed out") from exc
            raise
        candidates = body.get("candidates") or []
        candidate = candidates[0] if candidates else {}
        response_parts = ((candidate.get("content") or {}).get("parts") or [])
        text = "".join(
            part.get("text", "") for part in response_parts if isinstance(part, dict)
        )
        usage = body.get("usageMetadata") or {}
        return LLMResponse(
            text=text,
            model=spec.model,
            provider=self.name,
            input_tokens=int(usage.get("promptTokenCount") or 0),
            output_tokens=int(usage.get("candidatesTokenCount") or 0),
            cost_usd=0.0,
            raw=body,
            stop_reason=str(candidate.get("finishReason") or ""),
        )


__all__ = ["GoogleProvider"]
