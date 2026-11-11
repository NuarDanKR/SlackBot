from __future__ import annotations

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from tybot.console import app as console_app
from tybot.console import image_description_secret_admin as admin
from tybot.console import image_description_secret_routes as routes
from tybot.console.auth import ConsoleUser

PATH = "/api/archiver/image-description/provider-secrets"


def _user(*, all_workspaces=True, role="admin") -> ConsoleUser:
    return ConsoleUser(
        email="owner@example.com",
        name="owner",
        role=role,
        workspaces=frozenset({"tyit"}),
        all_workspaces=all_workspaces,
    )


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "PUT",
        "scheme": "https",
        "path": PATH,
        "headers": [
            (b"host", b"console.example"),
            (b"origin", b"https://console.example"),
            (b"x-tybot-csrf", b"1"),
        ],
        "server": ("console.example", 443),
        "client": ("127.0.0.1", 1),
    })


def _endpoints(*, audit=lambda **_kwargs: None):
    router = routes.build_router(
        current_user=console_app.current_user,
        require_admin=console_app._require_admin,
        check_write_request=console_app._check_write_request,
        audit_event=audit,
    )
    return {(route.path, next(iter(route.methods))): route.endpoint for route in router.routes}


ROW = {
    "provider": "google",
    "mask": "AIza…1234",
    "enabled": True,
    "updated_by": "owner@example.com",
    "updated_at": "now",
}


def test_routes_are_registered_and_never_return_a_key(monkeypatch):
    monkeypatch.setattr(admin, "list_secrets", lambda: (ROW,))
    endpoint = _endpoints()[(PATH, "GET")]

    result = endpoint(_user())

    assert result["secrets"][0]["provider"] == "google"
    assert result["secrets"][0]["mask"] == "AIza…1234"
    assert "key" not in result["secrets"][0]
    assert any(route.path == PATH for route in console_app.image_description_secret_router.routes)


def test_only_global_admin_can_manage_the_shared_provider_key(monkeypatch):
    monkeypatch.setattr(admin, "list_secrets", lambda: (ROW,))
    endpoint = _endpoints()[(PATH, "GET")]

    with pytest.raises(HTTPException) as error:
        endpoint(_user(all_workspaces=False))
    assert error.value.status_code == 403


def test_put_uses_the_logged_in_actor_and_audits_without_the_key(monkeypatch):
    saved = {}
    audited = {}

    def save(provider, **kwargs):
        saved.update(provider=provider, **kwargs)
        return (ROW,)

    monkeypatch.setattr(admin, "save_secret", save)
    endpoint = _endpoints(audit=lambda **kwargs: audited.update(kwargs))[(PATH, "PUT")]
    key = "AIza" + "x" * 32

    result = endpoint(
        routes.SecretBody(
            provider="google", key=key, enabled=True, reason="무료 Vision 키 등록"
        ),
        _request(),
        _user(),
    )

    assert saved == {
        "provider": "google",
        "key": key,
        "enabled": True,
        "actor": "owner@example.com",
    }
    assert audited["metadata"]["keyReplaced"] is True
    assert key not in repr(audited)
    assert result["audited"] is True
