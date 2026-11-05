from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tybot import vip_profile

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "tybot" / "vip_profile.py"
UNIT = ROOT / "deploy" / "tybot-vipbot.service"


def _code(call) -> str:
    with pytest.raises(vip_profile.VipProfileError) as caught:
        call()
    return caught.value.code


def test_profile_uses_the_reviewed_high_quality_model_and_separate_budget():
    profile = vip_profile.load_profile({"VIPBOT_DAILY_LIMIT_USD": "12.50"})

    assert profile.revision == "vip-executive-v1"
    assert profile.model == "claude-opus-5-5"
    assert profile.daily_limit_usd == 12.5
    assert profile.cost_state_path == "/var/lib/tybot-vipbot/cost-state.json"
    assert profile.max_output_tokens == 1800


def test_missing_cost_ceiling_does_not_fall_back_to_the_tybot_budget():
    assert _code(lambda: vip_profile.load_profile({})) == (
        vip_profile.COST_LIMIT_UNCONFIGURED
    )


def test_scientific_cost_ceiling_is_explicitly_supported():
    profile = vip_profile.load_profile({"VIPBOT_DAILY_LIMIT_USD": "1e-9"})

    assert profile.daily_limit_usd == 1e-9


def test_the_persistent_cost_ledger_has_a_private_systemd_state_directory():
    unit = UNIT.read_text(encoding="utf-8")

    assert vip_profile.COST_STATE_PATH == "/var/lib/tybot-vipbot/cost-state.json"
    assert "StateDirectory=tybot-vipbot" in unit
    assert "StateDirectoryMode=0750" in unit


@pytest.mark.parametrize(
    "value", ["0", "-1", "nan", "inf", "not-money", "1_0", "+10", ".5"]
)
def test_invalid_cost_ceiling_is_refused(value):
    assert _code(
        lambda: vip_profile.load_profile({"VIPBOT_DAILY_LIMIT_USD": value})
    ) == vip_profile.COST_LIMIT_INVALID


@pytest.mark.parametrize(
    "model", ["claude-haiku-5-5", "claude-sonnet-5-5", "gpt-4o", ""]
)
def test_an_unreviewed_or_weaker_model_cannot_replace_the_vip_model(model):
    assert _code(
        lambda: vip_profile.load_profile(
            {"VIPBOT_MODEL": model, "VIPBOT_DAILY_LIMIT_USD": "10"}
        )
    ) == vip_profile.MODEL_NOT_APPROVED


def test_all_answer_modes_keep_the_source_rules_and_executive_contract():
    profile = vip_profile.load_profile({"VIPBOT_DAILY_LIMIT_USD": "10"})

    for prompt in (
        profile.prompts.answer,
        profile.prompts.summary,
        profile.prompts.advice,
        profile.prompts.interim_advice,
    ):
        assert "원문" in prompt
        assert "BLUF" in prompt
        assert "재무적 영향" in prompt
        assert "운영 리스크" in prompt
        assert "대안 2~3개" in prompt
        assert "출처를 지어내" in prompt


def test_common_answer_engine_accepts_the_profile_without_changing_defaults():
    from tybot.answer import AnswerEngine, AnswerPrompts

    default = AnswerEngine(store=None, router=None)
    profile = vip_profile.load_profile({"VIPBOT_DAILY_LIMIT_USD": "10"})
    vip = AnswerEngine(
        store=None,
        router=None,
        prompts=profile.prompts,
        max_output_tokens=profile.max_output_tokens,
    )

    assert default._prompts == AnswerPrompts()
    assert vip._prompts == profile.prompts
    assert default._output_limit(2048) == 2048
    assert vip._output_limit(2048) == profile.max_output_tokens
    assert vip._output_limit(1024) == profile.max_output_tokens
    assert vip._output_limit(1000) == profile.max_output_tokens


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_common_answer_engine_rejects_an_invalid_output_ceiling(value):
    from tybot.answer import AnswerEngine

    with pytest.raises((TypeError, ValueError)):
        AnswerEngine(store=None, router=None, max_output_tokens=value)


def test_user_model_flags_are_removed_including_repeated_flags():
    assert vip_profile.question_for_engine(
        "--model=gpt-4o 오늘 현황 --model=claude-haiku-5-5 요약"
    ) == "오늘 현황  요약"


def test_profile_module_has_no_provider_database_or_slack_dependency():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imports = {
        alias.name.split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imports.update(
        (node.module or "").split(".", 1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.level == 0
    )

    assert not imports & {
        "anthropic",
        "openai",
        "psycopg",
        "requests",
        "slack_bolt",
        "slack_sdk",
    }
