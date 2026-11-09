"""VIP policy adapters for the shared TYBot workspace pipeline."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .access import RequestContext
from .answer import AnswerEngine
from .archive.store import ArchiveStore
from .db import connect as db_connect
from .gateway.router import Router
from .slack.pilot import RequestAuthorizationDenied
from .vip_access import authorize_slack
from .vip_profile import VipProfile, question_for_engine


def context_resolver(
    workspace: str, *, connect: Callable = db_connect
) -> Callable[[object, str], RequestContext]:
    """Return the only VIP-specific authorization seam in ``WorkspaceBot``."""

    def resolve(client: object, slack_user: str) -> RequestContext:
        decision = authorize_slack(
            connect=connect,
            client=client,
            workspace=workspace,
            slack_user=slack_user,
        )
        if not decision.allowed:
            raise RequestAuthorizationDenied(decision.code)
        try:
            return decision.context()
        except ValueError:
            raise RequestAuthorizationDenied(decision.code) from None

    return resolve


def build_engine(
    *,
    store: ArchiveStore,
    profile: VipProfile,
    specialist=None,
    specialist_factory: Callable[[Router], object] | None = None,
    workspace_labels: Mapping[str, str] | None = None,
) -> AnswerEngine:
    """Build the common answer engine with only VIP model/prompt/budget choices."""
    if specialist is not None and specialist_factory is not None:
        raise ValueError("choose specialist or specialist_factory")
    router = Router.from_default_registry(
        default_model=profile.model,
        daily_limit_usd=profile.daily_limit_usd,
        cost_state_path=profile.cost_state_path,
        fallback_models=(),
    )
    if specialist_factory is not None:
        specialist = specialist_factory(router)
    return AnswerEngine(
        store,
        router,
        specialist=specialist,
        prompts=profile.prompts,
        max_output_tokens=profile.max_output_tokens,
        workspace_labels=workspace_labels,
        allow_master_business_answers=lambda ctx: bool(ctx.evidence_sources),
    )


__all__ = [
    "build_engine",
    "context_resolver",
    "question_for_engine",
]
