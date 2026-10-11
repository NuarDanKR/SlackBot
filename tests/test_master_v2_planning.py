from types import SimpleNamespace

import pytest

from tybot.failures import failure_message
from tybot.intent import PlanningUnavailable, plan


class BrokenRouter:
    def complete(self, *args, **kwargs):
        raise RuntimeError("provider unavailable")


class InvalidRouter:
    def complete(self, *args, **kwargs):
        return SimpleNamespace(text="not json", model="test", cost_usd=0.0)


def test_master_v2_does_not_use_rules_without_a_router():
    with pytest.raises(PlanningUnavailable):
        plan("이번주 요약해줘", None, allow_rule_fallback=False)


def test_master_v2_does_not_use_rules_when_the_model_call_fails():
    with pytest.raises(PlanningUnavailable):
        plan("이번주 요약해줘", BrokenRouter(), allow_rule_fallback=False)


def test_master_v2_does_not_use_rules_for_invalid_model_output():
    with pytest.raises(PlanningUnavailable):
        plan("이번주 요약해줘", InvalidRouter(), allow_rule_fallback=False)


def test_legacy_workspaces_keep_the_existing_rule_fallback():
    assert plan("이번주 요약해줘", BrokenRouter())[0].source == "regex"


def test_planning_failure_message_does_not_expose_provider_details():
    shown = failure_message(PlanningUnavailable("secret upstream detail"))

    assert "분류할 수 없어" in shown
    assert "secret" not in shown
