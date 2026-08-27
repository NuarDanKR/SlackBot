"""관리 콘솔 읽기 API.

DB 없이 아카이브 MD·감사기록에서 화면 데이터를 만드는 경로를 검증한다.
특히 **권한으로 응답이 좁혀지는지**와 **내려보내면 안 되는 것이 새지 않는지**를 본다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tybot.console import app as console_app
from tybot.console.auth import Authenticator, hash_password

KST = timezone(timedelta(hours=9))

OWNER_PW = "owner-pass"
MEMBER_PW = "member-pass"
# 아이디:비밀번호해시:이메일:역할[:워크스페이스]
ACCOUNTS = (
    f"dan:{hash_password(OWNER_PW)}:dan@taeyoung.com:owner:*, "
    f"sukhyun:{hash_password(MEMBER_PW)}:sh.kim@taeyoung.com:member:fin"
)

DOC_FIN = """---
workspace: fin
channel: "#팀_자금(ABB540)_주간보고"
visibility: private
acl: [#팀_자금(ABB540)_주간보고]
share_with: [mgmt]
doc_count: 3
last_ingested: {stamp}
---

## 요약 (사람이 관리, 봇은 수정 금지)
- 8월 3주 기성 청구

## 원문 (자동 취합, 편집 금지)
> [{day} 09:12] 김수현: 김해외동 3차 기성 청구서 접수했습니다
> [{day} 09:31] 김수현: [첨부:변환] 기성내역_3차.xlsx (xlsx, 84KB)
> [{day} 10:15] 이순신: 결재 승인 났습니다
"""

DOC_SITE = """---
workspace: site-gimhae
channel: "#현장_김해외동(180182)_채팅방"
visibility: private
acl: [#현장_김해외동(180182)_채팅방]
doc_count: 1
last_ingested: {stamp}
---

## 원문 (자동 취합, 편집 금지)
> [{day} 08:05] 박정호: 3층 슬래브 타설 시작합니다
"""

BROKEN = """워크스페이스 표시가 없는 문서
> [2026-08-21 08:30] 정한길: 일일점검 시작합니다
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    """아카이브·감사기록·규칙 문서를 임시 폴더에 만들고 환경변수를 맞춘다."""
    archive = tmp_path / "archive"
    qa = tmp_path / "qa-log"
    harness = tmp_path / "harness"
    now = datetime.now(KST)
    day = now.date().isoformat()
    stamp = now.isoformat(timespec="minutes")

    for ws, text in (("fin", DOC_FIN), ("site-gimhae", DOC_SITE)):
        d = archive / "channels" / ws
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{ws}-채널.md").write_text(text.format(day=day, stamp=stamp), encoding="utf-8")
    broken_dir = archive / "channels" / "safety"
    broken_dir.mkdir(parents=True, exist_ok=True)
    (broken_dir / "점검.md").write_text(BROKEN, encoding="utf-8")

    (harness / "fin").mkdir(parents=True, exist_ok=True)
    (harness / "fin" / "rules.md").write_text("# 자금팀 답변 규칙\n- 금액은 원문 그대로\n", encoding="utf-8")
    (harness / "mgmt").mkdir(parents=True, exist_ok=True)
    (harness / "mgmt" / "glossary.md").write_text("# 용어 사전\n", encoding="utf-8")

    qa.mkdir(parents=True, exist_ok=True)
    rows = [
        {
            "ts": f"{day}T09:20:00+09:00",
            "workspace": "fin",
            "channel": "#팀_자금(ABB540)_주간보고",
            "user": "U1",
            "user_name": "김수현",
            "question": "기성금 얼마야?",
            "answer": "3억 2천만원입니다",
            "intent_kind": "search",
            "intent_source": "llm",
            "reason": "answered",
            "hits": 4,
            "model": "claude-sonnet-5",
            "cost_usd": 0.021,
            "elapsed_ms": 2400,
        },
        {
            "ts": f"{day}T10:05:00+09:00",
            "workspace": "site-gimhae",
            "channel": "#현장",
            "user": "U2",
            "user_name": "박정호",
            "question": "타설 언제야?",
            "answer": "8월 18일입니다",
            "intent_kind": "summary",
            "intent_source": "llm",
            "reason": "answered",
            "hits": 12,
            "model": "claude-sonnet-5",
            "cost_usd": 0.035,
            "elapsed_ms": 3100,
        },
    ]
    with (qa / f"qa-{day[:7]}.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    monkeypatch.setenv("ARCHIVE_DIR", str(archive))
    monkeypatch.setenv("QA_LOG_DIR", str(qa))
    monkeypatch.setenv("HARNESS_DIR", str(harness))
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("ENV_SETTINGS_PATH", str(tmp_path / "state" / "config" / "managed.env"))
    monkeypatch.setenv("WORKSPACES", "fin,site-gimhae,mgmt")
    monkeypatch.setenv("WORKSPACE_LABEL_FIN", "자금팀")
    monkeypatch.setenv("WORKSPACE_LABEL_SITE_GIMHAE", "현장 김해외동(180182)")
    monkeypatch.setenv("WORKSPACE_LABEL_MGMT", "경영본부")
    monkeypatch.setenv("ROOT_WORKSPACES", "mgmt")
    monkeypatch.setenv("CROSS_WS_READ", "mgmt:fin|site-gimhae")
    monkeypatch.setenv("DAILY_COST_LIMIT_USD", "10")
    monkeypatch.delenv("COST_STATE_PATH", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)

    console_app.reset_state()
    auth = Authenticator(accounts_spec=ACCOUNTS, secret="test-secret")
    console_app.app.dependency_overrides[console_app.authenticator] = lambda: auth
    yield tmp_path
    console_app.app.dependency_overrides.clear()
    console_app.reset_state()


@pytest.fixture
def client(env):
    return TestClient(console_app.app)


def _login(client, username: str, password: str) -> dict:
    """로그인하고 세션 쿠키를 헤더로 돌려준다.

    TestClient 는 쿠키를 자동으로 들고 다니지만, 테스트마다 어느 계정으로 보내는지
    분명히 보이도록 헤더를 직접 만든다.
    """
    r = client.post("/api/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    cookie = r.cookies.get("tybot_console")
    assert cookie
    client.cookies.clear()  # 다음 호출이 자동 쿠키에 기대지 않게 한다
    return {"Cookie": f"tybot_console={cookie}"}


def owner(client):
    return _login(client, "dan", OWNER_PW)


def member(client):
    return _login(client, "sukhyun", MEMBER_PW)


# --- 인증 -----------------------------------------------------------------

def test_without_login_is_rejected(client):
    assert client.get("/api/status").status_code == 401


def test_tampered_session_is_rejected(client):
    """서명이 맞지 않는 쿠키는 거절한다 — 값을 지어내 관리자가 될 수 없어야 한다."""
    r = client.get("/api/status", headers={"Cookie": "tybot_console=ZGFu.aaaa"})
    assert r.status_code == 401
    assert "서명" in r.json()["detail"] or "올바르지 않" in r.json()["detail"]


def test_wrong_password_is_rejected(client):
    r = client.post("/api/login", json={"username": "dan", "password": "틀린비번"})
    assert r.status_code == 401
    # 아이디가 있는지 없는지 알려 주지 않는다
    assert r.json()["detail"] == "아이디 또는 비밀번호가 맞지 않습니다."


def test_unknown_user_gives_same_message(client):
    r = client.post("/api/login", json={"username": "없는사람", "password": "아무거나"})
    assert r.status_code == 401
    assert r.json()["detail"] == "아이디 또는 비밀번호가 맞지 않습니다."


def test_login_sets_httponly_cookie(client):
    r = client.post("/api/login", json={"username": "dan", "password": OWNER_PW})
    assert r.status_code == 200
    raw = r.headers["set-cookie"]
    assert "HttpOnly" in raw  # 화면 스크립트가 읽지 못하게
    assert "SameSite=strict" in raw  # 다른 사이트에서 요청을 보낼 수 없게


def test_logout_clears_session(client):
    headers = owner(client)
    assert client.get("/api/me", headers=headers).status_code == 200
    client.post("/api/logout")
    # 쿠키를 지웠으므로 자동 쿠키로는 더 이상 들어갈 수 없다
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401


def test_health_needs_no_token(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_me_reports_role(client):
    assert client.get("/api/me", headers=owner(client)).json()["role"] == "owner"
    m = client.get("/api/me", headers=member(client)).json()
    assert m["role"] == "member"
    assert m["workspaces"] == ["fin"]


def test_proxy_mode_uses_forwarded_email(env):
    console_app.app.dependency_overrides[console_app.authenticator] = lambda: Authenticator(
        mode="proxy", accounts_spec=ACCOUNTS, secret="test-secret"
    )
    c = TestClient(console_app.app)
    r = c.get("/api/me", headers={"X-Forwarded-Email": "dan@taeyoung.com"})
    assert r.status_code == 200
    assert r.json()["role"] == "owner"
    assert c.get("/api/me", headers={"X-Forwarded-Email": "nobody@x.com"}).status_code == 401


# --- 데이터 현황 -----------------------------------------------------------

def test_status_lists_workspaces_from_archive(client):
    rows = client.get("/api/status", headers=owner(client)).json()["workspaces"]
    keys = {r["key"] for r in rows}
    assert {"fin", "site-gimhae", "mgmt", "safety"} <= keys

    fin = next(r for r in rows if r["key"] == "fin")
    assert fin["label"] == "자금팀"
    assert fin["docs"] == 1
    assert fin["rawLines"] == 3
    assert fin["lastIngestedAt"] is not None
    assert len(fin["courses"]) == 30
    assert fin["courses"][-1]["lines"] == 3  # 오늘 3줄


def test_status_marks_broken_documents(client):
    rows = client.get("/api/status", headers=owner(client)).json()["workspaces"]
    safety = next(r for r in rows if r["key"] == "safety")
    assert safety["brokenDocs"] == 1
    assert safety["health"] == "stalled"  # 근거로 쓸 수 있는 원문이 없다


def test_connected_is_unknown_without_heartbeat(client):
    """봇 상태 파일이 없으면 '연결 끊김'이 아니라 '모름'이어야 한다."""
    rows = client.get("/api/status", headers=owner(client)).json()["workspaces"]
    assert all(r["connected"] is None for r in rows)


def test_connected_reads_bot_heartbeat(client, env):
    from tybot import heartbeat

    heartbeat.write(
        heartbeat.BotStatus(
            workspace="fin",
            connected=True,
            realtime=True,
            channels=7,
            uninvited_channels=2,
            spend_today_usd=0.62,
            limit_usd=2.0,
            started_at=heartbeat.now_iso(),
            updated_at=heartbeat.now_iso(),
        )
    )
    rows = client.get("/api/status", headers=owner(client)).json()["workspaces"]
    fin = next(r for r in rows if r["key"] == "fin")
    assert fin["connected"] is True
    assert fin["channels"] == 7
    assert fin["uninvitedChannels"] == 2
    assert fin["limitUsd"] == 2.0


def test_stale_heartbeat_is_not_trusted(client, env):
    """오래된 상태 파일을 그대로 믿으면 죽은 봇이 '연결됨'으로 보인다."""
    from tybot import heartbeat

    old = (datetime.now(KST) - timedelta(hours=2)).isoformat(timespec="seconds")
    heartbeat.write(
        heartbeat.BotStatus(
            workspace="fin",
            connected=True,
            realtime=True,
            channels=7,
            uninvited_channels=0,
            spend_today_usd=0,
            limit_usd=2.0,
            started_at=old,
            updated_at=old,
        )
    )
    rows = client.get("/api/status", headers=owner(client)).json()["workspaces"]
    fin = next(r for r in rows if r["key"] == "fin")
    assert fin["connected"] is None


def test_member_sees_only_own_workspace(client):
    rows = client.get("/api/status", headers=member(client)).json()["workspaces"]
    assert {r["key"] for r in rows} == {"fin"}


# --- 사용량 ---------------------------------------------------------------

def test_usage_totals(client):
    u = client.get("/api/usage", headers=owner(client)).json()
    assert u["callsToday"] == 2
    assert u["spentUsd"] == pytest.approx(0.056)
    assert u["limitUsd"] == 10.0
    assert {w["key"] for w in u["byWorkspace"]} == {"fin", "site-gimhae"}


def test_usage_never_returns_question_text(client):
    """질문·답변 본문은 감사기록에 있지만 화면으로 내려보내지 않는다."""
    body = client.get("/api/usage", headers=owner(client)).text
    assert "기성금 얼마야" not in body
    assert "3억 2천만원입니다" not in body
    assert "김수현" not in body


def test_usage_is_scoped_for_member(client):
    u = client.get("/api/usage", headers=member(client)).json()
    assert {w["key"] for w in u["byWorkspace"]} == {"fin"}
    assert u["callsToday"] == 1
    assert u["spentUsd"] == pytest.approx(0.021)


def test_usage_aggregates_are_scoped_too(client):
    """목록만 거르고 합계를 그대로 두면 다른 워크스페이스의 사용 패턴이 새어 나간다.

    자금팀 담당자에게는 현장 김해외동의 호출(10:05, $0.035)이 시간대별·모델별 어디에도
    섞이면 안 된다.
    """
    owner_view = client.get("/api/usage", headers=owner(client)).json()
    member_view = client.get("/api/usage", headers=member(client)).json()

    # 관리자에게는 두 워크스페이스가 모두 보인다 (대조군)
    assert owner_view["callsToday"] == 2
    assert {h["hour"] for h in owner_view["byHour"]} == {"09:00", "10:00"}

    # 담당자에게는 자기 호출 한 건만 남는다
    assert {h["hour"] for h in member_view["byHour"]} == {"09:00"}
    assert sum(h["calls"] for h in member_view["byHour"]) == 1
    assert sum(m["calls"] for m in member_view["byModel"]) == 1
    assert sum(m["costUsd"] for m in member_view["byModel"]) == pytest.approx(0.021)
    # 상한도 자기 워크스페이스 기준이어야 한다(전체 합산 상한을 그대로 주면 안 된다)
    assert member_view["limitUsd"] != owner_view["limitUsd"]


def test_usage_baseline_excludes_other_workspaces(client, env):
    """기준선(평소 대비 배수)도 자기 워크스페이스 기록에서만 나와야 한다."""
    import json as _json

    day = (datetime.now(KST) - timedelta(days=1)).date().isoformat()
    month = day[:7]
    path = env / "qa-log" / f"qa-{month}.jsonl"
    rows = [
        {
            "ts": f"{day}T08:00:00+09:00",
            "workspace": "site-gimhae",
            "intent_kind": "search",
            "intent_source": "llm",
            "reason": "answered",
            "hits": 1,
            "model": "claude-sonnet-5",
            "cost_usd": 5.0,  # 다른 워크스페이스의 큰 지출
            "elapsed_ms": 100,
        }
    ]
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(_json.dumps(r, ensure_ascii=False) + "\n")

    member_view = client.get("/api/usage", headers=member(client)).json()
    assert member_view["baselineUsd"] == 0.0  # 자금팀은 어제 기록이 없다
    owner_view = client.get("/api/usage", headers=owner(client)).json()
    assert owner_view["baselineUsd"] == pytest.approx(5.0)


# --- 수집 문서 ------------------------------------------------------------

def test_collected_list_has_no_content(client):
    docs = client.get("/api/collected", headers=owner(client)).json()["docs"]
    assert len(docs) == 3  # 정상 2건 + 형식 오류 1건
    assert all(d["content"] is None for d in docs)
    fin = next(d for d in docs if d["workspace"] == "fin")
    assert fin["lines"] == 3
    assert fin["attachmentLines"] == 1
    assert fin["shareWith"] == ["mgmt"]


def test_collected_list_is_scoped_for_member(client):
    docs = client.get("/api/collected", headers=member(client)).json()["docs"]
    assert {d["workspace"] for d in docs} == {"fin"}


def test_member_cannot_open_document_body(client):
    docs = client.get("/api/collected", headers=member(client)).json()["docs"]
    r = client.get(
        "/api/collected/content", params={"path": docs[0]["path"]}, headers=member(client)
    )
    assert r.status_code == 403
    assert "관리자만" in r.json()["detail"]


def test_owner_opens_document_and_it_is_recorded(client):
    docs = client.get("/api/collected", headers=owner(client)).json()["docs"]
    fin = next(d for d in docs if d["workspace"] == "fin")
    r = client.get("/api/collected/content", params={"path": fin["path"]}, headers=owner(client))
    assert r.status_code == 200
    assert "김해외동 3차 기성 청구서" in r.json()["content"]

    entries = client.get("/api/collected/audit", headers=owner(client)).json()["entries"]
    assert len(entries) == 1
    assert entries[0]["email"] == "dan@taeyoung.com"
    assert entries[0]["path"] == fin["path"]


@pytest.mark.parametrize(
    "bad",
    [
        "../../../etc/passwd",
        "..\\..\\.env",
        "channels/../../.env",
        "channels/fin/../../../secrets.md",
    ],
)
def test_path_traversal_is_refused(client, bad):
    """경로는 사용자가 보내는 값이다. 아카이브 밖 파일이 열리면 안 된다."""
    r = client.get("/api/collected/content", params={"path": bad}, headers=owner(client))
    assert r.status_code == 404


def test_member_cannot_read_audit(client):
    assert client.get("/api/collected/audit", headers=member(client)).status_code == 403


# --- 봇 규칙 문서 ----------------------------------------------------------

def test_harness_files_are_listed(client):
    files = client.get("/api/harness", headers=owner(client)).json()["files"]
    assert {f["workspace"] for f in files} == {"fin", "mgmt"}
    rules = next(f for f in files if f["workspace"] == "fin")
    assert rules["kind"] == "rules"
    assert rules["title"] == "답변 규칙"
    assert "금액은 원문 그대로" in rules["content"]


def test_harness_is_scoped_for_member(client):
    files = client.get("/api/harness", headers=member(client)).json()["files"]
    assert {f["workspace"] for f in files} == {"fin"}


# --- 환경변수 설정 ---------------------------------------------------------

def _write_headers(headers: dict) -> dict:
    return {**headers, "Origin": "http://testserver", "X-TYBot-CSRF": "1"}


def _env_payload(client, headers: dict) -> dict:
    data = client.get("/api/env-settings", headers=headers).json()
    return {
        "workspaces": data["workspaces"],
        "realtimeIngest": data["realtimeIngest"],
        "autojoinChannels": data["autojoinChannels"],
        "replyInThread": data["replyInThread"],
    }


def test_env_settings_are_owner_only(client):
    assert client.get("/api/env-settings", headers=member(client)).status_code == 403
    assert client.get("/api/env-settings", headers=owner(client)).status_code == 200


def test_env_settings_never_return_secrets(client, monkeypatch):
    monkeypatch.setenv("SLACK_BOT_TOKEN_FIN", "xoxb-secret-value")
    monkeypatch.setenv("SLACK_APP_TOKEN_FIN", "xapp-secret-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret-value")
    body = client.get("/api/env-settings", headers=owner(client)).text
    assert "xoxb-" not in body
    assert "xapp-" not in body
    assert "sk-ant-" not in body


def test_owner_saves_validated_env_overlay_and_restart_request(client, env):
    headers = owner(client)
    payload = _env_payload(client, headers)
    mgmt = next(row for row in payload["workspaces"] if row["key"] == "mgmt")
    mgmt["readable"] = ["fin"]
    payload["replyInThread"] = False

    response = client.put("/api/env-settings", json=payload, headers=_write_headers(headers))
    assert response.status_code == 200, response.text
    assert {"CROSS_WS_READ", "REPLY_IN_THREAD"} <= set(response.json()["changed"])

    managed = (env / "state" / "config" / "managed.env").read_text(encoding="utf-8")
    assert 'CROSS_WS_READ="mgmt:fin"' in managed
    assert 'REPLY_IN_THREAD="0"' in managed
    assert "SLACK_BOT_TOKEN" not in managed
    assert (env / "state" / "restart-request.json").is_file()

    audit = (env / "qa-log" / "env-settings.jsonl").read_text(encoding="utf-8")
    assert "CROSS_WS_READ" in audit
    assert "xoxb-" not in audit


def test_env_write_requires_owner_origin_and_csrf(client):
    owner_headers = owner(client)
    payload = _env_payload(client, owner_headers)
    assert client.put("/api/env-settings", json=payload, headers=owner_headers).status_code == 403
    assert (
        client.put(
            "/api/env-settings",
            json=payload,
            headers={**owner_headers, "Origin": "https://evil.example", "X-TYBot-CSRF": "1"},
        ).status_code
        == 403
    )
    assert (
        client.put(
            "/api/env-settings", json=payload, headers=_write_headers(member(client))
        ).status_code
        == 403
    )


def test_env_write_rejects_unknown_or_self_read_target(client, env):
    headers = owner(client)
    payload = _env_payload(client, headers)
    payload["workspaces"][0]["readable"] = [payload["workspaces"][0]["key"]]
    response = client.put("/api/env-settings", json=payload, headers=_write_headers(headers))
    assert response.status_code == 422
    assert not (env / "state" / "config" / "managed.env").exists()


def test_default_admin_cannot_write_env_settings(env):
    auth = Authenticator(accounts_spec="", secret="test-secret")
    console_app.app.dependency_overrides[console_app.authenticator] = lambda: auth
    c = TestClient(console_app.app)
    login = c.post("/api/login", json={"username": "admin", "password": "1111"})
    payload = _env_payload(c, {"Cookie": f"tybot_console={login.cookies.get('tybot_console')}"})
    response = c.put(
        "/api/env-settings",
        json=payload,
        headers={"Origin": "http://testserver", "X-TYBot-CSRF": "1"},
    )
    assert response.status_code == 403
    assert "임시" in response.json()["detail"]


# --- 임시 기본 계정 ---------------------------------------------------------

def test_default_account_opens_when_unconfigured():
    """`CONSOLE_ACCOUNTS` 가 없으면 임시 계정 admin/1111 로 들어갈 수 있다.

    파일럿에서 화면을 보기 위한 편의값이다. 운영에서 이 상태로 두면 VPN 에 들어온 누구나
    관리자가 되므로, 아래 테스트가 '기본값이 쓰이고 있다'는 표시가 함께 나오는지 확인한다.
    """
    auth = Authenticator(accounts_spec="", secret="test-secret")
    assert auth.using_default is True
    session = auth.login("admin", "1111")
    user = auth.verify(session)
    assert user.role == "owner"
    assert user.all_workspaces is True


def test_default_account_is_replaced_by_config():
    auth = Authenticator(accounts_spec=ACCOUNTS, secret="test-secret")
    assert auth.using_default is False
    with pytest.raises(Exception):
        auth.login("admin", "1111")


def test_me_flags_default_account(monkeypatch):
    """화면이 경고를 띄울 수 있게 `/api/me` 가 기본 계정 사용 여부를 알려 준다."""
    monkeypatch.delenv("CONSOLE_ACCOUNTS", raising=False)
    console_app.reset_state()
    console_app.app.dependency_overrides.clear()
    c = TestClient(console_app.app)
    r = c.post("/api/login", json={"username": "admin", "password": "1111"})
    assert r.status_code == 200
    assert r.json()["usingDefaultAccount"] is True
    console_app.reset_state()


def test_session_expires(monkeypatch):
    """만료된 세션은 거절한다."""
    import time as _time

    auth = Authenticator(accounts_spec=ACCOUNTS, secret="test-secret")
    session = auth.issue("dan", now=_time.time() - 13 * 3600)  # 12시간 유효, 13시간 전 발급
    with pytest.raises(Exception) as err:
        auth.verify(session)
    assert "만료" in str(err.value)


def test_session_from_other_secret_is_rejected():
    """다른 키로 서명한 세션은 통하지 않는다."""
    mine = Authenticator(accounts_spec=ACCOUNTS, secret="secret-a")
    theirs = Authenticator(accounts_spec=ACCOUNTS, secret="secret-b")
    with pytest.raises(Exception):
        mine.verify(theirs.issue("dan"))
