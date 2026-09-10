"""전문 봇 2단계 HTTP 어댑터 — Unix socket 전송과 자동 차단.

설계: [`docs/design/specialist-runtime-v2.md`](../../docs/design/specialist-runtime-v2.md)
계약(요청 조립·서명·응답 검사)은 [`specialist_wire`](specialist_wire.py) 가 가진다.

## 전송을 갈라 둔 이유

`Transport` 프로토콜 하나만 두고 실제 소켓 구현을 그 뒤에 숨긴다. 그러면 계약
테스트가 소켓 없이 돌고(개발 PC 에는 `AF_UNIX` 가 없다), 나중에 전송이 바뀌어도
계약 코드는 그대로다.

## 재시도하지 않는다

4xx/5xx, JSON 오류, 버전 불일치, 시간 초과 — 전부 **한 번 실패하면 마스터**다.
재시도하면 사용자가 보낸 한 질문이 전문 봇에서 두 번 처리되고, 비용과 지연이
그만큼 는다. 그리고 전문 봇이 느려서 실패하는 상황에서 재시도는 정확히 그 상황을
악화시킨다.

## 자동 차단이 컨테이너를 멈추지 않는다

circuit 이 열리면 **라우팅만 닫는다.** 컨테이너 중지는 뒤따르는 정리이고, 사용자
입장에서 중요한 것은 「다음 질문이 마스터로 간다」 가 즉시 성립하는 것이다.
컨테이너를 먼저 멈추려 들면 그 과정이 실패했을 때 라우팅이 열린 채 남는다.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Protocol

from . import specialist_wire as wire

log = logging.getLogger("tybot.specialist_http")

# 자동 차단 기준. 설계 §헬스와 자동 차단 과 같은 값이다.
CONSECUTIVE_FAILURES = 3
VIOLATION_WINDOW_SECONDS = 300
VIOLATION_RATE = 0.20
# 이 표본 아래에서는 비율을 보지 않는다. 1건 중 1건 실패로 100% 를 만들면
# 첫 호출 한 번이 전문가를 통째로 끈다.
VIOLATION_MIN_SAMPLES = 5
# circuit 을 연 뒤 health 를 다시 보기까지. 열자마자 계속 찌르면 죽어 가는
# 컨테이너를 더 밀어붙인다.
PROBE_AFTER_SECONDS = 60


class TransportError(RuntimeError):
    """소켓·시간 초과·프로토콜 오류. 호출부는 마스터로 간다."""


@dataclass(frozen=True)
class HttpReply:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)


class Transport(Protocol):
    """한 전문 봇에 대한 전송. 실패는 `TransportError` 로 올린다."""

    def post(
        self, path: str, body: bytes, headers: dict[str, str], *, timeout_s: float
    ) -> HttpReply: ...


class UnixSocketTransport:
    """`/run/tybot-subbots/<key>/http.sock` 위의 HTTP/1.1.

    **경로를 사람이 입력하지 않는다.** key 에서 코드가 계산한다 — 입력받으면
    그 값이 곧 임의 소켓 호출 권한이 된다.
    """

    def __init__(self, socket_path: str) -> None:
        self._path = socket_path

    def post(
        self, path: str, body: bytes, headers: dict[str, str], *, timeout_s: float
    ) -> HttpReply:
        import socket

        if not hasattr(socket, "AF_UNIX"):  # pragma: no cover - 리눅스 전용 경로
            raise TransportError("이 플랫폼에는 Unix domain socket 이 없습니다.")
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout_s)
                sock.connect(self._path)
                sock.sendall(_http_request(path, body, headers))
                raw = _read_all(sock, timeout_s)
        except TimeoutError as exc:
            raise TransportError("전문 봇이 시간 안에 답하지 않았습니다.") from exc
        except OSError as exc:
            # 오류 문자열에 소켓 경로가 들어가지 않게 종류만 남긴다.
            raise TransportError(f"소켓 오류: {type(exc).__name__}") from exc
        return _parse_http(raw)


def socket_path(key: str) -> str:
    """전문가 key → 소켓 경로. **DB 나 업로드에서 받지 않는다.**"""
    if not key or not key.replace("-", "").isalnum():
        raise TransportError(f"전문가 key 가 올바르지 않습니다: {key!r}")
    return f"/run/tybot-subbots/{key}/http.sock"


def _http_request(path: str, body: bytes, headers: dict[str, str]) -> bytes:
    lines = [f"POST {path} HTTP/1.1", "Host: tybot", "Connection: close",
             f"Content-Length: {len(body)}"]
    lines += [f"{k}: {v}" for k, v in headers.items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body


def _read_all(sock, timeout_s: float) -> bytes:
    """응답 전체. **상한을 두고 읽는다** — 두지 않으면 상대가 무한히 흘려보내
    우리 메모리를 채울 수 있다."""
    deadline = time.monotonic() + timeout_s
    chunks: list[bytes] = []
    total = 0
    # 헤더 몫을 넉넉히 더한다. 본문 상한은 계약이 다시 본다.
    cap = wire.MAX_RESPONSE_BYTES + 8192
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError
        chunk = sock.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > cap:
            raise TransportError("전문 봇 응답이 상한을 넘습니다.")
    return b"".join(chunks)


def _parse_http(raw: bytes) -> HttpReply:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1", errors="replace").split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        raise TransportError("HTTP 응답이 아닙니다.")
    try:
        status = int(lines[0].split()[1])
    except (IndexError, ValueError) as exc:
        raise TransportError("HTTP 상태 줄을 읽지 못했습니다.") from exc
    headers = {}
    for line in lines[1:]:
        key, sep, value = line.partition(":")
        if sep:
            headers[key.strip().lower()] = value.strip()
    return HttpReply(status=status, body=body, headers=headers)


# --- 자동 차단 ----------------------------------------------------------------
@dataclass
class CircuitBreaker:
    """전문가 하나의 상태. **라우팅만 닫는다** — 컨테이너는 건드리지 않는다.

    두 가지를 본다.

    1. **연속 전송 실패** — 소켓이 없거나 프로세스가 죽었다. 비율로 보면 느리다.
    2. **최근 계약 위반률** — 도는데 잘못 답한다. 연속으로 보면 못 잡는다
       (성공과 위반이 섞여 오기 때문이다).
    """

    consecutive_failures: int = 0
    opened_at: float | None = None
    # (시각, 위반이었나). 창 밖은 버린다.
    _recent: deque = field(default_factory=lambda: deque(maxlen=200))

    def allows(self, *, now: float | None = None) -> bool:
        """지금 전문가를 불러도 되는가."""
        if self.opened_at is None:
            return True
        clock = time.monotonic() if now is None else now
        # 열린 뒤에도 시간이 지나면 한 번은 찔러 본다. 영원히 닫아 두면
        # 사람이 콘솔을 볼 때까지 복구되지 않는다.
        return clock - self.opened_at >= PROBE_AFTER_SECONDS

    @property
    def open(self) -> bool:
        return self.opened_at is not None

    def record(self, *, ok: bool, violation: bool = False, now: float | None = None) -> None:
        clock = time.monotonic() if now is None else now
        self._recent.append((clock, violation))
        if ok:
            self.consecutive_failures = 0
            # **성공 하나로 닫지 않는다.** 열린 circuit 은 사람이 되돌린다
            # (설계: 자동 복구는 standby 까지, enabled 복귀는 관리자 동작).
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= CONSECUTIVE_FAILURES:
            self._open(clock, "consecutive-failures")
            return
        if self._violation_rate(clock) >= VIOLATION_RATE:
            self._open(clock, "violation-rate")

    def _violation_rate(self, clock: float) -> float:
        window = [v for at, v in self._recent if clock - at <= VIOLATION_WINDOW_SECONDS]
        if len(window) < VIOLATION_MIN_SAMPLES:
            return 0.0
        return sum(1 for v in window if v) / len(window)

    def _open(self, clock: float, why: str) -> None:
        if self.opened_at is None:
            log.warning("전문가 라우팅을 닫습니다 (%s)", why)
        self.opened_at = clock

    def reset(self) -> None:
        """관리자가 다시 켤 때만 부른다."""
        self.opened_at = None
        self.consecutive_failures = 0
        self._recent.clear()


# --- 호출 --------------------------------------------------------------------
@dataclass(frozen=True)
class CallOutcome:
    """한 번의 호출 결과. `answer` 가 없으면 마스터가 답한다."""

    answer: wire.RuntimeAnswer | None
    error_code: str = ""
    http_status: int | None = None
    elapsed_ms: int = 0

    @property
    def ok(self) -> bool:
        return self.answer is not None


def call(
    transport: Transport,
    *,
    request: wire.RuntimeRequest,
    secret: bytes,
    expected_version: str,
    breaker: CircuitBreaker | None = None,
    now: float | None = None,
) -> CallOutcome:
    """전문 봇 한 번 호출. **예외를 밖으로 내지 않는다.**

    전문 봇 장애 때문에 Slack 답변 전체가 실패해서는 안 된다(설계 §실패와 롤백).
    실패는 전부 `error_code` 가 붙은 결과로 돌아오고, 호출부는 마스터로 간다.
    """
    if breaker is not None and not breaker.allows(now=now):
        return CallOutcome(None, error_code="circuit-open")

    started = time.monotonic()
    try:
        body = request.body()
    except wire.WireViolation as exc:
        # 우리가 만든 요청이 계약을 어겼다. 보내지 않는다.
        log.warning("전문가 요청을 만들지 못했습니다: %s", exc)
        return CallOutcome(None, error_code="bad-request")

    headers = wire.signature_headers(
        secret, method="POST", path=wire.COMPLETE_PATH, body=body
    )
    timeout_s = request.deadline_ms / 1000

    try:
        reply = transport.post(
            wire.COMPLETE_PATH, body, headers, timeout_s=timeout_s
        )
    except TransportError as exc:
        elapsed = _ms(started)
        code = "timeout" if "시간" in str(exc) else "transport"
        log.warning("전문가 전송 실패 code=%s", code)
        if breaker is not None:
            breaker.record(ok=False, now=now)
        return CallOutcome(None, error_code=code, elapsed_ms=elapsed)

    elapsed = _ms(started)

    if reply.status != 200:
        # 오류 본문은 허용 코드로 접는다. 상대 문자열을 그대로 쓰지 않는다.
        code = _error_code(reply)
        if breaker is not None:
            breaker.record(ok=False, now=now)
        return CallOutcome(
            None, error_code=code, http_status=reply.status, elapsed_ms=elapsed
        )

    # **응답 서명을 먼저 본다.** 본문을 파싱하기 전에 확인해야, 위조된 본문을
    # 우리 파서에 통과시키는 일이 없다.
    signature = reply.headers.get("x-tybot-signature", "")
    if not wire.verify_response_signature(
        secret, request_id=request.request_id, body=reply.body, provided=signature
    ):
        log.warning("전문가 응답 서명이 맞지 않습니다")
        if breaker is not None:
            breaker.record(ok=False, violation=True, now=now)
        return CallOutcome(
            None, error_code="bad-signature", http_status=200, elapsed_ms=elapsed
        )

    try:
        answer = wire.validate_response(
            reply.body, request=request, expected_version=expected_version
        )
    except wire.WireViolation as exc:
        # **위반 사유는 남기되 본문은 남기지 않는다.** 사유 문구는 우리가 쓴 것이다.
        log.warning("전문가 응답 계약 위반: %s", exc)
        if breaker is not None:
            breaker.record(ok=False, violation=True, now=now)
        return CallOutcome(
            None, error_code="invalid-output", http_status=200, elapsed_ms=elapsed
        )

    if breaker is not None:
        breaker.record(ok=True, now=now)
    return CallOutcome(answer, http_status=200, elapsed_ms=elapsed)


def health(
    transport: Transport, *, secret: bytes, now: float | None = None
) -> tuple[bool, str]:
    """`(정상인가, 버전)`. 모델을 부르지 않고 2초 안에 와야 한다."""
    body = b"{}"
    headers = wire.signature_headers(
        secret, method="POST", path=wire.HEALTH_PATH, body=body
    )
    try:
        reply = transport.post(
            wire.HEALTH_PATH, body, headers,
            timeout_s=wire.HEALTH_DEADLINE_MS / 1000,
        )
    except TransportError:
        return False, ""
    if reply.status != 200:
        return False, ""
    try:
        got = wire.validate_health(reply.body)
    except wire.WireViolation:
        return False, ""
    return True, got["version"]


def _error_code(reply: HttpReply) -> str:
    import json

    try:
        payload = json.loads(reply.body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return "internal_error"
    if not isinstance(payload, dict):
        return "internal_error"
    return wire.parse_error(payload)


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


__all__ = [
    "CONSECUTIVE_FAILURES",
    "PROBE_AFTER_SECONDS",
    "VIOLATION_MIN_SAMPLES",
    "VIOLATION_RATE",
    "VIOLATION_WINDOW_SECONDS",
    "CallOutcome",
    "CircuitBreaker",
    "HttpReply",
    "Transport",
    "TransportError",
    "UnixSocketTransport",
    "call",
    "health",
    "socket_path",
]
