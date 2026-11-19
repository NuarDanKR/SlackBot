"""B-80 PDF 쪽 Vision 정책 콘솔 — 서버가 막는가. DB·모델·네트워크 없음."""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import ClassVar

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tybot.archive.vision_store import CONFIG_KEYS
from tybot.console import app as console_app
from tybot.console import pdf_vision_admin as admin
from tybot.console.auth import ConsoleUser

routes = console_app.pdf_vision_routes

FULL = {
    "provider": "google", "model": "gemini-2.5-flash", "max_image_bytes": 4_000_000,
    "max_output_tokens": 4000, "timeout_seconds": 60, "daily_usd": "5", "job_usd": "0.05",
    "max_concurrency": 2, "input_usd_per_mtok": "0.3", "output_usd_per_mtok": "2.5",
}


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, sql, args=None):
        self.conn.sql.append(sql)
        if "FROM archiver_vision_policy" in sql:
            self.rows = list(self.conn.policy_rows)
        elif "GROUP BY state" in sql:
            self.rows = [{"state": "settled", "n": 3}]
        elif "AS charged" in sql:
            self.rows = [{"charged": Decimal("0.0123")}]
        else:
            self.rows = [{"id": "j1", "state": "settled", "file_id": "F1", "page": 2,
                          "reserved_usd": Decimal("0.05"), "actual_usd": Decimal("0.0041"),
                          "created_at": datetime(2026, 11, 19, tzinfo=UTC), "finished_at": None}]

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return self.rows[0]


class Conn:
    def __init__(self, policy_rows):
        self.policy_rows = policy_rows
        self.sql = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def cursor(self):
        return Cursor(self)


def _rows(global_config=None, local=None):
    rows = [{"workspace": "", "revision": "g1", "enabled": True,
             "config": global_config if global_config is not None else FULL,
             "updated_by": "s", "updated_at": None}]
    if local is not None:
        rows.append({"workspace": "tyit", "revision": "w1", "enabled": False, "config": local,
                     "updated_by": "s", "updated_at": None})
    return rows


class FakeStore:
    saved: ClassVar[list] = []

    def __init__(self, conn):
        self.conn = conn

    def save_policy(self, workspace, *, expected_revision, enabled, config, actor):
        FakeStore.saved.append((workspace, expected_revision, enabled, config, actor))
        return "new-rev"


@pytest.fixture(autouse=True)
def _store(monkeypatch):
    FakeStore.saved = []
    monkeypatch.setattr(admin, "VisionStore", FakeStore)


def _req(**over):
    base = dict(workspace="tyit", expected_revision="g1:w1", enabled=True, config={},
                reason="PDF 소급 시작", actor="ops@taeyoung.com")
    return admin.PolicyChangeRequest(**(base | over))


def test_console_validates_every_ledger_config_key():
    assert set(admin.VALIDATORS) == set(CONFIG_KEYS)


def test_enabling_without_a_tariff_is_blocked_with_the_missing_keys():
    config = {k: v for k, v in FULL.items() if not k.endswith("_per_mtok")}
    with pytest.raises(admin.PdfVisionAdminError) as caught:
        admin.save_policy_change(_req(), connect=lambda: Conn(_rows(config, {})))
    assert caught.value.code == "activation_blocked"
    assert set(caught.value.blockers) == {
        "activation_requires_input_usd_per_mtok", "activation_requires_output_usd_per_mtok",
        "activation_requires_global_input_usd_per_mtok",
        "activation_requires_global_output_usd_per_mtok",
    }
    assert FakeStore.saved == []


def test_global_caps_must_live_on_the_global_row():
    """`reserve` 는 전역 행의 daily_usd·max_concurrency 를 따로 본다."""
    global_config = {k: v for k, v in FULL.items() if k not in ("daily_usd", "max_concurrency")}
    local = {"daily_usd": "5", "max_concurrency": 2}
    with pytest.raises(admin.PdfVisionAdminError) as caught:
        admin.save_policy_change(_req(), connect=lambda: Conn(_rows(global_config, local)))
    assert set(caught.value.blockers) == {"activation_requires_global_daily_usd",
                                          "activation_requires_global_max_concurrency"}


def test_a_job_cap_below_the_output_bound_cannot_be_enabled():
    # 4000 tok × $2.5/M = $0.01 > $0.005 → 모든 예약이 reservation_below_tariff 가 된다
    with pytest.raises(admin.PdfVisionAdminError) as caught:
        admin.save_policy_change(_req(config={"job_usd": "0.005"}),
                                 connect=lambda: Conn(_rows(FULL, {})))
    assert caught.value.blockers == ("job_usd_below_output_bound",)


def test_disabling_is_always_allowed_even_with_missing_settings():
    out = admin.save_policy_change(_req(enabled=False), connect=lambda: Conn(_rows({}, {})))
    assert out.revision == "new-rev"
    assert FakeStore.saved[0][2] is False


def test_save_merges_into_the_current_row_and_stores_money_as_text():
    out = admin.save_policy_change(
        _req(config={"job_usd": 0.05, "daily_usd": "3"}),
        connect=lambda: Conn(_rows(FULL, {"timeout_seconds": 30})),
    )
    workspace, expected, enabled, config, actor = FakeStore.saved[0]
    assert (workspace, expected, enabled, actor) == ("tyit", "w1", True, "ops@taeyoung.com")
    assert config == {"timeout_seconds": 30, "job_usd": "0.05", "daily_usd": "3"}
    assert set(out.changed_keys) == {"enabled", "job_usd", "daily_usd"}
    assert out.audit["action"] == "pdf_vision_policy_save"
    assert out.audit["metadata"]["reason"] == "PDF 소급 시작"


@pytest.mark.parametrize("key", ["provider", "model", "input_usd_per_mtok", "output_usd_per_mtok"])
def test_model_and_tariff_are_global_only(key):
    """워크스페이스 관리자가 요금표를 0 에 가깝게 바꾸면 전역 하루 예산을 우회한다."""
    with pytest.raises(admin.PdfVisionAdminError) as caught:
        admin.save_policy_change(_req(config={key: FULL[key]}), connect=lambda: pytest.fail("db"))
    assert (caught.value.code, caught.value.blockers) == ("config_global_only", (key,))


def test_the_global_row_can_set_model_and_tariff():
    admin.save_policy_change(
        _req(workspace="", expected_revision="g1:w1",
             config={"output_usd_per_mtok": 2.5, "model": " gemini-2.5-flash "}),
        connect=lambda: Conn(_rows(FULL, {})),
    )
    workspace, expected, _enabled, config, _actor = FakeStore.saved[0]
    assert (workspace, expected) == ("", "g1")
    assert config["output_usd_per_mtok"] == "2.5" and config["model"] == "gemini-2.5-flash"


def test_an_existing_workspace_override_blocks_activation():
    with pytest.raises(admin.PdfVisionAdminError) as caught:
        admin.save_policy_change(_req(), connect=lambda: Conn(_rows(FULL, {"model": "other"})))
    assert caught.value.blockers == ("workspace_overrides_model",)


@pytest.mark.parametrize(("config", "code"), [
    ({"bogus": 1}, "config_key_unknown"),
    ({"input_usd_per_mtok": "-1"}, "config_invalid_input_usd_per_mtok"),
    ({"output_usd_per_mtok": "0"}, "config_invalid_output_usd_per_mtok"),
    ({"input_usd_per_mtok": "NaN"}, "config_invalid_input_usd_per_mtok"),
    ({"daily_usd": "0"}, "config_invalid_daily_usd"),
    ({"max_concurrency": True}, "config_invalid_max_concurrency"),
    ({"sensitivity": "secret"}, "config_invalid_sensitivity"),
])
def test_invalid_values_are_refused_before_touching_the_db(config, code):
    with pytest.raises(admin.PdfVisionAdminError, match=code):
        admin.save_policy_change(_req(config=config), connect=lambda: pytest.fail("db"))


def test_reason_is_required():
    with pytest.raises(admin.PdfVisionAdminError, match="reason_required"):
        admin.save_policy_change(_req(reason="  "), connect=lambda: pytest.fail("db"))


def test_snapshot_shows_tariff_budget_and_job_coordinates_only():
    got = admin.snapshot("tyit", connect=lambda: Conn(_rows(FULL, {})))
    assert got["policy"]["tariffConfigured"] is True
    assert got["policy"]["blockers"] == []
    assert got["todayChargedUsd"] == "0.0123"
    assert got["jobs"] == [{"id": "j1", "state": "settled", "fileId": "F1", "page": 2,
                            "reservedUsd": "0.05", "actualUsd": "0.0041",
                            "createdAt": "2026-11-19T00:00:00+00:00", "finishedAt": None}]


def test_db_errors_do_not_leak_their_text():
    def boom():
        raise RuntimeError("password=secret host=10.0.0.1")
    with pytest.raises(admin.PdfVisionAdminError) as caught:
        admin.snapshot("tyit", connect=boom)
    assert caught.value.code == "pdf_vision_unavailable"


# --- 라우트 -------------------------------------------------------------------


def _user(*, role="admin", workspaces=("tyit",), all_workspaces=False) -> ConsoleUser:
    return ConsoleUser(email="ops@taeyoung.com", name="운영자", role=role,
                       workspaces=frozenset(workspaces), all_workspaces=all_workspaces)


def _request(csrf="1") -> Request:
    headers = [(b"host", b"console.example"), (b"origin", b"https://console.example")]
    if csrf:
        headers.append((b"x-tybot-csrf", csrf.encode()))
    return Request({"type": "http", "method": "PUT", "scheme": "https", "path": "/",
                    "headers": headers, "server": ("console.example", 443),
                    "client": ("127.0.0.1", 1)})


def _endpoints(audit=lambda **_: None):
    router = routes.build_router(
        current_user=console_app.current_user, require_admin=console_app._require_admin,
        check_write_request=console_app._check_write_request, audit_event=audit,
    )
    return {(r.path, next(iter(r.methods))): r.endpoint for r in router.routes}


def _body(**over):
    base = dict(workspace="tyit", expectedRevision="g1:w1", enabled=True, config={},
                reason="PDF 소급 시작")
    return routes.PolicyBody(**(base | over))


def _route_paths(routes) -> set[str]:
    """FastAPI 0.16x wraps included routers (`_IncludedRouter`); follow them."""
    paths: set[str] = set()
    for route in routes:
        if hasattr(route, "path"):
            paths.add(route.path)
        inner = getattr(route, "original_router", None)
        if inner is not None:
            paths |= _route_paths(inner.routes)
    return paths


def test_routes_are_registered_on_the_console_app():
    paths = _route_paths(console_app.app.routes)
    assert {"/api/archiver/pdf-vision", "/api/archiver/pdf-vision/policy"} <= paths


def test_body_refuses_an_actor_field():
    with pytest.raises(ValueError):
        routes.PolicyBody(enabled=True, reason="x", actor="someone@else")


def test_non_admin_cannot_save():
    put = _endpoints()[("/api/archiver/pdf-vision/policy", "PUT")]
    with pytest.raises(HTTPException) as caught:
        put(_body(), _request(), _user(role="viewer"))
    assert caught.value.status_code == 403


def test_missing_csrf_is_refused_before_saving(monkeypatch):
    monkeypatch.setattr(admin, "save_policy_change", lambda *_a, **_k: pytest.fail("saved"))
    put = _endpoints()[("/api/archiver/pdf-vision/policy", "PUT")]
    with pytest.raises(HTTPException):
        put(_body(), _request(csrf=""), _user())


def test_partial_scope_admin_cannot_edit_the_global_row():
    put = _endpoints()[("/api/archiver/pdf-vision/policy", "PUT")]
    with pytest.raises(HTTPException) as caught:
        put(_body(workspace=""), _request(), _user())
    assert caught.value.status_code == 403


def test_other_workspace_is_forbidden():
    get = _endpoints()[("/api/archiver/pdf-vision", "GET")]
    with pytest.raises(HTTPException) as caught:
        get("mgmt", 50, _user())
    assert caught.value.status_code == 403


def test_blocked_activation_maps_to_422_with_blockers(monkeypatch):
    def refuse(*_a, **_k):
        raise admin.PdfVisionAdminError("activation_blocked",
                                        blockers=("activation_requires_input_usd_per_mtok",))
    monkeypatch.setattr(admin, "save_policy_change", refuse)
    put = _endpoints()[("/api/archiver/pdf-vision/policy", "PUT")]
    with pytest.raises(HTTPException) as caught:
        put(_body(), _request(), _user())
    assert caught.value.status_code == 422
    assert caught.value.detail == {"code": "activation_blocked",
                                   "blockers": ["activation_requires_input_usd_per_mtok"]}


def test_conflict_is_409_and_save_audits_with_the_logged_in_actor(monkeypatch):
    seen = []
    outcome = admin.PolicyChangeOutcome("tyit", "r2", "w1", True, ("enabled",),
                                        audit={"actor": "ops@taeyoung.com", "action": "x"})
    monkeypatch.setattr(admin, "save_policy_change", lambda req: (seen.append(req), outcome)[1])
    put = _endpoints(audit=lambda **kw: seen.append(kw))[("/api/archiver/pdf-vision/policy", "PUT")]
    got = put(_body(), _request(), _user())
    assert got["revision"] == "r2" and got["audited"] is True
    assert seen[0].actor == "ops@taeyoung.com"

    def conflict(*_a, **_k):
        raise admin.PdfVisionAdminError("policy_conflict")
    monkeypatch.setattr(admin, "save_policy_change", conflict)
    with pytest.raises(HTTPException) as caught:
        put(_body(), _request(), _user())
    assert caught.value.status_code == 409

