"""라우터 — 모델 선택, 민감도 라우팅, 비용 가드, 로깅을 묶는다."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from threading import Lock

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

# 외부 검토 호출의 출력 견적. JSON 후보 수만으로 모델의 전체 출력 토큰을
# 추정하지 않는다. thinking 등을 포함한 worker의 기본 출력 예산과 맞춘다.
# workspace가 기본보다 큰 상한을 설정하면 여전히 사전 견적을 넘길 수 있다.
#
# 이 숫자는 **한도 검사에만** 쓰인다. 실제 기록은 보고된 토큰으로 한다.
REVIEW_OUTPUT_TOKEN_ENVELOPE = 16000


@dataclass(frozen=True)
class PromptCacheStats:
    """이 프로세스에서 관측한 Anthropic prompt cache 누적치."""

    eligible_calls: int
    hits: int
    write_tokens: int
    read_tokens: int
    net_saved_usd: float

    @property
    def hit_rate(self) -> float:
        return self.hits / self.eligible_calls if self.eligible_calls else 0.0


@dataclass(frozen=True)
class ResponseCacheStats:
    """Opt-in exact completion cache counters for this process."""

    lookups: int
    hits: int
    stores: int
    expired: int
    evictions: int
    saved_input_tokens: int
    saved_output_tokens: int
    saved_usd: float

    @property
    def hit_rate(self) -> float:
        return self.hits / self.lookups if self.lookups else 0.0


def _reported(metrics: dict) -> bool:
    """사용량을 실제로 받았나. **0 도 받은 값이다** — 안 받은 것과 가른다."""
    return all(isinstance(metrics.get(key), int) and metrics[key] >= 0
               for key in ("input_tokens", "output_tokens"))


class UnknownModel(KeyError):
    """레지스트리에 없는 모델."""


class ModelNotAllowed(PermissionError):
    """요청 민감도가 모델 허용 범위를 초과."""


# 기본 모델 레지스트리.
# 단가는 2026-10-15 에 Anthropic 1st-party 가격표로 확정했다(claude-api 스킬).
# 민감도 라우팅 표는 DPA/zero-retention 확인 후 조정한다.
#
# **단가가 틀리면 예산이 엉뚱한 데서 막힌다.** 2026-10-15 이전에는 자리표시자가
# 남아 있어 opus-4-8 이 실제의 3배(15/75)였고, 그 값이 「가장 비싼 confidential」
# 로 뽑혀 외부 호출 예약액을 끌어올렸다. 비용은 안 늘지만 **한도가 먼저 닫힌다** —
# 그 차단은 오류가 아니라 「오늘은 요약이 안 되네」 로만 나타난다.
#
# 모델을 더할 때는 **worker 가 실제로 쓰는 ID** 를 반드시 넣는다. 빠지면 그 호출은
# 전부 추정 경로를 타고, 추정은 보수적이라 한도를 빨리 먹는다.
DEFAULT_REGISTRY: dict[str, ModelSpec] = {
    "claude-opus-4-8": ModelSpec(
        "claude-opus-4-8", "anthropic", 5.0, 25.0, Sensitivity.CONFIDENTIAL,
        supports_sampling=False,
    ),
    # 현행 Opus. **RPC worker 의 기본 모델이다**(`HERMES_RPC_MODEL` 기본값).
    "claude-opus-5-5": ModelSpec(
        "claude-opus-5-5", "anthropic", 4.0, 20.0, Sensitivity.CONFIDENTIAL,
        supports_sampling=False, cache_read_multiplier=0.05,
    ),
    "claude-sonnet-5-5": ModelSpec(
        "claude-sonnet-5-5", "anthropic", 2.0, 10.0, Sensitivity.CONFIDENTIAL,
        supports_sampling=False, cache_read_multiplier=0.05,
    ),
    "claude-sonnet-5": ModelSpec(
        "claude-sonnet-5", "anthropic", 2.0, 10.0, Sensitivity.CONFIDENTIAL,
        supports_sampling=False,
    ),
    "claude-haiku-5-5": ModelSpec(
        "claude-haiku-5-5", "anthropic", 0.10, 0.50, Sensitivity.CONFIDENTIAL,
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
        response_cache_ttl_seconds: float | None = None,
        response_cache_max_entries: int | None = None,
    ) -> None:
        self._providers = providers
        self._registry = registry
        self._cost = cost_guard
        self._default_model = default_model
        # 장애 때 순서대로 시도할 모델. **비어 있으면 폴백하지 않는다** —
        # 어느 모델이 대체 가능한지는 우리가 짐작할 일이 아니다.
        self._fallback_models = tuple(fallback_models)
        self._cache_lock = Lock()
        self._cache_eligible_calls = 0
        self._cache_hits = 0
        self._cache_write_tokens = 0
        self._cache_read_tokens = 0
        self._cache_net_saved_usd = 0.0
        self._response_cache_ttl = _cache_ttl(response_cache_ttl_seconds)
        self._response_cache_max = _cache_max(response_cache_max_entries)
        self._response_cache_lock = Lock()
        self._response_cache: OrderedDict[str, tuple[float, LLMResponse]] = OrderedDict()
        self._response_cache_lookups = 0
        self._response_cache_hits = 0
        self._response_cache_stores = 0
        self._response_cache_expired = 0
        self._response_cache_evictions = 0
        self._response_cache_saved_input_tokens = 0
        self._response_cache_saved_output_tokens = 0
        self._response_cache_saved_usd = 0.0

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
        response_cache_ttl_seconds: float | None = None,
        response_cache_max_entries: int | None = None,
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
            response_cache_ttl_seconds=response_cache_ttl_seconds,
            response_cache_max_entries=response_cache_max_entries,
        )

    @property
    def spent_today(self) -> float:
        return self._cost.spent_today

    @property
    def prompt_cache_stats(self) -> PromptCacheStats:
        """재시작 전까지의 cache 적중률과 **write premium 포함** 순절감액."""
        with self._cache_lock:
            return PromptCacheStats(
                eligible_calls=self._cache_eligible_calls,
                hits=self._cache_hits,
                write_tokens=self._cache_write_tokens,
                read_tokens=self._cache_read_tokens,
                net_saved_usd=self._cache_net_saved_usd,
            )

    @property
    def response_cache_stats(self) -> ResponseCacheStats:
        with self._response_cache_lock:
            return ResponseCacheStats(
                lookups=self._response_cache_lookups,
                hits=self._response_cache_hits,
                stores=self._response_cache_stores,
                expired=self._response_cache_expired,
                evictions=self._response_cache_evictions,
                saved_input_tokens=self._response_cache_saved_input_tokens,
                saved_output_tokens=self._response_cache_saved_output_tokens,
                saved_usd=self._response_cache_saved_usd,
            )

    def _response_cache_get(
        self, key: str, now: float
    ) -> tuple[LLMResponse | None, str]:
        with self._response_cache_lock:
            self._response_cache_lookups += 1
            entry = self._response_cache.get(key)
            if entry is None:
                return None, "not_found"
            expires_at, cached = entry
            if expires_at <= now:
                del self._response_cache[key]
                self._response_cache_expired += 1
                return None, "expired"
            self._response_cache.move_to_end(key)
            self._response_cache_hits += 1
            self._response_cache_saved_input_tokens += cached.input_tokens
            self._response_cache_saved_output_tokens += cached.output_tokens
            self._response_cache_saved_usd += cached.cost_usd
            return (
                replace(
                    cached,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    raw=None,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                    prompt_cache_eligible=False,
                    response_cache_hit=True,
                    response_cache_saved_input_tokens=cached.input_tokens,
                    response_cache_saved_output_tokens=cached.output_tokens,
                    response_cache_saved_usd=cached.cost_usd,
                ),
                "hit",
            )

    def _response_cache_put(
        self, key: str, response: LLMResponse, now: float
    ) -> None:
        if not response.text or response.tool_calls:
            return
        cached = replace(
            response,
            raw=None,
            response_cache_hit=False,
            response_cache_saved_input_tokens=0,
            response_cache_saved_output_tokens=0,
            response_cache_saved_usd=0.0,
        )
        with self._response_cache_lock:
            self._response_cache[key] = (now + self._response_cache_ttl, cached)
            self._response_cache.move_to_end(key)
            self._response_cache_stores += 1
            while len(self._response_cache) > self._response_cache_max:
                self._response_cache.popitem(last=False)
                self._response_cache_evictions += 1

    def _record_prompt_cache(
        self, response: LLMResponse, spec: ModelSpec
    ) -> PromptCacheStats:
        if not response.prompt_cache_eligible:
            return self.prompt_cache_stats
        read_saved = (
            response.cache_read_input_tokens
            * (1.0 - spec.cache_read_multiplier)
            * spec.input_price_per_mtok
            / 1_000_000
        )
        write_premium = (
            response.cache_creation_input_tokens
            * (spec.cache_write_5m_multiplier - 1.0)
            * spec.input_price_per_mtok
            / 1_000_000
        )
        with self._cache_lock:
            self._cache_eligible_calls += 1
            self._cache_hits += int(response.cache_read_input_tokens > 0)
            self._cache_write_tokens += response.cache_creation_input_tokens
            self._cache_read_tokens += response.cache_read_input_tokens
            self._cache_net_saved_usd += read_saved - write_premium
            return PromptCacheStats(
                eligible_calls=self._cache_eligible_calls,
                hits=self._cache_hits,
                write_tokens=self._cache_write_tokens,
                read_tokens=self._cache_read_tokens,
                net_saved_usd=self._cache_net_saved_usd,
            )

    def _priciest_confidential(self) -> ModelSpec:
        """가장 비싼 confidential 단가. **모르는 모델의 상한으로만 쓴다.**"""
        specs = [s for s in self._registry.values()
                 if s.max_sensitivity.rank() >= Sensitivity.CONFIDENTIAL.rank()]
        if not specs:
            raise ModelNotAllowed("No confidential model tariff for external review")
        return max(specs, key=lambda spec: spec.cost(1_000_000, 1_000_000))

    def check_external_review(self, workspace: str, *, input_chars: int) -> float:
        """호출 **전** 한도를 본다. 자리를 잡아 두지는 않는다.

        봉투를 어디까지 키울지가 이 함수의 전부다. 너무 키우면 **첫 호출부터**
        한도에 걸리고, 그 차단은 「오늘은 요약이 안 되네」 로만 보인다. 너무 줄이면
        한도를 넘긴 뒤에야 안다.

        worker의 검토 기본 출력 예산을 사용한다. 후보 JSON 외의 모델 출력도 예산을
        쓰므로 후보 10건만 보고 출력이 짧다고 가정하지 않는다. 이것은 견적이지
        모든 사용자 지정 worker 설정의 비용을 보장하는 상한은 아니다.

        입력은 글자 수를 그대로 토큰으로 센다. 한국어는 글자보다 토큰이 적은 쪽이
        흔해 **보수적인 방향**이고, 토크나이저를 여기서 흉내 내지 않는다.
        """
        spec = self._priciest_confidential()
        reservation = spec.cost(input_chars + 4096, REVIEW_OUTPUT_TOKEN_ENVELOPE)
        self._cost.check(reservation, workspace=workspace)
        return reservation

    def record_external_review(self, workspace: str, metrics: dict, *, reservation: float) -> None:
        """끝난 호출의 비용. **실제 청구액이 아니라 우리 추정이다.**

        세 갈래다.

        1. 모델이 단가표에 있다 — 보고된 토큰으로 계산한다. 가장 정확하다
        2. 모델을 모르는데 **토큰 수는 받았다** — 가장 비싼 confidential 단가로
           그 토큰을 계산한다. 보수적이되 **자릿수가 맞는다**. 여기서 봉투를 쓰면
           실제의 수십 배가 기록되고, 그 숫자가 한도를 먹는다
        3. 아무것도 못 받았다(전송 실패·서명 불일치) — 그때만 봉투를 쓴다.
           정말로 모르는 상태이고, 모르면 비싸게 잡는 쪽이 맞다
        """
        spec = self._registry.get(str(metrics.get("model") or ""))
        if spec is not None:
            usd = spec.cost(metrics["input_tokens"], metrics["output_tokens"])
        elif _reported(metrics):
            fallback = self._priciest_confidential()
            usd = fallback.cost(metrics["input_tokens"], metrics["output_tokens"])
            logger.warning(
                "external_review_cost ws=%s model=%s estimated=true tariff=%s usd=%.4f"
                " — 단가표에 없는 모델입니다. DEFAULT_REGISTRY 에 더하세요.",
                workspace, metrics.get("model") or "-", fallback.model, usd,
            )
        else:
            usd = reservation
            logger.warning(
                "external_review_cost ws=%s estimated=true source=envelope usd=%.4f"
                " — 사용량을 못 받아 보수적으로 기록합니다.",
                workspace, usd,
            )
        self._cost.record(usd, workspace=workspace)

    def spent_today_for(self, workspace: str) -> float:
        """이 워크스페이스의 당일 누적. 화면이 「누가 썼나」 를 말할 근거다."""
        return self._cost.spent_by_workspace().get(workspace.strip().lower(), 0.0)

    def limit_for(self, workspace: str) -> float | None:
        """이 워크스페이스에 실제로 걸리는 상한. 없으면 `None`(전체 상한만)."""
        return self._cost.limit_for(workspace)

    @property
    def default_model(self) -> str:
        return self._default_model

    def restricted_to(self, model: str, *, sensitivity: Sensitivity) -> Router:
        """Share accounting but expose only one approved model and no fallback."""
        spec = self.resolve(model, sensitivity)
        return Router(
            providers={spec.provider: self._providers[spec.provider]},
            registry={spec.model: spec},
            cost_guard=self._cost,
            default_model=spec.model,
            fallback_models=(),
            response_cache_ttl_seconds=self._response_cache_ttl,
            response_cache_max_entries=self._response_cache_max,
        )

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
        # Only trusted orchestration code may opt in. The scope must bind the
        # current requester permissions and source revisions; raw user input is
        # never logged and the final key is a SHA-256 digest.
        cache_scope: str | None = None,
    ) -> LLMResponse:
        spec = self.resolve(model, sensitivity)
        cache_key, cache_reason = _response_cache_key(
            cache_scope=cache_scope,
            spec=spec,
            sensitivity=sensitivity,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            tools=tools,
            enabled=self._response_cache_ttl > 0 and self._response_cache_max > 0,
        )
        if cache_key is not None:
            cached, lookup_reason = self._response_cache_get(
                cache_key, time.monotonic()
            )
            if cached is not None:
                stats = self.response_cache_stats
                logger.info(
                    "llm_response_cache hit model=%s key=%s hits=%d/%d"
                    " saved_in=%d saved_out=%d saved_usd=$%.4f",
                    cached.model,
                    cache_key[:12],
                    stats.hits,
                    stats.lookups,
                    cached.response_cache_saved_input_tokens,
                    cached.response_cache_saved_output_tokens,
                    cached.response_cache_saved_usd,
                )
                return cached
            logger.info(
                "llm_response_cache miss model=%s key=%s reason=%s",
                spec.model,
                cache_key[:12],
                lookup_reason,
            )
        elif cache_scope:
            logger.info("llm_response_cache bypass model=%s reason=%s", spec.model, cache_reason)
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
        cache_stats = self._record_prompt_cache(resp, candidate)
        # **어느 워크스페이스가 썼는지 로그에 남긴다.** 안 남기면 「누가 지갑을
        # 비웠나」 를 사후에 되짚을 수 없다 — 2026-09-15 에 그 질문에 답하지 못했다.
        workspace = current_workspace()
        logger.info(
            "llm_call model=%s provider=%s ws=%s in=%d out=%d cost=$%.4f"
            " cache_write=%d cache_read=%d cache_eligible=%s"
            " cache_hits=%d/%d cache_net_saved=$%.4f"
            " ws_today=$%.2f spent_today=$%.2f",
            resp.model,
            resp.provider,
            workspace or "-",
            resp.input_tokens,
            resp.output_tokens,
            resp.cost_usd,
            resp.cache_creation_input_tokens,
            resp.cache_read_input_tokens,
            resp.prompt_cache_eligible,
            cache_stats.hits,
            cache_stats.eligible_calls,
            cache_stats.net_saved_usd,
            self._cost.spent_by_workspace().get(workspace, 0.0),
            self._cost.spent_today,
        )
        if cache_key is not None:
            self._response_cache_put(cache_key, resp, time.monotonic())
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


def _cache_ttl(value: float | None) -> float:
    raw = os.getenv("TYBOT_LLM_RESPONSE_CACHE_TTL_SECONDS", "300") if value is None else value
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return 300.0
    return max(0.0, parsed)


def _cache_max(value: int | None) -> int:
    raw = os.getenv("TYBOT_LLM_RESPONSE_CACHE_MAX_ENTRIES", "256") if value is None else value
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return 256
    return max(0, parsed)


def _response_cache_key(
    *,
    cache_scope: str | None,
    spec: ModelSpec,
    sensitivity: Sensitivity,
    messages: Sequence[Message],
    max_tokens: int,
    temperature: float,
    tools: Sequence[ToolSpec],
    enabled: bool,
) -> tuple[str | None, str]:
    if not enabled:
        return None, "disabled"
    if not cache_scope:
        return None, "scope_missing"
    if tools:
        return None, "tools_present"
    if any(not isinstance(message.content, str) for message in messages):
        return None, "structured_content"
    payload = {
        "v": 1,
        "scope": cache_scope,
        "model": spec.model,
        "provider": spec.provider,
        "sensitivity": sensitivity.value,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "messages": [(message.role, message.content) for message in messages],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest(), "eligible"


def _left(timeout_seconds: float | None, started: float) -> float | None:
    """폴백 후보에게 남은 시간. 상한이 없으면 `None`.

    후보마다 새로 상한을 주지 않는다 — 그러면 후보 수만큼 시간이 늘어나고,
    바깥에서 본 「한 번의 호출」 이 실제로는 몇 배가 된다.
    """
    if timeout_seconds is None:
        return None
    return timeout_seconds - (time.monotonic() - started)
