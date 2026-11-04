"""봇 사용 권한 — 판정 규칙(DB 없이)과 콘솔 경계."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tybot import bot_access
from tybot.console import user_directory as store

ROOT = Path(__file__).resolve().parents[1]


class _Cursor:
    def __init__(self, rows_by_call):
        self._rows = list(rows_by_call)
        self._current = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._current = self._rows.pop(0)

    def fetchone(self):
        return self._current[0] if self._current else None

    def fetchall(self):
        return list(self._current)


class _Conn:
    def __init__(self, *rows_by_call):
        self._cursor = _Cursor(rows_by_call)

    def cursor(self):
        return self._cursor


class _Broken:
    def cursor(self):
        raise RuntimeError("password=secret host=db")


def test_no_identity_is_denied():
    decision = bot_access.may_use_bot(_Conn([]), bot="vipbot", workspace="tyit",
                                      slack_user="U1")
    assert decision == bot_access.Decision(False, bot_access.NO_IDENTITY)


def test_allowed_needs_live_grant():
    conn = _Conn([{"emp_no": "1001"}],
                 [{"emp_no": "1001", "active": True, "grant_id": 7,
                   "expires_at": None, "live": True}])
    decision = bot_access.may_use_bot(conn, bot="vipbot", workspace="tyit", slack_user="U1")
    assert decision.allowed and decision.code == bot_access.ALLOWED
    assert decision.grant_id == 7 and decision.emp_no == "1001"


@pytest.mark.parametrize(("row", "code"), [
    ({"emp_no": "1001", "active": False, "grant_id": 7, "expires_at": None, "live": True},
     bot_access.INACTIVE_EMPLOYEE),
    ({"emp_no": "1001", "active": True, "grant_id": None, "expires_at": None, "live": False},
     bot_access.NO_GRANT),
    ({"emp_no": "1001", "active": True, "grant_id": 7,
      "expires_at": datetime(2026, 1, 1, tzinfo=UTC), "live": False},
     bot_access.EXPIRED),
])
def test_denials(row, code):
    conn = _Conn([{"emp_no": "1001"}], [row])
    decision = bot_access.may_use_bot(conn, bot="vipbot", workspace="tyit", slack_user="U1")
    assert not decision.allowed and decision.code == code


def test_ambiguous_identity_is_denied():
    conn = _Conn([{"emp_no": "1"}, {"emp_no": "2"}])
    assert bot_access.may_use_bot(conn, bot="vipbot", workspace="tyit",
                                  slack_user="U1").code == bot_access.NO_IDENTITY


def test_database_error_is_denial_not_exception():
    decision = bot_access.may_use_bot(_Broken(), bot="vipbot", workspace="tyit",
                                      slack_user="U1")
    assert decision == bot_access.Decision(False, bot_access.CHECK_FAILED)


def test_unknown_bot_is_denied_before_db():
    assert bot_access.may_use_bot(_Broken(), bot="nobot", workspace="tyit",
                                  slack_user="U1").code == bot_access.UNKNOWN_BOT


def test_deny_message_hides_reason():
    for word in ("사번", "퇴직", "만료", "매핑"):
        assert word not in bot_access.DENY_MESSAGE


def test_bot_keys_match_schema_check():
    assert all(bot_access.BOT_KEY.fullmatch(key) for key in bot_access.BOTS)


def test_store_refuses_bad_input_without_db():
    def never():
        raise AssertionError("DB 에 가면 안 된다")

    with pytest.raises(store.UserDirectoryError, match="unknown_bot"):
        store.list_grants(bot="nobot", connect=never)
    with pytest.raises(store.UserDirectoryError, match="invalid_request"):
        store.list_people(view="everyone", connect=never)
    with pytest.raises(store.UserDirectoryError, match="invalid_request"):
        store.list_people(limit=1000, connect=never)
    with pytest.raises(store.UserDirectoryError, match="expires_in_past"):
        store.grant(store.GrantRequest(
            bot="vipbot", emp_no="1", reason="r", actor="a@x",
            expires_at=datetime.now(UTC) - timedelta(minutes=1)), connect=never)
    with pytest.raises(store.UserDirectoryError, match="invalid_request"):
        store.grant(store.GrantRequest(bot="vipbot", emp_no="1", reason="  ",
                                       actor="a@x"), connect=never)
    with pytest.raises(store.UserDirectoryError, match="invalid_request"):
        store.check(bot="vipbot", connect=never)
    with pytest.raises(store.UserDirectoryError, match="invalid_request"):
        store.check(bot="vipbot", emp_no="1", workspace="tyit", slack_user="U1",
                    connect=never)


def test_store_error_text_is_code_only():
    def broken():
        raise RuntimeError("password=secret")

    with pytest.raises(store.UserDirectoryError) as info:
        store.list_people(connect=broken)
    assert str(info.value) == "database_unavailable"
    assert set(store.CODES.values()) <= {404, 409, 422, 503}


def test_schema_is_applied_after_employee():
    files = (ROOT / "deploy/apply-schema.sh").read_text(encoding="utf-8")
    assert files.index("index_schema.sql") < files.index("bot_access_schema.sql")


def test_schema_declares_closed_by_default_shape():
    sql = (ROOT / "deploy/sql/bot_access_schema.sql").read_text(encoding="utf-8")
    assert "REFERENCES employee(emp_no) ON DELETE RESTRICT" in sql
    assert "bot_access_one_open" in sql and "WHERE revoked_at IS NULL" in sql
    # 회수는 하되 지우지는 않는다 — 운영 역할에 DELETE 를 주지 않는다.
    assert "DELETE" not in sql.split("DO $$", 1)[1]


def test_routes_are_registered():
    from tybot.console.app import user_access_router

    paths = {(m, r.path) for r in user_access_router.routes for m in r.methods}
    assert paths >= {("GET", "/api/people"), ("GET", "/api/bot-access"),
                     ("GET", "/api/bot-access/check"), ("POST", "/api/bot-access"),
                     ("POST", "/api/bot-access/{grant_id}/revoke")}


def _endpoints(audit):
    from fastapi import HTTPException

    from tybot.console import app as console_app
    from tybot.console import user_access_routes as routes

    def no_csrf(request):
        if request is None:
            raise HTTPException(status_code=403, detail="csrf")

    router = routes.build_router(current_user=console_app.current_user,
                                 require_admin=console_app._require_admin,
                                 check_write_request=no_csrf, audit_event=audit)
    return {(next(iter(r.methods)), r.path): r.endpoint for r in router.routes}


def _user(role):
    from tybot.console.auth import ConsoleUser

    return ConsoleUser(email="who@ty.co.kr", name="", role=role, all_workspaces=role == "admin")


@pytest.mark.parametrize("role", ["guest", "developer"])
def test_non_admin_is_refused_everywhere(role):
    from fastapi import HTTPException

    from tybot.console.user_access_routes import GrantBody, RevokeBody

    ep = _endpoints(lambda **kw: None)
    calls = [
        lambda: ep[("GET", "/api/people")](view="slack", q=None, workspace=None, bot=None, org=None, account=None, vip=None,
                                           limit=50, offset=0, user=_user(role)),
        lambda: ep[("GET", "/api/bot-access")](bot="vipbot", empNo=None, user=_user(role)),
        lambda: ep[("GET", "/api/bot-access/check")](bot="vipbot", empNo="1", workspace=None,
                                                     slackUser=None, user=_user(role)),
        lambda: ep[("POST", "/api/bot-access")](
            GrantBody(bot="vipbot", empNo="1", reason="r"), object(), user=_user(role)),
        lambda: ep[("POST", "/api/bot-access/{grant_id}/revoke")](
            1, RevokeBody(reason="r"), object(), user=_user(role)),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as info:
            call()
        assert info.value.status_code == 403


def test_grant_is_audited_and_errors_map_to_status(monkeypatch):
    from fastapi import HTTPException

    from tybot.console.user_access_routes import GrantBody, RevokeBody

    audits = []
    ep = _endpoints(lambda **kw: audits.append(kw))
    row = {"id": 5, "bot": "vipbot", "empNo": "E1", "expiresAt": None}
    monkeypatch.setattr(store, "grant", lambda req: row)
    assert ep[("POST", "/api/bot-access")](
        GrantBody(bot="vipbot", empNo="E1", reason="r"), object(), user=_user("admin")) is row
    assert audits[0]["action"] == "grant" and audits[0]["target_id"] == "5"

    def refuse(*a, **kw):
        raise store.UserDirectoryError("already_revoked")

    monkeypatch.setattr(store, "revoke", refuse)
    with pytest.raises(HTTPException) as info:
        ep[("POST", "/api/bot-access/{grant_id}/revoke")](
            5, RevokeBody(reason="r"), object(), user=_user("admin"))
    assert info.value.status_code == 409 and info.value.detail == "already_revoked"
    assert len(audits) == 1                       # 실패는 감사에 「성공」 으로 남기지 않는다


@pytest.mark.parametrize("role", ["guest", "developer"])
def test_source_routes_are_admin_only(role):
    from fastapi import HTTPException

    from tybot.console.user_access_routes import SourcesBody

    ep = _endpoints(lambda **kw: None)
    body = SourcesBody(bot="vipbot", channels=[{"workspace": "mgmt", "channelId": "C0AAA1111"}],
                       reason="r")
    calls = [
        lambda: ep[("GET", "/api/bot-sources")](bot="vipbot", user=_user(role)),
        lambda: ep[("POST", "/api/bot-sources")](body, object(), user=_user(role)),
        lambda: ep[("POST", "/api/bot-sources/remove")](body, object(), user=_user(role)),
    ]
    for call in calls:
        with pytest.raises(HTTPException) as info:
            call()
        assert info.value.status_code == 403
