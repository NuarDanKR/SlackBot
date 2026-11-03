"""콘솔 계정 자동 관리 — Slack 읽기(무DB)와 실 PostgreSQL 회차.

DB 테스트는 TYBOT_SCHEMA_TEST_DSN 이 있을 때만 돈다(격리 DB 만, 임시 스키마).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tybot.console import account_sync
from tybot.console.account_sync import SlackSnapshot

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("TYBOT_SCHEMA_TEST_DSN", "").strip()
needs_db = pytest.mark.skipif(not DSN, reason="TYBOT_SCHEMA_TEST_DSN 없음: 실DB 미검증")


# ---------------------------------------------------------------------------
# Slack 읽기
# ---------------------------------------------------------------------------

class _Client:
    def __init__(self, pages=None, error=None):
        self.pages = list(pages or [])
        self.error = error

    def users_list(self, limit=200, cursor=None):
        if self.error:
            raise self.error
        return self.pages.pop(0)


def test_fetch_slack_skips_bots_deleted_and_records_failures():
    pages = [
        {"members": [
            {"id": "U1", "profile": {"email": "A@ty.co.kr "}},
            {"id": "U2", "deleted": True, "profile": {"email": "b@ty.co.kr"}},
            {"id": "B1", "is_bot": True},
            {"id": "USLACKBOT"},
        ], "response_metadata": {"next_cursor": "c2"}},
        {"members": [{"id": "U3", "is_app_user": True}, {"id": "U4", "profile": {}}]},
    ]
    clients = {"tok-a": _Client(pages), "tok-b": _Client(error=RuntimeError("boom"))}
    snap = account_sync.fetch_slack(
        {"aaa": "tok-a", "bbb": "tok-b", "ccc": None},
        client_factory=lambda token, timeout: clients[token],
    )
    assert snap.members == {"aaa": {"U1": "A@ty.co.kr", "U4": ""}}
    assert snap.unread == {"bbb": "RuntimeError", "ccc": "no_token"}


def test_run_refuses_when_no_workspace_could_be_read():
    with pytest.raises(account_sync.AccountSyncError, match="slack_unavailable"):
        account_sync.run(actor="a", dry_run=True, tokens={"aaa": None},
                         connect=lambda: pytest.fail("DB 에 가면 안 된다"))


def test_timer_is_installed_and_switchable():
    install = (ROOT / "deploy/install.sh").read_text(encoding="utf-8")
    helper = (ROOT / "deploy/tybot-console-timers").read_text(encoding="utf-8")
    unit = (ROOT / "deploy/tybot-account-sync.service").read_text(encoding="utf-8")
    assert " tybot-account-sync " in install
    assert "enable --now tybot-account-sync" not in install      # 켜는 것은 사람이
    assert "tybot-account-sync.timer" in helper
    assert "-m tybot.console.account_sync --apply" in unit
    from tybot.console import timer_manager
    assert "tybot-account-sync.timer" in timer_manager.TIMERS
    schema = (ROOT / "deploy/apply-schema.sh").read_text(encoding="utf-8")
    assert schema.index("console_schema.sql") < schema.index("console_account_sync_schema.sql")


# ---------------------------------------------------------------------------
# 실 DB
# ---------------------------------------------------------------------------

SEED = """
INSERT INTO workspace (key, label, archive_path, created_by) VALUES ('mgmt', '경영', '/a/mgmt', 't'), ('tyit', '전산', '/a/tyit', 't');
INSERT INTO org_unit (code, name, kind) VALUES ('IT', '전산팀', 'team');
INSERT INTO employee (emp_no, name, email, org_code, active) VALUES
  ('E1', '가사원', 'a@ty.co.kr', 'IT', true),
  ('E2', '나사원', 'b@ty.co.kr', 'IT', true),
  ('E3', '다퇴직', 'c@ty.co.kr', 'IT', false),
  ('E4', '라관리', 'admin@ty.co.kr', 'IT', true),
  ('E5', '마퇴직', 'gone@ty.co.kr', 'IT', false);
INSERT INTO user_identity (workspace, slack_user, emp_no, verified_by) VALUES
  ('mgmt', 'UA', 'E1', 'email_match'),
  ('mgmt', 'UC', 'E3', 'email_match');
INSERT INTO console_user (email, name, password_hash, role, active) VALUES
  ('admin@ty.co.kr', '라관리', 'hash', 'admin', true),
  ('gone@ty.co.kr', '마퇴직', 'hash', 'developer', true);
"""


@pytest.fixture
def connect():
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_schema_isolated import operational_db_refusal

    assert not operational_db_refusal(DSN), "격리 DB 가 아니면 돌지 않는다"
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    name = "account_sync_test_" + uuid4().hex
    conns = []

    def _connect():
        conn = psycopg.connect(DSN, row_factory=dict_row)
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
        conns.append(conn)
        return conn

    with psycopg.connect(DSN, autocommit=True) as owner:
        owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            with _connect() as conn:
                for f in ("index_schema.sql", "console_schema.sql",
                          "console_account_sync_schema.sql", "console_account_sync_schema.sql"):
                    conn.execute((ROOT / "deploy/sql" / f).read_text(encoding="utf-8"))
                conn.execute(SEED)
            yield _connect
        finally:
            for conn in conns:
                conn.close()
            owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _run(connect, members, *, dry_run=False, unread=None):
    with connect() as conn:
        return account_sync.sync(conn, SlackSnapshot(members=members, unread=unread or {}),
                                 actor="test", dry_run=dry_run)


def _accounts(connect):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT u.email, u.role, u.active, u.managed_by, u.stop_reason,
                              u.password_hash, coalesce(array_agg(w.workspace ORDER BY w.workspace)
                              FILTER (WHERE w.workspace IS NOT NULL), '{}') AS ws
                         FROM console_user u LEFT JOIN console_user_workspace w ON w.email = u.email
                        GROUP BY u.email""")
        return {r["email"]: r for r in cur.fetchall()}


SLACK = {"mgmt": {"UA": "a@ty.co.kr", "UC": "c@ty.co.kr"},
         "tyit": {"UB": "B@ty.co.kr", "UA2": "a@ty.co.kr"}}


@needs_db
def test_preview_writes_nothing(connect):
    result = _run(connect, SLACK, dry_run=True)
    assert [p["empNo"] for p in result["created"]] == ["E1", "E2"]
    assert result["linked"] == 2                     # UB→E2, UA2→E1 을 이메일로 이었다
    assert set(_accounts(connect)) == {"admin@ty.co.kr", "gone@ty.co.kr"}


@needs_db
def test_apply_creates_guest_without_password_for_live_staff(connect):
    result = _run(connect, SLACK)
    acc = _accounts(connect)
    assert acc["a@ty.co.kr"]["role"] == "guest" and acc["a@ty.co.kr"]["managed_by"] == "slack_sync"
    assert acc["a@ty.co.kr"]["password_hash"] == ""          # 로그인 대상 아님
    assert acc["a@ty.co.kr"]["ws"] == ["mgmt", "tyit"]
    assert acc["b@ty.co.kr"]["ws"] == ["tyit"]
    assert "c@ty.co.kr" not in acc                           # 퇴직자는 만들지 않는다
    # 사람 손 계정은 그대로, 퇴직이면 경고만
    assert acc["admin@ty.co.kr"]["active"] and acc["admin@ty.co.kr"]["managed_by"] is None
    assert acc["gone@ty.co.kr"]["active"]
    assert [p["empNo"] for p in result["manualRetired"]] == ["E5"]
    assert result["runId"] > 0
    # 다시 돌려도 바뀌는 것이 없다
    again = _run(connect, SLACK)
    assert again["counts"] == {"created": 0, "reactivated": 0, "stopped": 0,
                               "workspacesChanged": 0, "protected": 0, "manualRetired": 1,
                               "linked": 0}


@needs_db
def test_slack_deleted_stops_then_returns(connect):
    _run(connect, SLACK)
    gone = {"mgmt": {"UC": "c@ty.co.kr"}, "tyit": {"UB": "b@ty.co.kr"}}   # E1 이 양쪽에서 사라짐
    result = _run(connect, gone)
    assert [(p["empNo"], p["reason"]) for p in result["stopped"]] == [("E1", "slack_gone")]
    acc = _accounts(connect)["a@ty.co.kr"]
    assert not acc["active"] and acc["stop_reason"] == "slack_gone"
    back = _run(connect, {"mgmt": {"UA": "a@ty.co.kr"}, "tyit": {"UB": "b@ty.co.kr"}})
    assert [p["empNo"] for p in back["reactivated"]] == ["E1"]
    acc = _accounts(connect)["a@ty.co.kr"]
    assert acc["active"] and acc["stop_reason"] is None and acc["ws"] == ["mgmt"]


@needs_db
def test_retirement_stops_even_with_slack(connect):
    _run(connect, SLACK)
    with connect() as conn:
        conn.execute("UPDATE employee SET active = false WHERE emp_no = 'E2'")
    result = _run(connect, SLACK)
    assert [(p["empNo"], p["reason"]) for p in result["stopped"]] == [("E2", "retired")]


@needs_db
def test_unread_workspace_protects_instead_of_stopping(connect):
    _run(connect, SLACK)
    # tyit 을 못 읽었다. E2 는 tyit 에만 있다 → 「없다」 로 읽으면 안 된다.
    result = _run(connect, {"mgmt": SLACK["mgmt"]}, unread={"tyit": "ratelimited"})
    assert result["stopped"] == []
    assert [p["empNo"] for p in result["protected"]] == ["E2"]
    assert _accounts(connect)["b@ty.co.kr"]["active"]


@needs_db
def test_people_list_shows_presence_and_managed(connect, monkeypatch):
    from tybot.console import user_directory

    _run(connect, SLACK)
    _run(connect, {"mgmt": {"UC": "c@ty.co.kr"}, "tyit": {"UB": "b@ty.co.kr"}})
    people = {p["empNo"]: p for p in user_directory.list_people(connect=connect)["people"]}
    a = people["E1"]
    assert {s["workspace"]: s["live"] for s in a["slack"]} == {"mgmt": False, "tyit": False}
    assert a["console"]["managed"] is True and a["console"]["active"] is False
    assert a["console"]["stopReason"] == "slack_gone"
    assert people["E4"]["slack"] == [] if "E4" in people else True
