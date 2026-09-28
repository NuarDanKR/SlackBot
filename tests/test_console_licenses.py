"""라이선스 현황 — 할당은 사람이, 활성은 Slack 이 말한다.

지켜야 하는 것.

1. **admin 만** 본다. 조직별 인원이 드러난다
2. 활성은 **사람 계정만** 센다 — 봇·Slackbot·비활성·단일 채널 게스트는 빠진다
3. 표에는 **Slack 연동 워크스페이스만** 나온다. 할당은 사람이 적는다
4. Slack 이 실패한 워크스페이스는 **0 이 아니라 「모름」** 이다. 0 으로 보이면
   할당을 줄여도 된다고 읽힌다
5. 토큰은 응답에 실리지 않는다
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="fastapi TestClient 가 httpx 를 쓴다")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient
from test_console_api import env, guest, member, owner  # noqa: F401

from tybot.console import app as console_app
from tybot.console import license_store
from tybot.console.license_store import ActiveCount

CSRF = {"X-TYBot-CSRF": "1", "Origin": "http://testserver"}
TOKEN = "xoxb-test"


@pytest.fixture
def client(request):
    request.getfixturevalue("env")
    return TestClient(console_app.app)


class FakeSlack:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def users_list(self, limit=200, cursor=None):
        page = self.pages[self.calls]
        self.calls += 1
        return page


def _member(uid, **flags):
    return {"id": uid, **flags}


# --- 활성 수 세기 ------------------------------------------------------------

def test_only_billable_humans_are_counted_across_pages():
    slack = FakeSlack([
        {"members": [
            _member("U1"),
            _member("U2", is_restricted=True),                            # 다중 채널 게스트
            _member("U3", is_restricted=True, is_ultra_restricted=True),  # 단일 채널 게스트
            _member("B1", is_bot=True),
            _member("USLACKBOT"),
        ], "response_metadata": {"next_cursor": "next"}},
        {"members": [
            _member("U4"),
            _member("U5", deleted=True),
            _member("A1", is_app_user=True),
        ], "response_metadata": {"next_cursor": ""}},
    ])

    assert license_store.count_active_members(slack) == (3, 1)
    assert slack.calls == 2


def test_a_failed_workspace_is_unknown_not_zero():
    license_store.reset_cache()

    class Broken:
        def __init__(self, **_kwargs):
            pass

        def users_list(self, **_kwargs):
            raise RuntimeError("boom")

    counts = license_store.active_counts({"tyit": TOKEN}, client_factory=Broken)
    report = license_store.build_report({"tyit": ("전산팀", TOKEN)}, {}, counts)

    row = report["rows"][0]
    assert row["active"] is None
    assert "Slack 조회 실패" in row["error"]
    assert report["syncedCount"] == 0


def test_results_are_reused_until_refresh():
    license_store.reset_cache()
    made = []

    def factory(**_kwargs):
        client = FakeSlack([{"members": [_member("U1")]}])
        made.append(client)
        return client

    license_store.active_counts({"tyit": TOKEN}, client_factory=factory)
    license_store.active_counts({"tyit": TOKEN}, client_factory=factory)
    assert len(made) == 1
    license_store.active_counts({"tyit": TOKEN}, refresh=True, client_factory=factory)
    assert len(made) == 2


# --- 표 합치기 ---------------------------------------------------------------

def test_only_linked_workspaces_are_listed_with_stored_allocation():
    linked = {"tyit": ("전산팀", TOKEN), "mgmt": ("경영본부", TOKEN)}
    stored = {"tyit": 8, "gone": 30}   # gone: 할당은 남았지만 연동이 끊겼다
    counts = {
        "tyit": ActiveCount(active=8, guests=0, fetched_at="2026-09-28T10:00:00+09:00"),
        "mgmt": ActiveCount(active=40, guests=2, fetched_at="2026-09-28T10:00:00+09:00"),
    }

    report = license_store.build_report(linked, stored, counts)
    rows = {row["workspace"]: row for row in report["rows"]}

    assert set(rows) == {"tyit", "mgmt"}
    assert rows["tyit"]["allocated"] == 8 and rows["tyit"]["active"] == 8
    assert rows["mgmt"]["allocated"] == 0          # 아직 입력 전 — 0 으로 시작
    assert rows["mgmt"]["guests"] == 2


def test_validation_accepts_only_linked_workspaces_and_counts():
    assert license_store.validate([{"workspace": "TYIT", "allocated": 8}], linked={"tyit"}) == [("tyit", 8)]
    for bad in (
        [{"workspace": "ghost", "allocated": 1}],
        [{"workspace": "tyit", "allocated": -1}],
        [{"workspace": "tyit", "allocated": True}],
        [{"workspace": "tyit", "allocated": 1}, {"workspace": "tyit", "allocated": 2}],
    ):
        with pytest.raises(license_store.LicenseStoreError):
            license_store.validate(bad, linked={"tyit"})


# --- 라우트 -----------------------------------------------------------------

@pytest.fixture
def stub(monkeypatch):
    saved = {}
    monkeypatch.setattr(license_store, "linked_workspaces", lambda: {"tyit": ("전산팀", TOKEN)})
    monkeypatch.setattr(license_store, "list_stored", lambda: {})
    monkeypatch.setattr(
        license_store, "active_counts",
        lambda tokens, refresh=False: {
            key: ActiveCount(active=5, guests=0, fetched_at="2026-09-28T10:00:00+09:00")
            for key in tokens
        },
    )

    def fake_save(**values):
        license_store.validate(values["rows"], values["linked"])
        saved.update(values)
        return {"saved": len(values["rows"])}

    monkeypatch.setattr(license_store, "save", fake_save)
    return saved


def test_only_admins_see_licenses(client, stub):
    assert client.get("/api/licenses", headers=guest(client)).status_code == 403
    assert client.get("/api/licenses", headers=member(client)).status_code == 403
    response = client.get("/api/licenses", headers=owner(client))
    assert response.status_code == 200
    assert response.json()["rows"][0]["active"] == 5
    assert TOKEN not in response.text


def test_saving_requires_csrf_and_rejects_bad_rows(client, stub):
    body = {"rows": [{"workspace": "tyit", "allocated": 8}]}
    headers = owner(client)

    assert client.put("/api/licenses", json=body, headers=headers).status_code == 403
    ok = client.put("/api/licenses", json=body, headers=headers | CSRF)
    assert ok.status_code == 200, ok.text
    assert stub["actor"] == "dan@taeyoung.com"

    bad = {"rows": [{"workspace": "ghost", "allocated": 1}]}
    assert client.put("/api/licenses", json=bad, headers=headers | CSRF).status_code == 422
    negative = {"rows": [{"workspace": "tyit", "allocated": -1}]}
    assert client.put("/api/licenses", json=negative, headers=headers | CSRF).status_code == 422
