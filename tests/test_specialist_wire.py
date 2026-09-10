"""전문 봇 2단계 와이어 계약 v2 (2026-09-10).

설계: `docs/design/specialist-runtime-v2.md` — 「필수 검증 / 단위·계약 테스트」

이 계약이 막는 것은 하나다. **전문 봇이 정할 수 있는 것은 문장뿐**이고, 출처·권한·
표시 형식은 마스터가 정한다. 그 선이 흐려지면 우리 권한 판정을 거치지 않은 내용에
우리 출처가 붙는다 — 권한 유출인데 출처가 위장돼 추적조차 안 되는 실패다.
"""
from __future__ import annotations

import json

import pytest

from tybot import specialist_wire as wire
from tybot.specialist_contract import AuthorizedEvidence


def _request(**kw) -> wire.RuntimeRequest:
    base = {
        "workspace": "tyit",
        "authorization_id": "tyit:member",
        "question": "3공구 기성금은?",
        "evidence": tuple(wire.evidence_from(["기성금 3.2억", "지급 완료"])),
        "request_id": "req-1",
    }
    base.update(kw)
    return wire.RuntimeRequest(**base)


def _response(request: wire.RuntimeRequest, **over) -> bytes:
    payload = {
        "schema": wire.SCHEMA_RESPONSE,
        "request_id": request.request_id,
        "text": "3공구 기성금은 3.2억입니다.",
        "version": "1.2.0",
        "contract_version": "v2",
        "evidence_ids": ["e1"],
        "usage": {"input_tokens": 10, "output_tokens": 5, "cost_usd": None},
    }
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


# --- 나가는 것 ----------------------------------------------------------------
def test_the_request_carries_only_question_and_evidence():
    """파일 경로·Slack URL·채널 ID·이메일은 나가지 않는다."""
    body = json.loads(_request().body())

    assert set(body) == {
        "schema", "request_id", "authorization_id", "workspace",
        "question", "evidence", "limits",
    }
    assert set(body["evidence"][0]) == {"id", "text"}


def test_evidence_ids_are_request_local():
    """채널 ID·메시지 ts 를 쓰면 전문 봇이 그것을 모아 우리 구조를 재구성한다."""
    items = wire.evidence_from(["가", "나", "다"])

    assert [e.id for e in items] == ["e1", "e2", "e3"]


def test_a_request_without_evidence_is_refused():
    """근거가 없으면 그쪽이 자기 색인이나 기억으로 답한다."""
    with pytest.raises(wire.WireViolation):
        _request(evidence=())


def test_mixed_authorization_scopes_are_refused():
    """A 권한 근거와 B 권한 근거가 한 프롬프트에 들어가면, 그 답이 누구에게
    보여도 되는지 아무도 말할 수 없다."""
    a = AuthorizedEvidence.from_acl_filter(
        workspace="tyit", text="가", authorization_id="tyit:member"
    )
    b = AuthorizedEvidence.from_acl_filter(
        workspace="tyit", text="나", authorization_id="tyit:exec"
    )

    with pytest.raises(wire.WireViolation):
        wire.request_from_authorized("질문", [a, b])


def test_mixed_workspaces_are_refused():
    a = AuthorizedEvidence.from_acl_filter(
        workspace="tyit", text="가", authorization_id="x"
    )
    b = AuthorizedEvidence.from_acl_filter(
        workspace="mgmt", text="나", authorization_id="x"
    )

    with pytest.raises(wire.WireViolation):
        wire.request_from_authorized("질문", [a, b])


def test_one_scope_builds_a_request():
    items = [
        AuthorizedEvidence.from_acl_filter(
            workspace="tyit", text=t, authorization_id="tyit:member"
        )
        for t in ("가", "나")
    ]

    got = wire.request_from_authorized("질문", items)

    assert got.workspace == "tyit"
    assert [e.text for e in got.evidence] == ["가", "나"]


def test_an_oversized_request_is_refused():
    """컨테이너 본문 상한과 같은 값이다. 갈리면 상대는 받아 놓고 우리가 버린다."""
    with pytest.raises(wire.WireViolation):
        _request(evidence=tuple(wire.evidence_from(["가" * 200_000]))).body()


# --- 들어오는 것 --------------------------------------------------------------
def test_a_good_response_passes():
    request = _request()

    got = wire.validate_response(
        _response(request), request=request, expected_version="1.2.0"
    )

    assert got.text.startswith("3공구")
    assert got.evidence_ids == ("e1",)


def test_a_response_that_attaches_its_own_source_is_discarded():
    """출처는 마스터만 붙인다. 그쪽이 붙이면 우리 권한 판정을 거치지 않은 출처가
    사용자에게 사실로 보인다."""
    request = _request()

    with pytest.raises(wire.WireViolation, match="출처"):
        wire.validate_response(
            _response(request, text="답입니다.\n출처: #팀-전산_ABB110"),
            request=request, expected_version="1.2.0",
        )


@pytest.mark.parametrize("bad", [
    "보세요 https://ty.slack.com/archives/C1/p123",
    "file:///var/lib/tybot/archive/x.md 참고",
    "<script>alert(1)</script>",
    "<iframe src=x>",
])
def test_links_and_markup_are_discarded(bad):
    request = _request()

    with pytest.raises(wire.WireViolation):
        wire.validate_response(
            _response(request, text=bad), request=request, expected_version="1.2.0"
        )


def test_an_unknown_evidence_id_is_discarded():
    """우리가 주지 않은 근거를 썼다 = 그쪽 색인이나 기억으로 답했다."""
    request = _request()

    with pytest.raises(wire.WireViolation, match="보내지 않은"):
        wire.validate_response(
            _response(request, evidence_ids=["e1", "e9"]),
            request=request, expected_version="1.2.0",
        )


def test_an_empty_evidence_list_is_discarded():
    """어떤 근거를 썼는지 밝히지 않으면 출처를 붙일 수 없다."""
    request = _request()

    with pytest.raises(wire.WireViolation):
        wire.validate_response(
            _response(request, evidence_ids=[]),
            request=request, expected_version="1.2.0",
        )


def test_an_oversized_answer_is_discarded():
    request = _request()

    with pytest.raises(wire.WireViolation):
        wire.validate_response(
            _response(request, text="가" * 25_000),
            request=request, expected_version="1.2.0",
        )


def test_a_version_mismatch_is_discarded():
    """승인된 digest 의 버전이 기준이다. 응답값으로 DB 를 갱신하지 않는다 —
    전문 봇이 운영 버전 기록을 바꿀 수 있으면 그게 곧 승인 우회다."""
    request = _request()

    with pytest.raises(wire.WireViolation, match="승인 버전"):
        wire.validate_response(
            _response(request, version="9.9.9"),
            request=request, expected_version="1.2.0",
        )


def test_a_response_for_another_request_is_discarded():
    """다르면 A 의 질문에 B 의 근거로 만든 답이 붙는다."""
    request = _request()

    with pytest.raises(wire.WireViolation, match="request_id"):
        wire.validate_response(
            _response(request, request_id="req-other"),
            request=request, expected_version="1.2.0",
        )


@pytest.mark.parametrize("raw", [
    b"not json",
    b"[]",
    b"{}",
    b'{"schema":"other/v1"}',
])
def test_malformed_responses_are_discarded(raw):
    request = _request()

    with pytest.raises(wire.WireViolation):
        wire.validate_response(raw, request=request, expected_version="")


def test_a_wrong_contract_version_is_discarded():
    request = _request()

    with pytest.raises(wire.WireViolation, match="contract_version"):
        wire.validate_response(
            _response(request, contract_version="v1"),
            request=request, expected_version="1.2.0",
        )


def test_control_characters_are_stripped():
    """제어문자를 두면 Slack·콘솔·로그에서 다르게 보이고, 금지 문자열 검사
    사이에 끼워 넣어 우회할 수도 있다."""
    request = _request()

    got = wire.validate_response(
        _response(request, text="답입\u200b니다"),
        request=request, expected_version="1.2.0",
    )

    assert "\u200b" not in got.text


def test_a_hidden_source_marker_does_not_slip_through():
    """제어문자를 걷어낸 **뒤에** 금지 문자열을 본다. 순서가 반대면 통과한다."""
    request = _request()

    with pytest.raises(wire.WireViolation, match="출처"):
        wire.validate_response(
            _response(request, text="답입니다. 출\u200b처: #채널"),
            request=request, expected_version="1.2.0",
        )


def test_an_unknown_error_code_is_folded():
    """상대 문자열을 그대로 통계에 넣으면 값이 무한히 늘고 내부 메시지가 섞인다."""
    assert wire.parse_error({"error": {"code": "overloaded"}}) == "overloaded"
    assert wire.parse_error({"error": {"code": "boom: /opt/x.js:12"}}) == "internal_error"
    assert wire.parse_error({}) == "internal_error"


# --- 서명 --------------------------------------------------------------------
def test_the_signature_covers_the_body():
    """본문 해시를 넣지 않으면 같은 서명으로 다른 본문을 보낼 수 있다."""
    secret = b"k" * 32
    one = wire.canonical(method="POST", path="/v1/complete", timestamp=1, nonce="n", body=b"a")
    two = wire.canonical(method="POST", path="/v1/complete", timestamp=1, nonce="n", body=b"b")

    assert wire.sign(secret, one) != wire.sign(secret, two)


def test_the_signature_covers_the_path():
    secret = b"k" * 32
    one = wire.canonical(method="POST", path="/v1/complete", timestamp=1, nonce="n", body=b"a")
    two = wire.canonical(method="POST", path="/v1/health", timestamp=1, nonce="n", body=b"a")

    assert wire.sign(secret, one) != wire.sign(secret, two)


def test_the_headers_carry_time_and_nonce():
    """둘 다 있어야 상대가 재생 공격을 걸러낼 수 있다."""
    headers = wire.signature_headers(
        b"k" * 32, method="POST", path="/v1/complete", body=b"{}", now=1_700_000_000
    )

    assert headers["X-TYBot-Timestamp"] == "1700000000"
    assert len(headers["X-TYBot-Nonce"]) >= 16
    assert len(headers["X-TYBot-Signature"]) == 64


def test_a_forged_response_signature_is_rejected():
    secret = b"k" * 32
    body = b'{"ok":true}'
    good = wire.sign(secret, f"req-1\n{__import__('hashlib').sha256(body).hexdigest()}".encode())

    assert wire.verify_response_signature(secret, request_id="req-1", body=body, provided=good)
    assert not wire.verify_response_signature(
        secret, request_id="req-1", body=body, provided="0" * 64
    )
    # 다른 요청의 서명을 재사용할 수 없다.
    assert not wire.verify_response_signature(
        secret, request_id="req-2", body=body, provided=good
    )


def test_signature_comparison_is_constant_time():
    """`==` 로 비교하면 일치하는 앞자리 수만큼 시간이 달라져, 서명을 한 바이트씩
    맞춰 갈 수 있다."""
    import inspect

    source = inspect.getsource(wire.verify_response_signature)

    assert "compare_digest" in source


# --- health -------------------------------------------------------------------
def test_health_requires_the_contract_version():
    assert wire.validate_health(
        b'{"status":"ok","version":"1.2.0","contract_version":"v2"}'
    )["version"] == "1.2.0"

    with pytest.raises(wire.WireViolation):
        wire.validate_health(b'{"status":"ok","version":"1.2.0","contract_version":"v1"}')


def test_a_degraded_health_is_not_ok():
    with pytest.raises(wire.WireViolation):
        wire.validate_health(b'{"status":"degraded","contract_version":"v2"}')


# --- 남기지 않는 것 -----------------------------------------------------------
def test_the_module_never_logs():
    """요청 본문과 응답 본문은 로그에 남기지 않는다(설계 §HTTP 와이어 계약).

    이 모듈은 질문과 근거를 **둘 다** 손에 쥐는 유일한 자리다. 여기서 한 줄만
    찍어도 아카이브 원문이 journald 로 나간다.
    """
    import inspect

    source = inspect.getsource(wire)

    for leaked in ("logging", "print(", "log.", "logger"):
        assert leaked not in source, f"와이어 계약이 로그를 만진다: {leaked}"
