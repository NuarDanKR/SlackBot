import pytest

from tybot.master_v2_rollout import (
    ENV_NAME,
    RolloutConfigError,
    enabled_workspaces,
    is_enabled,
)


def test_new_master_router_is_off_by_default():
    assert enabled_workspaces({}) == frozenset()
    assert not is_enabled("tyit", {})


def test_rollout_can_be_limited_to_tyit():
    env = {ENV_NAME: "tyit"}

    assert is_enabled("tyit", env)
    assert not is_enabled("mgmt", env)
    assert not is_enabled("pilot", env)
    assert not is_enabled("invest", env)
    assert not is_enabled("archi", env)
    assert not is_enabled("civil", env)


def test_rollout_accepts_an_explicit_reviewed_list():
    env = {ENV_NAME: "tyit, invest,tyit"}

    assert enabled_workspaces(env) == frozenset({"tyit", "invest"})


@pytest.mark.parametrize("value", ["*", "tyit,*", "../tyit", "ty it", ",tyit"])
def test_rollout_rejects_wildcards_and_malformed_keys(value):
    with pytest.raises(RolloutConfigError):
        enabled_workspaces({ENV_NAME: value})
