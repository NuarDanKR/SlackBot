import pytest

from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard, attribute_to
from tybot.gateway.router import ModelNotAllowed, Router, UnknownModel


class Provider:
    def __init__(self, name, *, broken=False):
        self.name = name
        self.broken = broken
        self.calls = []

    def complete(self, spec, messages, **kwargs):
        self.calls.append(spec.model)
        if self.broken:
            raise RuntimeError("provider failed")
        return LLMResponse("ok", spec.model, self.name, 1, 1, 0.01)


def routers(*, broken=False):
    one = Provider("anthropic", broken=broken)
    two = Provider("openai")
    parent = Router(
        {"anthropic": one, "openai": two},
        {"approved": ModelSpec("approved", "anthropic", 1, 1, Sensitivity.CONFIDENTIAL),
         "fallback": ModelSpec("fallback", "openai", 1, 1, Sensitivity.CONFIDENTIAL)},
        CostGuard(1), default_model="approved", fallback_models=("fallback",),
    )
    return parent, one, two


def test_restricted_gateway_never_sends_failed_pages_to_fallback_provider():
    parent, one, two = routers(broken=True)
    scoped = parent.restricted_to("approved", sensitivity=Sensitivity.CONFIDENTIAL)
    with pytest.raises(RuntimeError):
        scoped.complete([Message("user", "page")])
    assert one.calls == ["approved"] and two.calls == []
    assert parent.complete([Message("user", "ordinary question")]).model == "fallback"
    assert two.calls == ["fallback"], "ordinary routing policy must remain unchanged"


def test_restricted_gateway_also_rejects_explicit_other_model_before_transmission():
    parent, one, two = routers()
    scoped = parent.restricted_to("approved", sensitivity=Sensitivity.CONFIDENTIAL)
    with pytest.raises(UnknownModel):
        scoped.complete([], model="fallback")
    assert one.calls == two.calls == []


def test_restricted_gateway_shares_the_same_workspace_accounting():
    parent, one, two = routers()
    scoped = parent.restricted_to("approved", sensitivity=Sensitivity.CONFIDENTIAL)
    with attribute_to("tyit"):
        assert scoped.complete([]).model == "approved"
    assert parent.spent_today == scoped.spent_today == 0.01
    assert parent.spent_today_for("tyit") == 0.01
    assert one.calls == ["approved"] and two.calls == []


def test_restriction_does_not_bypass_sensitivity_or_unknown_models():
    parent, one, two = routers()
    with pytest.raises(UnknownModel):
        parent.restricted_to("missing", sensitivity=Sensitivity.CONFIDENTIAL)
    parent._registry["internal"] = ModelSpec("internal", "anthropic", 1, 1, Sensitivity.INTERNAL)
    with pytest.raises(ModelNotAllowed):
        parent.restricted_to("internal", sensitivity=Sensitivity.CONFIDENTIAL)
    assert one.calls == two.calls == []
