"""호출자 등급별 Hermes 모델 (2026-11-12 오너 결정: TYBot→Sonnet, VIPBot→Opus).

요청은 모델을 **말할 수만** 있고, 받을지는 Hermes 서버 `allowed_models` 가 정한다.
비우면 예전처럼 본문에 model 이 없다 — Hermes 기본 모델이 답한다.
"""

from __future__ import annotations

import json

import pytest

from tybot import specialist_wire_v3 as wire


def _request(**over):
    base = dict(workspace="mgmt", authorization_id="auth-1", question="질문", tool_token="tok")
    base.update(over)
    return wire.Request(**base)


def test_no_model_keeps_the_old_body():
    assert "model" not in json.loads(_request().body())


@pytest.mark.parametrize("model", ["claude-sonnet-5-5", "claude-opus-5-5"])
def test_the_caller_tier_model_is_sent(model):
    assert json.loads(_request(model=model).body())["model"] == model


@pytest.mark.parametrize("model", ["Opus 5", "claude/opus", "-x", "a" * 65])
def test_malformed_models_are_refused_before_sending(model):
    with pytest.raises(wire.WireViolation):
        _request(model=model)


def test_the_hermes_side_uses_the_same_model_shape():
    """두 곳의 모양 규칙이 갈리면 한쪽만 통과하는 모델 id 가 400 이 된다."""
    from pathlib import Path

    js = (Path(__file__).resolve().parents[1] / "subbots/hermes/src/rpc/wire.js").read_text(
        encoding="utf-8"
    )
    assert "MODEL_ID_RE = /^[a-z0-9][a-z0-9.-]{0,63}$/" in js
    assert wire.MODEL_ID_RE.pattern == "[a-z0-9][a-z0-9.-]{0,63}"
