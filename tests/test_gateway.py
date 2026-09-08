"""게이트웨이 테스트 — 네트워크/SDK 없이 fake provider 로 검증."""
from __future__ import annotations

import pytest

from tybot.gateway import (
    CostLimitExceeded,
    LLMResponse,
    Message,
    ModelNotAllowed,
    Router,
    Sensitivity,
    UnknownModel,
)
from tybot.gateway.router import DEFAULT_REGISTRY, _content_size


class FakeProvider:
    """토큰/비용을 결정적으로 반환하는 가짜 프로바이더."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls += 1
        in_tok, out_tok = 1000, 500
        return LLMResponse(
            text=f"[{spec.model}] ok",
            model=spec.model,
            provider=self.name,
            input_tokens=in_tok,
            output_tokens=out_tok,
            cost_usd=spec.cost(in_tok, out_tok),
        )


def make_router(daily_limit=50.0):
    providers = {"anthropic": FakeProvider("anthropic"), "openai": FakeProvider("openai")}
    return Router.from_default_registry(daily_limit_usd=daily_limit, providers=providers)


def test_model_selection_and_response():
    r = make_router()
    resp = r.complete([Message("user", "안녕")], model="claude-sonnet-5")
    assert resp.model == "claude-sonnet-5"
    assert resp.provider == "anthropic"
    assert resp.cost_usd > 0


def test_default_model_used_when_none():
    r = make_router()
    resp = r.complete([Message("user", "요약해줘")])
    assert resp.model == "claude-sonnet-5"


def test_unknown_model_raises():
    r = make_router()
    with pytest.raises(UnknownModel):
        r.complete([Message("user", "x")], model="does-not-exist")


def test_sensitivity_routing_blocks_confidential_on_internal_only_model():
    r = make_router()
    # gpt-4o-mini 는 최대 internal → confidential 요청은 차단
    with pytest.raises(ModelNotAllowed):
        r.complete(
            [Message("user", "기밀 자료")],
            model="gpt-4o-mini",
            sensitivity=Sensitivity.CONFIDENTIAL,
        )


def test_confidential_allowed_on_claude():
    r = make_router()
    resp = r.complete(
        [Message("user", "기밀 자료")],
        model="claude-opus-4-8",
        sensitivity=Sensitivity.CONFIDENTIAL,
    )
    assert resp.model == "claude-opus-4-8"


def test_cost_guard_blocks_over_limit():
    # opus 단가로 금방 넘도록 아주 낮은 한도
    r = make_router(daily_limit=0.001)
    with pytest.raises(CostLimitExceeded):
        r.complete([Message("user", "x" * 10_000)], model="claude-opus-4-8")


def test_content_size_counts_document_and_image_payloads():
    content = [
        {"type": "text", "text": "질문"},
        {"type": "document", "source": {"type": "base64", "data": "x" * 1000}},
    ]
    assert _content_size(content) >= 1002


def test_registry_specs_have_valid_sensitivity():
    for spec in DEFAULT_REGISTRY.values():
        assert spec.max_sensitivity in Sensitivity
        assert spec.input_price_per_mtok >= 0


# --- Anthropic 은 `system` 을 배열로 받는다 (2026-09-08 실측) ----------------
#
# 문자열로 보내도 받는 모델이 있어서 오래 안 드러났다. `claude-opus-5` 가
# `system: Input should be a valid array` 로 400 을 돌려줬고, 겉으로는 **모델을
# 바꾸면 전문 봇이 답을 못 하는** 것으로 나타났다 — 마스터는 sonnet/haiku 라
# 문자열이 통했기 때문이다. 라우팅은 맞고 전문가만 조용히 폴백했다.
class _FakeMessages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs

        class Usage:
            input_tokens = 10
            output_tokens = 5

        class Block:
            text = "답"

        class Resp:
            def __init__(self):
                self.content = [Block()]
                self.usage = Usage()

        return Resp()


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


def _provider_call(messages):
    from tybot.gateway.base import ModelSpec, Sensitivity
    from tybot.gateway.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(api_key="test")
    client = _FakeClient()
    provider._client = client
    spec = ModelSpec("claude-opus-5", "anthropic", 5.0, 25.0, Sensitivity.CONFIDENTIAL)
    provider.complete(spec, messages, max_tokens=16)
    return client.messages.kwargs


def test_the_system_prompt_is_sent_as_a_content_block_array():
    from tybot.gateway.base import Message

    kwargs = _provider_call(
        [Message("system", "너는 기록 담당이다"), Message("user", "합계는?")]
    )

    assert isinstance(kwargs["system"], list), "문자열로 보내면 opus 가 400 을 준다"
    assert kwargs["system"] == [{"type": "text", "text": "너는 기록 담당이다"}]


def test_several_system_messages_become_one_block():
    """여러 개를 각각 블록으로 보내면 순서·구분이 프롬프트마다 달라진다."""
    from tybot.gateway.base import Message

    kwargs = _provider_call(
        [Message("system", "가"), Message("system", "나"), Message("user", "?")]
    )

    assert kwargs["system"] == [{"type": "text", "text": "가\n\n나"}]


def test_no_system_message_sends_none():
    """빈 배열을 보내면 그것도 거부당한다. 없을 때는 아예 안 보낸다."""
    from tybot.gateway.base import Message

    kwargs = _provider_call([Message("user", "합계는?")])

    assert kwargs["system"] is None


def test_the_user_turns_are_untouched():
    from tybot.gateway.base import Message

    kwargs = _provider_call([Message("system", "규칙"), Message("user", "합계는?")])

    assert kwargs["messages"] == [{"role": "user", "content": "합계는?"}]
