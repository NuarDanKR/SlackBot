"""B-81 정책 변경 라우트. **서버가 막는가**를 건다 — 화면을 믿지 않는다."""
from __future__ import annotations

import inspect
from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tybot.console import app as console_app
from tybot.console import image_description_policy_admin as admin
from tybot.console.auth import ConsoleUser

routes = console_app.image_description_policy_routes

PREVIEW = "/api/archiver/image-description/policy/preview"
SAVE = "/api/archiver/image-description/policy"
NOW = datetime(2026, 11, 2, 9, tzinfo=UTC)


def _router(*, audit_event=lambda **_: None):
    router = routes.build_router(
        current_user=console_app.current_user,
        require_admin=console_app._require_admin,
        check_write_request=console_app._check_write_request,
        audit_event=audit_event,
        now=lambda: NOW,
    )
    return {route.path: route.endpoint for route in router.routes}


def _user(*, role="admin", workspaces=("tyit",), all_workspaces=False) -> ConsoleUser:
    return ConsoleUser(
        email="owner@taeyoung.com",
        name="운영자",
        role=role,
        workspaces=frozenset(workspaces),
        all_workspaces=all_workspaces,
    )


def _request(*, method="PUT", csrf="1", origin="https://console.example") -> Request:
    headers = [(b"host", b"console.example"), (b"origin", origin.encode())]
    if csrf:
        headers.append((b"x-tybot-csrf", csrf.encode()))
    return Request({
        "type": "http", "method": method, "scheme": "https", "path": "/",
        "headers": headers, "server": ("console.example", 443),
        "client": ("127.0.0.1", 1),
    })


def body(**over) -> routes.PolicyBody:
    base = {
        "workspace": "tyit",
        "expectedRevision": "rev-global:rev-tyit",
        "enabled": True,
        "config": {"timeout_seconds": 20},
        "reason": "현장 사진 검색을 켠다",
    }
    return routes.PolicyBody(**(base | over))


OUTCOME = admin.PolicyChangeOutcome(
    workspace="tyit",
    revision="rev-new",
    previous_revision="rev-tyit",
    enabled=True,
    changed_keys=("enabled", "timeout_seconds"),
    audit={
        "actor": "owner@taeyoung.com",
        "category": "archiver",
        "action": "image_description_policy_save",
        "target_type": "image_description_policy",
        "target_id": "tyit",
        "workspace": "tyit",
        "outcome": "succeeded",
        "metadata": {"reason": "현장 사진 검색을 켠다"},
    },
)

PREVIEW_OK = admin.PolicyChangePreview(
    workspace="tyit", enabled=True, row_revision="rev-tyit",
    changed_keys=("timeout_seconds",), blockers=(),
)


@pytest.fixture
def seen(monkeypatch):
    """경계로 들어간 것. 라우트가 본문을 그대로 넘기면 안 되는 칸이 있다."""
    calls: dict[str, list] = {"save": [], "preview": []}

    def save(request, *, now):
        calls["save"].append((request, now))
        return OUTCOME

    def preview(request, *, now):
        calls["preview"].append((request, now))
        return PREVIEW_OK

    monkeypatch.setattr(admin, "save_policy_change", save)
    monkeypatch.setattr(admin, "preview_policy_change", preview)
    return calls


def save_call(seen, *, user=None, request=None, payload=None):
    return _router()[SAVE](payload or body(), request or _request(), user or _user())


def preview_call(seen, *, user=None, request=None, payload=None):
    return _router()[PREVIEW](
        payload or body(), request or _request(method="POST"), user or _user()
    )


def refused(call, **kwargs) -> HTTPException:
    with pytest.raises(HTTPException) as error:
        call(**kwargs)
    return error.value


# --- 라우트가 붙어 있는가 ----------------------------------------------------


def test_both_routes_are_registered_on_the_app():
    found = {
        (route.path, tuple(sorted(route.methods)))
        for route in console_app.image_description_policy_router.routes
    }
    assert (SAVE, ("PUT",)) in found
    assert (PREVIEW, ("POST",)) in found
    deferred = any(
        getattr(included, "original_router", None)
        is console_app.image_description_policy_router
        for included in console_app.app.routes
    )
    flattened = all(
        any(
            getattr(included, "path", "") == route.path
            and set(getattr(included, "methods", ()) or ())
            == set(route.methods or ())
            and getattr(included, "endpoint", None) is route.endpoint
            for included in console_app.app.routes
        )
        for route in console_app.image_description_policy_router.routes
    )
    assert deferred or flattened


def test_the_read_contract_still_stands():
    """기존 GET 을 깨지 않는다."""
    methods = {
        tuple(sorted(route.methods))
        for route in console_app.app.routes
        if getattr(route, "path", "") == SAVE
    }
    methods.update(
        tuple(sorted(route.methods))
        for route in console_app.image_description_policy_router.routes
        if route.path == SAVE
    )
    assert ("GET",) in methods and ("PUT",) in methods


def test_the_write_routes_come_before_the_static_mount():
    """Starlette 는 등록 순서로 찾는다. 뒤에 있으면 정적 404 가 가로챈다.

    정적 마운트는 `dist` 가 있을 때만 붙으므로(개발 PC 에는 없다) 없을 때는
    「앞에 있다」 가 자동으로 참이다 — 그래서 **있을 때만** 잰다.
    """
    app_routes = list(console_app.app.routes)
    policy_route = next(
        route
        for route in console_app.image_description_policy_router.routes
        if route.path == SAVE and "PUT" in (route.methods or ())
    )
    included = next(
        route
        for route in app_routes
        if getattr(route, "original_router", None)
        is console_app.image_description_policy_router
        or (
            getattr(route, "path", "") == policy_route.path
            and "PUT" in (getattr(route, "methods", ()) or ())
            and getattr(route, "endpoint", None) is policy_route.endpoint
        )
    )
    static = next(
        (route for route in app_routes if getattr(route, "path", "") == "/"),
        None,
    )
    if static is not None:
        assert app_routes.index(included) < app_routes.index(static)


def test_the_route_module_never_imports_the_app():
    """모듈 수준에서 서로를 부르면 순환 import 다 — 실제로 터진 적이 있다."""
    source = inspect.getsource(routes)
    assert "from .app import" not in source
    assert "import app" not in source


# --- 권한 · 워크스페이스 · CSRF ----------------------------------------------


@pytest.mark.parametrize("call", [save_call, preview_call])
@pytest.mark.parametrize("role", ["guest", "developer", "viewer"])
def test_only_an_admin_may_change_the_policy(seen, call, role):
    said = refused(call, seen=seen, user=_user(role=role))
    assert said.status_code == 403
    assert seen["save"] == [] and seen["preview"] == []


@pytest.mark.parametrize("call", [save_call, preview_call])
def test_a_scoped_admin_cannot_touch_another_workspace(seen, call):
    said = refused(call, seen=seen, user=_user(workspaces=("mgmt",)))
    assert said.status_code == 403
    assert said.detail["code"] == "workspace_forbidden"
    assert seen["save"] == [] and seen["preview"] == []


@pytest.mark.parametrize("call", [save_call, preview_call])
def test_the_global_row_needs_every_workspace(seen, call):
    """전역 기본값은 모든 워크스페이스에 걸린다. 일부만 보는 사람이 고치면
    자기가 못 보는 곳의 설정을 바꾸는 것이 된다(원칙 3)."""
    said = refused(call, seen=seen, payload=body(workspace=""))
    assert said.status_code == 403
    assert said.detail["code"] == "workspace_forbidden"


@pytest.mark.parametrize("call", [save_call, preview_call])
def test_an_all_workspace_admin_may_change_the_global_row(seen, call):
    call(seen=seen, payload=body(workspace=""), user=_user(all_workspaces=True))
    entered = (seen["save"] or seen["preview"])[0][0]
    assert entered.workspace == admin.GLOBAL


@pytest.mark.parametrize("call,method", [(save_call, "PUT"), (preview_call, "POST")])
def test_a_write_without_the_csrf_header_is_refused(seen, call, method):
    said = refused(call, seen=seen, request=_request(method=method, csrf=""))
    assert said.status_code == 403
    assert seen["save"] == [] and seen["preview"] == []


@pytest.mark.parametrize("call,method", [(save_call, "PUT"), (preview_call, "POST")])
def test_a_cross_origin_write_is_refused(seen, call, method):
    said = refused(
        call, seen=seen, request=_request(method=method, origin="https://evil.example")
    )
    assert said.status_code == 403
    assert seen["save"] == [] and seen["preview"] == []


def test_the_workspace_is_normalised_by_the_server():
    """본문이 말하는 대소문자·여백을 믿지 않는다."""
    assert routes._workspace(_user(), "  TYIT  ") == "tyit"


# --- 실행자는 로그인한 사람이다 ---------------------------------------------


def test_the_actor_comes_from_the_session_not_the_body(seen):
    save_call(seen=seen, user=_user())
    assert seen["save"][0][0].actor == "owner@taeyoung.com"


def test_an_actor_field_in_the_body_is_refused_not_ignored():
    """조용히 버리면 다른 사람 이름으로 감사가 남는 줄 안다."""
    with pytest.raises(Exception) as error:
        routes.PolicyBody(
            workspace="tyit", enabled=True, config={}, reason="사유",
            actor="someone@else.com",
        )
    assert "actor" in str(error.value)


@pytest.mark.parametrize("field", ["audited", "revision", "actorEmail"])
def test_any_unknown_body_field_is_refused(field):
    with pytest.raises(Exception):
        routes.PolicyBody(
            workspace="tyit", enabled=True, config={}, reason="사유", **{field: "x"}
        )


# --- 미리보기는 쓰지 않는다 --------------------------------------------------


def test_the_preview_never_calls_the_save_path(seen):
    said = preview_call(seen=seen)
    assert seen["save"] == []
    assert len(seen["preview"]) == 1
    assert said["savable"] is True
    assert said["rowRevision"] == "rev-tyit"


def test_the_preview_reports_blockers_for_the_screen(seen, monkeypatch):
    blocked = admin.PolicyChangePreview(
        workspace="tyit", enabled=True, row_revision="rev-tyit",
        changed_keys=(), blockers=("activation_requires_daily_calls",),
    )
    monkeypatch.setattr(admin, "preview_policy_change", lambda *_a, **_k: blocked)

    said = preview_call(seen=seen)

    assert said["savable"] is False
    assert said["blockers"] == ["activation_requires_daily_calls"]


def test_the_preview_never_writes_an_audit_event(seen):
    recorded = []
    router = routes.build_router(
        current_user=console_app.current_user,
        require_admin=console_app._require_admin,
        check_write_request=console_app._check_write_request,
        audit_event=lambda **kwargs: recorded.append(kwargs),
        now=lambda: NOW,
    )
    endpoint = {route.path: route.endpoint for route in router.routes}[PREVIEW]
    endpoint(body(), _request(method="POST"), _user())
    assert recorded == []


# --- 오류 코드 ---------------------------------------------------------------


@pytest.mark.parametrize("code,status", [
    ("policy_conflict", 409),
    ("policy_invalid", 422),
    ("reason_required", 422),
    ("reason_too_long", 422),
    ("config_key_unknown", 422),
    ("config_invalid_max_image_bytes", 422),
    ("activation_blocked", 422),
    ("policy_unavailable", 503),
    ("connection_not_idle_autocommit", 503),
    ("vision_database_unconfigured", 503),
    ("vision_database_credential_mode", 503),
    ("vision_database_wrong_role", 503),
    ("vision_database_shared_credential", 503),
])
@pytest.mark.parametrize("call", [save_call, preview_call])
def test_each_refusal_gets_its_own_status(seen, monkeypatch, call, code, status):
    def refuse(*_a, **_k):
        raise admin.ImageDescriptionPolicyAdminError(code)

    monkeypatch.setattr(admin, "save_policy_change", refuse)
    monkeypatch.setattr(admin, "preview_policy_change", refuse)

    said = refused(call, seen=seen)

    assert said.status_code == status
    assert said.detail["code"] == code


def test_a_conflict_is_409_so_the_screen_can_reload(seen, monkeypatch):
    """422 로 내보내면 화면은 입력을 고치라고 말한다. 고칠 것은 입력이 아니다."""
    monkeypatch.setattr(
        admin, "save_policy_change",
        lambda *_a, **_k: (_ for _ in ()).throw(
            admin.ImageDescriptionPolicyAdminError("policy_conflict")
        ),
    )
    assert refused(save_call, seen=seen).status_code == 409


def test_blockers_reach_the_screen(seen, monkeypatch):
    blockers = ("activation_requires_global_daily_calls", "model_quota_exhausted")

    def refuse(*_a, **_k):
        raise admin.ImageDescriptionPolicyAdminError(
            "activation_blocked", blockers=blockers
        )

    monkeypatch.setattr(admin, "save_policy_change", refuse)
    said = refused(save_call, seen=seen)

    assert said.status_code == 422
    assert said.detail["blockers"] == list(blockers)


def test_the_detail_shape_is_the_same_whether_or_not_there_are_blockers(seen, monkeypatch):
    monkeypatch.setattr(
        admin, "save_policy_change",
        lambda *_a, **_k: (_ for _ in ()).throw(
            admin.ImageDescriptionPolicyAdminError("policy_invalid")
        ),
    )
    said = refused(save_call, seen=seen)
    assert said.detail == {"code": "policy_invalid", "blockers": []}


def test_a_database_body_never_reaches_the_response(seen, monkeypatch):
    """예외 본문에는 행 값과 DSN 이 들어 있다."""
    leak = "postgresql://tybot_vision_runtime:PW@h/db relation config={'model':'x'}"

    def refuse(*_a, **_k):
        raise admin.ImageDescriptionPolicyAdminError("policy_unavailable")

    monkeypatch.setattr(admin, "save_policy_change", refuse)
    said = refused(save_call, seen=seen)

    text = str(said.detail) + str(said.status_code)
    assert leak not in text
    assert "postgresql://" not in text
    assert said.detail["code"].isascii()


def test_an_oversized_config_is_refused_before_the_boundary(seen):
    payload = body(config={f"k{index}": 1 for index in range(routes.MAX_CONFIG_KEYS + 1)})
    said = refused(save_call, seen=seen, payload=payload)
    assert said.status_code == 422
    assert seen["save"] == []


# --- 감사 --------------------------------------------------------------------


def test_the_audit_is_recorded_after_a_successful_save(seen):
    recorded = []
    router = _router(audit_event=lambda **kwargs: recorded.append(kwargs))
    said = router[SAVE](body(), _request(), _user())

    assert recorded == [OUTCOME.audit]
    assert said["audited"] is True
    assert said["revision"] == "rev-new"
    assert said["previousRevision"] == "rev-tyit"


def test_the_audit_record_is_passed_through_untouched(seen):
    recorded = []
    _router(audit_event=lambda **kwargs: recorded.append(kwargs))[SAVE](
        body(), _request(), _user()
    )
    assert recorded[0]["actor"] == "owner@taeyoung.com"
    assert recorded[0]["metadata"]["reason"] == "현장 사진 검색을 켠다"


def test_a_failed_save_records_no_audit(seen, monkeypatch):
    recorded = []
    monkeypatch.setattr(
        admin, "save_policy_change",
        lambda *_a, **_k: (_ for _ in ()).throw(
            admin.ImageDescriptionPolicyAdminError("policy_conflict")
        ),
    )
    router = _router(audit_event=lambda **kwargs: recorded.append(kwargs))
    with pytest.raises(HTTPException):
        router[SAVE](body(), _request(), _user())
    assert recorded == []


def test_a_broken_audit_does_not_undo_a_finished_save(seen):
    """저장은 끝났다. 500 을 내면 운영자가 다시 누르고 revision 이 둘 남는다."""
    def explode(**_kwargs):
        raise RuntimeError("console_audit_event 가 없다")

    said = _router(audit_event=explode)[SAVE](body(), _request(), _user())

    assert said["revision"] == "rev-new"
    assert said["audited"] is False


def test_a_broken_audit_is_visible_not_silent(seen):
    """조용히 성공으로 보이면 감사 공백을 아무도 모른다."""
    ok = _router(audit_event=lambda **_: None)[SAVE](body(), _request(), _user())
    assert ok["audited"] is True


# --- 저장만으로 아무것도 돌지 않는다 -----------------------------------------


def test_the_route_module_never_reaches_a_model_or_a_worker():
    source = inspect.getsource(routes).replace(routes.__doc__ or "", "")
    code = [line for line in source.splitlines() if not line.strip().startswith("#")]
    forbidden = ("router_", "describe_image", "coordinator", "image_description_job",
                 "image_description_runtime", "dispatch", "reserve", "load_original",
                 "subprocess", "systemd", "requests", "httpx", "backfill")
    hits = [line for line in code for word in forbidden if word in line]
    assert hits == [], hits


def test_saving_calls_the_boundary_once_and_nothing_else(seen):
    save_call(seen=seen)
    assert len(seen["save"]) == 1
    assert seen["preview"] == []


def test_the_clock_is_injected_so_the_boundary_judges_at_request_time(seen):
    save_call(seen=seen)
    assert seen["save"][0][1] == NOW
