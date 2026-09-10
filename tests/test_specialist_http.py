"""전문 봇 2단계 HTTP 어댑터 — 전송 실패와 자동 차단 (2026-09-10).

설계: `docs/design/specialist-runtime-v2.md` 구현 순서 2단계.

지키는 것 하나. **전문 봇 장애 때문에 Slack 답변 전체가 실패해서는 안 된다.**
그래서 여기 있는 모든 실패 경로는 예외가 아니라 「마스터가 답한다」 로 끝난다.

개발 PC 에는 `AF_UNIX` 가 없다. 그래서 전송을 프로토콜로 갈라 두고, 계약과 차단
로직은 가짜 전송으로 전부 고정한다 — 실제 소켓 검사는 Rocky staging 통합
테스트 몫이다(설계 §필수 검증).
"""
from __future__ import annotations

import hashlib
import json

import pytest

from tybot import specialist_http as http
from tybot import specialist_wire as wire

SECRET = b"k" * 32


def _request(**kw) -> wire.RuntimeRequest:
    base = {
        "workspace": "tyit",
        "authorization_id": "tyit:member",
        "question": "기성금은?",
        "evidence": tuple(wire.evidence_from(["기성금 3.2억"])),
        "request_id": "req-1",
    }
    base.update(kw)
    return wire.RuntimeRequest(**base)


def _signed(request_id: str, body: bytes) -> dict[str, str]:
    mac = wire.sign(
        SECRET, f"{request_id}\n{hashlib.sha256(body).hexdigest()}".encode()
    )
    return {"x-tybot-signature": mac}


def _good_body(request: wire.RuntimeRequest, **over) -> bytes:
    payload = {
        "schema": wire.SCHEMA_RESPONSE,
        "request_id": request.request_id,
        "text": "3.2억입니다.",
        "version": "1.2.0",
        "contract_version": "v2",
        "evidence_ids": ["e1"],
    }
    payload.update(over)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


class Fake:
    """가짜 전송. 실제 소켓 없이 계약과 차단을 전부 돌린다."""

    def __init__(self, *replies, boom: Exception | None = None):
        self.replies = list(replies)
        self.boom = boom
        self.calls: list[tuple[str, bytes, dict]] = []

    def post(self, path, body, headers, *, timeout_s):
        self.calls.append((path, body, headers))
        if self.boom is not None:
            raise self.boom
        return self.replies.pop(0) if self.replies else self.replies[-1]


def _reply(request, *, status=200, body=None, sign_it=True, **over):
    raw = _good_body(request, **over) if body is None else body
    headers = _signed(request.request_id, raw) if sign_it else {}
    return http.HttpReply(status=status, body=raw, headers=headers)


# --- 잘 되는 길 ---------------------------------------------------------------
def test_a_good_call_returns_the_answer():
    request = _request()
    transport = Fake(_reply(request))

    got = http.call(
        transport, request=request, secret=SECRET, expected_version="1.2.0"
    )

    assert got.ok
    assert got.answer.text == "3.2억입니다."
    assert got.http_status == 200


def test_the_request_is_signed():
    request = _request()
    transport = Fake(_reply(request))

    http.call(transport, request=request, secret=SECRET, expected_version="1.2.0")

    _, body, headers = transport.calls[0]
    expected = wire.sign(SECRET, wire.canonical(
        method="POST", path=wire.COMPLETE_PATH,
        timestamp=int(headers["X-TYBot-Timestamp"]),
        nonce=headers["X-TYBot-Nonce"], body=body,
    ))
    assert headers["X-TYBot-Signature"] == expected


# --- 실패는 전부 마스터로 -----------------------------------------------------
def test_a_transport_error_falls_back():
    """소켓이 없거나 프로세스가 죽었다. 예외가 밖으로 나가면 Slack 답변이 죽는다."""
    request = _request()
    transport = Fake(boom=http.TransportError("소켓 오류: FileNotFoundError"))

    got = http.call(
        transport, request=request, secret=SECRET, expected_version="1.2.0"
    )

    assert not got.ok
    assert got.error_code == "transport"


def test_a_timeout_falls_back_with_its_own_code():
    """시간 초과와 소켓 없음은 사람이 볼 곳이 다르다."""
    request = _request()
    transport = Fake(boom=http.TransportError("전문 봇이 시간 안에 답하지 않았습니다."))

    got = http.call(
        transport, request=request, secret=SECRET, expected_version="1.2.0"
    )

    assert got.error_code == "timeout"


@pytest.mark.parametrize("status", [400, 401, 429, 500, 503])
def test_non_200_falls_back(status):
    request = _request()
    transport = Fake(_reply(
        request, status=status, body=b'{"error":{"code":"overloaded"}}'
    ))

    got = http.call(
        transport, request=request, secret=SECRET, expected_version="1.2.0"
    )

    assert not got.ok
    assert got.http_status == status
    assert got.error_code == "overloaded"


def test_an_unknown_error_code_is_folded():
    """상대 문자열을 그대로 통계에 넣으면 값이 무한히 늘고 내부 메시지가 섞인다."""
    request = _request()
    transport = Fake(_reply(
        request, status=500,
        body=b'{"error":{"code":"TypeError at /opt/app/server.js:41"}}',
    ))

    got = http.call(
        transport, request=request, secret=SECRET, expected_version="1.2.0"
    )

    assert got.error_code == "internal_error"


def test_a_contract_violation_falls_back():
    request = _request()
    transport = Fake(_reply(request, text="답입니다.\n출처: #채널"))

    got = http.call(
        transport, request=request, secret=SECRET, expected_version="1.2.0"
    )

    assert got.error_code == "invalid-output"


def test_a_version_mismatch_falls_back():
    request = _request()
    transport = Fake(_reply(request, version="9.9.9"))

    got = http.call(
        transport, request=request, secret=SECRET, expected_version="1.2.0"
    )

    assert got.error_code == "invalid-output"


def test_an_unsigned_response_is_rejected_before_parsing():
    """서명을 **먼저** 본다. 나중에 보면 위조된 본문이 우리 파서를 통과한다."""
    request = _request()
    transport = Fake(_reply(request, sign_it=False))

    got = http.call(
        transport, request=request, secret=SECRET, expected_version="1.2.0"
    )

    assert got.error_code == "bad-signature"


def test_the_signature_is_checked_before_the_body():
    import inspect

    source = inspect.getsource(http.call)
    at_signature = source.index("verify_response_signature")
    at_validate = source.index("validate_response")

    assert at_signature < at_validate, "본문을 먼저 파싱한다"


def test_it_never_retries():
    """재시도하면 한 질문이 전문 봇에서 두 번 처리된다 — 느려서 실패하는
    상황을 정확히 악화시킨다."""
    request = _request()
    transport = Fake(_reply(request, status=503, body=b"{}"))

    http.call(transport, request=request, secret=SECRET, expected_version="1.2.0")

    assert len(transport.calls) == 1


# --- 자동 차단 ----------------------------------------------------------------
def test_three_consecutive_failures_open_the_circuit():
    breaker = http.CircuitBreaker()

    for i in range(http.CONSECUTIVE_FAILURES):
        assert not breaker.open, f"{i}번째에 이미 열렸다"
        breaker.record(ok=False, now=100 + i)

    assert breaker.open
    assert not breaker.allows(now=100)


def test_a_success_resets_the_streak_but_does_not_close_it():
    """열린 circuit 은 사람이 되돌린다. 자동 복구는 standby 까지만이다."""
    breaker = http.CircuitBreaker()
    for i in range(http.CONSECUTIVE_FAILURES):
        breaker.record(ok=False, now=100 + i)

    breaker.record(ok=True, now=110)

    assert breaker.consecutive_failures == 0
    assert breaker.open, "성공 하나로 닫혔다"


def test_a_violation_rate_opens_the_circuit_even_when_calls_succeed_between():
    """도는데 잘못 답하는 경우다. 연속 실패만 보면 성공이 사이에 섞여 못 잡는다."""
    breaker = http.CircuitBreaker()
    now = 1000.0
    for i in range(8):
        breaker.record(ok=(i % 2 == 0), violation=(i % 2 == 1), now=now + i)

    assert breaker.open


def test_one_early_failure_does_not_kill_the_specialist():
    """1건 중 1건 실패로 100% 를 만들면 첫 호출 한 번이 전문가를 통째로 끈다."""
    breaker = http.CircuitBreaker()

    breaker.record(ok=False, violation=True, now=1000)

    assert not breaker.open


def test_old_violations_leave_the_window():
    """창 밖 위반이 계속 세이면 어제 고친 고장으로 오늘 라우팅이 닫힌다."""
    breaker = http.CircuitBreaker()
    for i in range(8):
        breaker.record(ok=False, violation=True, now=1000 + i)
    breaker.reset()

    # 오래된 위반 뒤에 최근 성공만 있다.
    for i in range(8):
        breaker.record(ok=True, now=1000 + i)
    breaker.record(ok=False, violation=True, now=1000 + wire.CLOCK_SKEW_SECONDS + 400)

    assert not breaker.open


def test_an_open_circuit_probes_again_later():
    """영원히 닫아 두면 사람이 콘솔을 볼 때까지 복구되지 않는다."""
    breaker = http.CircuitBreaker()
    for i in range(http.CONSECUTIVE_FAILURES):
        breaker.record(ok=False, now=100 + i)

    # 기준은 **열린 시각**이다. 세 번째 실패에서 열렸으므로 100 이 아니다.
    opened = breaker.opened_at
    assert not breaker.allows(now=opened + http.PROBE_AFTER_SECONDS - 1)
    assert breaker.allows(now=opened + http.PROBE_AFTER_SECONDS)


def test_an_open_circuit_skips_the_call_entirely():
    """circuit 이 열리면 **부르지 않고** 마스터로 간다. 라우팅만 닫는 것이 핵심 —
    컨테이너 중지를 기다리면 그 사이 요청이 계속 그쪽으로 간다."""
    request = _request()
    transport = Fake(_reply(request))
    breaker = http.CircuitBreaker()
    for i in range(http.CONSECUTIVE_FAILURES):
        breaker.record(ok=False, now=100 + i)

    got = http.call(
        transport, request=request, secret=SECRET,
        expected_version="1.2.0", breaker=breaker, now=100,
    )

    assert got.error_code == "circuit-open"
    assert transport.calls == [], "닫힌 circuit 인데 호출했다"


def test_reset_is_the_only_way_back():
    breaker = http.CircuitBreaker()
    for i in range(http.CONSECUTIVE_FAILURES):
        breaker.record(ok=False, now=100 + i)

    breaker.reset()

    assert not breaker.open


# --- health -------------------------------------------------------------------
def test_health_reports_the_running_version():
    body = b'{"status":"ok","version":"1.2.0","contract_version":"v2"}'
    transport = Fake(http.HttpReply(200, body))

    ok, version = http.health(transport, secret=SECRET)

    assert ok and version == "1.2.0"


def test_health_failure_is_not_an_exception():
    transport = Fake(boom=http.TransportError("소켓 오류: ConnectionRefusedError"))

    ok, version = http.health(transport, secret=SECRET)

    assert not ok and version == ""


def test_health_uses_a_short_deadline():
    """모델을 부르지 않는 경로다. 15초를 주면 죽은 컨테이너를 15초씩 기다린다."""
    captured = {}

    class Timed(Fake):
        def post(self, path, body, headers, *, timeout_s):
            captured["timeout"] = timeout_s
            return http.HttpReply(
                200, b'{"status":"ok","version":"1","contract_version":"v2"}'
            )

    http.health(Timed(), secret=SECRET)

    assert captured["timeout"] <= 2.0


# --- 경로와 로그 --------------------------------------------------------------
def test_the_socket_path_is_derived_not_supplied():
    """경로를 입력받으면 그 값이 곧 임의 소켓 호출 권한이 된다."""
    assert http.socket_path("hermes") == "/run/tybot-subbots/hermes/http.sock"

    for bad in ("../etc/passwd", "a/b", "", "he mes"):
        with pytest.raises(http.TransportError):
            http.socket_path(bad)


def test_the_logs_carry_no_question_or_answer(caplog):
    """journald 에는 request ID 와 오류 코드만 남긴다(설계 §런타임 격리)."""
    request = _request(question="김해외동 기성금은 얼마인가")
    transport = Fake(_reply(request, text="비밀 금액 3.2억"))

    with caplog.at_level("WARNING"):
        http.call(
            transport, request=request, secret=SECRET, expected_version="9.9.9"
        )

    assert "김해외동" not in caplog.text
    assert "3.2억" not in caplog.text
