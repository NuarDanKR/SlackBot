"""Construct the shared answer engine under VIPBot-only routing constraints."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .access import RequestContext
from .answer import AnswerEngine
from .archive.store import ArchiveStore
from .gateway.providers.anthropic_provider import AnthropicProvider
from .gateway.router import Router
from .vip_profile import VipProfile

ARCHIVE_UNCONFIGURED = "vipbot_archive_unconfigured"
ARCHIVE_UNREADABLE = "vipbot_archive_unreadable"
MODEL_CREDENTIAL_INVALID = "vipbot_model_credential_invalid"

#: VIPBot 근거 줄 수. TYBot 기본(20)보다 넓게 본다 — 종합 질문(「Top5 를 표로」)은 여러
#: 문서를 함께 봐야 답이 된다(2026-11-12 오너 요청: 가능한 전수 탐색).
VIP_MAX_HITS = 60
#: 한 문서가 차지하는 근거 줄 상한. 새 문서 하나가 상위 자리를 독차지하지 않게 한다.
VIP_PER_DOCUMENT_HITS = 4


class VipbotEngineError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _managed_scope(ctx: RequestContext) -> bool:
    """VIPBot may answer only with an explicit, non-empty managed source set."""
    return bool(ctx.evidence_sources)


def build_engine(
    *,
    archive_dir: str | Path,
    profile: VipProfile,
    anthropic_api_key: str,
    provider_factory: Callable[..., object] = AnthropicProvider,
) -> AnswerEngine:
    """Build an Anthropic-only engine without shared secrets or model fallback."""
    raw_archive = str(archive_dir or "").strip()
    if not raw_archive or not Path(raw_archive).is_absolute():
        raise VipbotEngineError(ARCHIVE_UNCONFIGURED)
    root = Path(raw_archive)
    if not root.is_dir():
        raise VipbotEngineError(ARCHIVE_UNREADABLE)

    key = str(anthropic_api_key or "")
    if not key.startswith("sk-ant-"):
        raise VipbotEngineError(MODEL_CREDENTIAL_INVALID)
    provider = provider_factory(api_key=key)
    router = Router.from_default_registry(
        providers={"anthropic": provider},
        default_model=profile.model,
        daily_limit_usd=profile.daily_limit_usd,
        cost_state_path=profile.cost_state_path,
        fallback_models=(),
    )
    return AnswerEngine(
        ArchiveStore(root),
        router,
        prompts=profile.prompts,
        max_output_tokens=profile.max_output_tokens,
        allow_master_business_answers=_managed_scope,
        response_cache_namespace="vipbot",
        # 근거 채널이 관리자가 고른 집합이라 훑을 범위가 정해져 있다. 색인 후보 창(500줄)
        # 에서 잘린 문서는 「근거 없음」 으로만 보이므로 보이는 문서를 전부 점수 매긴다.
        max_hits=VIP_MAX_HITS,
        exhaustive_search=True,
        per_document_hits=VIP_PER_DOCUMENT_HITS,
    )


__all__ = [
    "ARCHIVE_UNCONFIGURED",
    "ARCHIVE_UNREADABLE",
    "MODEL_CREDENTIAL_INVALID",
    "VipbotEngineError",
    "build_engine",
]
