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

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0, tools=()):
        self.calls += 1
        self.last_tools = tuple(tools)
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


def test_openai_provider_translates_a_common_image_block():
    from tybot.gateway.providers.openai_provider import _content

    got = _content([
        {"type": "text", "text": "무엇이 보이나?"},
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "YWJj"},
        },
    ])

    assert got[0] == {"type": "text", "text": "무엇이 보이나?"}
    assert got[1] == {
        "type": "image_url",
        "image_url": {"url": "data:image/png;base64,YWJj"},
    }


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
            type = "text"
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


def test_no_system_message_omits_the_key():
    """`None` 을 명시하면 SDK 가 `"system": null` 로 **실어 보낸다.**

    SDK 기본값은 `Omit` — 즉 안 보내는 것이다. 우리가 `None` 을 넣어 그 기본값을
    덮었고, API 는 null 을 같은 문구로 거부했다: `system: Input should be a valid
    array`. 문자열 문제를 고친 뒤에도 **같은 오류 문구**가 나와서 한 번 더 헛돌았다.
    """
    from tybot.gateway.base import Message

    kwargs = _provider_call([Message("user", "합계는?")])

    assert "system" not in kwargs, "없는 system 을 null 로 실어 보낸다"


def test_the_user_turns_are_untouched():
    from tybot.gateway.base import Message

    kwargs = _provider_call([Message("system", "규칙"), Message("user", "합계는?")])

    assert kwargs["messages"] == [{"role": "user", "content": "합계는?"}]


# --- 샘플링 파라미터를 제거한 모델 (2026-09-08) -------------------------------
#
# Opus 5·4.8·4.7 과 Sonnet 5 는 `temperature`/`top_p`/`top_k` 를 없앴다.
# 보내면 `temperature is deprecated for this model` 로 400 이다.
#
# **이 부류가 세 번째다** — system 문자열, system null, 그리고 이것. 셋 다 같은
# 모양으로 나타났다: 마스터는 멀쩡한데 **모델을 지정한 전문 봇만** 조용히 폴백.
def _spec(name, *, sampling):
    from tybot.gateway.base import ModelSpec, Sensitivity

    return ModelSpec(name, "anthropic", 1.0, 1.0, Sensitivity.CONFIDENTIAL,
                     supports_sampling=sampling)


def _call_with_spec(spec):
    from tybot.gateway.base import Message
    from tybot.gateway.providers.anthropic_provider import AnthropicProvider

    provider = AnthropicProvider(api_key="test")
    client = _FakeClient()
    provider._client = client
    provider.complete(spec, [Message("user", "합계는?")], max_tokens=16)
    return client.messages.kwargs


def test_a_model_without_sampling_gets_no_temperature():
    kwargs = _call_with_spec(_spec("claude-opus-5", sampling=False))

    assert "temperature" not in kwargs, "보내면 400 이 난다"


def test_a_model_with_sampling_still_gets_it():
    """haiku·sonnet 4.6 은 받는다. 다 빼 버리면 그 모델들의 결정성이 사라진다."""
    kwargs = _call_with_spec(_spec("claude-haiku-4-5", sampling=True))

    assert kwargs["temperature"] == 0.0


def test_the_registry_knows_which_models_dropped_sampling():
    """**프로바이더에 모델 이름을 박지 않는다.** 박으면 새 모델마다 썩고,
    그 고장은 그 모델을 지정한 전문 봇만 조용히 폴백하는 모양으로 나타난다."""
    from tybot.gateway.router import DEFAULT_REGISTRY

    for name in ("claude-opus-5", "claude-opus-4-8", "claude-sonnet-5"):
        assert not DEFAULT_REGISTRY[name].supports_sampling, name
    assert DEFAULT_REGISTRY["claude-haiku-4-5"].supports_sampling


def test_the_provider_has_no_model_names_in_it():
    import inspect

    from tybot.gateway.providers import anthropic_provider

    source = inspect.getsource(anthropic_provider.AnthropicProvider.complete)

    assert "claude-" not in source, "모델 이름이 프로바이더에 박혔다 — 레지스트리가 정한다"


def test_the_specialist_budget_covers_thinking():
    """현재 모델들은 사고가 기본으로 켜져 있고 그 토큰이 `max_tokens` 에서 나간다.

    1024 로 두면 사고하다 예산이 끝나 본문이 비고, 계약이 그것을 「빈 응답」 으로
    막아 **매번 마스터로 폴백**한다 — 오류는 안 나고 전문가만 조용히 안 쓰인다.
    """
    import inspect

    from tybot import specialist_adapters

    source = inspect.getsource(specialist_adapters.PromptSpecialist.complete)

    assert "max_tokens=1024" not in source
    assert "max_tokens=8192" in source


# --- 도구 (2026-09-11) --------------------------------------------------------
#
# Hermes 를 흡수하려면 검색·읽기 루프가 필요하고, 그 루프는 게이트웨이가
# 도구를 실어 보낼 수 있어야 돈다. **루프 자체는 여기 없다** — 몇 번 부를지는
# 정책이라 호출부(`ToolSpecialist`)가 정한다.
def test_no_tools_means_the_argument_is_not_sent():
    """`system`·`temperature` 와 같은 이유다. 안 쓰는 것을 보내면 그것을
    모르는 구현이 거부하고, 그 실패는 「전문가만 답을 못 한다」 로 보인다."""
    from tybot.gateway.base import Message

    seen = {}

    class Old:
        """도구를 모르는 구현. 인자가 오면 터진다."""

        name = "anthropic"

        def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
            seen["called"] = True
            return LLMResponse(text="답", model=spec.model, provider=self.name,
                               input_tokens=1, output_tokens=1, cost_usd=0.0)

    router = Router.from_default_registry(providers={"anthropic": Old()})

    got = router.complete([Message("user", "안녕")], model="claude-sonnet-5")

    assert got.text == "답"
    assert seen["called"]


def test_tools_reach_the_provider():
    from tybot.gateway.base import Message, ToolSpec

    provider = FakeProvider("anthropic")
    router = Router.from_default_registry(providers={"anthropic": provider})
    spec = ToolSpec(name="search", description="찾는다", input_schema={"type": "object"})

    router.complete([Message("user", "?")], model="claude-sonnet-5", tools=[spec])

    assert provider.last_tools == (spec,)


def test_tool_use_blocks_become_tool_calls():
    """`tool_use` 를 텍스트에 섞으면 도구 인자가 답변 본문에 붙는다."""
    from tybot.gateway.base import Message, ModelSpec, Sensitivity
    from tybot.gateway.providers.anthropic_provider import AnthropicProvider

    class Use:
        type = "tool_use"
        id = "toolu_1"
        name = "search"

        def __init__(self):
            self.input = {"query": "기성금"}

    class Text:
        type = "text"
        text = "찾아볼게요."

    class Usage:
        input_tokens = 10
        output_tokens = 5

    class Resp:
        def __init__(self):
            self.content = [Text(), Use()]
            self.usage = Usage()
            self.stop_reason = "tool_use"

    class Client:
        # SDK 모양을 그대로 흉내 낸다.
        class messages:
            @staticmethod
            def create(**kwargs):
                Client.seen = kwargs
                return Resp()

    provider = AnthropicProvider(api_key="test")
    provider._client = Client()
    model = ModelSpec("claude-opus-5", "anthropic", 1.0, 1.0, Sensitivity.CONFIDENTIAL)

    got = provider.complete(
        model, [Message("user", "?")],
        tools=[_tool_spec_for_test()],
    )

    assert got.text == "찾아볼게요."
    assert got.wants_tools
    assert got.tool_calls[0].name == "search"
    assert got.tool_calls[0].input == {"query": "기성금"}
    assert Client.seen["tools"][0]["name"] == "search"


# 위 테스트에서만 쓰는 작은 생성자.
def _tool_spec_for_test():
    from tybot.gateway.base import ToolSpec

    return ToolSpec(name="search", description="찾는다",
                    input_schema={"type": "object", "properties": {}})
