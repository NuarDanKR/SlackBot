from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tybot.console import app as console_app
from tybot.console.auth import ConsoleUser

routes = console_app.image_description_clearance_routes


def _endpoint(path: str):
    return next(
        route.endpoint
        for route in console_app.image_description_clearance_router.routes
        if route.path == path
    )


def _router_endpoints(*, audit_event):
    router = routes.build_router(
        current_user=console_app.current_user,
        require_admin=console_app._require_admin,
        check_write_request=console_app._check_write_request,
        audit_event=audit_event,
    )
    return {route.path: route.endpoint for route in router.routes}


post_clearance = _endpoint("/api/archiver/image-description/clearances")
post_revoke_clearance = _endpoint(
    "/api/archiver/image-description/clearances/{clearance_id}/revoke"
)

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


def _user(*, role="admin", workspaces=("tyit",)) -> ConsoleUser:
    return ConsoleUser(
        email="owner@example.com",
        name="owner",
        role=role,
        workspaces=frozenset(workspaces),
    )


def _request(*, csrf="1", origin="https://console.example") -> Request:
    headers = [(b"host", b"console.example"), (b"origin", origin.encode())]
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


def _body(**changes) -> routes.ClearanceBody:
    values = {
        "workspace": "tyit",
        "channelId": "C12345678",
        "fileId": "F12345678",
        "expectedOriginalSha256": "a" * 64,
        "expiresAt": NOW + timedelta(hours=1),
        "piiCleared": True,
        "reason": "reviewed",
    }
    values.update(changes)
    return routes.ClearanceBody(**values)


def test_create_route_requires_admin_before_calling_service(monkeypatch) -> None:
    monkeypatch.setattr(
        routes.admin,
        "create_clearance",
        lambda *_a, **_k: pytest.fail("service must not be called"),
    )
    with pytest.raises(HTTPException) as error:
        post_clearance(_body(), _request(), _user(role="guest"))
    assert error.value.status_code == 403


def test_create_route_requires_csrf_and_workspace_scope(monkeypatch) -> None:
    monkeypatch.setattr(
        routes.admin,
        "create_clearance",
        lambda *_a, **_k: pytest.fail("service must not be called"),
    )
    with pytest.raises(HTTPException) as csrf:
        post_clearance(_body(), _request(csrf=""), _user())
    assert csrf.value.status_code == 403

    with pytest.raises(HTTPException) as scope:
        post_clearance(_body(), _request(), _user(workspaces=("mgmt",)))
    assert scope.value.status_code == 403


def test_create_route_audits_coordinates_but_not_review_reason(monkeypatch) -> None:
    seen = []
    audits = []

    def create(request):
        seen.append(request)
        return {
            "id": "clearance-1",
            "channelId": request.channel_id,
            "fileId": request.file_id,
            "policyRevision": "policy-1",
        }

    monkeypatch.setattr(routes.admin, "create_clearance", create)
    endpoint = _router_endpoints(audit_event=lambda **values: audits.append(values))[
        "/api/archiver/image-description/clearances"
    ]

    result = endpoint(_body(reason="sensitive review note"), _request(), _user())

    assert result["id"] == "clearance-1"
    assert seen[0].workspace == "tyit"
    assert seen[0].reason == "sensitive review note"
    assert "sensitive review note" not in repr(audits)
    assert audits[0]["metadata"] == {
        "channel_id": "C12345678",
        "file_id": "F12345678",
        "policy_revision": "policy-1",
    }


def test_database_failure_is_a_fixed_503(monkeypatch) -> None:
    def unavailable(*_args, **_kwargs):
        raise routes.admin.ImageDescriptionClearanceAdminError(
            "vision_database_unavailable"
        )

    monkeypatch.setattr(routes.admin, "create_clearance", unavailable)
    with pytest.raises(HTTPException) as error:
        post_clearance(_body(), _request(), _user())
    assert error.value.status_code == 503
    assert error.value.detail == "vision_database_unavailable"


def test_revoke_route_passes_the_scoped_workspace_and_omits_reason_from_audit(
    monkeypatch,
) -> None:
    calls = []
    audits = []

    def revoke(clearance_id, **values):
        calls.append((clearance_id, values))
        return {"ok": True, "id": clearance_id}

    monkeypatch.setattr(routes.admin, "revoke_clearance", revoke)
    endpoint = _router_endpoints(audit_event=lambda **values: audits.append(values))[
        "/api/archiver/image-description/clearances/{clearance_id}/revoke"
    ]
    body = routes.RevokeBody(workspace="tyit", reason="sensitive revoke note")

    result = endpoint(
        "clearance-1", body, _request(), _user()
    )

    assert result == {"ok": True, "id": "clearance-1"}
    assert calls == [
        (
            "clearance-1",
            {
                "workspace": "tyit",
                "actor": "owner@example.com",
                "reason": "sensitive revoke note",
            },
        )
    ]
    assert "sensitive revoke note" not in repr(audits)
