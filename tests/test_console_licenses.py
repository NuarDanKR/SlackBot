"""라이선스 현황 — 할당은 사람이, 활성은 Slack 이 말한다.

지켜야 하는 것.

1. **admin 만** 본다. 조직별 인원이 드러난다
2. 활성은 **사람 계정만** 센다 — 봇·Slackbot·비활성·단일 채널 게스트는 빠진다
3. 연동 워크스페이스는 활성을 Slack 에서, 직접 추가한 워크스페이스는 사람이 적은 값을 쓴다.
   할당은 둘 다 사람이 적는다. 「Slack확산TFT」 는 표에서 뺀다
4. Slack 이 실패한 워크스페이스는 **0 이 아니라 「모름」** 이다. 0 으로 보이면
   할당을 줄여도 된다고 읽힌다
5. 토큰은 응답에 실리지 않는다
6. **지울 수 있는 것은 직접 추가한 워크스페이스뿐**이다
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


def test_the_rollout_workspace_is_left_out_whatever_its_spacing():
    linked = {
        "tyit": ("전산팀", TOKEN),
        "tft": ("Slack확산TFT", TOKEN),
        "tft2": (" slack 확산 tft ", TOKEN),
    }

    assert set(license_store.without_excluded(linked)) == {"tyit"}
    report = license_store.build_report(linked, {}, {})
    assert [row["label"] for row in report["rows"]] == ["전산팀"]
    assert report["linkedCount"] == 1


def test_manual_rows_use_the_typed_active_count():
    manual = [{"id": 7, "label": "토목", "allocated": 80, "active": 3, "created_by": "dan@taeyoung.com"}]

    report = license_store.build_report({"tyit": ("전산팀", TOKEN)}, {}, {}, manual)
    row = next(item for item in report["rows"] if item["kind"] == "manual")

    assert row == {
        "kind": "manual", "id": 7, "workspace": None, "label": "토목",
        "allocated": 80, "active": 3, "guests": 0, "fetchedAt": None, "error": None,
    }
    assert report["linkedCount"] == 1           # 직접 추가한 곳은 연동 수에 들지 않는다


def test_manual_names_must_be_unique_and_not_clash():
    ok = license_store.validate_manual(
        [{"label": " 토목 ", "allocated": 80, "active": 3}], linked_labels={"전산팀"}
    )
    assert ok == [{"id": None, "label": "토목", "allocated": 80, "active": 3}]
    for bad in (
        [{"label": "", "allocated": 1, "active": 0}],
        [{"label": "토목", "allocated": 1, "active": -1}],
        [{"label": "전 산 팀", "allocated": 1, "active": 0}],        # 연동 이름과 같다
        [{"label": "slack확산tft", "allocated": 1, "active": 0}],    # 제외한 이름
        [{"label": "토목", "allocated": 1, "active": 0}, {"label": "토 목", "allocated": 2, "active": 0}],
    ):
        with pytest.raises(license_store.LicenseStoreError):
            license_store.validate_manual(bad, linked_labels={"전산팀"})


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
    monkeypatch.setattr(license_store, "list_manual", lambda: [])
    monkeypatch.setattr(
        license_store, "active_counts",
        lambda tokens, refresh=False: {
            key: ActiveCount(active=5, guests=0, fetched_at="2026-09-28T10:00:00+09:00")
            for key in tokens
        },
    )

    def fake_save(**values):
        license_store.validate(values["rows"], set(values["linked"]))
        license_store.validate_manual(values["manual"], {label for label, _ in values["linked"].values()})
        saved.update(values)
        return {"saved": len(values["rows"]), "added": 0, "updated": 0, "deleted": len(values["removed"])}

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


def test_added_and_removed_workspaces_reach_the_store(client, stub):
    body = {
        "rows": [],
        "manual": [{"label": "토목", "allocated": 80, "active": 3}],
        "removed": [7],
    }

    response = client.put("/api/licenses", json=body, headers=owner(client) | CSRF)

    assert response.status_code == 200, response.text
    assert stub["manual"] == [{"id": None, "label": "토목", "allocated": 80, "active": 3}]
    assert stub["removed"] == [7]


def test_a_linked_workspace_cannot_be_deleted(client, stub):
    """삭제 칸은 직접 추가한 행의 번호만 받는다 — 연동 워크스페이스 키는 들어갈 자리가 없다."""
    body = {"rows": [], "removed": ["tyit"]}

    assert client.put("/api/licenses", json=body, headers=owner(client) | CSRF).status_code == 422
