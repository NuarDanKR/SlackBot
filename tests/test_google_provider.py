from __future__ import annotations

import json

import pytest

from tybot.gateway.base import Message, ModelSpec, Sensitivity
from tybot.gateway.providers.google_provider import GoogleProvider


class Response:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def read(self):
        return json.dumps({
            "candidates": [{
                "content": {"parts": [{"text": '{"description":"현장"}'}]},
                "finishReason": "STOP",
            }],
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 4},
        }).encode()


def test_google_provider_sends_image_and_key_to_exact_model():
    seen = {}

    def open_request(request, *, timeout):
        seen.update(
            url=request.full_url,
            headers=dict(request.header_items()),
            payload=json.loads(request.data),
            timeout=timeout,
        )
        return Response()

    provider = GoogleProvider("AIza" + "x" * 32, opener=open_request)
    spec = ModelSpec("gemini-2.5-flash", "google", 0, 0, Sensitivity.PUBLIC)
    result = provider.complete(
        spec,
        [
            Message("system", "JSON만 출력"),
            Message("user", [
                {"type": "image", "source": {
                    "media_type": "image/png", "data": "aW1hZ2U="
                }},
                {"type": "text", "text": "읽어줘"},
            ]),
        ],
        max_tokens=200,
        timeout_seconds=7,
    )

    assert seen["url"].endswith("/models/gemini-2.5-flash:generateContent")
    assert seen["headers"]["X-goog-api-key"].startswith("AIza")
    assert seen["payload"]["contents"][0]["parts"][0]["inline_data"]["data"] == "aW1hZ2U="
    assert seen["payload"]["system_instruction"]["parts"][0]["text"] == "JSON만 출력"
    assert seen["timeout"] == 7
    assert result.provider == "google"
    assert result.model == "gemini-2.5-flash"
    assert result.cost_usd == 0.0
    assert result.input_tokens == 12


def test_google_provider_refuses_model_path_injection():
    provider = GoogleProvider("AIza" + "x" * 32, opener=lambda *_a, **_k: Response())
    spec = ModelSpec("../models/paid", "google", 0, 0, Sensitivity.PUBLIC)

    with pytest.raises(ValueError, match="google_model_invalid"):
        provider.complete(spec, [Message("user", "hello")])
