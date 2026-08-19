"""게이트웨이 공통 타입 및 프로바이더 프로토콜."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Sequence, runtime_checkable


class Sensitivity(str, Enum):
    """데이터 민감도. 민감도별로 허용 프로바이더를 제한한다."""

    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"

    def rank(self) -> int:
        return {"public": 0, "internal": 1, "confidential": 2}[self.value]


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


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

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (
            input_tokens / 1_000_000 * self.input_price_per_mtok
            + output_tokens / 1_000_000 * self.output_price_per_mtok
        )


@dataclass
class LLMResponse:
    text: str
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    raw: object | None = None


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
    ) -> LLMResponse: ...
