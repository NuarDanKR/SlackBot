"""Archiving 콘솔 라우트 — **화면이 보는 것과 막는 것.**

결정: 2026-09-25 오너 §1·§2·§10.

라우트가 지켜야 하는 것 넷.

1. **admin 만** 본다. 이 화면은 수집 주인을 바꾼다
2. **사유 없이는 요청 자체가 안 만들어진다**(422). 서버까지 온 뒤 거절하면
   화면이 「저장이 안 되네」 로 읽는다
3. 규칙이 막은 것은 **422** 다. 500 으로 주면 화면이 「서버 고장」 이라고 말하고,
   사람은 고칠 수 있는 것을 고칠 수 없는 것으로 읽는다
4. 화면이 **버튼을 회색으로 만들 근거**를 응답에 담는다
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="fastapi TestClient 가 httpx 를 쓴다")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_archiving_repo import FakeArchivingRepo
from fastapi.testclient import TestClient

# `env` 는 픽스처, `owner`·`guest` 는 그냥 함수다. `client` 는 여기서 다시 만든다 —
# 픽스처를 import 하면 같은 이름의 인자와 부딪혀 린터가 재정의로 읽는다.
from test_console_api import env, guest, owner  # noqa: F401

from tybot.console import app as console_app
from tybot.console import archiving_admin


@pytest.fixture
def client(request):
    """`env` 를 이름으로 받지 않고 **불러서** 쓴다.

    인자로 받으면 import 한 픽스처와 이름이 부딪혀 린터가 재정의로 읽는다.
    `getfixturevalue` 는 같은 순서(환경을 세운 뒤 앱을 만든다)를 지키면서 그
    충돌이 없다.
    """
    request.getfixturevalue("env")
    return TestClient(console_app.app)

BASE = "/api/workspaces/tyit/archiving"
CSRF = {"X-TYBot-CSRF": "1", "Origin": "http://testserver"}
#: CSRF 헤더만 있고 Origin 이 없으면 콘솔은 거절한다. 그 규약을 이 화면만
#: 예외로 두지 않는다.
CSRF_ONLY = {"X-TYBot-CSRF": "1"}


@pytest.fixture
def repo(monkeypatch) -> FakeArchivingRepo:
    """진짜 저장소 대신 가짜를 물린다. **DB 없이 라우트를 전부 본다.**"""
    fake = FakeArchivingRepo()
    fake.given_channel("tyit", "C0FUND", "shadow")
    fake.given_flag("archiver_writes_live", False)
    fake.given_flag("require_attachment_ack", False)
    fake.given_retention("bot_conversation_audit")
    fake.given_retention("bot_dm_attachment")
    monkeypatch.setattr(archiving_admin, "default_repo", lambda: fake)
    return fake


# --- 권한 -------------------------------------------------------------------

def test_a_guest_cannot_see_the_archiving_screen(client, guest_headers=None):
    headers = guest(client)

    assert client.get(BASE, headers=headers).status_code == 403


def test_a_guest_cannot_change_a_channel_mode(client):
    headers = guest(client) | CSRF

    response = client.put(
        f"{BASE}/channels/C0FUND",
        json={"mode": "shadow", "reason": "해 보기"},
        headers=headers,
    )

    assert response.status_code == 403


# --- 읽기 -------------------------------------------------------------------

def test_the_detail_carries_everything_the_screen_needs(client, repo):
    headers = owner(client)

    body = client.get(BASE, headers=headers).json()

    for key in ("services", "channels", "flags", "retention", "audit",
                "schemaGate", "blockers", "gatedModes", "gatedFlags"):
        assert key in body, key


def test_the_screen_is_told_what_is_locked_and_why(client, repo):
    """눌러 보고 거절당하는 것과 왜 못 누르는지 보이는 것은 다르다."""
    headers = owner(client)

    body = client.get(BASE, headers=headers).json()

    assert body["schemaGate"]["verified"] is False
    assert "검증" in body["schemaGate"]["reason"]
    assert "active" in body["gatedModes"]
    assert "archiver_writes_live" in body["gatedFlags"]


def test_unset_retention_shows_up_as_a_blocker(client, repo):
    """값이 없으면 기본이 「영구 보관」 이 된다. 화면이 그걸 말해야 한다."""
    headers = owner(client)

    body = client.get(BASE, headers=headers).json()

    retention = [item for item in body["blockers"] if "보존 기간" in item]
    assert len(retention) == 2


def test_service_tokens_are_saved_but_never_returned(client, repo, monkeypatch):
    captured = {}

    def save_service(workspace, service, **kwargs):
        captured.update(workspace=workspace, service=service, **kwargs)
        repo.service_rows.append({
            "service": service,
            "state": "disabled",
            "error": None,
            "team_id": "",
            "bot_user_id": "",
            "identity_ok": None,
            "identity_error": "",
            "identity_checked_at": None,
            "bot_mask": "xoxb-1234…cdef",
            "app_mask": "xapp-1234…cdef",
            "token_count": 2,
        })

    monkeypatch.setattr(console_app.workspace_service_store, "save_service", save_service)
    headers = owner(client) | CSRF

    response = client.put(
        f"{BASE}/services/archiver",
        json={
            "botToken": "xoxb-secret-bot",
            "appToken": "xapp-secret-app",
            "reason": "별도 앱 등록",
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert captured["workspace"] == "tyit"
    assert captured["service"] == "archiver"
    body = response.text
    assert "xoxb-secret-bot" not in body
    assert "xapp-secret-app" not in body


def test_service_identity_is_checked_server_side(client, repo, monkeypatch):
    calls = []
    monkeypatch.setattr(
        console_app.workspace_service_identity,
        "verify_service",
        lambda workspace, service, *, actor: calls.append((workspace, service, actor)) or "",
    )
    headers = owner(client) | CSRF

    response = client.put(
        f"{BASE}/services/archiver/verify",
        json={"reason": "토큰 교체 후 확인"},
        headers=headers,
    )

    assert response.status_code == 200
    assert calls == [("tyit", "archiver", "dan@taeyoung.com")]


# --- 사유 --------------------------------------------------------------------

@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/channels/C0FUND", {"mode": "shadow"}),
        ("/flags", {"name": "require_attachment_ack", "enabled": True}),
        ("/retention", {"name": "bot_conversation_audit", "days": 90}),
    ],
    ids=["channel", "flag", "retention"],
)
def test_a_request_without_a_reason_is_rejected(client, repo, path, payload):
    """기본값을 두면 전부 그 기본값으로 남고, 그건 기록이 아니다."""
    headers = owner(client) | CSRF

    response = client.put(f"{BASE}{path}", json=payload, headers=headers)

    assert response.status_code == 422


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/channels/C0FUND", {"mode": "shadow", "reason": "   "}),
        ("/flags", {"name": "x", "enabled": False, "reason": ""}),
    ],
    ids=["channel-blank", "flag-empty"],
)
def test_a_blank_reason_is_rejected(client, repo, path, payload):
    headers = owner(client) | CSRF

    assert client.put(f"{BASE}{path}", json=payload, headers=headers).status_code == 422


# --- 게이트 -------------------------------------------------------------------

def test_active_is_refused_with_a_readable_reason(client, repo):
    """**422 다.** 500 으로 주면 화면이 「서버 고장」 이라고 말한다."""
    headers = owner(client) | CSRF

    response = client.put(
        f"{BASE}/channels/C0FUND",
        json={"mode": "active", "reason": "인수", "cutoverTs": "1700000000.0001"},
        headers=headers,
    )

    assert response.status_code == 422
    assert "검증" in response.json()["detail"]
    assert repo.channel_rows[("tyit", "C0FUND")]["mode"] == "shadow"


def test_turning_on_a_gated_flag_is_refused(client, repo):
    headers = owner(client) | CSRF

    response = client.put(
        f"{BASE}/flags",
        json={"name": "archiver_writes_live", "enabled": True, "reason": "지금"},
        headers=headers,
    )

    assert response.status_code == 422
    assert repo.flag_rows[("archiver_writes_live", "global", "")]["enabled"] is False


def test_turning_a_gated_flag_off_is_allowed(client, repo):
    """사고 때 내리는 손잡이를 검증 상태로 막으면 막아야 할 순간에 못 막는다."""
    repo.given_flag("archiver_writes_live", True)
    headers = owner(client) | CSRF

    response = client.put(
        f"{BASE}/flags",
        json={"name": "archiver_writes_live", "enabled": False, "reason": "사고 대응"},
        headers=headers,
    )

    assert response.status_code == 200
    assert repo.flag_rows[("archiver_writes_live", "global", "")]["enabled"] is False


def test_workspace_flag_cannot_target_a_different_workspace(client, repo):
    headers = owner(client) | CSRF

    response = client.put(
        f"{BASE}/flags",
        json={
            "name": "require_attachment_ack",
            "enabled": True,
            "reason": "범위 확인",
            "scope": "workspace",
            "scopeKey": "mgmt",
        },
        headers=headers,
    )

    assert response.status_code == 422
    assert ("require_attachment_ack", "workspace", "mgmt") not in repo.flag_rows


def test_channel_flag_cannot_target_another_workspaces_channel(client, repo):
    repo.given_channel("mgmt", "C0OTHER", "shadow")
    headers = owner(client) | CSRF

    response = client.put(
        f"{BASE}/flags",
        json={
            "name": "require_attachment_ack",
            "enabled": True,
            "reason": "범위 확인",
            "scope": "channel",
            "scopeKey": "C0OTHER",
        },
        headers=headers,
    )

    assert response.status_code == 422


# --- 되는 것 ------------------------------------------------------------------

def test_shadow_is_not_gated(client, repo):
    """그림자는 운영 원문을 안 건드린다. 막으면 개발이 멈춘다."""
    headers = owner(client) | CSRF

    response = client.put(
        f"{BASE}/channels/C0FUND",
        json={"mode": "off", "reason": "파일럿 종료"},
        headers=headers,
    )

    assert response.status_code == 200
    assert repo.channel_rows[("tyit", "C0FUND")]["mode"] == "off"


def test_a_successful_change_returns_the_whole_detail(client, repo):
    """화면이 다시 불러오지 않아도 되게 한다 — 두 번 부르면 그 사이에 값이 갈린다."""
    headers = owner(client) | CSRF

    body = client.put(
        f"{BASE}/channels/C0FUND",
        json={"mode": "paused", "reason": "점검"},
        headers=headers,
    ).json()

    assert body["channels"][0]["mode"] == "paused"
    assert "schemaGate" in body


def test_the_change_is_recorded_with_the_logged_in_user(client, repo):
    """감사의 actor 는 요청 본문이 아니라 **세션**에서 온다.

    본문에서 받으면 누구든 남의 이름으로 바꿀 수 있다.
    """
    headers = owner(client) | CSRF

    client.put(
        f"{BASE}/channels/C0FUND",
        json={"mode": "paused", "reason": "점검"},
        headers=headers,
    )

    assert repo.audit_rows[-1]["actor"] == "dan@taeyoung.com"
    assert repo.audit_rows[-1]["reason"] == "점검"


def test_retention_below_one_day_is_rejected_by_the_schema(client, repo):
    """`0` 을 즉시 삭제로 읽으면 기록이 생기자마자 사라진다."""
    headers = owner(client) | CSRF

    response = client.put(
        f"{BASE}/retention",
        json={"name": "bot_conversation_audit", "days": 0, "reason": "법무"},
        headers=headers,
    )

    assert response.status_code == 422


def test_setting_retention_clears_that_blocker(client, repo):
    headers = owner(client) | CSRF

    body = client.put(
        f"{BASE}/retention",
        json={"name": "bot_conversation_audit", "days": 90, "reason": "법무 승인"},
        headers=headers,
    ).json()

    retention = [item for item in body["blockers"] if "보존 기간" in item]
    assert len(retention) == 1
    assert "bot_dm_attachment" in retention[0]


def test_a_write_without_the_csrf_header_is_rejected(client, repo):
    """콘솔의 쓰기 규약. 이 화면만 예외로 두지 않는다."""
    headers = owner(client)

    response = client.put(
        f"{BASE}/channels/C0FUND",
        json={"mode": "paused", "reason": "점검"},
        headers=headers,
    )

    assert response.status_code in (400, 403)
    assert repo.channel_rows[("tyit", "C0FUND")]["mode"] == "shadow"


def test_the_csrf_header_alone_is_not_enough(client, repo):
    """헤더만 보면 다른 화면에서 보낸 요청도 통과한다. Origin 까지 본다."""
    headers = owner(client) | CSRF_ONLY

    response = client.put(
        f"{BASE}/channels/C0FUND",
        json={"mode": "paused", "reason": "점검"},
        headers=headers,
    )

    assert response.status_code == 403
    assert repo.channel_rows[("tyit", "C0FUND")]["mode"] == "shadow"
