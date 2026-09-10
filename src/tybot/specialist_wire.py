"""전문 봇 2단계 HTTP 와이어 계약 v2 — 요청 조립 · 서명 · 응답 검사.

설계: [`docs/design/specialist-runtime-v2.md`](../../docs/design/specialist-runtime-v2.md)

## 왜 순수 모듈인가

전송(Unix socket)과 계약(무엇을 보내고 무엇을 받아들이는가)을 갈라 둔다.
계약이 전송에 묶여 있으면 **소켓 없이는 한 줄도 검증할 수 없고**, 그러면 검증은
운영에서 처음 돌아간다. 여기 있는 것은 전부 함수라 소켓 없는 개발 PC 에서도
모든 경우를 고정할 수 있다.

## 이 모듈이 지키는 것

**나가는 것**: 질문과 근거 텍스트뿐이다. 파일 경로·Slack URL·채널 ID·사용자
이메일·토큰은 넣지 않는다. 근거는 한 워크스페이스, 한 `authorization_id` 에서만
온다 — 섞이면 권한 판정이 무의미해진다.

**들어오는 것**: 전문 봇이 정할 수 있는 것은 **문장뿐**이다. 출처·권한·표시 형식은
마스터가 정한다. 그래서 응답에 `출처:`·Slack URL·`file://`·HTML 이 있으면 계약
위반으로 폐기한다 — 고쳐 쓰지 않는다. 고쳐 쓰기 시작하면 무엇이 계약인지가
코드마다 달라진다.

**버전**: 응답의 `version` 은 **관찰값**이다. 승인된 digest 의 버전과 다르면 폐기하고
마스터로 간다. 전문 봇이 스스로 운영 버전 기록을 바꿀 수 있으면 그게 곧 승인 우회다.

## 재시도하지 않는다

4xx/5xx·JSON 오류·버전 불일치·시간 초과는 전부 마스터 폴백이다. 재시도하면
사용자가 보낸 한 질문이 전문 봇에서 두 번 처리되고, 비용과 지연이 그만큼 는다.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

SCHEMA_REQUEST = "tybot.specialist.request/v2"
SCHEMA_RESPONSE = "tybot.specialist.response/v2"
CONTRACT_VERSION = "v2"

HEALTH_PATH = "/v1/health"
COMPLETE_PATH = "/v1/complete"

# 상한. 설계표와 같은 값이다 — 갈리면 컨테이너는 받아 놓고 우리가 버리는,
# 아무도 이유를 모르는 실패가 생긴다.
MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
MAX_OUTPUT_CHARS = 20_000
DEADLINE_MS = 15_000
HEALTH_DEADLINE_MS = 2_000

# HMAC 허용 시계 오차. 넓히면 탈취한 서명의 수명이 그만큼 는다.
CLOCK_SKEW_SECONDS = 30

# 전문 봇이 돌려줄 수 있는 오류 코드. **열린 문자열을 받지 않는다** —
# 받으면 그 문자열이 로그·화면·통계로 흘러 들어가고, 그 안에 내부 메시지가 섞인다.
ERROR_CODES = frozenset({
    "bad_request",
    "unauthorized",
    "unsupported_contract",
    "overloaded",
    "internal_error",
    "upstream_error",
})

# 응답 본문에 있으면 폐기하는 것들.
#
# `출처:` — 출처는 마스터만 붙인다. 전문 봇이 붙이면 그 출처가 우리 권한 판정을
#   거치지 않은 채 사용자에게 사실로 보인다.
# Slack archive URL·`file://` — 우리 저장소 위치가 그쪽 손에서 나온다는 뜻이다.
# `<script`·`<iframe` — Slack 은 렌더하지 않지만 콘솔·캔버스로 흘러갈 수 있다.
_FORBIDDEN = (
    ("출처:", "출처는 마스터 봇만 부착할 수 있습니다."),
    ("slack.com/archives/", "전문 봇 응답에 Slack 링크가 있습니다."),
    ("file://", "전문 봇 응답에 파일 경로가 있습니다."),
    ("<script", "전문 봇 응답에 스크립트가 있습니다."),
    ("<iframe", "전문 봇 응답에 프레임이 있습니다."),
)

_EVIDENCE_ID_RE = re.compile(r"^e[0-9]{1,4}$")


class WireViolation(RuntimeError):
    """계약 위반. 호출부는 응답을 버리고 마스터로 간다."""


@dataclass(frozen=True)
class Evidence:
    """전문 봇에 보내는 근거 한 조각.

    `id` 는 **이 요청 안에서만** 유효한 임의 식별자다. 채널 ID·파일 경로·메시지 ts
    같은 우리 식별자를 쓰면, 전문 봇이 그것을 모아 우리 구조를 재구성할 수 있다.
    """

    id: str
    text: str


def evidence_from(texts: list[str]) -> list[Evidence]:
    """근거 텍스트 → 요청 지역 ID 를 붙인 목록. 빈 줄은 버린다."""
    return [
        Evidence(id=f"e{i + 1}", text=text)
        for i, text in enumerate(t for t in texts if t and t.strip())
    ]


@dataclass(frozen=True)
class RuntimeRequest:
    """v2 요청. `body()` 가 그대로 전송된다."""

    workspace: str
    authorization_id: str
    question: str
    evidence: tuple[Evidence, ...]
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    deadline_ms: int = DEADLINE_MS
    max_output_chars: int = MAX_OUTPUT_CHARS

    def __post_init__(self) -> None:
        if not self.workspace.strip() or not self.authorization_id.strip():
            raise WireViolation("워크스페이스와 권한 판정 식별자가 필요합니다.")
        if not self.question.strip():
            raise WireViolation("빈 질문은 전문 봇에 보내지 않습니다.")
        # 근거 없는 호출을 막는다. 전문 봇은 **스스로 자료를 가져오지 않는다** —
        # 근거가 없으면 그쪽이 자기 색인이나 기억으로 답하게 된다.
        if not self.evidence:
            raise WireViolation("근거 없는 요청은 전문 봇에 보내지 않습니다.")
        if len({e.id for e in self.evidence}) != len(self.evidence):
            raise WireViolation("근거 식별자가 중복됩니다.")

    def body(self) -> bytes:
        """전송할 JSON. **직렬화를 한 곳에 둔다** — 서명 대상과 전송 본문이
        갈리면 서명은 맞는데 상대가 다른 것을 읽는 상태가 된다."""
        payload = {
            "schema": SCHEMA_REQUEST,
            "request_id": self.request_id,
            "authorization_id": self.authorization_id,
            "workspace": self.workspace,
            "question": self.question,
            "evidence": [{"id": e.id, "text": e.text} for e in self.evidence],
            "limits": {
                "deadline_ms": self.deadline_ms,
                "max_output_chars": self.max_output_chars,
            },
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        if len(raw) > MAX_REQUEST_BYTES:
            raise WireViolation(
                f"요청 본문이 상한을 넘습니다({len(raw)} > {MAX_REQUEST_BYTES})."
            )
        return raw

    @property
    def evidence_ids(self) -> frozenset[str]:
        return frozenset(e.id for e in self.evidence)


def request_from_authorized(
    question: str,
    authorized,
    *,
    deadline_ms: int = DEADLINE_MS,
) -> RuntimeRequest:
    """`AuthorizedEvidence` 목록 → v2 요청.

    **한 요청의 근거는 한 워크스페이스, 한 `authorization_id` 에서만 온다.**
    섞이면 권한 판정이 무의미해진다 — A 권한으로 판정한 근거와 B 권한으로 판정한
    근거가 한 프롬프트에 들어가면, 그 답이 누구에게 보여도 되는지 아무도 말할 수 없다.
    그리고 나중에 「무엇이 그쪽에 갔나」 를 되짚을 때 기준이 둘이 된다.
    """
    items = list(authorized)
    if not items:
        raise WireViolation("근거 없는 요청은 전문 봇에 보내지 않습니다.")
    scopes = {(a.workspace, a.authorization_id) for a in items}
    if len(scopes) != 1:
        raise WireViolation(
            "서로 다른 권한 범위의 근거를 한 요청에 섞을 수 없습니다."
        )
    workspace, authorization_id = next(iter(scopes))
    return RuntimeRequest(
        workspace=workspace,
        authorization_id=authorization_id,
        question=question,
        evidence=tuple(evidence_from([a.text for a in items])),
        deadline_ms=deadline_ms,
    )


# --- 서명 --------------------------------------------------------------------
#
# Unix socket 권한이 1차 인증이다. HMAC 은 **같은 호스트의 다른 프로세스**가
# 전문 봇을 부르는 것을 막는 2차 장치다. 소켓 권한만 믿으면, 그 사용자로 도는
# 아무 프로세스나 근거를 넣어 호출할 수 있다.
def canonical(*, method: str, path: str, timestamp: int, nonce: str, body: bytes) -> bytes:
    """서명 대상. **본문 해시까지 넣는다** — 넣지 않으면 같은 서명으로 다른 본문을
    보낼 수 있고, 그건 서명이 아니라 통행증이다."""
    digest = hashlib.sha256(body).hexdigest()
    return "\n".join([
        method.upper(), path, str(timestamp), nonce, digest,
    ]).encode("utf-8")


def sign(secret: bytes, message: bytes) -> str:
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def signature_headers(
    secret: bytes,
    *,
    method: str,
    path: str,
    body: bytes,
    now: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    """요청 헤더. 시각과 nonce 를 함께 보내야 상대가 재생 공격을 걸러낼 수 있다."""
    stamp = int(datetime.now(UTC).timestamp()) if now is None else now
    token = nonce or secrets.token_hex(16)
    mac = sign(secret, canonical(
        method=method, path=path, timestamp=stamp, nonce=token, body=body
    ))
    return {
        "X-TYBot-Timestamp": str(stamp),
        "X-TYBot-Nonce": token,
        "X-TYBot-Signature": mac,
        "Content-Type": "application/json; charset=utf-8",
    }


def verify_response_signature(
    secret: bytes, *, request_id: str, body: bytes, provided: str
) -> bool:
    """응답 서명 확인. 상대가 우리 요청에 답했다는 것까지 묶는다.

    **`compare_digest` 를 쓴다.** `==` 로 비교하면 일치하는 앞자리 수만큼 시간이
    달라져, 서명을 한 바이트씩 맞춰 갈 수 있다.
    """
    expected = sign(secret, f"{request_id}\n{hashlib.sha256(body).hexdigest()}".encode())
    return hmac.compare_digest(expected, (provided or "").strip())


# --- 응답 검사 ----------------------------------------------------------------
@dataclass(frozen=True)
class RuntimeAnswer:
    """검사를 통과한 전문 봇 응답. 출처는 아직 없다 — 마스터가 붙인다."""

    text: str
    version: str
    evidence_ids: tuple[str, ...]
    input_tokens: int = 0
    output_tokens: int = 0


def parse_error(payload: dict) -> str:
    """오류 응답 → 허용 코드. 모르는 코드는 `internal_error` 로 접는다.

    상대가 준 문자열을 그대로 통계에 넣으면 그 값이 무한히 늘어나 표가 못 쓰게
    되고, 내부 메시지가 섞여 들어온다.
    """
    error = payload.get("error")
    code = str((error or {}).get("code") or "").strip() if isinstance(error, dict) else ""
    return code if code in ERROR_CODES else "internal_error"


def validate_response(
    raw: bytes,
    *,
    request: RuntimeRequest,
    expected_version: str,
) -> RuntimeAnswer:
    """엄격 검사. 하나라도 어긋나면 `WireViolation` 이고 호출부는 마스터로 간다.

    관대하게 받지 않는다 — 모르는 필드를 무시하고 넘기면, 계약이 무엇인지가
    양쪽 코드에서 서서히 갈라진다.
    """
    if len(raw) > MAX_RESPONSE_BYTES:
        raise WireViolation(f"응답이 상한을 넘습니다({len(raw)} > {MAX_RESPONSE_BYTES}).")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise WireViolation("응답이 UTF-8 이 아닙니다.") from exc
    except json.JSONDecodeError as exc:
        raise WireViolation("응답이 JSON 이 아닙니다.") from exc
    if not isinstance(payload, dict):
        raise WireViolation("응답이 객체가 아닙니다.")

    if payload.get("schema") != SCHEMA_RESPONSE:
        raise WireViolation(f"응답 schema 가 다릅니다: {payload.get('schema')!r}")
    # **요청 ID 를 대조한다.** 다르면 다른 요청의 답이 온 것이고, 그것을 그대로
    # 쓰면 A 의 질문에 B 의 근거로 만든 답이 붙는다.
    if payload.get("request_id") != request.request_id:
        raise WireViolation("응답의 request_id 가 요청과 다릅니다.")
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise WireViolation(
            f"contract_version 이 다릅니다: {payload.get('contract_version')!r}"
        )

    version = str(payload.get("version") or "")
    if expected_version and version != expected_version:
        # 승인된 digest 의 버전이 기준이다. 응답값으로 DB 를 갱신하지 않는다.
        raise WireViolation(
            f"승인 버전과 다릅니다(승인 {expected_version} · 응답 {version or '없음'})."
        )

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise WireViolation("전문 봇이 비어 있는 응답을 반환했습니다.")
    # 제어문자를 걷어낸다. 그대로 두면 Slack·콘솔·로그에서 서로 다르게 보이고,
    # 금지 문자열 검사를 사이에 끼워 넣어 우회할 수도 있다.
    text = "".join(
        ch for ch in unicodedata.normalize("NFC", text)
        if ch in "\n\t" or unicodedata.category(ch)[0] != "C"
    ).strip()
    if len(text) > request.max_output_chars:
        raise WireViolation(
            f"응답 길이가 계약 범위를 넘습니다({len(text)} > {request.max_output_chars})."
        )
    lowered = text.lower()
    for needle, why in _FORBIDDEN:
        if needle in text or needle in lowered:
            raise WireViolation(why)

    ids = payload.get("evidence_ids")
    if not isinstance(ids, list) or not ids:
        raise WireViolation("어떤 근거를 썼는지 밝히지 않았습니다.")
    used = tuple(str(i) for i in ids)
    if any(not _EVIDENCE_ID_RE.match(i) for i in used):
        raise WireViolation("근거 식별자 형식이 다릅니다.")
    unknown = set(used) - request.evidence_ids
    if unknown:
        # 우리가 주지 않은 근거를 썼다는 뜻이다 — 그쪽 색인이나 기억으로 답했다.
        raise WireViolation(f"보내지 않은 근거를 인용했습니다: {sorted(unknown)}")

    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
    return RuntimeAnswer(
        text=text,
        version=version,
        evidence_ids=used,
        input_tokens=_int(usage.get("input_tokens")),
        output_tokens=_int(usage.get("output_tokens")),
    )


def _int(value: object) -> int:
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def validate_health(raw: bytes) -> dict:
    """`/v1/health` 응답. 모델을 부르지 않고 2초 안에 와야 한다."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireViolation("health 응답을 읽지 못했습니다.") from exc
    if not isinstance(payload, dict):
        raise WireViolation("health 응답이 객체가 아닙니다.")
    if payload.get("contract_version") != CONTRACT_VERSION:
        raise WireViolation("health 의 contract_version 이 다릅니다.")
    status = str(payload.get("status") or "")
    if status != "ok":
        raise WireViolation(f"health 가 정상이 아닙니다: {status or '없음'}")
    return {"version": str(payload.get("version") or ""), "status": status}


__all__ = [
    "CLOCK_SKEW_SECONDS",
    "COMPLETE_PATH",
    "CONTRACT_VERSION",
    "DEADLINE_MS",
    "ERROR_CODES",
    "HEALTH_DEADLINE_MS",
    "HEALTH_PATH",
    "MAX_OUTPUT_CHARS",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "SCHEMA_REQUEST",
    "SCHEMA_RESPONSE",
    "Evidence",
    "RuntimeAnswer",
    "RuntimeRequest",
    "WireViolation",
    "canonical",
    "evidence_from",
    "parse_error",
    "request_from_authorized",
    "sign",
    "signature_headers",
    "validate_health",
    "validate_response",
    "verify_response_signature",
]
