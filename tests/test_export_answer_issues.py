from __future__ import annotations

import importlib.util
import json
from datetime import UTC, date, datetime
from pathlib import Path


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "export_answer_issues.py"
    spec = importlib.util.spec_from_file_location("export_answer_issues", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reporter = _module()


def test_load_records_filters_date_and_workspace(tmp_path):
    rows = [
        {"ts": "2026-09-16T09:00:00+09:00", "workspace": "tyit"},
        {"ts": "2026-09-10T09:00:00+09:00", "workspace": "tyit"},
        {"ts": "2026-09-16T09:00:00+09:00", "workspace": "mgmt"},
    ]
    (tmp_path / "qa-2026-09.jsonl").write_text(
        "\n".join(json.dumps(row) for row in rows) + "\nnot-json\n",
        encoding="utf-8",
    )

    got = reporter.load_records(tmp_path, since=date(2026, 9, 15), workspace="tyit")

    assert got == [rows[0]]


def test_report_includes_problems_and_linked_feedback():
    record = {
        "record_id": "abc123",
        "ts": "2026-09-16T09:00:00+09:00",
        "workspace": "tyit",
        "channel": "#test",
        "question": "정산 자료를 정리해줘",
        "answer": "근거를 찾지 못했습니다.",
        "reason": "no_hits",
        "hits": 0,
        "elapsed_ms": 20_000,
        "intent_kind": "summary",
    }
    feedback = [{
        "at": "2026-09-16T09:01:00+09:00",
        "workspace": "tyit",
        "qa_record_id": "abc123",
        "actor": "U1",
        "kind": "missing",
        "action": "submitted",
        "text": "채널에 문서가 있습니다.",
    }]

    got = reporter.render_report(
        [record],
        feedback,
        since=date(2026, 9, 10),
        generated_at=datetime(2026, 9, 16, tzinfo=UTC),
    )

    assert "문제 후보: 1건" in got
    assert "no_evidence, slow, feedback" in got
    assert "정산 자료를 정리해줘" in got
    assert "채널에 문서가 있습니다." in got


def test_positive_feedback_alone_does_not_make_an_answer_a_problem():
    record = {
        "record_id": "ok1",
        "ts": "2026-09-16T09:00:00+09:00",
        "workspace": "tyit",
        "reason": "answered",
        "hits": 3,
        "elapsed_ms": 1000,
    }
    feedback = [{
        "at": "2026-09-16T09:01:00+09:00",
        "workspace": "tyit",
        "qa_record_id": "ok1",
        "actor": "U1",
        "kind": "positive",
        "action": "added",
    }]

    got = reporter.render_report(
        [record],
        feedback,
        since=date(2026, 9, 10),
        generated_at=datetime(2026, 9, 16, tzinfo=UTC),
    )

    assert "문제 후보: 0건" in got
    assert "피드백: 긍정 1건 / 문제·정정 0건" in got


def test_removed_feedback_is_not_reported_as_active():
    base = {
        "at": "2026-09-16T09:01:00+09:00",
        "workspace": "tyit",
        "qa_record_id": "ok1",
        "actor": "U1",
        "kind": "negative",
        "text": "취소할 피드백",
    }

    assert reporter.active_feedback([
        dict(base, action="added"),
        dict(base, action="removed"),
    ]) == []


def test_main_writes_report_to_requested_path(tmp_path, monkeypatch):
    qa_root = tmp_path / "qa"
    qa_root.mkdir()
    output = tmp_path / "report.md"
    monkeypatch.setattr(
        reporter,
        "parse_args",
        lambda: type("Args", (), {
            "days": 7,
            "workspace": "",
            "limit": 20,
            "qa_log_dir": str(qa_root),
            "env_file": "",
            "output": str(output),
        })(),
    )
    monkeypatch.setattr(reporter, "load_env_file", lambda: "")

    assert reporter.main() == 0
    assert "TYBot 답변 문제 검토 패킷" in output.read_text(encoding="utf-8")


def test_distribution_counts_every_issue_even_when_details_are_limited():
    records = [
        {
            "record_id": f"q{index}",
            "ts": f"2026-09-16T09:{index:02d}:00+09:00",
            "workspace": "tyit",
            "question": "질문",
            "answer": "근거를 찾지 못했습니다.",
            "reason": "no_hits",
            "hits": 0,
            "citations": [],
        }
        for index in range(3)
    ]

    got = reporter.render_report(
        records,
        [],
        since=date(2026, 9, 10),
        generated_at=datetime(2026, 9, 16, tzinfo=UTC),
        limit=1,
    )

    assert "문제 후보: 3건 (상세 표시 1건 · 상한 1건)" in got
    assert "| 근거 없음/권한 범위 없음 | 3 |" in got


def test_operational_failure_codes_are_classified_from_the_shared_status_rule():
    row = {
        "answer": "요청한 문서 답변을 생성할 수 없습니다.",
        "reason": "specialist_unavailable",
        "hits": 42,
        "citations": [],
        "final_responder": "none",
        "specialist_error_code": "specialist-timeout:external-v3",
        "scope": "채널 7개 · 검색 42건",
        "elapsed_ms": 318_700,
    }

    codes = reporter.issue_codes(row, [])

    assert codes == [
        "error",
        "timeout",
        "missing_citations",
        "slow",
        "high_search_volume",
    ]
    assert reporter.recorded_search_count(row) == 42


def test_empty_and_partial_answers_are_distinct_findings():
    assert "empty_answer" in reporter.issue_codes(
        {"answer": "", "reason": "answered", "hits": 1, "citations": ["#채널"]},
        [],
    )
    partial = {
        "answer": "요약입니다.\n※ 확인하지 못한 범위: 3공구 9월",
        "reason": "answered",
        "hits": 3,
        "citations": ["#현장"],
    }
    assert "partial_scope" in reporter.issue_codes(partial, [])


def test_resource_exhaustion_is_only_claimed_when_the_record_contains_a_code():
    assert "resource_exhaustion" in reporter.issue_codes(
        {"answer": "실패", "error": "MemoryError", "reason": "error"}, []
    )
    assert "resource_exhaustion" not in reporter.issue_codes(
        {"answer": "실패", "error": "RuntimeError", "reason": "error"}, []
    )


def test_grounded_successes_are_included_as_a_human_review_sample():
    record = {
        "record_id": "ok-grounded",
        "ts": "2026-09-16T09:00:00+09:00",
        "workspace": "tyit",
        "question": "오늘 주요 내용을 알려줘",
        "answer": "확인된 내용입니다.",
        "reason": "answered",
        "hits": 2,
        "citations": ["#현장, 문서(2026-09-16)"],
    }

    got = reporter.render_report(
        [record],
        [],
        since=date(2026, 9, 10),
        generated_at=datetime(2026, 9, 16, tzinfo=UTC),
    )

    assert "근거 있는 성공 답변: 1건 (표본 1건" in got
    assert "## 성공 답변 근거 충실도 표본" in got
    assert "QA `ok-grounded`" in got
