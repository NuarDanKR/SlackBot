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
from tybot.gateway.router import DEFAULT_REGISTRY


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


def test_registry_specs_have_valid_sensitivity():
    for spec in DEFAULT_REGISTRY.values():
        assert spec.max_sensitivity in Sensitivity
        assert spec.input_price_per_mtok >= 0
