from __future__ import annotations

import pytest

pytest.importorskip("httpx", reason="fastapi TestClient 가 httpx 를 쓴다")

from fastapi.testclient import TestClient
from test_console_api import env, guest, owner  # noqa: F401

from tybot.console import app as console_app
from tybot.console import image_description_admin
from tybot.console.auth import ConsoleUser

PATH = "/api/archiver/image-description/policy?workspace=tyit"


@pytest.fixture
def client(request):
    request.getfixturevalue("env")
    return TestClient(console_app.app)


def test_only_an_admin_can_read_image_description_status(client, monkeypatch):
    monkeypatch.setattr(image_description_admin, "snapshot", lambda *_a, **_k: {})

    assert client.get(PATH, headers=guest(client)).status_code == 403


def test_admin_gets_only_the_backend_snapshot(client, monkeypatch):
    seen = []
    monkeypatch.setattr(
        image_description_admin,
        "snapshot",
        lambda workspace, limit: seen.append((workspace, limit)) or {
            "workspace": workspace,
            "policy": {"effectiveEnabled": False},
        },
    )

    response = client.get(PATH, headers=owner(client))

    assert response.status_code == 200
    assert response.json()["policy"]["effectiveEnabled"] is False
    assert seen == [("tyit", 50)]


def test_scoped_admin_cannot_read_another_workspace(client, monkeypatch):
    scoped = ConsoleUser(
        email="admin@taeyoung.com",
        name="관리자",
        role="admin",
        workspaces=frozenset({"mgmt"}),
    )
    console_app.app.dependency_overrides[console_app.current_user] = lambda: scoped
    monkeypatch.setattr(
        image_description_admin,
        "snapshot",
        lambda *_a, **_k: pytest.fail("repository must not be called"),
    )
    try:
        assert client.get(PATH).status_code == 403
    finally:
        console_app.app.dependency_overrides.pop(console_app.current_user, None)


def test_unconfigured_vision_database_is_503_not_an_empty_success(client, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise image_description_admin.ImageDescriptionAdminError(
            "vision_database_unconfigured"
        )

    monkeypatch.setattr(image_description_admin, "snapshot", unavailable)
    response = client.get(PATH, headers=owner(client))

    assert response.status_code == 503
    assert response.json()["detail"] == "vision_database_unconfigured"
