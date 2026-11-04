"""콘솔 임시 비밀번호 — 규칙(무DB)과 발급·로그인·변경·초기화·정리(실 PostgreSQL).

DB 테스트는 TYBOT_SCHEMA_TEST_DSN 이 있을 때만 돈다(격리 DB, 임시 스키마).
"""
from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from tybot.console import password_issue as pi

ROOT = Path(__file__).resolve().parents[1]
DSN = os.environ.get("TYBOT_SCHEMA_TEST_DSN", "").strip()
needs_db = pytest.mark.skipif(not DSN, reason="TYBOT_SCHEMA_TEST_DSN 없음: 실DB 미검증")


def test_temp_password_shape():
    seen = set()
    for _ in range(300):
        temp = pi.generate_temp()
        assert len(temp) == 6
        assert re.search(r"[A-Z]", temp) and re.search(r"[a-z]", temp)
        assert re.search(r"[0-9]", temp) and re.search(r"[^A-Za-z0-9]", temp)
        assert not set(temp) & set("0O1lI")
        seen.add(temp)
    assert len(seen) > 290


@pytest.mark.parametrize(("new", "code"), [
    ("Ab1!", "too_short"),
    ("Abcdefg1!xyz34", "too_long"),          # 14자
    ("abcdefgh", "too_simple"),
    ("abcd1234", "too_simple"),              # 2종류
    ("Kim#2026a", "same_as_email"),          # 이메일 앞부분 포함
    ("Temp#123", "same_as_current"),
])
def test_new_password_rules(new, code):
    with pytest.raises(pi.PasswordError, match=code):
        pi.validate_new(new, email="kim@ty.co.kr", current="Temp#123")


@pytest.mark.parametrize("ok", ["Abc12!", "Tybot#2026xy", "zz99!!AA"])
def test_new_password_accepts_6_to_13(ok):
    pi.validate_new(ok, email="kim@ty.co.kr", current="other")


def test_dm_text_has_required_sentences_and_no_url():
    text = pi.dm_text("kim@ty.co.kr", "Ab3#xY", datetime(2026, 11, 6, 6, 0, tzinfo=UTC))
    assert "kim@ty.co.kr" in text and "`Ab3#xY`" in text and "11/06 15:00" in text
    assert "최초 로그인 시 새로운 비밀번호를 설정해야 합니다." in text
    assert "72시간 동안 유효하므로" in text and "전산팀 최수연 사원에게 문의해" in text
    assert "이 대화창에 비밀번호를 입력하지 마세요" in text
    assert "http" not in text


# ---------------------------------------------------------------------------
# 실 DB
# ---------------------------------------------------------------------------

class FakeSlack:
    def __init__(self, fail=False):
        self.sent, self.deleted, self.fail = [], [], fail

    def send(self, token, slack_user, text):
        if self.fail:
            raise RuntimeError("channel_not_found")
        self.sent.append((token, slack_user, text))
        return f"D{slack_user}", f"{len(self.sent)}.0001"

    def delete(self, token, channel, ts):
        self.deleted.append((channel, ts))

    def last_temp(self):
        return re.search(r"`([^`]+)`", self.sent[-1][2]).group(1)


SEED = """
INSERT INTO org_unit (code, name, kind) VALUES ('IT', '전산팀', 'team');
INSERT INTO employee (emp_no, name, email, org_code, active) VALUES
  ('E1', '가사원', 'a@ty.co.kr', 'IT', true),
  ('E2', '나사원', 'b@ty.co.kr', 'IT', true),
  ('E3', '다사원', 'c@ty.co.kr', 'IT', true);
INSERT INTO user_identity (workspace, slack_user, emp_no, verified_by) VALUES
  ('mgmt', 'UA', 'E1', 'email_match'), ('tyit', 'UA2', 'E1', 'email_match'),
  ('mgmt', 'UB', 'E2', 'email_match'),
  ('mgmt', 'UC', 'E3', 'email_match');
INSERT INTO slack_presence (workspace, slack_user, active) VALUES
  ('mgmt', 'UA', false), ('tyit', 'UA2', true), ('mgmt', 'UB', true), ('mgmt', 'UC', true);
INSERT INTO console_user (email, name, password_hash, role, active, managed_by, emp_no) VALUES
  ('a@ty.co.kr', '가사원', '', 'guest', true, 'slack_sync', 'E1'),
  ('b@ty.co.kr', '나사원', '', 'guest', true, 'slack_sync', 'E2'),
  ('c@ty.co.kr', '다사원', '', 'guest', false, 'slack_sync', 'E3');
"""
TOKENS = {"mgmt": "xoxb-m", "tyit": "xoxb-t"}


@pytest.fixture
def db():
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_schema_isolated import operational_db_refusal

    assert not operational_db_refusal(DSN), "격리 DB 가 아니면 돌지 않는다"
    import psycopg
    from psycopg import sql
    from psycopg.rows import dict_row

    name = "password_test_" + uuid4().hex
    conns = []

    def connect():
        conn = psycopg.connect(DSN, row_factory=dict_row)
        conn.execute(sql.SQL("SET search_path TO {}, public").format(sql.Identifier(name)))
        conns.append(conn)
        return conn

    with psycopg.connect(DSN, autocommit=True) as owner:
        owner.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(name)))
        try:
            with connect() as conn:
                for f in ("index_schema.sql", "console_schema.sql",
                          "console_account_sync_schema.sql", "console_password_schema.sql",
                          "console_password_schema.sql", "bot_access_schema.sql"):
                    conn.execute((ROOT / "deploy/sql" / f).read_text(encoding="utf-8"))
                conn.execute(SEED)
            yield connect
        finally:
            for conn in conns:
                conn.close()
            owner.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(name)))


def _row(connect, email):
    with connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT * FROM console_user WHERE email = %s", (email,))
        return cur.fetchone()


def _issue(connect, slack, email="a@ty.co.kr", kind="initial"):
    return pi.issue(email, kind=kind, actor="admin@ty.co.kr", connect=connect,
                    tokens=TOKENS, sender=slack)


@needs_db
def test_issue_sends_dm_to_live_slack_and_stores_only_hash(db):
    slack = FakeSlack()
    result = _issue(db, slack)
    token, user, _text = slack.sent[0]
    assert (token, user) == ("xoxb-t", "UA2")           # mgmt 의 UA 는 Slack 에서 지워짐
    temp = slack.last_temp()
    row = _row(db, "a@ty.co.kr")
    assert row["must_change_password"] and temp not in row["password_hash"]
    assert result["workspace"] == "tyit"
    assert row["temp_expires_at"] - datetime.now(UTC) > timedelta(hours=71)


@needs_db
def test_dm_failure_changes_nothing(db):
    with pytest.raises(pi.PasswordError, match="dm_failed"):
        _issue(db, FakeSlack(fail=True))
    row = _row(db, "a@ty.co.kr")
    assert row["password_hash"] == "" and not row["must_change_password"]


@needs_db
def test_inactive_or_unknown_account_refused(db):
    with pytest.raises(pi.PasswordError, match="account_not_found"):
        _issue(db, FakeSlack(), email="c@ty.co.kr")
    with pytest.raises(pi.PasswordError, match="account_not_found"):
        _issue(db, FakeSlack(), email="nobody@ty.co.kr")


@needs_db
def test_temp_login_requires_change_then_change_works(db):
    slack = FakeSlack()
    _issue(db, slack)
    temp = slack.last_temp()
    with pytest.raises(pi.PasswordError, match="password_change_required"):
        pi.temp_login("A@ty.co.kr", temp, connect=db)
    with pytest.raises(pi.PasswordError, match="too_simple"):
        pi.change("a@ty.co.kr", temp, "abcdefgh", connect=db, tokens=TOKENS, sender=slack)
    pi.change("a@ty.co.kr", temp, "New#pass9", connect=db, tokens=TOKENS, sender=slack)
    row = _row(db, "a@ty.co.kr")
    assert not row["must_change_password"] and row["password_changed_at"] is not None
    assert pi.temp_login("a@ty.co.kr", "New#pass9", connect=db) is False   # 보통 로그인으로
    assert slack.deleted == [("DUA2", "1.0001")]                           # DM 지움
    with pytest.raises(pi.PasswordError, match="bad_password"):            # 임시는 이제 무효
        pi.change("a@ty.co.kr", temp, "Other#77x", connect=db, tokens=TOKENS, sender=slack)


@needs_db
def test_five_wrong_attempts_lock_the_temp(db):
    slack = FakeSlack()
    _issue(db, slack)
    temp = slack.last_temp()
    for _ in range(4):
        with pytest.raises(pi.PasswordError, match="bad_password"):
            pi.temp_login("a@ty.co.kr", "wrong", connect=db)
    with pytest.raises(pi.PasswordError, match="temp_locked"):
        pi.temp_login("a@ty.co.kr", "wrong", connect=db)
    with pytest.raises(pi.PasswordError, match="temp_locked"):          # 맞혀도 잠김
        pi.temp_login("a@ty.co.kr", temp, connect=db)
    _issue(db, slack, kind="reset")                                     # 재발급하면 풀림
    with pytest.raises(pi.PasswordError, match="password_change_required"):
        pi.temp_login("a@ty.co.kr", slack.last_temp(), connect=db)


@needs_db
def test_expired_temp_and_cleanup_deletes_dm(db):
    slack = FakeSlack()
    _issue(db, slack)
    temp = slack.last_temp()
    with db() as conn:
        conn.execute("UPDATE console_user SET temp_expires_at = now() - interval '1 minute'")
        conn.execute("UPDATE console_password_issue SET expires_at = now() - interval '1 minute'")
    with pytest.raises(pi.PasswordError, match="temp_expired"):
        pi.temp_login("a@ty.co.kr", temp, connect=db)
    result = pi.cleanup(connect=db, tokens=TOKENS, sender=slack)
    assert result["expired"] == 1 and slack.deleted == [("DUA2", "1.0001")]
    assert pi.cleanup(connect=db, tokens=TOKENS, sender=slack)["dmDeleteTried"] == 0


@needs_db
def test_reset_replaces_old_temp_and_deletes_old_dm(db):
    slack = FakeSlack()
    _issue(db, slack)
    first = slack.last_temp()
    _issue(db, slack, kind="reset")
    assert slack.deleted == [("DUA2", "1.0001")]
    with pytest.raises(pi.PasswordError, match="bad_password"):
        pi.temp_login("a@ty.co.kr", first, connect=db)


@needs_db
def test_initial_preview_and_send(db):
    slack = FakeSlack()
    preview = pi.initial(actor="admin@ty.co.kr", apply=False, connect=db, tokens=TOKENS,
                         sender=slack)
    assert [t["email"] for t in preview["targets"]] == ["a@ty.co.kr", "b@ty.co.kr"]
    assert slack.sent == []
    done = pi.initial(actor="admin@ty.co.kr", apply=True, connect=db, tokens=TOKENS,
                      sender=slack)
    assert len(done["sent"]) == 2 and done["failed"] == []
    again = pi.initial(actor="admin@ty.co.kr", apply=False, connect=db, tokens=TOKENS,
                       sender=slack)
    assert again["targets"] == []                                     # 이미 발급됨


@needs_db
def test_people_list_shows_password_state(db):
    from tybot.console import user_directory

    slack = FakeSlack()
    _issue(db, slack)
    people = {p["empNo"]: p for p in user_directory.list_people(connect=db)["people"]}
    assert people["E1"]["console"]["password"] == "temp"
    assert people["E2"]["console"]["password"] == "none"


# ---------------------------------------------------------------------------
# 라우트 — 로그인 분기와 변경
# ---------------------------------------------------------------------------

@pytest.fixture
def http(monkeypatch):
    from fastapi.testclient import TestClient

    from tybot.console import app as console_app
    from tybot.console.auth import Authenticator, account, hash_password

    auth = Authenticator(accounts=[
        account("admin@ty.co.kr", hash_password("Admin#pass1"), "관리자", "admin"),
    ], secret="s")
    console_app.app.dependency_overrides[console_app.authenticator] = lambda: auth
    monkeypatch.setattr(console_app, "_audit_event", lambda **kw: None)
    yield TestClient(console_app.app), console_app
    console_app.app.dependency_overrides.clear()


def _raise(code):
    def f(*a, **kw):
        raise pi.PasswordError(code)
    return f


def test_login_with_temp_asks_for_change_without_session(http, monkeypatch):
    client, app = http
    monkeypatch.setattr(app.password_issue, "temp_login", _raise("password_change_required"))
    r = client.post("/api/login", json={"email": "a@ty.co.kr", "password": "Ab3#xY"})
    assert r.status_code == 409 and r.json()["detail"] == "password_change_required"
    assert "tybot_console" not in r.headers.get("set-cookie", "")


@pytest.mark.parametrize(("code", "status"), [("temp_expired", 401), ("temp_locked", 401)])
def test_login_temp_errors_are_codes(http, monkeypatch, code, status):
    client, app = http
    monkeypatch.setattr(app.password_issue, "temp_login", _raise(code))
    r = client.post("/api/login", json={"email": "a@ty.co.kr", "password": "x"})
    assert r.status_code == status and r.json()["detail"] == code


def test_login_wrong_temp_looks_like_any_wrong_password(http, monkeypatch):
    client, app = http
    monkeypatch.setattr(app.password_issue, "temp_login", _raise("bad_password"))
    r = client.post("/api/login", json={"email": "a@ty.co.kr", "password": "x"})
    assert r.status_code == 401 and r.json()["detail"] == "아이디 또는 비밀번호가 맞지 않습니다."


def test_login_falls_back_when_temp_state_unreadable(http, monkeypatch):
    client, app = http
    monkeypatch.setattr(app.password_issue, "temp_login", _raise("database_unavailable"))
    r = client.post("/api/login", json={"email": "admin@ty.co.kr", "password": "Admin#pass1"})
    assert r.status_code == 200


def test_change_needs_csrf_and_maps_codes(http, monkeypatch):
    client, app = http
    body = {"email": "a@ty.co.kr", "currentPassword": "Ab3#xY", "newPassword": "short"}
    assert client.post("/api/password/change", json=body).status_code == 403
    monkeypatch.setattr(app.password_issue, "change", _raise("too_short"))
    r = client.post("/api/password/change", json=body, headers={"X-TYBot-CSRF": "1", "Origin": "http://testserver"})
    assert r.status_code == 422 and r.json()["detail"] == "too_short"
    monkeypatch.setattr(app.password_issue, "change", lambda *a, **kw: {"email": "a@ty.co.kr"})
    r = client.post("/api/password/change", json=body, headers={"X-TYBot-CSRF": "1", "Origin": "http://testserver"})
    assert r.status_code == 200


@needs_db
def test_login_email_accepts_emp_no(db):
    assert pi.login_email("E1", connect=db) == "a@ty.co.kr"
    assert pi.login_email("e1", connect=db) == "a@ty.co.kr"          # 대소문자 무시
    assert pi.login_email(" A@TY.co.kr ", connect=db) == "a@ty.co.kr"
    assert pi.login_email("NOPE", connect=db) == "NOPE"               # 모르면 그대로 → 로그인 거절


@needs_db
def test_dm_shows_emp_no_as_login_id(db):
    slack = FakeSlack()
    _issue(db, slack)
    assert "아이디(사번): E1" in slack.sent[-1][2]


def test_login_with_emp_no_resolves_before_checks(http, monkeypatch):
    client, app = http
    seen = {}
    monkeypatch.setattr(app.password_issue, "login_email", lambda ident: "admin@ty.co.kr" if ident == "9318-F" else ident)
    monkeypatch.setattr(app.password_issue, "temp_login", lambda email, pw: seen.setdefault("email", email) and False)
    r = client.post("/api/login", json={"email": "9318-F", "password": "Admin#pass1"})
    assert r.status_code == 200 and seen["email"] == "admin@ty.co.kr"
