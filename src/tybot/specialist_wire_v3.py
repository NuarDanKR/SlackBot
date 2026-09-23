"""전문 봇 호출 계약 v3 — 검색 도구를 주는 호출의 **모양**.

설계: `docs/design/specialist-v3-tool-contract.md`

## 이 파일이 하는 일
요청·도구 호출·응답의 **형식만** 정한다. 도구를 실제로 실행하는 것도, 권한을 보는
것도 여기 없다 — 그건 `specialist_tools.ToolBox` 가 이미 한다.

형식을 코드로 두는 이유: PF 개발자가 맞출 대상이 우리 구현이면 우리가 코드를 고칠
때마다 그쪽이 깨진다. **고정된 스키마와 fixture 가 기준**이어야 양쪽이 각자 시험한다.

## v2 와 무엇이 다른가
v2(`specialist_wire.py`)는 「전문 봇은 스스로 자료를 가져오지 않는다」 가 전제라
빈 근거를 거부한다. 그 전제가 PF Hermes 의 다단계 검색을 없앤다.

v3 는 찾는 일을 되돌려 주되 **찾는 주체는 그대로 둔다.** 자료는 도구로만 나가고,
도구는 우리가 권한을 확인한 것만 준다. 그래서 씨앗 근거가 없어도 되고, 대신
**답변에 근거 ID 가 없으면 그 답을 버린다** — 규칙이 사라진 게 아니라 판정 시점이
요청에서 응답으로 옮겼다.

서명·재생 방지는 v2 와 **같은 함수**를 쓴다(`specialist_wire.canonical`·`sign`).
두 벌로 두면 한쪽만 고쳐지는 날이 온다.
"""
from __future__ import annotations

import json
import secrets
import uuid
from dataclasses import dataclass, field

from .specialist_wire import WireViolation

SCHEMA_REQUEST = "tybot.specialist.request.v3"
SCHEMA_TOOL_CALL = "tybot.specialist.tool.v3"
SCHEMA_TOOL_RESULT = "tybot.specialist.tool_result.v3"
SCHEMA_RESPONSE = "tybot.specialist.response.v3"

TOOL_ENDPOINT = "/v3/tools/call"
ANSWER_ENDPOINT = "/v3/answer"
CANCEL_ENDPOINT = "/v3/cancel"

# v2 는 15초다 — 근거를 다 받아 문장만 만들기 때문이다. v3 는 검색·읽기를 여러 번
# 하므로 그 시간으로는 첫 검색도 못 끝낸다. **예산이 시간보다 먼저 끝나게** 둔다.
# 시간으로만 끊으면 절반 읽은 상태에서 잘린다.
DEADLINE_MS = 90_000
MAX_OUTPUT_CHARS = 3_000

MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
# 도구 결과는 여러 번 오간다. 한 번에 큰 것을 주면 모델 문맥이 한 번에 차고,
# 그 뒤 호출은 읽어도 쓰지 못한다.
MAX_TOOL_RESULT_BYTES = 32 * 1024

# 줄 수 있는 도구. `ToolBox` 가 아는 이름과 같아야 한다 — 다르면 「알 수 없는 도구」
# 가 조용히 돌아오고, 모델은 그 도구가 없는 줄 알고 다른 길로 간다.
TOOLS = ("search", "read_channel", "read_document", "fetch_recent_slack")

# 응답 상태 넷. **「모른다」 와 「못 찾았다」 와 「고장났다」 를 가른다** — 셋을 하나로
# 묶으면 화면에서 원인을 못 가리고, 그러면 고칠 곳도 못 정한다.
STATUS_ANSWERED = "answered"
STATUS_NO_EVIDENCE = "no_evidence"
STATUS_REFUSED = "refused"
STATUS_FAILED = "failed"
STATUSES = (STATUS_ANSWERED, STATUS_NO_EVIDENCE, STATUS_REFUSED, STATUS_FAILED)

# 마스터가 폴백해야 하는 상태. `refused` 는 폴백하지 않는다 — 권한·계약 때문에
# 답하지 않은 것을 마스터가 대신 답하면 그 판단이 무의미해진다.
FALLBACK_STATUSES = (STATUS_FAILED,)


@dataclass(frozen=True)
class ToolBudget:
    """이 요청이 쓸 수 있는 도구 예산.

    `specialist_tools.ToolBudget` 와 같은 뜻이고, 이쪽은 **전선에 실리는 모양**이다.
    Hermes 가 자기 쪽에서도 세어 쓸데없는 호출을 줄일 수 있게 보낸다. 다만 **판정은
    우리가 한다** — 보낸 숫자를 그쪽이 지킬 것이라고 믿지 않는다.
    """

    max_calls: int = 12
    max_per_tool: int = 4
    max_chars: int = 60_000
    max_seconds: int = 45

    def to_json(self) -> dict:
        return {
            "max_calls": self.max_calls,
            "max_per_tool": self.max_per_tool,
            "max_chars": self.max_chars,
            "max_seconds": self.max_seconds,
        }


def new_tool_token() -> str:
    """요청 하나짜리 도구 토큰.

    **상시 키를 주지 않는다.** 이 토큰은 그 `request_id` 에만, `deadline` 까지만,
    만들 때의 권한으로만 쓰인다. 답변이 돌아오면 즉시 폐기한다.

    `authorization_id` 를 대신하지 않는다 — 그쪽은 「누구 권한으로 판정했나」 의
    이름이고, 토큰은 「이 요청이 지금 살아 있나」 의 열쇠다. 합치면 권한 판정
    기록이 수명에 끌려다닌다.
    """
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class FollowUpRef:
    """후속 질문이 이어 가는 **원문 좌표**. 이전 답변 문장이 아니다(절대 원칙 1).

    `locator` 는 opaque 하다. 열 때 현재 권한으로 다시 확인하므로, 권한이 바뀌었으면
    안 열린다.
    """

    id: str
    locator: str


@dataclass(frozen=True)
class Request:
    """v3 요청. `body()` 가 그대로 전송된다."""

    workspace: str
    authorization_id: str
    question: str
    tool_token: str
    # v2 와 달리 **선택**이다. 도구로만 자료가 나가므로 씨앗 근거가 없어도 된다.
    evidence: tuple = ()
    follow_up: tuple[FollowUpRef, ...] = ()
    allow: tuple[str, ...] = TOOLS
    budget: ToolBudget = field(default_factory=ToolBudget)
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    deadline_ms: int = DEADLINE_MS
    max_output_chars: int = MAX_OUTPUT_CHARS

    def __post_init__(self) -> None:
        if not self.workspace.strip() or not self.authorization_id.strip():
            raise WireViolation("워크스페이스와 권한 판정 식별자가 필요합니다.")
        if not self.question.strip():
            raise WireViolation("빈 질문은 전문 봇에 보내지 않습니다.")
        if not self.tool_token.strip():
            # 토큰 없이 보내면 Hermes 는 도구를 못 쓰고, v2 처럼 씨앗 근거로만
            # 답하게 된다. 그런데 v3 는 씨앗 근거를 요구하지 않으므로 **근거 없이
            # 답하라는 요청**이 된다. 그건 가장 나쁜 조합이다.
            raise WireViolation("도구 토큰 없이 v3 요청을 보내지 않습니다.")
        unknown = [name for name in self.allow if name not in TOOLS]
        if unknown:
            raise WireViolation(f"모르는 도구를 허용할 수 없습니다: {unknown}")
        if not self.allow:
            raise WireViolation("도구를 하나도 주지 않을 거면 v2 를 쓰세요.")
        ids = [e.id for e in self.evidence]
        if len(set(ids)) != len(ids):
            raise WireViolation("근거 식별자가 중복됩니다.")

    def body(self) -> bytes:
        payload = {
            "schema": SCHEMA_REQUEST,
            "request_id": self.request_id,
            "authorization_id": self.authorization_id,
            "workspace": self.workspace,
            "question": self.question,
            "evidence": [{"id": e.id, "text": e.text} for e in self.evidence],
            "follow_up": [{"id": f.id, "locator": f.locator} for f in self.follow_up],
            "tools": {
                "token": self.tool_token,
                "endpoint": TOOL_ENDPOINT,
                "allow": list(self.allow),
                "budget": self.budget.to_json(),
            },
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


# ---------------------------------------------------------------------------
# 도구 호출 — Hermes → TYBot 방향
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolCall:
    """Hermes 가 보내는 도구 호출."""

    request_id: str
    token: str
    call_id: str
    tool: str
    args: dict = field(default_factory=dict)


def parse_tool_call(raw: bytes, *, max_bytes: int = MAX_TOOL_RESULT_BYTES) -> ToolCall:
    """도구 호출 본문을 읽는다. **형식만 본다 — 토큰 확인은 호출부가 한다.**

    형식과 권한을 한 함수에서 보면, 형식 오류와 권한 거절이 같은 예외로 나온다.
    그러면 「계약이 틀렸나 권한이 없나」 를 로그에서 못 가린다.
    """
    if len(raw) > max_bytes:
        raise WireViolation(f"도구 호출이 상한을 넘습니다({len(raw)} > {max_bytes}).")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WireViolation("도구 호출이 JSON 이 아닙니다.") from exc
    if not isinstance(payload, dict):
        raise WireViolation("도구 호출의 최상위가 객체가 아닙니다.")
    if payload.get("schema") != SCHEMA_TOOL_CALL:
        raise WireViolation(f"도구 호출 스키마가 다릅니다: {payload.get('schema')!r}")

    tool = str(payload.get("tool") or "").strip()
    if tool not in TOOLS:
        raise WireViolation(f"모르는 도구입니다: {tool!r}")
    args = payload.get("args")
    if args is not None and not isinstance(args, dict):
        raise WireViolation("도구 인자가 객체가 아닙니다.")

    for name in ("request_id", "token", "call_id"):
        if not str(payload.get(name) or "").strip():
            raise WireViolation(f"도구 호출에 {name} 이 없습니다.")

    return ToolCall(
        request_id=str(payload["request_id"]),
        token=str(payload["token"]),
        call_id=str(payload["call_id"]),
        tool=tool,
        args=dict(args or {}),
    )


def tool_result(
    call_id: str,
    text: str,
    *,
    evidence: list[dict] | None = None,
    budget_left: dict | None = None,
) -> bytes:
    """도구 결과. **도구 실패도 여기로 온다.**

    도구가 터지거나 예산이 끝나도 `ok: true` 에 사람 말로 적어 보낸다
    (`ToolBox.run()` 이 이미 그렇게 한다). 모델이 도구 실패를 **모른 채** 답을 만드는
    것이 가장 나쁘다. HTTP 오류로 끊으면 Hermes 쪽 예외 처리에 맡기게 되고,
    그쪽이 조용히 삼키면 우리는 알 방법이 없다.
    """
    payload = {
        "schema": SCHEMA_TOOL_RESULT,
        "call_id": call_id,
        "ok": True,
        "text": text,
        "evidence": evidence or [],
        "budget": budget_left or {},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


def tool_refusal(call_id: str, reason: str) -> bytes:
    """**계약 위반**일 때만 쓴다 — 토큰 만료·모르는 도구·요청 불일치.

    도구 실패와 가른다. 이건 Hermes 가 재시도하면 안 되고 지금까지 읽은 것으로
    답해야 하는 상태다. 재시도하면 같은 거절이 반복되며 예산만 태운다.
    """
    payload = {
        "schema": SCHEMA_TOOL_RESULT,
        "call_id": call_id,
        "ok": False,
        "text": "",
        "reason": reason,
        "evidence": [],
        "budget": {},
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")


# ---------------------------------------------------------------------------
# 응답
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Answer:
    status: str
    text: str
    used_evidence: tuple[str, ...]
    uncertain: tuple[str, ...] = ()
    usage: dict = field(default_factory=dict)

    @property
    def should_fall_back(self) -> bool:
        return self.status in FALLBACK_STATUSES


def validate_response(
    raw: bytes,
    *,
    request_id: str,
    issued_evidence: frozenset[str],
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> Answer:
    """응답을 검사한다. **지어낸 근거 ID 가 섞이면 그 답을 버린다.**

    `used_evidence` 에 우리가 발급하지 않은 ID 가 있으면 둘 중 하나다 — 지어냈거나
    다른 요청의 것이다. 어느 쪽이든 그 답의 출처를 우리가 보증할 수 없고, 출처를
    보증 못 하는 답은 출처를 붙일 수 없다(절대 원칙 2).
    """
    if len(raw) > MAX_RESPONSE_BYTES:
        raise WireViolation(f"응답이 상한을 넘습니다({len(raw)} > {MAX_RESPONSE_BYTES}).")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise WireViolation("응답이 JSON 이 아닙니다.") from exc
    if not isinstance(payload, dict):
        raise WireViolation("응답의 최상위가 객체가 아닙니다.")
    if payload.get("schema") != SCHEMA_RESPONSE:
        raise WireViolation(f"응답 스키마가 다릅니다: {payload.get('schema')!r}")
    if str(payload.get("request_id") or "") != request_id:
        # 다른 요청의 답이다. 소켓을 공유하거나 재시도가 엇갈렸을 때 난다.
        raise WireViolation("응답의 request_id 가 요청과 다릅니다.")

    status = str(payload.get("status") or "").strip()
    if status not in STATUSES:
        raise WireViolation(f"모르는 상태입니다: {status!r}")

    used = payload.get("used_evidence")
    if used is None:
        used = []
    if not isinstance(used, list):
        raise WireViolation("used_evidence 가 목록이 아닙니다.")
    used_ids = tuple(str(item) for item in used)
    unknown = sorted(set(used_ids) - issued_evidence)
    if unknown:
        raise WireViolation(f"발급하지 않은 근거 식별자가 섞였습니다: {unknown}")

    text = str(payload.get("answer") or "")
    if len(text) > max_output_chars:
        raise WireViolation(
            f"답변이 상한을 넘습니다({len(text)} > {max_output_chars})."
        )

    if status == STATUS_ANSWERED:
        if not text.strip():
            raise WireViolation("answered 인데 답변이 비었습니다.")
        if not used_ids:
            # v2 는 요청에서 막았다. v3 는 여기서 막는다 — 규칙이 사라진 게 아니라
            # 판정 시점이 옮겼다. 근거 없이 답했으면 그건 기억으로 답한 것이다.
            raise WireViolation("근거 없이 answered 로 답할 수 없습니다.")

    uncertain = payload.get("uncertain") or []
    if not isinstance(uncertain, list):
        raise WireViolation("uncertain 이 목록이 아닙니다.")

    usage = payload.get("usage")
    return Answer(
        status=status,
        text=text,
        used_evidence=used_ids,
        uncertain=tuple(str(item) for item in uncertain),
        usage=dict(usage) if isinstance(usage, dict) else {},
    )


__all__ = [
    "ANSWER_ENDPOINT",
    "CANCEL_ENDPOINT",
    "DEADLINE_MS",
    "FALLBACK_STATUSES",
    "MAX_OUTPUT_CHARS",
    "MAX_REQUEST_BYTES",
    "MAX_RESPONSE_BYTES",
    "MAX_TOOL_RESULT_BYTES",
    "SCHEMA_REQUEST",
    "SCHEMA_RESPONSE",
    "SCHEMA_TOOL_CALL",
    "SCHEMA_TOOL_RESULT",
    "STATUSES",
    "STATUS_ANSWERED",
    "STATUS_FAILED",
    "STATUS_NO_EVIDENCE",
    "STATUS_REFUSED",
    "TOOLS",
    "TOOL_ENDPOINT",
    "Answer",
    "FollowUpRef",
    "Request",
    "ToolBudget",
    "ToolCall",
    "WireViolation",
    "new_tool_token",
    "parse_tool_call",
    "tool_refusal",
    "tool_result",
    "validate_response",
]
