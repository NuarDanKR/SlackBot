"""B-101 Vision 카탈로그 재검증 — **서버가 막는가**를 건다. 화면을 믿지 않는다."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tybot.archive.image_description_store import ImageDescriptionStore
from tybot.archive.image_model_catalog import FreeQuotaState
from tybot.console import app as console_app
from tybot.console import image_catalog_reverify as rv
from tybot.console.auth import ConsoleUser

NOW = datetime(2026, 11, 27, 2, 30, tzinfo=UTC)
OLD = datetime(2026, 11, 23, 23, 31, tzinfo=UTC)
REV = "google-vision-unlimited-owner-approved-2026-11-24"


def _row(**over) -> dict:
    base = {
        "provider": "google",
        "model": "gemini-3.1-flash-lite",
        "supports_vision": True,
        "zero_cost_verified": False,
        "verified_at": OLD,
        "verification_revision": REV,
        "max_sensitivity": "internal",
        "allowed_regions": ["global"],
        "credential_ready": True,
        "quota_state": "unknown",
        "quota_remaining": None,
        "quota_resets_at": None,
        "updated_by": "runtime-cost-guard",
        "updated_at": OLD,
    }
    return base | over


class _Cursor:
    def __init__(self, conn):
        self.conn = conn
        self._one = None
        self._all: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        if "FROM archiver_image_model_catalog" in sql:
            self._one = self.conn.catalog
        elif "FROM archiver_image_description_job" in sql:
            self._all = self.conn.jobs
        elif "FROM archiver_image_description_policy" in sql:
            self._all = self.conn.policies

    def fetchone(self):
        return self._one

    def fetchall(self):
        return list(self._all)


class _Conn:
    def __init__(self, catalog=None, jobs=(), policies=()):
        self.catalog = catalog
        self.jobs = list(jobs)
        self.policies = list(policies)
        self.sql: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _Cursor(self)


@pytest.fixture
def saved(monkeypatch):
    calls: list[dict] = []

    def save_catalog(self, entry, *, expected_revision, actor):
        calls.append({"entry": entry, "expected": expected_revision, "actor": actor})

    monkeypatch.setattr(ImageDescriptionStore, "save_catalog", save_catalog)
    return calls


def _req(**over) -> rv.ReverifyRequest:
    base = {
        "provider": "google",
        "model": "gemini-3.1-flash-lite",
        "expected_revision": REV,
        "quota_state": "unlimited",
        "quota_remaining": None,
        "confirm_no_charge": True,
        "reason": "11-24 타임아웃 오판 — Google 콘솔 과금 0 확인",
        "actor": "owner@taeyoung.com",
    }
    return rv.ReverifyRequest(**(base | over))


# --- 재검증 경계 -----------------------------------------------------------------


def test_reverify_turns_the_free_claim_back_on_with_a_fresh_verification(saved):
    conn = _Conn(_row())
    said = rv.reverify(_req(), now=NOW, connect=lambda: conn)

    (call,) = saved
    entry = call["entry"]
    assert call["expected"] == REV and call["actor"] == "owner@taeyoung.com"
    assert entry.zero_cost_verified is True
    assert entry.verified_at == NOW  # 옛 검증 시각을 두면 TTL 로 다시 막힌다
    assert entry.verification_revision == "console-reverified-20261127T023000Z"
    assert entry.quota_state is FreeQuotaState.UNLIMITED and entry.quota_remaining is None
    # 다른 칸은 그대로다.
    assert entry.supports_vision and entry.credential_ready
    assert entry.allowed_regions == frozenset({"global"})
    meta = said.audit["metadata"]
    assert said.audit["action"] == "image_model_catalog_reverify"
    assert said.audit["target_id"] == "google/gemini-3.1-flash-lite"
    assert meta["previous_updated_by"] == "runtime-cost-guard"
    assert meta["previous_revision"] == REV and meta["reason"].startswith("11-24")


def test_available_quota_needs_a_positive_remaining(saved):
    conn = _Conn(_row())
    with pytest.raises(rv.CatalogReverifyError) as exc:
        rv.reverify(_req(quota_state="available"), now=NOW, connect=lambda: conn)
    assert exc.value.code == "quota_remaining_invalid"
    rv.reverify(_req(quota_state="available", quota_remaining=1000), now=NOW, connect=lambda: conn)
    assert saved[-1]["entry"].quota_remaining == 1000


@pytest.mark.parametrize(
    ("over", "code"),
    [
        ({"confirm_no_charge": False}, "confirmation_required"),
        ({"reason": "   "}, "reason_required"),
        ({"quota_state": "unknown"}, "quota_state_invalid"),
        ({"quota_state": "exhausted"}, "quota_state_invalid"),
        ({"quota_remaining": 5}, "quota_remaining_invalid"),
        ({"model": "bad model; drop"}, "invalid_request"),
    ],
)
def test_bad_requests_are_refused_before_touching_the_database(saved, over, code):
    conn = _Conn(_row())
    with pytest.raises(rv.CatalogReverifyError) as exc:
        rv.reverify(_req(**over), now=NOW, connect=lambda: conn)
    assert exc.value.code == code
    assert saved == [] and conn.sql == []


def test_a_stale_revision_is_a_conflict(saved):
    conn = _Conn(_row(verification_revision="someone-else"))
    with pytest.raises(rv.CatalogReverifyError) as exc:
        rv.reverify(_req(), now=NOW, connect=lambda: conn)
    assert exc.value.code == "catalog_conflict"
    assert saved == []


def test_a_model_that_still_cannot_run_is_not_turned_on(saved):
    """다시 켜도 안 도는 모델을 켜면 운영자는 켰다고 믿는다."""
    conn = _Conn(_row(credential_ready=False, supports_vision=False))
    with pytest.raises(rv.CatalogReverifyError) as exc:
        rv.reverify(_req(), now=NOW, connect=lambda: conn)
    assert exc.value.code == "reverify_blocked"
    assert set(exc.value.blockers) == {"credential_not_ready", "vision_not_verified"}
    assert saved == []


def test_missing_model_and_database_errors_become_fixed_codes(saved):
    with pytest.raises(rv.CatalogReverifyError) as exc:
        rv.reverify(_req(), now=NOW, connect=lambda: _Conn(None))
    assert exc.value.code == "catalog_not_found"

    def broken():
        raise RuntimeError("password=secret host=db")

    with pytest.raises(rv.CatalogReverifyError) as exc:
        rv.reverify(_req(), now=NOW, connect=broken)
    assert exc.value.code == "catalog_unavailable"
    assert "secret" not in str(exc.value)


# --- 조회 ------------------------------------------------------------------------


def test_inspect_shows_who_turned_it_off_and_which_workspaces_will_resume():
    conn = _Conn(
        _row(),
        jobs=[
            {
                "created_at": OLD,
                "workspace": "tyit",
                "error_code": "model_timeout,cost_unverified",
                "actual_usd": None,
            },
        ],
        policies=[
            {
                "workspace": "",
                "enabled": True,
                "config": {"provider": "google", "model": "gemini-3.1-flash-lite"},
            },
            {"workspace": "tyit", "enabled": True, "config": {}},
            {"workspace": "tec", "enabled": False, "config": {}},
            {"workspace": "invest", "enabled": True, "config": {"model": "other"}},
        ],
    )
    got = rv.inspect("google", "gemini-3.1-flash-lite", connect=lambda: conn)
    assert got["disabledByGuard"] is True and got["zeroCostVerified"] is False
    assert got["costJobs"][0]["errorCode"] == "model_timeout,cost_unverified"
    assert got["chargeSeen"] is False
    assert got["affectedWorkspaces"] == [
        {"workspace": "tec", "enabled": False},
        {"workspace": "tyit", "enabled": True},
    ]
    assert not any("INSERT" in sql or "UPDATE" in sql for sql in conn.sql)


def test_inspect_flags_a_real_charge():
    conn = _Conn(
        _row(),
        jobs=[
            {
                "created_at": OLD,
                "workspace": "tyit",
                "error_code": "unexpected_charge",
                "actual_usd": "0.0012",
            }
        ],
    )
    assert rv.inspect("google", "gemini-3.1-flash-lite", connect=lambda: conn)["chargeSeen"]


# --- 라우트 ----------------------------------------------------------------------


def _user(*, role="admin", all_workspaces=True) -> ConsoleUser:
    return ConsoleUser(
        email="owner@taeyoung.com",
        name="운영자",
        role=role,
        workspaces=frozenset({"tyit"}),
        all_workspaces=all_workspaces,
    )


def _request(*, csrf="1") -> Request:
    headers = [(b"host", b"console.example"), (b"origin", b"https://console.example")]
    if csrf:
        headers.append((b"x-tybot-csrf", csrf.encode()))
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/",
            "headers": headers,
            "server": ("console.example", 443),
            "client": ("127.0.0.1", 1),
        }
    )


def _routes(conn, audits):
    router = rv.build_router(
        current_user=console_app.current_user,
        require_admin=console_app._require_admin,
        check_write_request=console_app._check_write_request,
        audit_event=lambda **kw: audits.append(kw),
        now=lambda: NOW,
        connect=lambda: conn,
    )
    return {(next(iter(r.methods)), r.path): r.endpoint for r in router.routes}


def _body(**over) -> rv.ReverifyBody:
    base = {
        "provider": "google",
        "model": "gemini-3.1-flash-lite",
        "expectedRevision": REV,
        "quotaState": "unlimited",
        "confirmNoCharge": True,
        "reason": "과금 0 확인",
    }
    return rv.ReverifyBody(**(base | over))


def test_the_route_is_registered_on_the_console_app():
    pairs = set()
    for route in console_app.app.routes:
        # 최신 FastAPI 는 include_router 를 `_IncludedRouter` 로 감싼다.
        inner = getattr(route, "original_router", None)
        for r in inner.routes if inner is not None else (route,):
            pairs |= {(m, getattr(r, "path", "")) for m in getattr(r, "methods", ()) or ()}
    assert ("GET", rv.PATH) in pairs and ("POST", rv.PATH) in pairs


def test_route_saves_and_audits_with_the_logged_in_actor(saved):
    audits: list[dict] = []
    post = _routes(_Conn(_row()), audits)[("POST", rv.PATH)]
    got = post(body=_body(), request=_request(), user=_user())
    assert got["revision"] == "console-reverified-20261127T023000Z"
    assert got["previousUpdatedBy"] == "runtime-cost-guard" and got["audited"] is True
    assert audits[0]["actor"] == "owner@taeyoung.com"
    assert saved[0]["actor"] == "owner@taeyoung.com"


@pytest.mark.parametrize(
    ("user", "csrf", "status"),
    [
        (_user(all_workspaces=False), "1", 403),  # 전역 카탈로그 — 일부만 보는 관리자 거절
        (_user(role="viewer"), "1", 403),
        (_user(), "", 403),  # CSRF
    ],
)
def test_route_refuses_without_global_admin_or_csrf(saved, user, csrf, status):
    post = _routes(_Conn(_row()), [])[("POST", rv.PATH)]
    with pytest.raises(HTTPException) as exc:
        post(body=_body(), request=_request(csrf=csrf), user=user)
    assert exc.value.status_code == status
    assert saved == []


def test_route_maps_conflict_and_missing_confirmation(saved):
    post = _routes(_Conn(_row(verification_revision="x")), [])[("POST", rv.PATH)]
    with pytest.raises(HTTPException) as exc:
        post(body=_body(), request=_request(), user=_user())
    assert exc.value.status_code == 409
    post = _routes(_Conn(_row()), [])[("POST", rv.PATH)]
    with pytest.raises(HTTPException) as exc:
        post(body=_body(confirmNoCharge=False), request=_request(), user=_user())
    assert exc.value.status_code == 422
    assert exc.value.detail["code"] == "confirmation_required"


def test_body_refuses_an_actor_field():
    with pytest.raises(ValueError):
        _body(actor="someone@else")


def test_get_route_is_admin_and_global_only():
    get = _routes(_Conn(_row()), [])[("GET", rv.PATH)]
    with pytest.raises(HTTPException) as exc:
        get(provider="google", model="gemini-3.1-flash-lite", user=_user(all_workspaces=False))
    assert exc.value.status_code == 403
    got = get(provider="google", model="gemini-3.1-flash-lite", user=_user())
    assert got["disabledByGuard"] is True
