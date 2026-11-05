from __future__ import annotations

import pytest

from tybot import vip_profile, vipbot_engine
from tybot.access import RequestContext
from tybot.gateway.providers import anthropic_provider

KEY = "sk-" + "ant-synthetic-vipbot-key-000000000000"


def profile(tmp_path):
    loaded = vip_profile.load_profile({"VIPBOT_DAILY_LIMIT_USD": "7.5"})
    return vip_profile.VipProfile(
        revision=loaded.revision,
        model=loaded.model,
        daily_limit_usd=loaded.daily_limit_usd,
        cost_state_path=str(tmp_path / "cost.json"),
        max_output_tokens=loaded.max_output_tokens,
        prompts=loaded.prompts,
    )


def code(call) -> str:
    with pytest.raises(vipbot_engine.VipbotEngineError) as caught:
        call()
    return caught.value.code


def test_the_engine_uses_only_the_explicit_anthropic_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(
        anthropic_provider,
        "_resolve_key",
        lambda: (_ for _ in ()).throw(AssertionError("shared resolver was called")),
    )
    engine = vipbot_engine.build_engine(
        archive_dir=tmp_path,
        profile=profile(tmp_path),
        anthropic_api_key=KEY,
    )
    assert set(engine.router._providers) == {"anthropic"}
    assert engine.router._fallback_models == ()
    assert engine.router.default_model == vip_profile.APPROVED_MODEL


def test_the_executive_profile_and_output_limit_reach_the_common_engine(tmp_path):
    expected = profile(tmp_path)
    engine = vipbot_engine.build_engine(
        archive_dir=tmp_path,
        profile=expected,
        anthropic_api_key=KEY,
    )
    assert engine._prompts == expected.prompts
    assert engine._max_output_tokens == expected.max_output_tokens
    assert engine._output_limit(1000) == vip_profile.MAX_OUTPUT_TOKENS


def test_master_answers_open_only_for_managed_vip_sources(tmp_path):
    engine = vipbot_engine.build_engine(
        archive_dir=tmp_path,
        profile=profile(tmp_path),
        anthropic_api_key=KEY,
    )
    managed = RequestContext(
        workspace="tyit",
        evidence_sources=frozenset({("mgmt", "C0SOURCE0")}),
    )
    empty = RequestContext(workspace="tyit", evidence_sources=frozenset())
    ordinary = RequestContext(workspace="tyit")
    assert engine._master_answers_allowed(managed) is True
    assert engine._master_answers_allowed(empty) is False
    assert engine._master_answers_allowed(ordinary) is False


@pytest.mark.parametrize(
    ("archive", "key", "expected"),
    [
        ("", KEY, vipbot_engine.ARCHIVE_UNCONFIGURED),
        ("relative/archive", KEY, vipbot_engine.ARCHIVE_UNCONFIGURED),
        (None, KEY, vipbot_engine.ARCHIVE_UNCONFIGURED),
        (None, "", vipbot_engine.ARCHIVE_UNCONFIGURED),
    ],
)
def test_invalid_archive_configuration_fails_before_provider_construction(
    tmp_path, archive, key, expected
):
    called = False

    def provider_factory(**_kwargs):
        nonlocal called
        called = True

    got = code(
        lambda: vipbot_engine.build_engine(
            archive_dir=archive,
            profile=profile(tmp_path),
            anthropic_api_key=key,
            provider_factory=provider_factory,
        )
    )
    assert got == expected
    assert called is False


def test_missing_absolute_archive_fails_before_provider_construction(tmp_path):
    called = False

    def provider_factory(**_kwargs):
        nonlocal called
        called = True

    missing = tmp_path / "missing"
    got = code(
        lambda: vipbot_engine.build_engine(
            archive_dir=missing,
            profile=profile(tmp_path),
            anthropic_api_key=KEY,
            provider_factory=provider_factory,
        )
    )
    assert got == vipbot_engine.ARCHIVE_UNREADABLE
    assert called is False


def test_invalid_model_key_never_enters_the_shared_provider(tmp_path):
    called = False

    def provider_factory(**_kwargs):
        nonlocal called
        called = True

    assert (
        code(
            lambda: vipbot_engine.build_engine(
                archive_dir=tmp_path,
                profile=profile(tmp_path),
                anthropic_api_key="",
                provider_factory=provider_factory,
            )
        )
        == vipbot_engine.MODEL_CREDENTIAL_INVALID
    )
    assert called is False
