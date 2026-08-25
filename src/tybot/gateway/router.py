"""라우터 — 모델 선택, 민감도 라우팅, 비용 가드, 로깅을 묶는다."""
from __future__ import annotations

import logging
from collections.abc import Sequence

from .base import LLMResponse, Message, ModelSpec, Provider, Sensitivity
from .cost import CostGuard

logger = logging.getLogger("tybot.gateway")


class UnknownModel(KeyError):
    """레지스트리에 없는 모델."""


class ModelNotAllowed(PermissionError):
    """요청 민감도가 모델 허용 범위를 초과."""


# 기본 모델 레지스트리.
# 주의: 단가는 예시 자리표시자다. 실제 값은 각 프로바이더 가격표로 확정하고
# (Claude 는 claude-api 스킬 참조), 민감도 라우팅 표는 DPA/zero-retention 확인 후 조정한다.
DEFAULT_REGISTRY: dict[str, ModelSpec] = {
    "claude-opus-4-8": ModelSpec(
        "claude-opus-4-8", "anthropic", 15.0, 75.0, Sensitivity.CONFIDENTIAL
    ),
    "claude-sonnet-5": ModelSpec(
        "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
    ),
    # 민감도는 모델 티어가 아니라 **벤더 계약(DPA/zero-retention)** 단위로 정한다.
    # Anthropic 계약 하나로 묶이므로 haiku 도 confidential 허용.
    "claude-haiku-4-5-20251001": ModelSpec(
        "claude-haiku-4-5-20251001", "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
    ),
    # OpenAI 모델 ID/단가는 배포 시 확정. 기본은 사내(internal) 이하로 제한.
    "gpt-4o": ModelSpec("gpt-4o", "openai", 2.5, 10.0, Sensitivity.INTERNAL),
    "gpt-4o-mini": ModelSpec("gpt-4o-mini", "openai", 0.15, 0.6, Sensitivity.INTERNAL),
}


class Router:
    """게이트웨이 진입점.

    프로바이더 SDK를 직접 쓰지 말고 항상 이 라우터를 통한다.
    """

    def __init__(
        self,
        providers: dict[str, Provider],
        registry: dict[str, ModelSpec],
        cost_guard: CostGuard,
        *,
        default_model: str = "claude-sonnet-5",
    ) -> None:
        self._providers = providers
        self._registry = registry
        self._cost = cost_guard
        self._default_model = default_model

    @classmethod
    def from_default_registry(
        cls,
        *,
        daily_limit_usd: float = 50.0,
        default_model: str = "claude-sonnet-5",
        providers: dict[str, Provider] | None = None,
        cost_state_path: str | None = None,
    ) -> Router:
        """기본 레지스트리로 라우터 생성.

        providers 를 주지 않으면 실 프로바이더를 lazy import 한다(SDK 필요).
        테스트에서는 fake provider dict 를 주입한다.

        cost_state_path 를 주면 당일 누적 비용이 재시작에도 유지된다(운영 기본값).
        """
        if providers is None:
            from .providers import build_default_providers

            providers = build_default_providers()
        return cls(
            providers=providers,
            registry=dict(DEFAULT_REGISTRY),
            cost_guard=CostGuard(daily_limit_usd, state_path=cost_state_path),
            default_model=default_model,
        )

    @property
    def spent_today(self) -> float:
        return self._cost.spent_today

    @property
    def default_model(self) -> str:
        return self._default_model

    def resolve(self, model: str | None, sensitivity: Sensitivity) -> ModelSpec:
        """모델 선택 + 민감도 검증. 부적합하면 예외."""
        name = model or self._default_model
        spec = self._registry.get(name)
        if spec is None:
            raise UnknownModel(f"등록되지 않은 모델: {name}")
        if sensitivity.rank() > spec.max_sensitivity.rank():
            allowed = sorted(
                m for m, s in self._registry.items()
                if sensitivity.rank() <= s.max_sensitivity.rank() and s.provider in self._providers
            )
            raise ModelNotAllowed(
                f"민감도 '{sensitivity.value}' 는 모델 '{name}'(허용 최대 "
                f"'{spec.max_sensitivity.value}')로 처리할 수 없습니다. "
                f"사용 가능: {', '.join(allowed) or '없음'}"
            )
        if spec.provider not in self._providers:
            raise UnknownModel(f"프로바이더 미등록: {spec.provider}")
        return spec

    def complete(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        sensitivity: Sensitivity = Sensitivity.INTERNAL,
        max_tokens: int = 1024,
        temperature: float = 0.0,
    ) -> LLMResponse:
        spec = self.resolve(model, sensitivity)
        # 러프 사전 견적(입력 토큰 근사 = 문자수/4, 출력은 max_tokens 로 상한 가정)
        approx_in = sum(len(m.content) for m in messages) // 4
        self._cost.check(spec.cost(approx_in, max_tokens))

        provider = self._providers[spec.provider]
        resp = provider.complete(
            spec, messages, max_tokens=max_tokens, temperature=temperature
        )
        self._cost.record(resp.cost_usd)
        logger.info(
            "llm_call model=%s provider=%s in=%d out=%d cost=$%.4f spent_today=$%.2f",
            resp.model,
            resp.provider,
            resp.input_tokens,
            resp.output_tokens,
            resp.cost_usd,
            self._cost.spent_today,
        )
        return resp
