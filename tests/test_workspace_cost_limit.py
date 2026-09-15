"""워크스페이스별 비용 상한 — 한 지갑 안에 칸을 나눈다.

## 재현하는 사례 (2026-09-15)
경영본부 워크스페이스가 `cost-limit` 으로 답을 못 했다. 콘솔에서 그 워크스페이스
상한을 $2 → $5 → $10 으로 올렸는데 **아무것도 달라지지 않았다.** 이유가 셋이었다.

1. 봇이 `workspace.limit_usd` 를 **읽지 않았다.** 콘솔이 쓰기만 하는 칸이었다.
2. 상한이 **워크스페이스별로 세어지지 않았다.** 다른 곳이 쓴 돈이 여기를 막았다.
3. 오류 문구가 **어느 한도인지 말하지 않았다.** 그래서 엉뚱한 쪽을 올렸다.

아래 시험은 그 셋을 각각 고정한다.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tybot.gateway.budget import WorkspaceLimits
from tybot.gateway.cost import (
    CostGuard,
    CostLimitExceeded,
    attribute_to,
    current_workspace,
)

TODAY = dt.date.today().isoformat()


def guard(*, total=100.0, limits=None, state=None):
    return CostGuard(
        total,
        state_path=state,
        workspace_limits=(lambda: dict(limits)) if limits is not None else None,
    )


# --- 칸 나누기 ----------------------------------------------------------------
def test_one_workspace_cannot_drain_the_whole_wallet():
    """이것이 그날 일어난 일이다. 전체 한도만 있으면 먼저 쓴 쪽이 다 가져간다."""
    g = guard(total=100.0, limits={"mgmt": 5.0, "tyit": 5.0})
    g.record(5.0, workspace="mgmt")

    with pytest.raises(CostLimitExceeded):
        g.check(0.15, workspace="mgmt")
    g.check(0.15, workspace="tyit")  # 옆 칸은 멀쩡하다


def test_workspace_spend_is_counted_separately():
    g = guard(limits={"mgmt": 5.0, "tyit": 5.0})
    g.record(3.0, workspace="mgmt")
    g.record(1.0, workspace="tyit")

    assert g.spent_by_workspace() == {"mgmt": 3.0, "tyit": 1.0}
    assert g.spent_today == pytest.approx(4.0)  # 지갑은 하나다


def test_the_total_limit_is_still_the_outer_bound():
    """워크스페이스 한도가 넉넉해도 지갑을 넘을 수는 없다. 결제는 한 계정에서 나간다."""
    g = guard(total=5.0, limits={"mgmt": 1000.0})
    g.record(4.9, workspace="mgmt")

    with pytest.raises(CostLimitExceeded, match="전체"):
        g.check(0.15, workspace="mgmt")


# --- 어느 한도에 걸렸는지 말한다 ----------------------------------------------
def test_the_message_names_the_workspace_limit():
    g = guard(total=100.0, limits={"mgmt": 5.0})
    g.record(5.0, workspace="mgmt")

    with pytest.raises(CostLimitExceeded) as e:
        g.check(0.15, workspace="mgmt")
    msg = str(e.value)
    assert "mgmt" in msg
    assert "콘솔" in msg  # 어디서 고치는지까지 말한다


def test_the_message_names_the_global_limit():
    g = guard(total=5.0, limits={"mgmt": 1000.0})
    g.record(4.9, workspace="mgmt")

    with pytest.raises(CostLimitExceeded) as e:
        g.check(0.15, workspace="mgmt")
    assert "DAILY_COST_LIMIT_USD" in str(e.value)


def test_the_workspace_limit_is_checked_before_the_global_one():
    """둘 다 걸리면 좁은 쪽을 말한다 — 그쪽이 사람이 고칠 수 있는 칸이다."""
    g = guard(total=5.0, limits={"mgmt": 2.0})
    g.record(4.9, workspace="mgmt")

    with pytest.raises(CostLimitExceeded, match="워크스페이스"):
        g.check(0.15, workspace="mgmt")


# --- 한도를 모를 때는 막지 않는다 ---------------------------------------------
def test_a_workspace_without_a_limit_only_meets_the_global_one():
    g = guard(total=10.0, limits={"mgmt": 1.0})
    g.record(5.0, workspace="unknown")
    g.check(1.0, workspace="unknown")  # 통과


def test_zero_is_not_a_limit_it_is_unset():
    """0 을 한도로 읽으면 그 워크스페이스는 영원히 답을 못 하고, 화면은 빈 칸이다."""
    g = guard(total=10.0, limits={"mgmt": 0.0})
    g.check(1.0, workspace="mgmt")
    assert g.limit_for("mgmt") is None


def test_a_broken_limit_source_does_not_block_answers():
    """DB 가 잠깐 죽었다고 전 워크스페이스가 답을 못 하는 쪽이 더 나쁘다."""
    def boom():
        raise RuntimeError("DB 연결 실패")

    g = CostGuard(10.0, workspace_limits=boom)
    g.check(1.0, workspace="mgmt")  # 전체 한도만 적용된다


def test_workspace_keys_are_matched_case_insensitively():
    g = guard(limits={"mgmt": 5.0})
    assert g.limit_for("MGMT") == 5.0


# --- 문맥 붙이기 --------------------------------------------------------------
def test_cost_lands_on_the_workspace_in_context():
    """호출 열 군데에 인자를 끼우는 대신 요청 단위로 한 번 세운다."""
    g = guard(limits={"mgmt": 5.0})
    with attribute_to("mgmt"):
        g.record(1.0)
    assert g.spent_by_workspace() == {"mgmt": 1.0}


def test_context_is_restored_so_a_reused_thread_does_not_inherit_it():
    """Slack 이벤트는 스레드 풀에서 돈다. 안 되돌리면 다음 요청이 물려받는다."""
    with attribute_to("mgmt"):
        with attribute_to("tyit"):
            assert current_workspace() == "tyit"
        assert current_workspace() == "mgmt"
    assert current_workspace() == ""


def test_context_survives_an_exception():
    with pytest.raises(RuntimeError), attribute_to("mgmt"):
        raise RuntimeError("답변 실패")
    assert current_workspace() == ""


def test_context_is_normalised():
    with attribute_to("  MGMT  "):
        assert current_workspace() == "mgmt"


def test_an_explicit_workspace_beats_the_context():
    g = guard(limits={"mgmt": 5.0, "tyit": 5.0})
    with attribute_to("mgmt"):
        g.record(1.0, workspace="tyit")
    assert g.spent_by_workspace() == {"tyit": 1.0}


def test_calls_outside_any_context_still_count_against_the_wallet():
    """배치·점검 호출이다. 주인이 없다고 공짜는 아니다."""
    g = guard(total=10.0)
    g.record(3.0)
    assert g.spent_today == pytest.approx(3.0)
    assert g.spent_by_workspace() == {}


# --- 파일에 남는다 ------------------------------------------------------------
def test_workspace_spend_survives_a_restart(tmp_path):
    state = tmp_path / "cost-state.json"
    guard(limits={"mgmt": 5.0}, state=state).record(4.9, workspace="mgmt")

    after = guard(limits={"mgmt": 5.0}, state=state)  # 재시작
    assert after.spent_by_workspace() == {"mgmt": 4.9}
    with pytest.raises(CostLimitExceeded, match="워크스페이스"):
        after.check(0.15, workspace="mgmt")


def test_an_old_state_file_without_workspaces_still_loads(tmp_path):
    """전체 누적은 지켜진다 — 워크스페이스 칸만 0에서 다시 센다."""
    state = tmp_path / "cost-state.json"
    state.write_text(json.dumps({"day": TODAY, "spent_usd": 4.0}), encoding="utf-8")

    g = guard(total=5.0, state=state)
    assert g.spent_today == pytest.approx(4.0)
    assert g.spent_by_workspace() == {}


def test_yesterday_workspace_spend_is_discarded(tmp_path):
    state = tmp_path / "cost-state.json"
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    state.write_text(
        json.dumps({"day": yesterday, "spent_usd": 9.0, "by_workspace": {"mgmt": 9.0}}),
        encoding="utf-8",
    )

    assert guard(state=state).spent_by_workspace() == {}


def test_the_state_file_stays_readable_by_a_person(tmp_path):
    state = tmp_path / "cost-state.json"
    guard(state=state).record(1.5, workspace="mgmt")

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data["day"] == TODAY
    assert data["by_workspace"] == {"mgmt": 1.5}


# --- 한도 공급기 --------------------------------------------------------------
class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_limits_are_cached_so_the_answer_path_does_not_wait_on_the_db():
    calls = []

    def loader():
        calls.append(1)
        return {"mgmt": 10.0}

    clock = FakeClock()
    limits = WorkspaceLimits(ttl_seconds=60, loader=loader, clock=clock)
    for _ in range(5):
        assert limits() == {"mgmt": 10.0}
    assert len(calls) == 1


def test_a_console_change_lands_without_a_bot_restart():
    """이것이 그날 안 되던 것이다. 기동 시 한 번만 읽으면 올린 값이 안 먹는다."""
    value = {"mgmt": 2.0}
    clock = FakeClock()
    limits = WorkspaceLimits(ttl_seconds=60, loader=lambda: dict(value), clock=clock)

    assert limits()["mgmt"] == 2.0
    value["mgmt"] = 10.0
    clock.now += 61

    assert limits()["mgmt"] == 10.0


def test_a_db_failure_falls_back_to_the_last_known_limits():
    state = {"ok": True}

    def loader():
        if not state["ok"]:
            raise RuntimeError("DB 연결 실패")
        return {"mgmt": 10.0}

    clock = FakeClock()
    limits = WorkspaceLimits(ttl_seconds=60, loader=loader, clock=clock)
    assert limits() == {"mgmt": 10.0}

    state["ok"] = False
    clock.now += 61
    assert limits() == {"mgmt": 10.0}  # 직전 값으로 계속한다


def test_a_db_failure_before_any_read_means_the_global_limit_only():
    def boom():
        raise RuntimeError("DB 연결 실패")

    assert WorkspaceLimits(loader=boom)() == {}


def test_limit_keys_are_normalised():
    limits = WorkspaceLimits(loader=lambda: {"MGMT": "10"})
    assert limits() == {"mgmt": 10.0}


# --- 화면이 미리 말한다 --------------------------------------------------------
def _row(**over):
    row = {
        "key": "mgmt", "label": "경영본부", "connected": True,
        "uninvitedChannels": 0, "writeProblem": None,
        "spendTodayUsd": 0.0, "limitUsd": 10.0,
    }
    row.update(over)
    return row


def _problems(row, monkeypatch, *, global_limit="50"):
    from tybot.console import health

    monkeypatch.setenv("DAILY_COST_LIMIT_USD", global_limit)
    (item,) = health.bot_section([row])["workspaces"]
    return item["level"], " ".join(item["problems"])


def test_a_workspace_limit_above_the_global_one_is_called_out(monkeypatch):
    """올려도 안 먹는 숫자를 화면에 두면 사람은 그 숫자를 계속 올린다(2026-09-15)."""
    level, text = _problems(_row(limitUsd=10.0), monkeypatch, global_limit="5")
    assert level == "warn"
    assert "DAILY_COST_LIMIT_USD" in text


def test_a_spent_out_workspace_is_bad(monkeypatch):
    level, text = _problems(_row(spendTodayUsd=10.0), monkeypatch)
    assert level == "bad"
    assert "더 답하지 못합니다" in text


def test_approaching_the_limit_warns_before_people_notice(monkeypatch):
    level, text = _problems(_row(spendTodayUsd=8.5), monkeypatch)
    assert level == "warn"
    assert "80%" in text or "85%" in text


def test_a_healthy_budget_says_nothing(monkeypatch):
    level, text = _problems(_row(spendTodayUsd=1.0), monkeypatch)
    assert level == "ok"
    assert text == ""


def test_no_limit_means_no_budget_complaint(monkeypatch):
    assert _problems(_row(limitUsd=0.0, spendTodayUsd=99.0), monkeypatch)[0] == "ok"


# --- 콘솔은 막는 숫자를 보여 준다 ----------------------------------------------
def test_console_prefers_the_counter_that_actually_blocks(tmp_path, monkeypatch):
    """감사기록에는 답변 비용만 남는다. 상한을 깎는 것은 분류 호출까지 포함한 쪽이다."""
    from tybot.console import reader

    state = tmp_path / "cost-state.json"
    state.write_text(
        json.dumps({
            "day": TODAY, "spent_usd": 4.92,
            "by_workspace": {"mgmt": 4.5, "tyit": 0.42},
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(reader, "cost_state_path", lambda: str(state))
    monkeypatch.setattr(reader, "_read_qa_records", lambda days: [
        {"ts": f"{TODAY}T10:00:00+09:00", "workspace": "mgmt", "cost_usd": 1.0},
    ])

    assert reader._spend_by_workspace_today() == {"mgmt": 4.5, "tyit": 0.42}


def test_console_does_not_understate_when_the_state_file_lost_a_workspace(
    tmp_path, monkeypatch
):
    """옛 형식에서 재시작하면 워크스페이스 칸이 비어 있다. 그때는 감사기록이 더 크다."""
    from tybot.console import reader

    state = tmp_path / "cost-state.json"
    state.write_text(json.dumps({"day": TODAY, "spent_usd": 4.92}), encoding="utf-8")
    monkeypatch.setattr(reader, "cost_state_path", lambda: str(state))
    monkeypatch.setattr(reader, "_read_qa_records", lambda days: [
        {"ts": f"{TODAY}T10:00:00+09:00", "workspace": "mgmt", "cost_usd": 1.0},
    ])

    assert reader._spend_by_workspace_today() == {"mgmt": 1.0}


# --- Bolt 계약 ----------------------------------------------------------------
def test_bolt_injects_next_into_a_plain_middleware_function():
    """미들웨어가 조용히 안 불리면 **모든 비용이 주인 없이 쌓인다.**

    `next` 라는 인자 이름은 Bolt 가 정한 것이다. 이름이 바뀌면 우리 미들웨어는
    인자 주입에서 빠지고, 그 사실은 예외가 아니라 「사용 $0」 으로 보인다.
    """
    pytest.importorskip("slack_bolt")
    from slack_bolt.middleware.custom_middleware import CustomMiddleware

    seen = []

    def attribute_cost(next):
        with attribute_to("mgmt"):
            seen.append(current_workspace())
            next()

    CustomMiddleware(app_name="t", func=attribute_cost)
    assert "next" in CustomMiddleware(app_name="t", func=attribute_cost).arg_names

    # 실제 주입까지: 문맥이 세워지고 블록을 나가면 되돌아온다
    attribute_cost(next=lambda: None)
    assert seen == ["mgmt"]
    assert current_workspace() == ""
