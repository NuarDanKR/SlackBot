from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot.access import RequestContext
from tybot.console import rule_test, specialist_store
from tybot.specialist_router import SUCCESS, SpecialistAnswer, SpecialistOutcome


def _row(**overrides):
    row = {
        "key": "hermes",
        "name": "Hermes",
        "domain": "내부 문서",
        "routing_hint": "",
        "adapter": "hermes",
        "model": "",
        "min_confidence": 0.6,
        "rules": "현재 규칙",
        "execution_mode": "tools",
        "workspaces": ["tyit"],
    }
    row.update(overrides)
    return row


class FakeRouter:
    def __init__(self):
        self.spent = 0.0

    def spent_today_for(self, _workspace):
        return self.spent


def test_rule_test_runs_same_specialist_with_current_and_draft_rules(monkeypatch):
    seen = []
    router = FakeRouter()

    def preview(specialist, **kwargs):
        seen.append((specialist.rules, kwargs["variant"], kwargs["authorization_id"]))
        router.spent += 0.01
        answer = SpecialistAnswer(
            text=f"{kwargs['variant']} 답변", specialist="hermes", model="fake",
            cost_usd=0.01,
        )
        return SpecialistOutcome(SUCCESS, answer=answer, selected="hermes")

    monkeypatch.setattr(rule_test, "preview_rules", preview)
    result = rule_test.run(
        _row(), draft_rules="편집 규칙", question="공사 현황을 알려줘",
        workspace="tyit", actor="dev@example.com",
        ctx=RequestContext(workspace="tyit", channels=frozenset({"#allowed"})),
        store=SimpleNamespace(search=lambda *_args, **_kwargs: []), router=router,
    )

    assert [(item[0], item[1]) for item in seen] == [
        ("현재 규칙", "current"), ("편집 규칙", "draft"),
    ]
    assert seen[0][2] == seen[1][2]
    assert result["current"]["costUsd"] == pytest.approx(0.01)
    assert result["draft"]["costUsd"] == pytest.approx(0.01)
    assert result["draftRulesHash"] == rule_test.rules_hash("편집 규칙")


def test_rule_test_refuses_a_workspace_not_approved_for_the_specialist():
    with pytest.raises(rule_test.RuleTestError, match="승인된 워크스페이스"):
        rule_test.run(
            _row(), draft_rules="편집", question="질문", workspace="mgmt",
            actor="dev@example.com",
            ctx=RequestContext(workspace="mgmt"),
            store=SimpleNamespace(), router=FakeRouter(),
        )


def test_console_actor_context_uses_slack_memberships(monkeypatch):
    cfg = SimpleNamespace(
        key="tyit", bot_token="xoxb-test", readable=frozenset({"shared"}),
        is_root=False,
    )
    monkeypatch.setattr(rule_test, "load_workspaces", lambda: [cfg])

    class Client:
        def __init__(self, **_kwargs):
            pass

        def users_lookupByEmail(self, **_kwargs):
            return {"user": {"id": "U1"}}

        def users_conversations(self, **_kwargs):
            return {
                "channels": [{"name": "one"}, {"name": "private"}],
                "response_metadata": {"next_cursor": ""},
            }

    import slack_sdk

    monkeypatch.setattr(slack_sdk, "WebClient", Client)
    ctx = rule_test._request_context("dev@example.com", "tyit")

    assert ctx.channels == frozenset({"#one", "#private"})
    assert ctx.readable_workspaces == frozenset({"shared"})
    assert not ctx.is_root


def test_rule_test_schema_is_derived_and_expires():
    sql = Path("deploy/sql/specialist_rules_schema.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS specialist_rule_test" in sql
    assert "expires_at" in sql
    assert "raw_line" not in sql
    assert "GRANT SELECT, INSERT, DELETE ON TABLE specialist_rule_test" in sql


class _AttachedTestCursor:
    def __init__(self, test: dict, current_rules: str):
        self.test = test
        self.current_rules = current_rules
        self.selected = ""

    def execute(self, sql, _params=()):
        self.selected = "test" if "FROM specialist_rule_test" in sql else "specialist"

    def fetchone(self):
        if self.selected == "specialist":
            return {"rules": self.current_rules}
        return self.test


def test_attached_rule_test_is_rechecked_against_the_current_rules():
    draft = "편집 규칙"
    test = {
        "id": "11111111-1111-1111-1111-111111111111",
        "specialist": "hermes", "workspace": "tyit",
        "requester": "dev@example.com", "question": "질문",
        "current_rules_hash": rule_test.rules_hash("시험 당시 규칙"),
        "draft_rules_hash": rule_test.rules_hash(draft),
        "current_result": {}, "draft_result": {},
        "created_at": "2026-09-17T00:00:00+09:00",
        "expires_at": "2026-10-17T00:00:00+09:00",
    }
    proposal = {
        "key": "hermes", "workspaces": ["tyit"], "rules": draft,
        "ruleTestId": test["id"],
    }

    with pytest.raises(specialist_store.SpecialistStoreError, match="일치하지 않습니다"):
        specialist_store._validate_attached_rule_test(
            _AttachedTestCursor(test, "그 뒤 바뀐 규칙"),
            proposal,
            actor="dev@example.com",
        )
