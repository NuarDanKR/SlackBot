"""요약 검토 실측 (B-60 1단계).

「이 수치 없이 2단계를 시작하지 않는다」 고 설계 문서에 적었다. 그러니 수치가
틀리면 착수 판단이 틀린다 — 특히 **무엇을 분모에 넣는가**가 그렇다.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import diagnose_summary_review as diagnose

from tybot import summary_review as sr


def _stat(form, state, n, *, channel_id="C1", name="#주간보고"):
    return {"workspace": "tyit", "channel_id": channel_id, "channel_name": name,
            "form": form, "state": state, "n": n}


def test_only_human_decisions_count_toward_the_approval_rate():
    """보류는 결정이 아니고 자동 폐기는 사람이 한 일이 아니다.

    둘을 분모에 넣으면 승인률이 사람의 판단과 무관해지고, 그 수치로 2단계 착수를
    판단하게 된다.
    """
    folded = sr.summarize_forms([
        _stat("abstract", "approved", 3),
        _stat("abstract", "rejected", 1),
        _stat("abstract", "pending", 10),
        _stat("abstract", "deferred", 4),
        _stat("abstract", "expired", 20),
    ])

    found = folded["abstract"]
    assert found.total == 38
    assert found.decided == 4
    assert found.approval_rate == 0.75


def test_no_decision_is_not_a_zero_percent_approval_rate():
    """0% 는 「다 반려됐다」 다. 아직 아무도 안 봤다는 것과 같을 수 없다."""
    folded = sr.summarize_forms([_stat("quote", "pending", 7)])

    assert folded["quote"].approval_rate is None
    assert diagnose._rate(None) == "-"
    assert diagnose._rate(0.0) == "0%"


def test_an_unknown_form_is_counted_as_a_quote():
    """옛 행이나 알 수 없는 값이 생성 요약 실적으로 잡히면 판단이 뒤집힌다."""
    folded = sr.summarize_forms([
        _stat("", "approved", 2),
        _stat("freeform", "approved", 1),
    ])

    assert set(folded) == {"quote"}
    assert folded["quote"].approved == 3


def test_the_report_shows_both_forms_side_by_side():
    stats = [
        _stat("quote", "approved", 8),
        _stat("quote", "rejected", 2),
        _stat("abstract", "approved", 3),
        _stat("abstract", "rejected", 3),
    ]
    latency = [{"workspace": "tyit", "channel_id": "C1", "form": "abstract",
                "decided": 6, "avg_seconds": 5400.0, "max_seconds": 90000.0}]

    body = "\n".join(diagnose.report(stats, latency, since=date(2026, 8, 19)))

    assert "quote" in body and "abstract" in body
    assert "80%" in body and "50%" in body
    assert "1.5시간" in body


def test_the_report_says_when_there_is_nothing_to_measure():
    """빈 표를 「후보 0건·승인률 0%」 로 보이면 실측했다고 오해한다."""
    body = "\n".join(diagnose.report([], [], since=date(2026, 8, 19)))

    assert "검토 후보가 없다" in body


def test_expired_candidates_are_excluded_from_the_latency_query():
    """자동 폐기를 섞으면 「검토에 얼마나 걸리는가」 가 아니라
    「얼마나 방치되는가」 를 재게 된다."""
    calls: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, args=None):
            calls.append((sql, args))

        def fetchall(self):
            return []

    store = sr.Store(type("C", (), {"cursor": lambda self: _Cur()})())
    store.review_latency(since=date(2026, 8, 19))

    sql = calls[0][0]
    assert "system:expired" in sql
    assert "delivered_at IS NOT NULL" in sql


def test_the_channel_filter_is_passed_as_a_parameter():
    """채널 ID 를 SQL 에 이어 붙이지 않는다."""
    calls: list[tuple] = []

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, args=None):
            calls.append((sql, args))

        def fetchall(self):
            return []

    store = sr.Store(type("C", (), {"cursor": lambda self: _Cur()})())
    store.form_stats(since=date(2026, 8, 19), workspace="tyit", channel_id="C1")

    sql, args = calls[0]
    assert "C1" not in sql
    assert "C1" in args
