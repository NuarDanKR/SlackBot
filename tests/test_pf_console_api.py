"""PF 콘솔 API — 누가 무엇까지 보는가.

## 무엇을 고정하나
오늘 열린 것은 조회뿐이다(오너 계획 §11). 그래서 시험이 답해야 하는 질문도 셋이다.

1. **로그인 없이 보이는 것이 있나** — 없어야 한다
2. **권한 없는 서비스가 보이나** — 안 보여야 하고, 「없다」 와 구분되지 않아야 한다
3. **상태 파일에 뭐가 들어와도 화면에 오르나** — 허용한 칸만 올라야 한다

3번이 이 화면의 진짜 위험이다. 상태 파일은 **우리가 쓰는 파일이 아니다.** 계약에
없는 값이 하나 들어가면 우리 화면이 그것을 그대로 사내에 띄운다.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest
from fastapi.testclient import TestClient

from tybot_pf import app as pf_app
from tybot_pf import auth as pf_auth
from tybot_pf import health, store

EMAIL = "dan@taeyoung.com"
PASSWORD = "비밀번호12345"
SERVICE = "pf-hermes"

READ_PATHS = [
    "/pf/api/me",
    "/pf/api/services",
    f"/pf/api/services/{SERVICE}/status",
    f"/pf/api/services/{SERVICE}/archive",
    f"/pf/api/services/{SERVICE}/batches",
    f"/pf/api/services/{SERVICE}/cost",
    "/pf/api/audit",
]


@pytest.fixture
def state_dir(tmp_path):
    return tmp_path / SERVICE


@pytest.fixture
def fake_db(monkeypatch, state_dir):
    """DB 를 대신한다. 이 시험이 보는 것은 권한 판정과 응답 모양이다."""
    password_hash = pf_auth.hash_password(PASSWORD)
    world = {
        "accounts": {EMAIL: (EMAIL, "단", password_hash)},
        "grants": {EMAIL: {SERVICE: frozenset({"viewer"})}},
        "services": {
            SERVICE: {
                "key": SERVICE,
                "label": "프금팀 Hermes",
                "state": "shadow",
                "business_owner": "프금팀",
                "infra_owner": "전산팀",
                "state_dir": str(state_dir),
            },
            "other": {
                "key": "other",
                "label": "다른 서비스",
                "state": "shadow",
                "business_owner": "",
                "infra_owner": "",
                "state_dir": str(state_dir / "other"),
            },
        },
        "audit": [],
    }

    monkeypatch.setattr(pf_auth, "load_account", lambda email: world["accounts"].get(email.strip().lower()))
    monkeypatch.setattr(pf_auth, "load_grants", lambda email: world["grants"].get(email.strip().lower(), {}))

    def services(keys=None):
        rows = list(world["services"].values())
        if keys is not None:
            wanted = {str(k).lower() for k in keys}
            rows = [row for row in rows if row["key"] in wanted]
        return rows

    monkeypatch.setattr(store, "services", services)
    monkeypatch.setattr(
        store, "record",
        lambda **kw: world["audit"].append(kw),
    )
    monkeypatch.setattr(
        store, "audit",
        lambda *, actor, allowed, limit=100: [
            {
                "at": "2026-09-22T10:00:00+09:00",
                "actor": actor,
                "service": key,
                "action": "login",
                "outcome": "succeeded",
                "detail": "",
            }
            for key in allowed
        ],
    )
    # 세션 서명 키를 고정해 시험 사이에 섞이지 않게 한다.
    monkeypatch.setattr(pf_app, "_authenticator", pf_auth.PFAuthenticator(secret="시험용키"))
    return world


@pytest.fixture
def client(fake_db):
    return TestClient(pf_app.app)


def sign_in(client) -> dict:
    response = client.post("/pf/api/login", json={"email": EMAIL, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()


def write_state(state_dir, **over):
    now = dt.datetime.now(dt.UTC)
    data = {
        "source_commit": "a1b2c3d",
        "started_at": (now - dt.timedelta(hours=3)).isoformat(timespec="seconds"),
        "generated_at": now.isoformat(timespec="seconds"),
        "last_slack_connect": now.isoformat(timespec="seconds"),
        "last_ingest": now.isoformat(timespec="seconds"),
        "last_archive_pull": now.isoformat(timespec="seconds"),
        "last_archive_push": now.isoformat(timespec="seconds"),
        "unpushed_commits": 0,
        "last_digest": now.isoformat(timespec="seconds"),
        "calls_today": 12,
        "tokens_today": 34567,
        "cost_usd_today": 0.42,
        "errors": [],
    }
    data.update(over)
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / health.STATE_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return data


# --- 1. 로그인하지 않으면 아무것도 없다 ---------------------------------------
@pytest.mark.parametrize("path", READ_PATHS)
def test_every_read_needs_a_session(client, path):
    assert client.get(path).status_code == 401


def test_the_liveness_probe_answers_without_a_session(client):
    """로그인 전에도 「프로세스가 떠 있나」 는 확인해야 한다. 그것만 말한다."""
    response = client.get("/pf/api/health")
    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_a_tybot_session_cookie_does_not_open_the_pf_console(client):
    """이름이 다르므로 브라우저가 같이 보내와도 이 자리에 오지 않는다."""
    client.cookies.set("tybot_console", "any-tybot-session-value")
    assert client.get("/pf/api/me").status_code == 401


def test_a_wrong_password_is_not_told_apart_from_a_missing_account(client):
    """둘을 가르면 로그인 화면이 회사 계정 존재 여부를 알려 주는 도구가 된다."""
    missing = client.post("/pf/api/login", json={"email": "없는사람@taeyoung.com", "password": "x"})
    wrong = client.post("/pf/api/login", json={"email": EMAIL, "password": "틀린값"})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json()["detail"] == wrong.json()["detail"]


# --- 2. 쿠키 ------------------------------------------------------------------
def test_the_session_cookie_is_scoped_to_pf(client):
    response = client.post("/pf/api/login", json={"email": EMAIL, "password": PASSWORD})
    raw = response.headers["set-cookie"]

    assert "pf_console=" in raw
    assert "Path=/pf" in raw
    assert "HttpOnly" in raw
    assert "SameSite=strict" in raw


def test_the_cookie_is_not_secure_until_tls_is_on(client, monkeypatch):
    """TLS 앞에서 켜면 쿠키가 전송되지 않아 로그인이 안 되고, 화면에는
    「비밀번호가 틀렸다」 처럼 보인다(B-35 와 같은 함정)."""
    monkeypatch.delenv("PF_CONSOLE_COOKIE_SECURE", raising=False)
    plain = client.post("/pf/api/login", json={"email": EMAIL, "password": PASSWORD})
    assert "Secure" not in plain.headers["set-cookie"]

    monkeypatch.setenv("PF_CONSOLE_COOKIE_SECURE", "1")
    secure = client.post("/pf/api/login", json={"email": EMAIL, "password": PASSWORD})
    assert "Secure" in secure.headers["set-cookie"]


def test_logging_out_clears_the_cookie_on_the_same_path(client):
    """경로가 다르면 브라우저가 다른 쿠키로 보고 남겨 둔다."""
    sign_in(client)
    raw = client.post("/pf/api/logout").headers["set-cookie"]
    assert "pf_console=" in raw
    assert "Path=/pf" in raw


# --- 3. 권한 ------------------------------------------------------------------
def test_a_service_without_a_grant_is_invisible(client, fake_db):
    """권한이 없는 서비스는 목록에도 없고, 직접 부르면 404 다."""
    sign_in(client)

    listed = client.get("/pf/api/services").json()["services"]
    assert [row["key"] for row in listed] == [SERVICE]

    assert client.get("/pf/api/services/other/status").status_code == 404


def test_a_missing_service_and_a_forbidden_one_look_the_same(client):
    """가르면 키를 바꿔 넣어 보는 것만으로 무엇이 존재하는지 알 수 있다."""
    sign_in(client)
    forbidden = client.get("/pf/api/services/other/status")
    absent = client.get("/pf/api/services/존재하지않음/status")
    assert forbidden.status_code == absent.status_code == 404
    assert forbidden.json()["detail"] == absent.json()["detail"]


def test_a_tybot_admin_without_a_pf_grant_sees_nothing(client, fake_db):
    """TYBot 에서 관리자라도 `console_user_service` 행이 없으면 아무것도 못 본다(§8.2)."""
    fake_db["grants"][EMAIL] = {}
    sign_in(client)

    assert client.get("/pf/api/me").json()["user"]["services"] == []
    assert client.get("/pf/api/services").json()["services"] == []
    assert client.get(f"/pf/api/services/{SERVICE}/status").status_code == 404


def test_revoking_a_grant_takes_effect_without_a_new_login(client, fake_db):
    """권한을 세션에 담으면 회수해도 쿠키가 만료될 때까지 계속 보인다."""
    sign_in(client)
    assert client.get("/pf/api/services").json()["services"]

    fake_db["grants"][EMAIL] = {}
    assert client.get("/pf/api/services").json()["services"] == []


def test_a_deactivated_account_cannot_use_an_old_cookie(client, fake_db):
    sign_in(client)
    fake_db["accounts"].pop(EMAIL)
    assert client.get("/pf/api/me").status_code == 401


# --- 4. 상태 파일 — 허용한 칸만 --------------------------------------------------
def test_values_outside_the_contract_never_reach_the_screen(client, state_dir):
    """이 화면의 진짜 위험이다. 상태 파일은 우리가 쓰는 파일이 아니다."""
    write_state(
        state_dir,
        slack_bot_token="xoxb-비밀토큰값",
        last_question="김부장 연봉이 얼마야",
        last_answer="답변 본문입니다",
        private_channels=["#임원-비공개"],
        anthropic_api_key="sk-ant-비밀키",
    )
    sign_in(client)

    body = client.get(f"/pf/api/services/{SERVICE}/status").text
    for leaked in ("xoxb-", "연봉", "답변 본문", "임원-비공개", "sk-ant-"):
        assert leaked not in body, f"계약 밖의 값이 화면으로 나갔습니다: {leaked}"


def test_an_error_message_is_folded_into_a_code(client, state_dir):
    """예외 메시지에는 경로·식별자·때로는 질문 조각이 섞인다."""
    write_state(
        state_dir,
        errors=[{"code": "anthropic", "at": "2026-09-22T10:00:00Z", "message": "질문 원문이 섞인 메시지"}],
    )
    sign_in(client)

    body = client.get(f"/pf/api/services/{SERVICE}/cost")
    assert "질문 원문" not in body.text
    assert body.json()["errors"] == [{"code": "anthropic", "at": "2026-09-22T10:00:00Z"}]


def test_an_unknown_error_code_is_not_rendered_verbatim(client, state_dir):
    write_state(state_dir, errors=[{"code": "Error: /var/lib/secret/path 를 열지 못함", "at": ""}])
    sign_in(client)

    body = client.get(f"/pf/api/services/{SERVICE}/status")
    assert "/var/lib/secret" not in body.text
    assert body.json()["health"]["errors"][0]["code"] == "unknown"


# --- 5. 없는 상태·낡은 상태·계약 미충족 ------------------------------------------
def test_a_missing_state_file_is_reported_as_unavailable_not_as_an_error(client):
    """아직 안 붙은 것은 고장이 아니다. 개발 환경과 이관 전 서버가 이 상태다."""
    sign_in(client)
    body = client.get(f"/pf/api/services/{SERVICE}/status").json()

    assert body["health"]["status"] == "unavailable"
    assert body["health"]["reason"]
    # 화면이 어디를 봐야 하는지 말한다.
    assert health.STATE_FILENAME in body["health"]["path"]


def test_a_broken_state_file_is_reported_as_unreadable(client, state_dir):
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / health.STATE_FILENAME).write_text("{이건 JSON 이 아니다", encoding="utf-8")
    sign_in(client)

    assert client.get(f"/pf/api/services/{SERVICE}/status").json()["health"]["status"] == "unreadable"


def test_a_state_file_missing_contract_fields_says_which_ones(client, state_dir):
    """「정상」 이라고 칠할 근거가 없는 상태다. 무엇이 없는지까지 말한다."""
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / health.STATE_FILENAME).write_text(
        json.dumps({"calls_today": 3}), encoding="utf-8"
    )
    sign_in(client)

    got = client.get(f"/pf/api/services/{SERVICE}/status").json()["health"]
    assert got["status"] == "incomplete"
    assert set(got["missing"]) == {"source_commit", "started_at", "generated_at"}


def test_an_old_state_file_is_reported_as_stale(client, state_dir):
    old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=4)).isoformat(timespec="seconds")
    write_state(state_dir, generated_at=old)
    sign_in(client)

    got = client.get(f"/pf/api/services/{SERVICE}/status").json()["health"]
    assert got["status"] == "stale"
    assert got["ageSeconds"] > 3600


def test_a_healthy_state_file_reads_as_ok(client, state_dir):
    write_state(state_dir)
    sign_in(client)

    body = client.get(f"/pf/api/services/{SERVICE}/status").json()
    assert body["health"]["status"] == "ok"
    assert body["health"]["fields"]["sourceCommit"] == "a1b2c3d"
    assert body["service"]["businessOwner"] == "프금팀"


# --- 6. 화면별 값 --------------------------------------------------------------
def test_the_archive_screen_shows_unpushed_work_and_conflicts(client, state_dir):
    write_state(state_dir, unpushed_commits=3, archive_conflict="refs/heads/main")
    sign_in(client)

    body = client.get(f"/pf/api/services/{SERVICE}/archive").json()
    assert body["unpushedCommits"] == 3
    assert body["archiveConflict"] == "refs/heads/main"


def test_the_cost_screen_says_who_owns_the_limit(client, state_dir):
    """상한을 우리가 걸 수 있다고 착각하면 초과했을 때 아무도 안 막는다."""
    write_state(state_dir)
    sign_in(client)

    body = client.get(f"/pf/api/services/{SERVICE}/cost").json()
    assert body["costUsdToday"] == 0.42
    assert body["limitOwner"] == "프금팀"


def test_the_batch_screen_lists_jobs_even_when_nothing_has_run(client):
    """빈 목록을 내면 「배치가 없는 서비스」 로 읽힌다. 칸은 두고 값을 비운다."""
    sign_in(client)

    body = client.get(f"/pf/api/services/{SERVICE}/batches").json()
    assert [job["key"] for job in body["jobs"]] == [
        "ingest", "archive-pull", "archive-push", "digest",
    ]
    assert all(job["lastRunAt"] == "" for job in body["jobs"])


# --- 7. 쓰는 길이 없다 ----------------------------------------------------------
def test_todays_console_has_no_write_route(client):
    """`restart`·`stop`·sync·activate·rollback 은 고정 helper 와 감사가 준비된 뒤에 연다.

    버튼을 먼저 만들면 「눌러도 아무 일 없음」 과 「눌렀는데 되돌릴 수 없음」 중
    하나가 된다.
    """
    writes = {
        (route.path, method)
        for route in pf_app.app.routes
        for method in getattr(route, "methods", set())
        if method in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert writes == {("/pf/api/login", "POST"), ("/pf/api/logout", "POST")}


def test_no_route_takes_a_path_or_unit_name(client):
    """request 가 경로·unit 을 정할 수 있으면 그것은 임의 파일 읽기이자 임의 실행이다."""
    banned = {"path", "unit", "state_dir", "command", "service_dir"}
    for route in pf_app.app.routes:
        for name in getattr(route, "param_convertors", {}):
            assert name not in banned, f"{route.path} 가 {name} 을 받습니다."


def test_the_audit_screen_shows_the_users_own_records(client):
    sign_in(client)
    rows = client.get("/pf/api/audit").json()["events"]
    assert rows and rows[0]["actor"] == EMAIL
