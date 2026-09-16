"""문제 패킷 내려받기 — 서버에 들어가지 않고 받는다.

## 재현하는 사례
같은 보고서를 만드는 CLI 는 이미 있었다. 그런데 그 파일을 쓰려면 서버에 들어가 FTP 로
꺼내 와야 했고, 그 몇 걸음 때문에 **실제로는 아무도 안 꺼냈다.** 문제를 모아 두고
아무도 읽지 않는 상태가 제일 나쁘다.

패킷에는 사내 질문·답변과 피드백 원문이 들어간다. 그래서 여기서 고정하는 것은
「받아진다」 만이 아니라 **누가 무엇까지 받는가** 다.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tybot import answer_issues

KST = answer_issues.KST
TODAY = dt.datetime.now(KST).date()


def _qa(**over) -> dict:
    row = {
        "ts": f"{TODAY.isoformat()}T09:00:00+09:00",
        "record_id": "QA-1",
        "workspace": "tyit",
        "channel": "#팀-전산_abb155-공지",
        "question": "주간보고 어디 있나",
        "answer": "찾지 못했습니다",
        "reason": "no_hits",
        "hits": 0,
        "elapsed_ms": 900,
        "model": "claude-sonnet-5",
        "intent_kind": "search",
        "intent_source": "regex",
        "citations": [],
    }
    row.update(over)
    return row


def _fb(**over) -> dict:
    row = {
        "at": f"{TODAY.isoformat()}T09:05:00+09:00",
        "workspace": "tyit",
        "kind": "negative",
        "qa_record_id": "QA-1",
        "text": "있는데 못 찾음",
    }
    row.update(over)
    return row


@pytest.fixture
def qa_log(tmp_path):
    def write(records, feedback=()):
        month = TODAY.strftime("%Y-%m")
        (tmp_path / f"qa-{month}.jsonl").write_text(
            "\n".join(json.dumps(row, ensure_ascii=False) for row in records),
            encoding="utf-8",
        )
        if feedback:
            (tmp_path / f"feedback-{month}.jsonl").write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in feedback),
                encoding="utf-8",
            )
        return tmp_path

    return write


# --- 범위는 보고서를 만들기 전에 건다 -----------------------------------------
def test_a_scoped_admin_never_sees_another_workspace(qa_log):
    """범위를 만든 뒤 목록만 걸러내면 분포 표와 합계에 남의 워크스페이스가 남는다."""
    root = qa_log([_qa(), _qa(record_id="QA-9", workspace="mgmt", channel="#팀-경영_a100-주간")])

    report = answer_issues.build_report(root, days=7, allowed={"tyit"})

    assert "QA-1" in report
    assert "QA-9" not in report
    assert "mgmt" not in report


def test_feedback_is_scoped_too(qa_log):
    """피드백만 새면 「연결되지 않은 피드백」 절로 남의 워크스페이스가 그대로 나온다."""
    root = qa_log(
        [_qa()],
        [_fb(), _fb(workspace="mgmt", qa_record_id="QA-9", text="경영본부 건")],
    )

    report = answer_issues.build_report(root, days=7, allowed={"tyit"})

    assert "경영본부 건" not in report


def test_an_unscoped_admin_sees_everything(qa_log):
    root = qa_log([_qa(), _qa(record_id="QA-9", workspace="mgmt")])
    report = answer_issues.build_report(root, days=7)
    assert "QA-1" in report and "QA-9" in report


def test_the_workspace_filter_still_works_inside_the_scope(qa_log):
    root = qa_log([_qa(), _qa(record_id="QA-9", workspace="mgmt")])
    report = answer_issues.build_report(root, days=7, workspace="mgmt")
    assert "QA-9" in report and "QA-1" not in report


# --- 파일 이름 ----------------------------------------------------------------
def test_the_filename_says_the_scope(qa_log):
    """같은 날 두 번 받으면 이름이 갈려야 한다 — 같으면 브라우저가 (1) 을 붙이고,
    그러면 어느 쪽이 무엇인지 파일명으로는 알 수 없다."""
    when = dt.datetime(2026, 9, 16, 14, 0, tzinfo=KST)

    assert answer_issues.report_filename(generated_at=when) == "answer-issues-2026-09-16-all.md"
    assert (
        answer_issues.report_filename(generated_at=when, workspace="tyit")
        == "answer-issues-2026-09-16-tyit.md"
    )


def test_the_filename_is_ascii_only():
    """한글이 들어가면 브라우저마다 다르게 깨진다."""
    name = answer_issues.report_filename(workspace="tyit")
    assert name.isascii()


# --- 입력 검증 ----------------------------------------------------------------
def test_a_zero_day_window_is_refused(tmp_path):
    with pytest.raises(ValueError):
        answer_issues.build_report(tmp_path, days=0)


def test_a_missing_log_directory_is_an_empty_report_not_a_crash(tmp_path):
    """수집이 아직 없는 새 서버다. 빈 보고서가 나오는 것이 맞다."""
    report = answer_issues.build_report(tmp_path / "없음", days=7)
    assert "문제 후보: 0건" in report


# --- CLI 와 콘솔이 같은 보고서를 만든다 ---------------------------------------
def test_the_cli_reexports_the_same_functions():
    """두 벌로 두면 한쪽만 고쳐지고, 「받은 파일과 서버가 만든 파일이 다르다」 가 된다."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "export_answer_issues.py"
    spec = importlib.util.spec_from_file_location("export_answer_issues", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.render_report is answer_issues.render_report
    assert module.build_report is answer_issues.build_report
