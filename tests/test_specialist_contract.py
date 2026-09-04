import time

import pytest

from tybot.specialist_contract import (
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
    assert result.error_code == "timeout"


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
