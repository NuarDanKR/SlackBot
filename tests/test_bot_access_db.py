"""봇 사용 권한 — 진짜 PostgreSQL 에서. TYBOT_SCHEMA_TEST_DSN 이 있을 때만 돈다.

운영 DB 에는 절대 붙지 않는다(`operational_db_refusal`). 임시 스키마 하나를 만들고
끝나면 지운다.
"""
from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from tybot import bot_access
from tybot.console import user_directory as store

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("TYBOT_SCHEMA_TEST_DSN", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="TYBOT_SCHEMA_TEST_DSN 없음: 실DB 미검증")

SEED = """
INSERT INTO org_unit (code, name, kind) VALUES ('EXEC', '경영진', 'hq'), ('IT', '전산팀', 'team');
INSERT INTO employee (emp_no, name, email, org_code, position, active) VALUES
  ('E1', '김임원', 'exec1@ty.co.kr', 'EXEC', '부사장', true),
  ('E2', '박임원', 'exec2@ty.co.kr', 'EXEC', '전무', true),
  ('E3', '이퇴직', 'gone@ty.co.kr', 'EXEC', '상무', false),
  ('S1', '최사원', 'staff@ty.co.kr', 'IT', '사원', true);
INSERT INTO user_identity (workspace, slack_user, emp_no, verified_by) VALUES
  ('mgmt', 'UE1', 'E1', 'email_match'),
  ('tyit', 'UE1B', 'E1', 'email_match'),
  ('mgmt', 'UE3', 'E3', 'email_match'),
  ('tyit', 'US1', 'S1', 'email_match'),
  ('tyit', 'UX', NULL, 'manual');
INSERT INTO console_user (email, name, password_hash, role, active)
VALUES ('staff@ty.co.kr', '최사원', 'x', 'admin', true);
"""


@pytest.fixture
def connect():
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_schema_isolated import operational_db_refusal

    assert not operational_db_refusal(DSN), "격리 DB 가 아니면 돌지 않는다"
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    name = "bot_access_test_" + uuid4().hex
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
                          "bot_access_schema.sql", "bot_access_schema.sql",
                          "console_account_sync_schema.sql", "console_password_schema.sql",
                          "bot_source_schema.sql", "bot_source_schema.sql"):
                    conn.execute((ROOT / "deploy/sql" / f).read_text(encoding="utf-8"))
                conn.execute(SEED)
            yield _connect
        finally:
            for conn in conns:
                conn.close()
            owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _grant(connect, emp_no="E1", **kw):
    return store.grant(store.GrantRequest(bot="vipbot", emp_no=emp_no, reason="임원 요청",
                                          actor="Admin@TY.co.kr", **kw), connect=connect)


def _decide(connect, ws, user):
    with connect() as conn:
        return bot_access.may_use_bot(conn, bot="vipbot", workspace=ws, slack_user=user)


def test_list_people_slack_view(connect):
    result = store.list_people(connect=connect)
    by_emp = {p["empNo"]: p for p in result["people"]}
    assert set(by_emp) == {"E1", "E3", "S1"}          # Slack 매핑이 있는 사번만
    assert result["total"] == 3 and result["unmappedSlack"] == 1
    assert [s["workspace"] for s in by_emp["E1"]["slack"]] == ["mgmt", "tyit"]
    assert by_emp["S1"]["console"]["role"] == "admin"
    assert by_emp["E1"]["console"] is None
    assert by_emp["E1"]["orgName"] == "경영진" and by_emp["E1"]["position"] == "부사장"
    assert result["bots"] == [{"key": "vipbot", "label": "VIP 봇"}]


def test_list_people_filters(connect):
    assert store.list_people(view="all", connect=connect)["total"] == 3   # 재직자
    assert store.list_people(workspace="mgmt", connect=connect)["total"] == 2
    assert [p["empNo"] for p in store.list_people(q="전산", connect=connect)["people"]] == ["S1"]
    page = store.list_people(view="all", limit=1, offset=1, connect=connect)
    assert page["total"] == 3 and len(page["people"]) == 1


def test_grant_allows_every_workspace_of_that_person(connect):
    assert _decide(connect, "mgmt", "UE1").code == bot_access.NO_GRANT
    row = _grant(connect)
    assert row["state"] == "active" and row["grantedBy"] == "admin@ty.co.kr"
    assert _decide(connect, "mgmt", "UE1").allowed
    assert _decide(connect, "tyit", "UE1B").allowed        # 같은 사번, 다른 워크스페이스
    assert _decide(connect, "tyit", "US1").code == bot_access.NO_GRANT
    assert _decide(connect, "tyit", "UX").code == bot_access.NO_IDENTITY
    granted = store.list_people(view="granted", bot="vipbot", connect=connect)
    assert [p["empNo"] for p in granted["people"]] == ["E1"]
    assert granted["people"][0]["grants"][0]["live"] is True


def test_double_grant_and_inactive_refused(connect):
    _grant(connect)
    with pytest.raises(store.UserDirectoryError, match="already_granted"):
        _grant(connect)
    with pytest.raises(store.UserDirectoryError, match="employee_inactive"):
        _grant(connect, emp_no="E3")
    with pytest.raises(store.UserDirectoryError, match="employee_not_found"):
        _grant(connect, emp_no="NOPE")


def test_revoke_keeps_history_and_denies(connect):
    row = _grant(connect)
    revoked = store.revoke(row["id"], reason="퇴임", actor="admin@ty.co.kr", connect=connect)
    assert revoked["state"] == "revoked" and revoked["revokeReason"] == "퇴임"
    assert _decide(connect, "mgmt", "UE1").code == bot_access.NO_GRANT
    with pytest.raises(store.UserDirectoryError, match="already_revoked"):
        store.revoke(row["id"], reason="x", actor="a", connect=connect)
    with pytest.raises(store.UserDirectoryError, match="grant_not_found"):
        store.revoke(999999, reason="x", actor="a", connect=connect)
    again = _grant(connect)
    history = store.list_grants(bot="vipbot", connect=connect)["grants"]
    assert [g["id"] for g in history] == [again["id"], row["id"]]


def test_expired_grant_denies_then_regrant_closes_it(connect):
    row = _grant(connect, expires_at=datetime.now(UTC) + timedelta(hours=1))
    with connect() as conn:
        conn.execute("UPDATE bot_access SET granted_at = now() - interval '2 days',"
                     " expires_at = now() - interval '1 day' WHERE id = %s", (row["id"],))
    assert _decide(connect, "mgmt", "UE1").code == bot_access.EXPIRED
    check = store.check(bot="vipbot", emp_no="E1", connect=connect)
    assert check["allowed"] is False and check["code"] == "expired"
    fresh = _grant(connect)
    states = {g["id"]: g for g in store.list_grants(bot="vipbot", connect=connect)["grants"]}
    assert states[row["id"]]["state"] == "revoked"
    assert states[row["id"]]["revokeReason"] == "만료 후 다시 부여"
    assert states[fresh["id"]]["state"] == "active"
    assert _decide(connect, "mgmt", "UE1").allowed


def test_retired_person_with_grant_is_denied(connect):
    _grant(connect)
    with connect() as conn:
        conn.execute("UPDATE employee SET active = false WHERE emp_no = 'E1'")
    assert _decide(connect, "mgmt", "UE1").code in (bot_access.NO_IDENTITY,
                                                     bot_access.INACTIVE_EMPLOYEE)
    assert store.check(bot="vipbot", emp_no="E1", connect=connect)["code"] == "inactive_employee"


def test_check_by_slack_user_matches_bot(connect):
    _grant(connect)
    result = store.check(bot="vipbot", workspace="tyit", slack_user="UE1B", connect=connect)
    assert result["allowed"] and result["empNo"] == "E1"


CH = [{"workspace": "mgmt", "channelId": "C0AAA1111", "name": "#팀-기획_이사회관리"},
      {"workspace": "invest", "channelId": "C0BBB2222", "name": "#팀-자금_공통"}]


def test_sources_add_remove_and_decision(connect):
    with connect() as conn:
        assert bot_access.source_channels(conn, bot="vipbot") == frozenset()   # 비면 근거 없음
    added = store.add_sources(bot="vipbot", channels=CH, reason="경영진 채널 선정",
                              actor="Admin@ty.co.kr", connect=connect)
    assert added["added"] == ["mgmt:C0AAA1111", "invest:C0BBB2222"] and added["already"] == []
    again = store.add_sources(bot="vipbot", channels=CH[:1], reason="중복", actor="a",
                              connect=connect)
    assert again["added"] == [] and again["already"] == ["mgmt:C0AAA1111"]
    with connect() as conn:
        assert bot_access.source_channels(conn, bot="vipbot") == {
            ("mgmt", "C0AAA1111"), ("invest", "C0BBB2222")}
    gone = store.remove_sources(bot="vipbot", channels=CH[1:], reason="대상 제외", actor="a",
                                connect=connect)
    assert gone["removed"] == ["invest:C0BBB2222"]
    listed = store.list_sources(bot="vipbot", connect=connect)
    assert [c["channelId"] for c in listed["channels"]] == ["C0AAA1111"]
    assert listed["channels"][0]["addedBy"] == "admin@ty.co.kr"
    assert {h["channelId"] for h in listed["history"]} == {"C0AAA1111", "C0BBB2222"}
    # 해제 후 다시 등록하면 새 줄 — 이력은 둘 다 남는다
    store.add_sources(bot="vipbot", channels=CH[1:], reason="재선정", actor="a", connect=connect)
    assert len(store.list_sources(bot="vipbot", connect=connect)["history"]) == 3


@pytest.mark.parametrize("bad", [
    [{"workspace": "mgmt", "channelId": "not-an-id"}],
    [{"workspace": "MG MT", "channelId": "C0AAA1111"}],
    [],
])
def test_sources_reject_bad_channels(connect, bad):
    with pytest.raises(store.UserDirectoryError, match="invalid_request"):
        store.add_sources(bot="vipbot", channels=bad, reason="x", actor="a", connect=connect)
