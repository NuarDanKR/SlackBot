"""당일 누적 비용이 재시작을 견디는지 — 상한이 실제로 상한 역할을 하는지 검증."""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tybot.config import cost_state_path
from tybot.gateway.cost import CostGuard, CostLimitExceeded

TODAY = dt.date.today().isoformat()


def test_spend_survives_restart(tmp_path):
    """재시작해도 당일 누적이 유지된다 — 예전에는 0으로 리셋돼 상한이 사라졌다."""
    state = tmp_path / "cost-state.json"
    g1 = CostGuard(10.0, state_path=state)
    g1.record(7.5)

    g2 = CostGuard(10.0, state_path=state)  # 재시작
    assert g2.spent_today == pytest.approx(7.5)
    with pytest.raises(CostLimitExceeded):
        g2.check(3.0)  # 7.5 + 3.0 > 10


def test_yesterday_record_is_discarded(tmp_path):
    """어제 기록은 오늘 누적으로 이어지지 않는다."""
    state = tmp_path / "cost-state.json"
    yesterday = (dt.date.today() - dt.timedelta(days=1)).isoformat()
    state.write_text(json.dumps({"day": yesterday, "spent_usd": 9.9}), encoding="utf-8")

    g = CostGuard(10.0, state_path=state)
    assert g.spent_today == 0.0
    g.check(5.0)  # 차단되지 않는다


def test_state_file_shape_is_readable(tmp_path):
    """사람이 열어 확인할 수 있는 형식으로 남는다."""
    state = tmp_path / "sub" / "cost-state.json"  # 상위 디렉터리도 만들어야 한다
    CostGuard(10.0, state_path=state).record(1.25)

    data = json.loads(state.read_text(encoding="utf-8"))
    assert data == {"day": TODAY, "spent_usd": 1.25}


def test_broken_state_file_does_not_block(tmp_path, caplog):
    """회계 파일 하나 때문에 봇이 답을 못 하는 쪽이 더 나쁘다 — 경고만 남기고 0에서 시작."""
    state = tmp_path / "cost-state.json"
    state.write_text("{ 깨진 JSON", encoding="utf-8")

    g = CostGuard(10.0, state_path=state)
    assert g.spent_today == 0.0
    g.record(1.0)  # 예외 없이 계속 쓴다
    assert json.loads(state.read_text(encoding="utf-8"))["spent_usd"] == pytest.approx(1.0)


def test_unwritable_state_path_does_not_block(tmp_path):
    """기록 실패도 답변을 막지 않는다(메모리 카운터로 계속)."""
    # 파일을 디렉터리 자리에 두어 mkdir/write 가 실패하게 만든다.
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir", encoding="utf-8")

    g = CostGuard(10.0, state_path=blocker / "cost-state.json")
    g.record(2.0)
    assert g.spent_today == pytest.approx(2.0)
    with pytest.raises(CostLimitExceeded):
        g.check(9.0)


def test_no_state_path_keeps_memory_only_behaviour(tmp_path):
    """state_path 를 주지 않으면 기존 동작(메모리 카운터)과 같다."""
    g = CostGuard(1.0)
    g.record(0.5)
    assert g.spent_today == pytest.approx(0.5)
    assert not list(tmp_path.iterdir())  # 파일을 만들지 않는다


def test_cost_state_path_defaults_under_qa_log(monkeypatch):
    """기본 위치는 감사기록 디렉터리 — 아카이브(archive/channels) 밖이어야 한다."""
    monkeypatch.delenv("COST_STATE_PATH", raising=False)
    path = cost_state_path("/var/lib/tybot/qa-log")
    assert path.replace("\\", "/") == "/var/lib/tybot/qa-log/cost-state.json"
    assert "channels" not in path

    monkeypatch.setenv("COST_STATE_PATH", "/tmp/elsewhere.json")
    assert cost_state_path("/var/lib/tybot/qa-log") == "/tmp/elsewhere.json"
