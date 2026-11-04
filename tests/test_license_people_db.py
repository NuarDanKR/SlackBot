"""라이선스 보유자 명단 — 진짜 PostgreSQL 에서. TYBOT_SCHEMA_TEST_DSN 이 있을 때만 돈다."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from tybot.console import license_people as people
from tybot.console import license_store
from tybot.console.license_store import ActiveCount, Member

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("TYBOT_SCHEMA_TEST_DSN", "").strip()
pytestmark = pytest.mark.skipif(not DSN, reason="TYBOT_SCHEMA_TEST_DSN 없음: 실DB 미검증")

SEED = """
INSERT INTO org_unit (code, name, kind) VALUES ('IT', '전산팀', 'team'), ('PF', '프로젝트금융팀', 'team');
INSERT INTO employee (emp_no, name, email, org_code, position, active) VALUES
  ('9318-F', '최사원', 'sychoi@taeyoung.com', 'IT', '사원', true),
  ('1000-A', '김퇴직', 'gone@taeyoung.com', 'IT', '과장', false),
  ('2000-B', '박금융', 'park@taeyoung.com', 'PF', '대리', true);
INSERT INTO user_identity (workspace, slack_user, emp_no, verified_by) VALUES
  ('tyit', 'U1', '9318-F', 'email_match');
INSERT INTO slack_license_manual (label, allocated, active) VALUES ('프로젝트금융팀', 16, 16);
"""

LINKED = {"tyit": ("전산팀", "xoxb-1"), "pf": ("프로젝트금융", None)}


@pytest.fixture
def connect():
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_schema_isolated import operational_db_refusal

    assert not operational_db_refusal(DSN)
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    name = "license_people_test_" + uuid4().hex
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
                for f in ("index_schema.sql", "slack_license_schema.sql",
                          "license_member_schema.sql", "license_member_schema.sql"):
                    conn.execute((ROOT / "deploy/sql" / f).read_text(encoding="utf-8"))
                conn.execute(SEED)
            yield _connect
        finally:
            for conn in conns:
                conn.close()
            owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _manual(connect):
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT id, label, allocated, active FROM slack_license_manual")]


def _counts():
    members = (Member("U1", "최수연", "sychoi@taeyoung.com", False),
               Member("U2", "퇴직자", "GONE@taeyoung.com".lower(), False),
               Member("U3", "외부", "x@partner.com", True))
    return {"tyit": ActiveCount(active=3, guests=1, fetched_at="t", members=members)}


def test_slack_members_are_mapped_by_identity_then_email(connect):
    result = people.holders(LINKED, _counts(), _manual(connect), connect=connect)
    by_user = {h["slackUser"]: h for h in result["holders"]}
    assert by_user["U1"]["empNo"] == "9318-F" and by_user["U1"]["org"] == "전산팀"
    assert by_user["U2"]["empNo"] == "1000-A" and by_user["U2"]["employeeActive"] is False
    assert by_user["U3"]["empNo"] is None and by_user["U3"]["guest"] is True
    assert result["unread"] == []


def test_manual_list_add_remove_and_counts(connect):
    manual_id = _manual(connect)[0]["id"]
    added = people.add(f"id:{manual_id}", ["2000-b", "9318-F", "1000-A", "NOPE", "2000-B"],
                       actor="Admin@taeyoung.com", linked=LINKED, connect=connect)
    assert added == {"added": ["2000-B", "9318-F"], "already": [], "notFound": ["NOPE"],
                     "inactive": ["1000-A"]}
    again = people.add(f"id:{manual_id}", ["2000-B"], actor="a", linked=LINKED, connect=connect)
    assert again["already"] == ["2000-B"]
    people.add("ws:pf", ["2000-B"], actor="a", linked=LINKED, connect=connect)
    counts = people.listed_counts(connect=connect)
    assert counts == {f"id:{manual_id}": 2, "ws:pf": 1}

    report = license_store.build_report(LINKED, {}, _counts(), _manual(connect), counts)
    rows = {r["uid"]: r for r in report["rows"]}
    assert rows["ws:pf"]["active"] == 1 and rows["ws:pf"]["activeSource"] == "list"
    assert rows["ws:pf"]["error"] is None and rows["ws:pf"]["listEditable"] is True
    assert rows[f"id:{manual_id}"]["active"] == 2
    assert rows["ws:tyit"]["listEditable"] is False and rows["ws:tyit"]["activeSource"] == "slack"

    listed = [h for h in people.holders(LINKED, _counts(), _manual(connect), connect=connect)["holders"]
              if h["source"] == "manual"]
    assert {(h["uid"], h["empNo"]) for h in listed} == {
        (f"id:{manual_id}", "2000-B"), (f"id:{manual_id}", "9318-F"), ("ws:pf", "2000-B")}
    assert listed[0]["addedBy"] in ("admin@taeyoung.com", "a")

    pf_member = next(h for h in listed if h["uid"] == "ws:pf")
    with pytest.raises(people.LicensePeopleError, match="사유"):
        people.release(pf_member, reason="  ", actor="a", connect=connect)
    gone = people.release(pf_member, reason="프로젝트 종료", actor="Admin@taeyoung.com", connect=connect)
    assert gone["source"] == "manual"
    with pytest.raises(people.LicensePeopleError, match="이미 명단에서"):
        people.release(pf_member, reason="또", actor="a", connect=connect)
    with connect() as conn:
        conn.execute("DELETE FROM slack_license_manual WHERE id = %s", (manual_id,))
    after = people.holders(LINKED, _counts(), _manual(connect), connect=connect)
    assert after["releases"][0]["status"] == "done" and after["releases"][0]["reason"] == "프로젝트 종료"
    assert after["releases"][0]["name"] == "박금융" and after["releases"][0]["releasedBy"] == "admin@taeyoung.com"
    assert people.listed_counts(connect=connect) == {}


def test_lists_are_refused_where_slack_is_the_source(connect):
    with pytest.raises(people.LicensePeopleError, match="Slack 에서 명단을 읽는"):
        people.add("ws:tyit", ["9318-F"], actor="a", linked=LINKED, connect=connect)
    with pytest.raises(people.LicensePeopleError, match="연동되지 않은"):
        people.add("ws:ghost", ["9318-F"], actor="a", linked=LINKED, connect=connect)
    with pytest.raises(people.LicensePeopleError, match="삭제된"):
        people.add("id:999999", ["9318-F"], actor="a", linked=LINKED, connect=connect)


def test_slack_release_is_recorded_pending_then_done_and_cancellable(connect):
    holder = next(h for h in people.holders(LINKED, _counts(), [], connect=connect)["holders"]
                  if h["slackUser"] == "U2")
    rid = people.release(holder, reason="퇴직", actor="a", connect=connect)["id"]
    with pytest.raises(people.LicensePeopleError, match="이미 회수"):
        people.release(holder, reason="퇴직", actor="a", connect=connect)
    now = people.holders(LINKED, _counts(), [], connect=connect)
    marked = next(h for h in now["holders"] if h["slackUser"] == "U2")
    assert marked["release"]["reason"] == "퇴직" and now["releases"][0]["status"] == "pending"

    gone = _counts()
    gone["tyit"] = ActiveCount(active=2, guests=1, fetched_at="t",
                               members=tuple(m for m in gone["tyit"].members if m.slack_user != "U2"))
    assert people.holders(LINKED, gone, [], connect=connect)["releases"][0]["status"] == "done"
    assert people.holders(LINKED, {}, [], connect=connect)["releases"][0]["status"] == "unknown"

    assert people.cancel(rid, actor="a", connect=connect)["id"] == rid
    with pytest.raises(people.LicensePeopleError):
        people.cancel(rid, actor="a", connect=connect)
    again = people.holders(LINKED, _counts(), [], connect=connect)
    assert next(h for h in again["holders"] if h["slackUser"] == "U2")["release"] is None
    assert again["releases"][0]["status"] == "cancelled"
