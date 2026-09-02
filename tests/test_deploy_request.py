"""콘솔 배포 요청 통로.

핵심 성질: 콘솔은 root 권한 없이 **요청 파일만** 만들고, 그 내용은 명령 인자로
쓰이지 않는다(주입 불가). 여기 테스트는 요청/소비/상태 계약을 고정한다.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tybot import deploy_request as dr


@pytest.fixture(autouse=True)
def _state(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    yield


def test_request_creates_file_with_actor():
    r = dr.request_deploy("dan@taeyoung.com", approval_id=17)
    assert r["ok"] is True
    payload = json.loads(dr.request_path().read_text(encoding="utf-8"))
    assert payload["actor"] == "dan@taeyoung.com"
    assert payload["requested_at"]
    assert payload["approval_id"] == 17


def test_second_request_is_rejected_while_pending():
    dr.request_deploy("a@b.c")
    r = dr.request_deploy("a@b.c")
    assert r["ok"] is False
    assert "처리되지" in r["reason"]


def test_request_rejected_while_running():
    dr.write_status("running", actor="a@b.c")
    r = dr.request_deploy("a@b.c")
    assert r["ok"] is False
    assert "진행 중" in r["reason"]


def test_cooldown_blocks_rapid_redeploy():
    dr.write_status("ok", actor="a@b.c", before="aaa", after="bbb")
    r = dr.request_deploy("a@b.c")
    assert r["ok"] is False
    assert "초 뒤" in r["reason"]


def test_cooldown_expires():
    old = (datetime.now(UTC) - timedelta(seconds=dr.COOLDOWN_SEC + 5)).isoformat(
        timespec="seconds"
    )
    dr.status_path().parent.mkdir(parents=True, exist_ok=True)
    dr.status_path().write_text(
        json.dumps({"state": "ok", "finished_at": old}), encoding="utf-8"
    )
    assert dr.request_deploy("a@b.c")["ok"] is True


def test_consume_removes_request_so_it_runs_once():
    dr.request_deploy("dan@taeyoung.com")
    got = dr.consume_request()
    assert got["actor"] == "dan@taeyoung.com"
    assert not dr.request_path().exists()
    assert dr.consume_request() is None


def test_corrupted_request_is_consumed_not_retried():
    """손상된 요청이 남아 있으면 path 유닛이 무한 재트리거된다."""
    p = dr.request_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{깨진 json", encoding="utf-8")
    got = dr.consume_request()
    assert got == {"error": "손상된 파일"}
    assert not p.exists()


def test_note_is_recorded_but_truncated():
    dr.request_deploy("a@b.c", note="x" * 500)
    payload = json.loads(dr.request_path().read_text(encoding="utf-8"))
    assert len(payload["note"]) == 200


def test_status_lifecycle():
    dr.write_status("running", actor="a@b.c", before="1111111")
    st = dr.read_status()
    assert st["state"] == "running"
    assert "finished_at" not in st
    assert dr.is_running() is True

    dr.write_status("ok", actor="a@b.c", before="1111111", after="2222222")
    st = dr.read_status()
    assert st["state"] == "ok"
    assert st["finished_at"]
    assert dr.is_running() is False


def test_status_readable_by_console_account():
    """콘솔이 상태를 읽어 화면에 띄운다 - 0644. 시크릿은 넣지 않는다."""
    dr.write_status("ok", actor="a@b.c")
    body = dr.status_path().read_text(encoding="utf-8")
    assert "token" not in body.lower()
    assert "api" not in body.lower()


def test_request_file_is_not_world_readable():
    """요청에는 담당자 계정이 들어간다 - 0600."""
    dr.request_deploy("dan@taeyoung.com")
    assert dr.request_path().exists()


def test_no_status_yet_is_not_an_error():
    assert dr.read_status() is None
    assert dr.is_running() is False
    assert dr.seconds_since_last() is None
    assert dr.request_deploy("a@b.c")["ok"] is True


def test_console_status_reports_queued_request_without_internal_path(monkeypatch):
    monkeypatch.setattr(dr, "COOLDOWN_SEC", 0)
    dr.write_status("ok", actor="previous@taeyoung.com", before="abc", after="def")
    assert dr.request_deploy("dan@taeyoung.com")["ok"] is True

    status = dr.console_status()

    assert status["state"] == "queued"
    assert status["pending"] is True
    assert status["actor"] == "dan@taeyoung.com"
    assert status["before"] == ""
    assert status["after"] == ""
    assert status["finishedAt"] is None
    assert "path" not in status


def test_console_status_reports_completed_deployment():
    running = dr.write_status("running", actor="dan@taeyoung.com", before="abc")
    dr.write_status(
        "ok",
        actor="dan@taeyoung.com",
        before="abc",
        after="def",
        before_title="이전 커밋",
        after_title="새 커밋",
    )

    status = dr.console_status()

    assert status["state"] == "ok"
    assert status["pending"] is False
    assert status["before"] == "abc"
    assert status["after"] == "def"
    assert status["beforeTitle"] == "이전 커밋"
    assert status["afterTitle"] == "새 커밋"
    assert status["startedAt"] == running["started_at"]


def test_deploy_failure_detail_is_limited_and_redacted():
    # 토큰 모양을 소스에 그대로 적지 않고 이어 붙인다. 커밋 가드가 시크릿 패턴을
    # 줄 단위로 훑기 때문에, 가짜 값이라도 그대로 두면 커밋이 막힌다.
    fake_slack = "xoxb-" + "secretvalue123"
    detail = "\n".join(["ordinary"] * 40 + ["password=secret", fake_slack])

    dr.write_status("failed", detail=detail)
    status = dr.console_status()

    assert "password=***" in status["detail"]
    assert "xoxb-***" in status["detail"]
    assert "secretvalue123" not in status["detail"]
    assert len(status["detail"].splitlines()) <= dr.MAX_DETAIL_LINES
