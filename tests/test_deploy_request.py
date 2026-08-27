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
    r = dr.request_deploy("dan@taeyoung.com")
    assert r["ok"] is True
    payload = json.loads(dr.request_path().read_text(encoding="utf-8"))
    assert payload["actor"] == "dan@taeyoung.com"
    assert payload["requested_at"]


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
