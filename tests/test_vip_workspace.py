from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tybot import vip_workspace
from tybot.access import RequestContext
from tybot.slack.pilot import RequestAuthorizationDenied
from tybot.vip_profile import load_profile


def test_allowed_vip_request_becomes_a_managed_request_context(monkeypatch) -> None:
    sources = frozenset({("mgmt", "C1")})
    decision = SimpleNamespace(
        allowed=True,
        code="allowed",
        context=lambda: RequestContext(
            workspace="tyit", user_id="U1", evidence_sources=sources
        ),
    )
    authorize = Mock(return_value=decision)
    monkeypatch.setattr(vip_workspace, "authorize_slack", authorize)
    connect = Mock()

    context = vip_workspace.context_resolver("tyit", connect=connect)(
        "client", "U1"
    )

    assert context.evidence_sources == sources
    authorize.assert_called_once_with(
        connect=connect,
        client="client",
        workspace="tyit",
        slack_user="U1",
    )


@pytest.mark.parametrize("code", ["no_grant", "no_sources", "check_failed"])
def test_denied_vip_request_never_produces_a_context(monkeypatch, code) -> None:
    monkeypatch.setattr(
        vip_workspace,
        "authorize_slack",
        lambda **_kwargs: SimpleNamespace(allowed=False, code=code),
    )

    with pytest.raises(RequestAuthorizationDenied, match=code):
        vip_workspace.context_resolver("tyit")("client", "U1")


def test_vip_engine_is_the_common_answer_engine_with_no_model_fallback(
    monkeypatch,
) -> None:
    router = object()
    router_factory = Mock(return_value=router)
    engine_factory = Mock(return_value="engine")
    monkeypatch.setattr(
        vip_workspace.Router, "from_default_registry", router_factory
    )
    monkeypatch.setattr(vip_workspace, "AnswerEngine", engine_factory)
    profile = load_profile(
        {"VIPBOT_MODEL": "claude-opus-5-5", "VIPBOT_DAILY_LIMIT_USD": "5"}
    )
    specialist_factory = Mock(return_value="specialist")

    result = vip_workspace.build_engine(
        store="store",
        profile=profile,
        specialist_factory=specialist_factory,
        workspace_labels={"mgmt": "경영본부", "civil": "토목본부"},
    )

    assert result == "engine"
    router_factory.assert_called_once_with(
        default_model=profile.model,
        daily_limit_usd=profile.daily_limit_usd,
        cost_state_path=profile.cost_state_path,
        fallback_models=(),
    )
    specialist_factory.assert_called_once_with(router)
    kwargs = engine_factory.call_args.kwargs
    assert kwargs["specialist"] == "specialist"
    assert kwargs["prompts"] is profile.prompts
    assert kwargs["max_output_tokens"] == profile.max_output_tokens
    assert kwargs["workspace_labels"] == {
        "mgmt": "경영본부",
        "civil": "토목본부",
    }


def test_specialist_must_have_one_owner() -> None:
    profile = load_profile({"VIPBOT_DAILY_LIMIT_USD": "5"})

    with pytest.raises(ValueError, match="choose specialist"):
        vip_workspace.build_engine(
            store="store",
            profile=profile,
            specialist="one",
            specialist_factory=lambda _router: "two",
        )
