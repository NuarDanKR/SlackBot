"""봇 관리 API — **권한·CSRF·토큰 비노출**, 그리고 Hermes 가 하나라는 것.

설계: `docs/design/workspace-service-console-redesign.md` §7 (2026-09-28)

여기서 막는 것.

1. **guest·developer 가 연결 화면을 여는 것.** 토큰 mask 와 워크스페이스 구조가
   드러난다. 라이선스 화면과 같은 기준(admin)이다
2. **CSRF 없이 쓰는 것.** 다른 탭이 로그인 쿠키로 토큰을 갈아 끼울 수 있다
3. **응답에 평문 토큰이 실리는 것**
4. **연결 저장이 무언가를 켜는 것**
5. **기존 라이선스·아카이빙 endpoint 가 깨지는 것**

저장소는 가짜다. 여기서 보는 것은 SQL 이 아니라 **HTTP 계약**이다.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="fastapi TestClient 가 httpx 를 쓴다")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_bot_repo import FakeBotRepo
from fastapi.testclient import TestClient
from test_console_api import env, guest, member, owner  # noqa: F401

from tybot.console import app as console_app
from tybot.console import bot_admin, bot_identity, bot_manifest

CSRF = {"X-TYBot-CSRF": "1", "Origin": "http://testserver"}
BOT = "xoxb-" + "1" * 30
APP = "xapp-" + "2" * 30


@pytest.fixture
def client(request):
    request.getfixturevalue("env")
    return TestClient(console_app.app)


@pytest.fixture
def repo(monkeypatch) -> FakeBotRepo:
    """가짜 저장소를 기본 저장소 자리에 끼운다."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("WORKSPACE_SECRET_KEY", Fernet.generate_key().decode("ascii"))
    store = FakeBotRepo()
    store.given_bot("master", category="orchestrator", slack_connectable=True,
                    internally_invokable=False, display_name="TYBot Master")
    store.given_bot("archiver", category="collector", slack_connectable=True,
                    internally_invokable=False, display_name="Archiving Bot")
    store.given_bot("hermes", category="specialist", slack_connectable=True,
                    internally_invokable=True, display_name="Hermes")
    store.given_specialist("hermes")
    monkeypatch.setattr(bot_admin, "default_repo", lambda: store)
    monkeypatch.setattr(bot_manifest, "default_repo", lambda: store)
    monkeypatch.setattr(bot_identity, "default_repo", lambda: store)
    return store


def repo_fingerprint(workspace: str, bot_key: str) -> str:
    """가짜 검증이 실제 경로처럼 **지금 지문**을 들고 오게 한다."""
    from tybot.console import bot_admin as admin

    store = admin.default_repo()
    row = store.connection(workspace, bot_key)
    return store.secret_fingerprint(int(row["id"])) if row else ""


# --- 권한 --------------------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/bots",
    "/api/bots/hermes",
    "/api/workspaces/tyit/bot-connections",
    "/api/workspaces/tyit/bot-routes",
    "/api/bot-audit",
    "/api/bot-manifests",
    "/api/bot-migration-check",
])
def test_reading_needs_admin(client, repo, path):
    """토큰 mask 와 워크스페이스 구조가 드러난다. 라이선스 화면과 같은 기준이다."""
    assert client.get(path, headers=guest(client)).status_code == 403
    assert client.get(path, headers=member(client)).status_code == 403
    assert client.get(path, headers=owner(client)).status_code == 200


def test_writing_needs_admin(client, repo):
    response = client.put(
        "/api/workspaces/tyit/bot-connections/archiver/slack",
        json={"botToken": BOT, "appToken": APP, "reason": "설치"},
        headers={**member(client), **CSRF},
    )

    assert response.status_code == 403


def test_writing_needs_csrf(client, repo):
    """다른 탭이 로그인 쿠키로 토큰을 갈아 끼울 수 있다."""
    response = client.put(
        "/api/workspaces/tyit/bot-connections/archiver/slack",
        json={"botToken": BOT, "appToken": APP, "reason": "설치"},
        headers=owner(client),
    )

    assert response.status_code == 403
    assert repo.connections() == []


# --- 하나의 Hermes -------------------------------------------------------------

def test_hermes_is_one_row_with_two_bindings(client, repo):
    repo.given_connection("pf", "hermes", state="enabled", team_id="T1", bot_user_id="U9")
    repo.given_assignment("hermes", "pf")
    repo.given_route("hermes", "pf", "shadow")

    body = client.get("/api/bots", headers=owner(client)).json()

    hermes = [row for row in body["bots"] if row["key"] == "hermes"]
    assert len(hermes) == 1
    assert sorted(b["type"] for b in hermes[0]["bindings"]) == [
        "master_internal", "slack_socket",
    ]


def test_an_unknown_bot_is_404(client, repo):
    assert client.get("/api/bots/nope", headers=owner(client)).status_code == 404


# --- 토큰 저장 ------------------------------------------------------------------

def test_saving_a_token_returns_only_masks(client, repo):
    response = client.put(
        "/api/workspaces/tyit/bot-connections/archiver/slack",
        json={"botToken": BOT, "appToken": APP, "reason": "설치"},
        headers={**owner(client), **CSRF},
    )

    assert response.status_code == 200
    body = response.text
    assert BOT not in body and APP not in body
    assert "xoxb-" in body, "mask 는 보여야 어떤 토큰인지 안다"


def test_saving_a_token_does_not_enable_anything(client, repo):
    """토큰을 넣는 것과 수집이 시작되는 것은 다른 일이다(§9)."""
    client.put(
        "/api/workspaces/tyit/bot-connections/archiver/slack",
        json={"botToken": BOT, "appToken": APP, "reason": "설치"},
        headers={**owner(client), **CSRF},
    )

    (row,) = repo.connections("tyit")
    assert row["state"] == "draft"
    assert row["identity_ok"] is None


def test_a_bad_token_shape_is_422(client, repo):
    response = client.put(
        "/api/workspaces/tyit/bot-connections/archiver/slack",
        json={"botToken": "xoxp-1", "appToken": APP, "reason": "설치"},
        headers={**owner(client), **CSRF},
    )

    assert response.status_code == 422
    assert "xoxp-1" not in response.text


def test_a_reason_is_required(client, repo):
    response = client.put(
        "/api/workspaces/tyit/bot-connections/archiver/slack",
        json={"botToken": BOT, "appToken": APP, "reason": ""},
        headers={**owner(client), **CSRF},
    )

    assert response.status_code == 422


# --- 신원 검사 ------------------------------------------------------------------

def test_token_pair_and_fingerprint_are_read_from_one_sql_snapshot(monkeypatch):
    """검증한 토큰과 기록에 쓸 지문이 서로 다른 시점의 값이면 안 된다."""
    rows = [
        {"kind": "app", "ciphertext": b"encrypted-app", "fingerprint": "fp-1"},
        {"kind": "bot", "ciphertext": b"encrypted-bot", "fingerprint": "fp-1"},
    ]

    class Cursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, query, params):
            self.calls.append((query, params))

        def fetchall(self):
            return rows

    cursor = Cursor()

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def cursor(self):
            return cursor

    class Cipher:
        @staticmethod
        def decrypt(value):
            return value.removeprefix(b"encrypted-")

    monkeypatch.setattr(bot_identity, "_connect", Connection)
    monkeypatch.setattr(bot_identity, "_fernet", Cipher)

    assert bot_identity._tokens("tyit", "master") == ("bot", "app", "fp-1")
    assert len(cursor.calls) == 1
    query, params = cursor.calls[0]
    assert params == ("tyit", "master")
    assert "WITH target AS" in query
    assert "fingerprint AS" in query
    assert "md5(s.ciphertext)" in query
    assert "f.value AS fingerprint" in query


def _slack(team="T1", user="U2", *, fail=False):
    class _Client:
        def __init__(self, token):
            self.token = token

        def auth_test(self):
            if fail:
                raise RuntimeError("timeout")
            return {"team_id": team, "user_id": user}

        def apps_connections_open(self):
            if fail:
                raise RuntimeError("timeout")
            return {"ok": True}

    return _Client


def test_identity_enables_the_connection(client, repo, monkeypatch):
    repo.given_connection("tyit", "archiver", state="draft")
    monkeypatch.setattr(bot_identity, "_tokens", lambda ws, key: (BOT, APP, "fp"))
    monkeypatch.setattr(
        bot_identity, "verify_connection",
        lambda ws, key, *, actor, client_factory=None, repo=None: bot_admin.record_identity(
            ws, key, team_id="T1", bot_user_id="U2", actor=actor,
            token_fingerprint=repo_fingerprint(ws, key),
            ),
    )

    response = client.post(
        "/api/workspaces/tyit/bot-connections/archiver/slack/verify-identity",
        json={"reason": "설치 확인"}, headers={**owner(client), **CSRF},
    )

    assert response.status_code == 200
    assert repo.connections("tyit")[0]["state"] == "enabled"


def test_a_duplicate_bot_user_is_409(client, repo, monkeypatch):
    """같은 앱을 두 번 등록하면 Socket Mode 를 두 곳에서 열게 된다(§7.5)."""
    repo.given_connection("tyit", "master", state="enabled", team_id="T1",
                          bot_user_id="U1", identity_ok=True)
    repo.given_connection("tyit", "archiver", state="draft")
    monkeypatch.setattr(
        bot_identity, "verify_connection",
        lambda ws, key, *, actor, client_factory=None, repo=None: bot_admin.record_identity(
            ws, key, team_id="T1", bot_user_id="U1", actor=actor,
            token_fingerprint=repo_fingerprint(ws, key),
            ),
    )

    response = client.post(
        "/api/workspaces/tyit/bot-connections/archiver/slack/verify-identity",
        json={"reason": "설치 확인"}, headers={**owner(client), **CSRF},
    )

    assert response.status_code == 409


def test_slack_being_down_says_the_token_is_saved(client, repo, monkeypatch):
    """「저장이 안 됐나」 를 사람이 다시 묻지 않게 적는다(§7.5)."""
    repo.given_connection("tyit", "archiver", state="draft")

    def _boom(*args, **kwargs):
        raise bot_identity.SlackUnavailable("Slack 토큰 쌍 확인 실패: TimeoutError.")

    monkeypatch.setattr(bot_identity, "verify_connection", _boom)

    response = client.post(
        "/api/workspaces/tyit/bot-connections/archiver/slack/verify-identity",
        json={"reason": "설치 확인"}, headers={**owner(client), **CSRF},
    )

    assert response.status_code == 503
    assert "저장돼 있습니다" in response.json()["detail"]


# --- 연결 중지 ------------------------------------------------------------------

def test_a_connection_can_be_disabled(client, repo):
    repo.given_connection("pf", "hermes", state="enabled", team_id="T1", bot_user_id="U9")

    response = client.post(
        "/api/workspaces/pf/bot-connections/hermes/slack/state",
        json={"state": "disabled", "reason": "PF 전환"}, headers={**owner(client), **CSRF},
    )

    assert response.status_code == 200
    assert repo.connections("pf")[0]["state"] == "disabled"


def test_enabling_by_hand_is_rejected_by_the_schema(client, repo):
    """켜는 것은 신원 검사를 통과할 때뿐이다. 본문 검증에서 막힌다."""
    repo.given_connection("pf", "hermes", state="disabled")

    response = client.post(
        "/api/workspaces/pf/bot-connections/hermes/slack/state",
        json={"state": "enabled", "reason": "그냥"}, headers={**owner(client), **CSRF},
    )

    assert response.status_code == 422


# --- 라우트 --------------------------------------------------------------------

def test_a_route_can_be_shadowed(client, repo):
    repo.given_assignment("hermes", "tyit")

    response = client.put(
        "/api/workspaces/tyit/bot-routes/hermes",
        json={"mode": "shadow", "reason": "대조 시작"}, headers={**owner(client), **CSRF},
    )

    assert response.status_code == 200
    assert repo.route_rows[("hermes", "tyit")]["route_mode"] == "shadow"


def test_an_active_route_with_a_sick_runtime_is_422(client, repo):
    repo.specialist_rows.clear()
    repo.given_specialist("hermes", state="enabled", health="error")
    repo.given_assignment("hermes", "tyit")

    response = client.put(
        "/api/workspaces/tyit/bot-routes/hermes",
        json={"mode": "active", "reason": "전환"}, headers={**owner(client), **CSRF},
    )

    assert response.status_code == 422


def test_routing_a_non_specialist_is_422(client, repo):
    repo.given_assignment("master", "tyit")

    response = client.put(
        "/api/workspaces/tyit/bot-routes/master",
        json={"mode": "shadow", "reason": "시도"}, headers={**owner(client), **CSRF},
    )

    assert response.status_code == 422


# --- Manifest ------------------------------------------------------------------

def test_the_manifest_catalog_reads_the_files(client, repo):
    """DB 에 hash 를 복사하면 파일이 바뀌어도 옛 값이 남는다."""
    body = client.get("/api/bot-manifests", headers=owner(client)).json()

    keys = {row["botKey"] for row in body["manifests"]}
    assert keys == {"master", "archiver"}
    assert all(len(row["sha256"]) == 64 for row in body["manifests"])


def test_attesting_with_a_wrong_hash_is_refused(client, repo):
    """다른 값을 그대로 받으면 「확인됨」 인데 무엇과 확인했는지 모르는 상태가 된다."""
    repo.given_connection("tyit", "archiver", state="draft")

    response = client.put(
        "/api/workspaces/tyit/bot-connections/archiver/slack/manifest-attestation",
        json={"manifestId": "archiver/slack_socket", "sha256": "0" * 64,
              "reason": "대조"},
        headers={**owner(client), **CSRF},
    )

    assert response.status_code == 422


def test_attesting_records_who_checked(client, repo):
    repo.given_connection("tyit", "archiver", state="draft")
    digest = bot_manifest.known_sha256("archiver/slack_socket")

    response = client.put(
        "/api/workspaces/tyit/bot-connections/archiver/slack/manifest-attestation",
        json={"manifestId": "archiver/slack_socket", "sha256": digest, "reason": "대조"},
        headers={**owner(client), **CSRF},
    )

    assert response.status_code == 200
    row = repo.connections("tyit")[0]
    assert row["manifest_attested_sha256"] == digest
    assert row["manifest_attested_by"] == "dan@taeyoung.com"


def test_identity_and_manifest_stay_separate(client, repo):
    """`auth.test` 성공만으로 Manifest 가 일치한다고 표시하지 않는다(§6.2)."""
    repo.given_connection("tyit", "archiver", state="enabled", team_id="T1",
                          bot_user_id="U2", identity_ok=True)

    body = client.get("/api/workspaces/tyit/bot-connections", headers=owner(client)).json()

    (archiver,) = [row for row in body["bots"] if row["key"] == "archiver"]
    (slack,) = archiver["slack"]
    assert slack["identityOk"] is True
    assert slack["manifestAttestedSha256"] == ""


# --- 기존 화면이 안 깨진다 --------------------------------------------------------

def test_the_license_endpoint_still_answers(client, monkeypatch):
    """PR #1 의 화면은 이 작업과 무관하게 계속 돌아야 한다."""
    from tybot.console import license_store

    monkeypatch.setattr(license_store, "linked_workspaces", lambda: {})
    monkeypatch.setattr(license_store, "list_stored", lambda: [])
    monkeypatch.setattr(license_store, "active_counts", lambda tokens, refresh=False: {})

    assert client.get("/api/licenses", headers=owner(client)).status_code == 200


def test_the_legacy_service_endpoint_still_exists():
    """`ArchivingPanel` 이 아직 부른다. 호출부가 사라진 뒤에 지운다(§7.4)."""
    paths = {getattr(route, "path", "") for route in console_app.app.routes}

    assert "/api/workspaces/{key}/archiving/services/{service}" in paths
    assert "/api/workspaces/{key}/archiving/services/{service}/verify" in paths


def test_a_manifest_from_another_bot_is_refused(client, repo):
    """Archiver 연결에 Master Manifest 를 적으면 **둘 다 「확인됨」 으로 보인다.**

    실제로 확인된 것은 하나뿐이고, 어느 쪽인지는 화면에 안 남는다.
    """
    repo.given_connection("tyit", "archiver", state="draft")
    digest = bot_manifest.known_sha256("master/slack_socket")

    response = client.put(
        "/api/workspaces/tyit/bot-connections/archiver/slack/manifest-attestation",
        json={"manifestId": "master/slack_socket", "sha256": digest, "reason": "대조"},
        headers={**owner(client), **CSRF},
    )

    assert response.status_code == 422
    assert "master" in response.json()["detail"]
    assert repo.connections("tyit")[0]["manifest_attested_sha256"] == ""


def test_a_manifest_hash_alone_does_not_decide_the_owner(client, repo):
    """hash 만 맞으면 통과시키면, 봇을 바꿔 붙이는 길이 열린 채로 남는다."""
    from tybot.console import bot_manifest as manifest

    assert manifest.owner_of("master/slack_socket") == "master"
    assert manifest.owner_of("archiver/slack_socket") == "archiver"
    assert manifest.owner_of("hermes/slack_socket") == "", "PF 승인본은 아직 없다"
