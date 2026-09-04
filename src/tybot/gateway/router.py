"""라우터 — 모델 선택, 민감도 라우팅, 비용 가드, 로깅을 묶는다."""
from __future__ import annotations

import logging
import os
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
        fallback_models: Sequence[str] = (),
    ) -> None:
        self._providers = providers
        self._registry = registry
        self._cost = cost_guard
        self._default_model = default_model
        # 장애 때 순서대로 시도할 모델. **비어 있으면 폴백하지 않는다** —
        # 어느 모델이 대체 가능한지는 우리가 짐작할 일이 아니다.
        self._fallback_models = tuple(fallback_models)

    @classmethod
    def from_default_registry(
        cls,
        *,
        daily_limit_usd: float = 50.0,
        default_model: str = "claude-sonnet-5",
        providers: dict[str, Provider] | None = None,
        cost_state_path: str | None = None,
        fallback_models: Sequence[str] | None = None,
    ) -> Router:
        """기본 레지스트리로 라우터 생성.

        providers 를 주지 않으면 실 프로바이더를 lazy import 한다(SDK 필요).
        테스트에서는 fake provider dict 를 주입한다.

        cost_state_path 를 주면 당일 누적 비용이 재시작에도 유지된다(운영 기본값).
        """
        if providers is None:
            from .providers import build_default_providers

            providers = build_default_providers()
        if fallback_models is None:
            fallback_models = [
                name.strip()
                for name in os.getenv("LLM_FALLBACK_MODELS", "").split(",")
                if name.strip()
            ]
        return cls(
            providers=providers,
            registry=dict(DEFAULT_REGISTRY),
            cost_guard=CostGuard(daily_limit_usd, state_path=cost_state_path),
            default_model=default_model,
            fallback_models=fallback_models,
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

    def _candidates(
        self, spec: ModelSpec, sensitivity: Sensitivity
    ) -> list[ModelSpec]:
        """처음 고른 모델 + 허용된 폴백. 순서가 곧 우선순위다.

        폴백도 **민감도 검사를 다시 통과해야 한다.** 장애 때만 조건이 느슨해지면,
        가장 급할 때 기밀 자료가 허용되지 않은 모델로 나간다.
        """
        out = [spec]
        for name in self._fallback_models:
            if name == spec.model:
                continue
            candidate = self._registry.get(name)
            if candidate is None or candidate.provider not in self._providers:
                continue
            if sensitivity.rank() > candidate.max_sensitivity.rank():
                continue
            out.append(candidate)
        return out

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
        # 러프 사전 견적(입력 토큰 근사 = 콘텐츠 크기/4). 문서·이미지 블록도 누락하지 않는다.
        approx_in = sum(_content_size(m.content) for m in messages) // 4
        self._cost.check(spec.cost(approx_in, max_tokens))

        # 프로바이더가 죽으면 **다른 모델로 한 번 더** 시도한다.
        # 폴백이 없던 동안 모델 장애 하나가 곧 답변 실패였고, qa-log 의 오류율이
        # 그만큼 올라갔다(2026-09-02 실측 22%). 라우팅(B-36)이 앞에 서면 실패 지점이
        # 하나 더 늘어나므로 여기서 받쳐 둔다.
        #
        # **후보를 우리가 짐작하지 않는다.** 어느 모델이 대체 가능한지는 정책이라
        # 설정으로 받는다(`fallback_models`). 비어 있으면 폴백하지 않는다 —
        # 조용히 다른 벤더로 보내는 것이 더 나쁘다.
        last_error: Exception | None = None
        for candidate in self._candidates(spec, sensitivity):
            provider = self._providers[candidate.provider]
            try:
                resp = provider.complete(
                    candidate, messages, max_tokens=max_tokens, temperature=temperature
                )
            except Exception as exc:  # noqa: BLE001 - 다음 후보로 넘긴다
                last_error = exc
                logger.warning(
                    "llm_call 실패 model=%s provider=%s error=%s — 다음 후보로",
                    candidate.model,
                    candidate.provider,
                    type(exc).__name__,
                )
                continue
            if candidate.model != spec.model:
                # 어느 모델이 실제로 답했는지 남는다. 남지 않으면 "왜 답이 달라졌나" 를
                # 되짚을 수 없다(qa-log 는 resp.model 을 기록한다).
                logger.warning(
                    "llm_call 폴백 %s -> %s", spec.model, candidate.model
                )
            break
        else:
            raise last_error if last_error else UnknownModel("호출할 모델이 없습니다")
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


def _content_size(content: str | list[dict]) -> int:
    if isinstance(content, str):
        return len(content)
    total = 0
    for block in content:
        for value in block.values():
            if isinstance(value, str):
                total += len(value)
            elif isinstance(value, dict):
                total += _content_size([value])
    return total
