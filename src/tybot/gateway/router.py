"""라우터 — 모델 선택, 민감도 라우팅, 비용 가드, 로깅을 묶는다."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Mapping, Sequence

from .base import (
    LLMResponse,
    Message,
    ModelSpec,
    Provider,
    ProviderTimeout,
    Sensitivity,
    ToolSpec,
    error_reason,
)
from .cost import CostGuard, current_workspace

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
        "claude-opus-4-8", "anthropic", 15.0, 75.0, Sensitivity.CONFIDENTIAL,
        supports_sampling=False,
    ),
    "claude-sonnet-5": ModelSpec(
        "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL,
        supports_sampling=False,
    ),
    # 민감도는 모델 티어가 아니라 **벤더 계약(DPA/zero-retention)** 단위로 정한다.
    # Anthropic 계약 하나로 묶이므로 haiku 도 confidential 허용.
    "claude-opus-5": ModelSpec(
        "claude-opus-5", "anthropic", 5.0, 25.0, Sensitivity.CONFIDENTIAL,
        # 샘플링 파라미터를 제거한 모델. 보내면 400 이다.
        supports_sampling=False,
    ),
    "claude-haiku-4-5": ModelSpec(
        "claude-haiku-4-5", "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
    ),
    # 날짜 꼬리를 붙인 옛 표기. **모델 ID 에 날짜를 붙이지 않는다** — 위가 맞는 값이다.
    # 설정에 이 값이 남아 있을 수 있어 지우지 않고 같은 자리를 가리키게 둔다.
    "claude-haiku-4-5-20251001": ModelSpec(
        "claude-haiku-4-5", "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
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
        workspace_limits: Callable[[], Mapping[str, float]] | None = None,
    ) -> Router:
        """기본 레지스트리로 라우터 생성.

        providers 를 주지 않으면 실 프로바이더를 lazy import 한다(SDK 필요).
        테스트에서는 fake provider dict 를 주입한다.

        cost_state_path 를 주면 당일 누적 비용이 재시작에도 유지된다(운영 기본값).

        workspace_limits 를 주면 **워크스페이스별 상한**이 전체 상한 안쪽에서
        따로 걸린다. 안 주면 전체 상한 하나만 본다 — 그러면 한 워크스페이스가
        지갑을 다 쓰고 나머지가 굶어도 아무 데도 안 보인다.
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
            cost_guard=CostGuard(
                daily_limit_usd,
                state_path=cost_state_path,
                workspace_limits=workspace_limits,
            ),
            default_model=default_model,
            fallback_models=fallback_models,
        )

    @property
    def spent_today(self) -> float:
        return self._cost.spent_today

    def spent_today_for(self, workspace: str) -> float:
        """이 워크스페이스의 당일 누적. 화면이 「누가 썼나」 를 말할 근거다."""
        return self._cost.spent_by_workspace().get(workspace.strip().lower(), 0.0)

    def limit_for(self, workspace: str) -> float | None:
        """이 워크스페이스에 실제로 걸리는 상한. 없으면 `None`(전체 상한만)."""
        return self._cost.limit_for(workspace)

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
        tools: Sequence[ToolSpec] = (),
        # 남은 시간. **폴백 후보도 같은 시계를 쓴다** — 첫 모델이 다 써 버렸으면
        # 두 번째를 시작하지 않는다(2026-09-16 장애 §5.2).
        timeout_seconds: float | None = None,
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
        started = time.monotonic()
        for candidate in self._candidates(spec, sensitivity):
            left = _left(timeout_seconds, started)
            if left is not None and left <= 0:
                # 남은 시간이 없다. **새 모델을 시작하지 않는다** — 시작하면
                # 사용자는 이미 실패를 받았는데 비용만 더 든다.
                #
                # `break` 로 빠지면 `for…else` 를 건너뛰어 `resp` 가 없는 채로
                # 아래로 내려간다 — 테스트가 `UnboundLocalError` 로 잡았다.
                # **끝났다는 것을 예외로 말한다.**
                logger.warning(
                    "llm_call 폴백 중단 model=%s — 남은 시간 없음", candidate.model
                )
                raise last_error or ProviderTimeout(
                    "남은 시간이 없어 다음 모델을 시작하지 않았습니다."
                )
            provider = self._providers[candidate.provider]
            try:
                # **도구가 없으면 인자를 넘기지 않는다.** `system` · `temperature`
                # 와 같은 이유다 — 안 쓰는 것을 보내면 그것을 모르는 구현이
                # 거부한다. 도구를 실제로 쓰는 호출에서만 계약이 넓어진다.
                extra = {"tools": tools} if tools else {}
                if left is not None:
                    extra["timeout_seconds"] = left
                resp = provider.complete(
                    candidate, messages, max_tokens=max_tokens,
                    temperature=temperature, **extra,
                )
            except Exception as exc:  # noqa: BLE001 - 다음 후보로 넘긴다
                last_error = exc
                # **예외 종류만 찍으면 원인을 못 가린다.** 400 의 이유는 응답
                # 본문에 있다(2026-09-08 실측: `error=BadRequestError` 한 줄뿐이라
                # 모델 이름 문제인지 입력 길이 문제인지 알 수 없었다).
                logger.warning(
                    "llm_call 실패 model=%s provider=%s error=%s — 다음 후보로",
                    candidate.model,
                    candidate.provider,
                    error_reason(exc),
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
        # **어느 워크스페이스가 썼는지 로그에 남긴다.** 안 남기면 「누가 지갑을
        # 비웠나」 를 사후에 되짚을 수 없다 — 2026-09-15 에 그 질문에 답하지 못했다.
        workspace = current_workspace()
        logger.info(
            "llm_call model=%s provider=%s ws=%s in=%d out=%d cost=$%.4f"
            " ws_today=$%.2f spent_today=$%.2f",
            resp.model,
            resp.provider,
            workspace or "-",
            resp.input_tokens,
            resp.output_tokens,
            resp.cost_usd,
            self._cost.spent_by_workspace().get(workspace, 0.0),
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


def _left(timeout_seconds: float | None, started: float) -> float | None:
    """폴백 후보에게 남은 시간. 상한이 없으면 `None`.

    후보마다 새로 상한을 주지 않는다 — 그러면 후보 수만큼 시간이 늘어나고,
    바깥에서 본 「한 번의 호출」 이 실제로는 몇 배가 된다.
    """
    if timeout_seconds is None:
        return None
    return timeout_seconds - (time.monotonic() - started)
