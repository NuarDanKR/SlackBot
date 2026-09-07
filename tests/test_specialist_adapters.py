"""프롬프트 계약 방식 전문가 (B-38).

설계: `docs/design/specialist-deployment.md`

전문가는 **프롬프트 파일 + 지정 모델**이다. 코드를 받지 않으므로
그 팀이 배포 중이어도 우리 봇은 답하고, 근거가 우리 밖으로 나가지 않는다.
"""
from __future__ import annotations

import pytest

from tybot import specialist_adapters as sa
from tybot.gateway.base import LLMResponse, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.specialist_contract import AuthorizedEvidence, SpecialistRequest


class Fake:
    name = "anthropic"

    def __init__(self, text: str = "8월 20일 3억으로 정정되었습니다.") -> None:
        self.text = text
        self.calls: list[tuple] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append((spec.model, list(messages)))
        return LLMResponse(self.text, spec.model, self.name, 100, 20, 0.001)


def _router(provider):
    return Router(
        providers={"anthropic": provider},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            ),
            "claude-haiku-4-5": ModelSpec(
                "claude-haiku-4-5", "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
            ),
        },
        cost_guard=CostGuard(10.0),
        default_model="claude-sonnet-5",
    )


def _request(question: str = "기성금 얼마야", text: str = "기성금 3억 청구") -> SpecialistRequest:
    return SpecialistRequest(
        question=question,
        evidence=(
            AuthorizedEvidence.from_acl_filter(
                workspace="pilot", text=text, authorization_id="auth-1"
            ),
        ),
    )


# --- 배포 여부는 파일이 사실이다 --------------------------------------------
def test_hermes_prompt_is_deployed():
    """프롬프트가 있으면 배포된 것이다. 목록을 손으로 적지 않는다."""
    assert "hermes" in sa.available_keys()


def test_a_missing_prompt_is_refused_not_silently_empty():
    """빈 프롬프트로 돌면 전문가가 아니라 그냥 모델이 답하는데,

    기록에는 전문가가 답한 것으로 남는다. 그러면 판정을 보는 일이 무의미해진다.
    """
    with pytest.raises(sa.AdapterError, match="없습니다"):
        sa.load_prompt("존재하지않는전문가")


def test_frontmatter_does_not_reach_the_model():
    """출처·버전 기록은 우리 것이다. 모델에 보내면 토큰만 쓴다."""
    prompt = sa.load_prompt("hermes")

    assert "derived_from" not in prompt
    assert prompt.startswith("당신은")


def test_the_prompt_version_is_readable():
    """콘솔에 「지금 무엇이 돌고 있나」 를 보이려면 버전이 있어야 한다."""
    assert sa.prompt_version("hermes") == "1"


# --- 모델은 전문가별로 ------------------------------------------------------
def test_the_specialist_model_is_used(tmp_path):
    """간단한 분야에 무거운 모델을 쓸 이유가 없다.

    콘솔에서 전문가별로 모델을 정하는 것이 이 자리에서 값을 갖는다.
    """
    provider = Fake()
    adapter = sa.build("hermes", _router(provider), model="claude-haiku-4-5")

    adapter.complete(_request())

    assert provider.calls[0][0] == "claude-haiku-4-5"


def test_no_model_falls_back_to_the_gateway_default():
    provider = Fake()
    adapter = sa.build("hermes", _router(provider), model="")

    adapter.complete(_request())

    assert provider.calls[0][0] == "claude-sonnet-5"


# --- 프롬프트와 근거의 자리 --------------------------------------------------
def test_evidence_comes_before_the_question():
    """캐시는 접두사 일치다. 같은 채널을 다시 물을 때 근거가 캐시되어야 한다."""
    provider = Fake()
    sa.build("hermes", _router(provider)).complete(_request())

    _, messages = provider.calls[0]
    user = next(m for m in messages if m.role == "user")

    assert user.content.index("기성금 3억 청구") < user.content.index("질문:")


def test_the_prompt_goes_in_as_a_system_message():
    provider = Fake()
    sa.build("hermes", _router(provider)).complete(_request())

    _, messages = provider.calls[0]

    assert messages[0].role == "system"
    assert "출처를 쓰지 않습니다" in messages[0].content


def test_the_prompt_forbids_writing_sources():
    """출처는 마스터가 붙인다(원칙 2). 전문가가 쓰면 본문과 출처가 어긋난다.

    계약(`_validated_text`)이 실행 시점에 거부하지만, 프롬프트에서도 말해야
    거부가 잦아지지 않는다.
    """
    prompt = sa.load_prompt("hermes")

    assert "출처를 쓰지 않습니다" in prompt


def test_the_prompt_warns_about_other_sites():
    """사업장이 30개가 넘고 금액 단위가 비슷하다.

    다른 현장 숫자가 흘러드는 것이 이 영역에서 가장 흔한 사고다.
    """
    prompt = sa.load_prompt("hermes")

    assert "다른 사업장" in prompt or "다른 현장" in prompt


# --- 근거가 커도 답이 나간다 -------------------------------------------------
def test_oversized_evidence_is_trimmed_not_dropped(monkeypatch):
    """컨텍스트를 넘겨 호출이 통째로 실패하면 답이 아예 안 나간다."""
    monkeypatch.setattr(sa, "MAX_EVIDENCE_CHARS", 100)
    provider = Fake()

    sa.build("hermes", _router(provider)).complete(_request(text="가" * 500))

    _, messages = provider.calls[0]
    user = next(m for m in messages if m.role == "user")

    assert user.content.count("가") == 100


# --- 전문가는 자기 데이터를 가져오지 않는다 ---------------------------------
def test_the_adapter_has_no_way_to_read_the_archive():
    """프롬프트 방식의 값이 여기 있다 — 읽을 통로가 아예 없다.

    같은 프로세스 안의 임의 코드는 DATABASE_URL·아카이브 파일을 그대로 읽지만,
    이 어댑터가 받는 것은 게이트웨이와 프롬프트 문자열뿐이다.
    """
    adapter = sa.build("hermes", _router(Fake()))

    for attr in ("store", "_store", "archive", "conn", "_conn"):
        assert not hasattr(adapter, attr), f"어댑터가 {attr} 를 들고 있다"
