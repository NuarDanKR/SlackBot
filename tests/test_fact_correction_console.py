"""콘솔 — 사람이 남긴 정정 목록과 관리자 철회."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from tybot.console import fact_correction_routes as routes
from tybot.console.auth import ConsoleUser


class Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=()):
        self.conn.executed.append((sql, tuple(params)))

    def fetchone(self):
        return self.conn.one

    def fetchall(self):
        return self.conn.rows


class Conn:
    def __init__(self, rows=(), one=None):
        self.rows = list(rows)
        self.one = one
        self.executed: list = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def cursor(self):
        return Cursor(self)

    def commit(self):
        pass


ROW = {
    "id": 7,
    "workspace": "tyit",
    "corrector_id": "U9",
    "corrector_name": "홍길동",
    "subject": "김해외동 기성금",
    "before_value": "15억",
    "after_value": "12억",
    "body": "15억이 아니라 12억",
    "source_channel_id": "C1",
    "evidence_refs": [{"kind": "archive_line"}],
    "created_at": datetime(2026, 11, 20, tzinfo=UTC),
    "expires_at": datetime(2027, 2, 18, tzinfo=UTC),
    "show_count": 2,
}


def _client(user, conn, audits, *, admin=True, csrf_ok=True):
    def require_admin(_user):
        if not admin:
            raise HTTPException(status_code=403, detail={"code": "admin_required"})

    def check_write(_request: Request):
        if not csrf_ok:
            raise HTTPException(status_code=403, detail={"code": "csrf"})

    app = FastAPI()
    app.include_router(
        routes.build_router(
            current_user=lambda: user,
            require_admin=require_admin,
            check_write_request=check_write,
            audit_event=lambda **kw: audits.append(kw),
            connect=lambda: contextlib.nullcontext(conn),
        )
    )
    return TestClient(app)


def _user(workspaces=("tyit",)):
    return ConsoleUser(
        email="admin@example.com", name="관리자", role="admin", workspaces=frozenset(workspaces)
    )


def test_list_returns_who_what_and_values_but_not_coordinates():
    conn = Conn(rows=[ROW])

    got = _client(_user(), conn, []).get("/api/fact-corrections", params={"workspace": "tyit"})

    assert got.status_code == 200
    item = got.json()["corrections"][0]
    assert (item["correctorName"], item["before"], item["after"]) == ("홍길동", "15억", "12억")
    assert "evidence_refs" not in item and "evidenceRefs" not in item


def test_other_workspace_is_forbidden():
    got = _client(_user(("tymgmt",)), Conn(), []).get(
        "/api/fact-corrections", params={"workspace": "tyit"}
    )

    assert got.status_code == 403


def test_non_admin_cannot_list():
    got = _client(_user(), Conn(), [], admin=False).get(
        "/api/fact-corrections", params={"workspace": "tyit"}
    )

    assert got.status_code == 403


def test_revoke_requires_reason_csrf_and_is_audited():
    audits: list = []
    conn = Conn(one={"id": 7})
    client = _client(_user(), conn, audits)

    assert (
        client.post("/api/fact-corrections/7/revoke", json={"workspace": "tyit"}).status_code == 422
    )
    got = client.post(
        "/api/fact-corrections/7/revoke", json={"workspace": "tyit", "reason": "틀린 정정"}
    )

    assert got.status_code == 200 and got.json()["revoked"] is True
    sql, params = conn.executed[-1]
    assert "UPDATE fact_correction SET revoked_at" in sql and "corrector_id" not in sql
    assert params[1:3] == ("admin@example.com", "틀린 정정")
    assert audits and audits[0]["action"] == "fact_correction_revoke"
    assert "틀린 정정" not in str(audits[0]["metadata"])

    blocked = _client(_user(), Conn(one={"id": 7}), [], csrf_ok=False).post(
        "/api/fact-corrections/7/revoke", json={"workspace": "tyit", "reason": "x"}
    )
    assert blocked.status_code == 403


def test_revoking_an_inactive_correction_is_404():
    got = _client(_user(), Conn(one=None), []).post(
        "/api/fact-corrections/9/revoke", json={"workspace": "tyit", "reason": "x"}
    )

    assert got.status_code == 404


def test_database_unavailable_is_503():
    def broken():
        raise routes.FactCorrectionUnavailable("database_unavailable")

    app = FastAPI()
    app.include_router(
        routes.build_router(
            current_user=_user,
            require_admin=lambda _u: None,
            check_write_request=lambda _r: None,
            audit_event=lambda **_k: None,
            connect=broken,
        )
    )

    got = TestClient(app).get("/api/fact-corrections", params={"workspace": "tyit"})

    assert got.status_code == 503
