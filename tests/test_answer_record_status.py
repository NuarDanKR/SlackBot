from datetime import date

from tybot import answer_status
from tybot.console import answer_records
from tybot.console.auth import ADMIN, ConsoleUser


def _failed_row(**over):
    row = {
        "ts": "2026-11-02T15:54:35+09:00",
        "workspace": "tyit",
        "channel_id": "C_TEST",
        "user": "U_TEST",
        "reason": "specialist_unavailable",
        "hits": 42,
        "elapsed_ms": 318_700,
        "final_responder": "none",
        "specialist_error_code": "specialist-timeout:external-v3",
        "task_traces": [
            {
                "result": "specialist_unavailable",
                "final_responder": "none",
                "error_code": "specialist-timeout:external-v3",
            }
        ],
    }
    row.update(over)
    return row


def test_specialist_timeout_is_a_failed_answer_even_with_search_hits():
    row = _failed_row()

    assert answer_records.answer_result(row) == "failed"
    assert answer_status.failure_code(row) == "specialist-timeout:external-v3"
    assert "answer_failed" in answer_records.quality_reasons(row)


def test_summary_does_not_count_a_delivered_failure_as_grounded():
    report = answer_records.summary([_failed_row()])

    assert report["questions"] == 1
    assert report["grounded"] == 0
    assert report["errors"] == 1


def test_summary_and_detail_expose_the_explicit_failure_state():
    row = _failed_row()

    listed = answer_records.summaries([row], [])[0]
    detail = answer_records.detail(row, [])

    assert listed["resultStatus"] == "failed"
    assert listed["failureCode"] == "specialist-timeout:external-v3"
    assert detail["resultStatus"] == "failed"
    assert detail["finalResponder"] == "none"
    assert detail["specialistErrorCode"] == "specialist-timeout:external-v3"


def test_error_filter_includes_specialist_failures(monkeypatch):
    monkeypatch.setattr(answer_records, "_all_records", lambda: [_failed_row()])
    user = ConsoleUser("admin@example.test", "admin", ADMIN, all_workspaces=True)

    rows, _scope = answer_records.visible_records(
        user,
        start=date(2026, 11, 2),
        end=date(2026, 11, 2),
        result="error",
    )

    assert len(rows) == 1


def test_answered_filter_excludes_specialist_failures(monkeypatch):
    monkeypatch.setattr(answer_records, "_all_records", lambda: [_failed_row()])
    user = ConsoleUser("admin@example.test", "admin", ADMIN, all_workspaces=True)

    rows, _scope = answer_records.visible_records(
        user,
        start=date(2026, 11, 2),
        end=date(2026, 11, 2),
        result="answered",
    )

    assert rows == []


def test_a_normal_grounded_answer_remains_answered():
    row = _failed_row(
        reason="answered",
        final_responder="hermes",
        specialist_error_code="",
        elapsed_ms=1_000,
        task_traces=[{"result": "answered", "final_responder": "hermes", "error_code": ""}],
    )

    assert answer_records.answer_result(row) == "answered"


def test_a_successful_fallback_is_reviewable_instead_of_failed():
    row = _failed_row(
        reason="answered",
        final_responder="master-interim",
        specialist_error_code="specialist-timeout:external-v3",
        elapsed_ms=1_000,
        task_traces=[],
    )

    assert answer_records.answer_result(row) == "needs_review"
    assert answer_status.failure_code(row) == ""
    assert "specialist_fallback" in answer_records.quality_reasons(row)
