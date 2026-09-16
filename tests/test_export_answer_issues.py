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
