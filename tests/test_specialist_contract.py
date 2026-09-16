import time

import pytest

from tybot.specialist_contract import (
    MAX_OUTPUT_CHARS,
    TARGET_OUTPUT_CHARS,
    AuthorizedEvidence,
    ContractViolation,
    SpecialistRequest,
    execute,
)


def request() -> SpecialistRequest:
    evidence = AuthorizedEvidence.from_acl_filter(
        workspace="tyit", text="권한을 확인한 원문", authorization_id="scope-1"
    )
    return SpecialistRequest("질문", (evidence,))


def test_request_refuses_mixed_authorization_scopes():
    one = AuthorizedEvidence.from_acl_filter(
        workspace="tyit", text="첫 근거", authorization_id="scope-1"
    )
    two = AuthorizedEvidence.from_acl_filter(
        workspace="mgmt", text="둘째 근거", authorization_id="scope-2"
    )
    with pytest.raises(ContractViolation):
        SpecialistRequest("질문", (one, two))


def test_specialist_cannot_attach_a_source():
    class Adapter:
        def complete(self, _request):
            return "답변\n출처: #임의채널"

    result = execute(Adapter(), request(), fallback=lambda: "마스터 답변")
    assert result.result == "contract_violation"
    assert result.text == "마스터 답변"


def test_contract_violation_reason_is_logged_without_the_output(caplog):
    class Adapter:
        def complete(self, _request):
            return "민감할 수 있는 전문 봇 본문\n출처: #임의채널"

    with caplog.at_level("WARNING"):
        result = execute(Adapter(), request(), fallback=lambda: "마스터 답변")

    # **어느 규칙이 깨졌는지** 코드로 남는다. 한 덩어리로 뭉개면 운영에서
    # 빈 응답·출처 포함·폭주를 구별할 수 없다.
    assert result.error_code == "invalid-output:sources"
    assert "출처는 마스터 봇만 부착할 수 있습니다" in caplog.text
    assert "민감할 수 있는 전문 봇 본문" not in caplog.text


def test_an_answer_over_the_limit_is_refused_not_quietly_shipped():
    """상한은 3,000 자 하나다(B-52).

    3,000 자를 넘어야 하는 답은 Hermes 를 늘려서 만들지 않는다 — 보고서 전문
    봇(`clio`)이 맡을 자리다. 상한을 올리면 그 분리가 조용히 없어진다.
    """
    class Adapter:
        def complete(self, _request):
            return "가" * (MAX_OUTPUT_CHARS + 1)

    result = execute(Adapter(), request(), fallback=lambda: "마스터 답변")

    assert result.result == "contract_violation"
    assert result.error_code == "invalid-output:too-long"


def test_an_answer_at_the_limit_passes_and_is_measured():
    class Adapter:
        def complete(self, _request):
            return "가" * MAX_OUTPUT_CHARS

    result = execute(Adapter(), request(), fallback=lambda: "마스터 답변")

    assert result.result == "success"
    # 길이를 **재 둔다** — 상한에 얼마나 붙어 있는지 봐야 프롬프트를 고칠지
    # 판단할 수 있다. 예전에는 「초과」 만 남아 3,050 인지 9,000 인지 몰랐다.
    assert result.output_chars == MAX_OUTPUT_CHARS
    assert result.over_target is False


def test_an_empty_answer_says_so_by_code():
    class Adapter:
        def complete(self, _request):
            return "   "

    result = execute(Adapter(), request(), fallback=lambda: "마스터 답변")

    assert result.error_code == "invalid-output:empty"


def test_the_prompt_target_and_the_ceiling_are_the_same_number():
    """부탁하는 숫자와 검사하는 숫자가 갈리면 상한을 지킬 이유가 없어진다.

    「부탁은 3,000, 검사는 20,000」 이면 전문 봇은 마음껏 길게 쓰고, 우리는
    그것을 통과시킨다 — 상한이 있다는 말만 남는다.
    """
    assert TARGET_OUTPUT_CHARS == MAX_OUTPUT_CHARS == 3_000


def test_specialist_failure_falls_back_to_master():
    class Adapter:
        def complete(self, _request):
            raise RuntimeError("down")

    result = execute(Adapter(), request(), fallback=lambda: "마스터 답변")
    assert result.result == "fallback"
    assert result.error_code == "adapter-error"


def test_specialist_timeout_falls_back_to_master():
    class Adapter:
        def complete(self, _request):
            time.sleep(0.05)
            return "늦은 답변"

    result = execute(Adapter(), request(), fallback=lambda: "마스터 답변", timeout_seconds=0.001)
    assert result.result == "fallback"
    # **어느 단계에서 끝났는지** 남긴다. `timeout` 한 덩어리면 primary 지연과
    # recovery 지연을 콘솔에서 구별할 수 없다(장애 §5.6).
    # 단계 이름은 `discovery`·`finalize`·`recovery`·`total` 뿐이다.
    # `primary` 는 탐색과 최종 합성을 함께 뜻해 어디서 끝났는지 알 수 없었다.
    assert result.error_code == "specialist-timeout:discovery"


def test_low_confidence_does_not_call_specialist():
    called = False

    class Adapter:
        def complete(self, _request):
            nonlocal called
            called = True
            return "전문 답변"

    result = execute(
        Adapter(), request(), fallback=lambda: "마스터 답변", confidence=0.4
    )

    assert result.result == "fallback"
    assert result.error_code == "low-confidence"
    assert result.text == "마스터 답변"
    assert called is False


# --- 실패 이유를 삼키지 않는다 (2026-09-08) ----------------------------------
#
# 콘솔에는 `adapter-error` 만, 로그에는 `error=BadRequestError` 만 남았다.
# 400 은 요청 형태가 틀렸다는 뜻이고 이유는 응답 본문에 있는데, 우리는 예외
# **종류**만 찍고 본문을 버렸다. 그래서 「모델 이름이 없는 값인가 / 입력이 너무
# 긴가」 를 가릴 수 없었다.
def test_the_error_code_says_which_kind_of_failure():
    """세 가지는 사람이 할 일이 완전히 다르다 — 이름을 고친다 / 키를 넣는다 / 재시도."""
    from tybot.gateway.cost import CostLimitExceeded
    from tybot.gateway.router import ModelNotAllowed, UnknownModel
    from tybot.specialist_contract import error_code_for

    assert error_code_for(UnknownModel("x")) == "unknown-model"
    assert error_code_for(ModelNotAllowed("x")) == "model-not-allowed"
    assert error_code_for(CostLimitExceeded("x")) == "cost-limit"
    assert error_code_for(RuntimeError("x")) == "adapter-error"


def test_a_failed_specialist_is_logged(caplog):
    """로그가 한 줄도 없어서 원인을 찾을 수 없었다. 그것이 실제로 겪은 고장이다."""
    from tybot.specialist_contract import (
        AuthorizedEvidence,
        SpecialistRequest,
        execute,
    )

    class Broken:
        def complete(self, request):
            raise RuntimeError("vendor said no")

    # 근거 없는 요청은 계약이 거부한다(전문가는 스스로 자료를 못 가져온다).
    request = SpecialistRequest(
        question="합계는?",
        evidence=(
            AuthorizedEvidence.from_acl_filter(
                workspace="tyit", text="기성금 3.2억", authorization_id="tyit:member"
            ),
        ),
    )

    with caplog.at_level("WARNING"):
        result = execute(Broken(), request, fallback=lambda: "마스터 답변")

    assert result.result == "fallback"
    assert "전문가 호출 실패" in caplog.text
    assert "vendor said no" in caplog.text, "이유가 로그에 없다"


def test_the_provider_reason_keeps_the_structural_part():
    """모델 이름·상태 코드·오류 종류는 조치를 가른다. 그건 반드시 남아야 한다."""
    from tybot.gateway.base import error_reason

    class Bad(Exception):
        status_code = 400

        def __init__(self, message: str) -> None:
            super().__init__(message)
            self.body = {"error": {"type": "invalid_request_error"}}

    line = error_reason(Bad("model: claude-opus-5"))

    assert "status=400" in line
    assert "invalid_request_error" in line
    assert "claude-opus-5" in line


def test_a_long_provider_message_is_cut():
    """400 중에는 문제가 된 필드를 되돌려 주는 것이 있고, 그 안에 근거 본문
    조각이 섞일 수 있다. 구조적 오류는 짧으니 앞부분만 남긴다."""
    from tybot.gateway.base import ERROR_REASON_LIMIT, error_reason

    line = error_reason(RuntimeError("가" * 5_000))

    assert len(line) < ERROR_REASON_LIMIT + 100
    assert line.endswith("…")
