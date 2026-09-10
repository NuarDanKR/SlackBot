"""게이트웨이 공통 타입 및 프로바이더 프로토콜."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable


class Sensitivity(str, Enum):  # noqa: UP042 - StrEnum 은 3.11+ 이지만 기존 직렬화 호환 유지
    """데이터 민감도. 민감도별로 허용 프로바이더를 제한한다."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"

    def rank(self) -> int:
        return {"public": 0, "internal": 1, "confidential": 2}[self.value]


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    # 보통은 문자열이다. 첨부 원본을 함께 보낼 때만 콘텐츠 블록 목록이 온다
    # (document/image + text). 프로바이더는 그대로 넘긴다.
    content: str | list[dict]


@dataclass
class ModelSpec:
    """모델 레지스트리 항목."""

    model: str
    provider: str
    # 100만 토큰당 USD 단가
    input_price_per_mtok: float
    output_price_per_mtok: float
    # 이 모델로 처리 허용되는 최대 민감도(이하 민감도만 허용)
    max_sensitivity: Sensitivity
    # `temperature`·`top_p`·`top_k` 를 받는가.
    #
    # **모델이 정하는 사실이라 레지스트리가 들고 있어야 한다.** 프로바이더에
    # 모델 이름 목록을 박으면 새 모델이 늘 때마다 그 목록이 썩고, 그 고장은
    # **그 모델을 지정한 전문 봇만** 조용히 폴백하는 모양으로 나타난다.
    # 실제로 그렇게 두 번 걸렸다(`system` 형식 → `temperature`, 2026-09-08).
    #
    # Opus 5·4.8·4.7 과 Sonnet 5 는 샘플링 파라미터를 제거했고 보내면 400 이다.
    # Haiku 4.5·Sonnet 4.6·Opus 4.6 은 받는다.
    supports_sampling: bool = True

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_price_per_mtok
            + output_tokens / 1_000_000 * self.output_price_per_mtok
        )


@dataclass(frozen=True)
class ToolSpec:
    """모델에게 주는 도구 하나.

    **설명문이 곧 규칙이다.** 「결과가 없다고 낱말을 바꿔 다시 부르지 말 것」 같은
    지시는 시스템 프롬프트가 아니라 여기 적어야 모델이 그 도구를 부르는 순간에 본다.
    """

    name: str
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    """모델이 부른 도구. `id` 를 그대로 결과에 붙여 돌려줘야 짝이 맞는다."""

    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    raw: object | None = None
    # 도구를 준 호출에서만 채워진다. **루프는 호출부가 돈다** — 게이트웨이는
    # 전송이고, 무엇을 몇 번 부를지는 정책이라 여기서 정하지 않는다.
    tool_calls: tuple[ToolCall, ...] = ()
    stop_reason: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@runtime_checkable
class Provider(Protocol):
    """LLM 프로바이더 어댑터. 실제 SDK 호출은 구현체에서 lazy import."""

    name: str

    def complete(
        self,
        spec: ModelSpec,
        messages: Sequence[Message],
        *,
        max_tokens: int = 1024,
        temperature: float = 0.0,
        tools: Sequence[ToolSpec] = (),
    ) -> LLMResponse: ...


# 프로바이더 오류에서 **사람이 조치할 수 있는 만큼**을 뽑는다.
#
# 2026-09-08 실측: 로그에 `error=BadRequestError` 만 남아 있었다. 400 은 요청
# 형태가 틀렸다는 뜻이고 그 이유는 응답 본문에 적혀 있는데, 우리는 예외 **종류**만
# 찍고 본문을 버렸다. 그래서 「모델 이름이 없는 값인가 / 입력이 너무 긴가 /
# 파라미터가 틀렸나」 를 가릴 수 없었다.
#
# 메시지를 자르는 이유: 400 중에는 문제가 된 필드를 되돌려 주는 것이 있고, 그 안에
# 근거 본문 조각이 섞일 수 있다. 구조적 오류(모델 이름·파라미터)는 짧으므로
# 앞부분만으로 충분하고, 긴 것은 잘려 본문이 로그로 흐르지 않는다.
ERROR_REASON_LIMIT = 200


def error_reason(exc: BaseException, *, limit: int = ERROR_REASON_LIMIT) -> str:
    """`BadRequestError status=400 type=invalid_request_error msg=...` 꼴 한 줄."""
    parts = [type(exc).__name__]
    status = getattr(exc, "status_code", None)
    if status is not None:
        parts.append(f"status={status}")
    # SDK 는 오류 본문을 `body` 로 들고 있다. 종류(`type`)만 꺼낸다 — 그것이
    # 조치를 가른다(not_found_error 는 이름, invalid_request_error 는 요청).
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        inner = body.get("error")
        if isinstance(inner, dict) and inner.get("type"):
            parts.append(f"type={inner['type']}")
    message = str(exc).strip().replace("\n", " ")
    if message:
        parts.append(f"msg={message[:limit]}" + ("…" if len(message) > limit else ""))
    return " ".join(parts)
