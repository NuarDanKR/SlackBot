"""Archiver supervisor 라우트 — **화면이 보는 것과 막는 것.**

결정: 2026-09-29 작업지시서 §3.3 · §7-6.

라우트가 지켜야 하는 것 넷.

1. **admin 만** 본다. 이 화면은 수집을 켜고 끄고 소급을 건다
2. 사유 없이는 요청 자체가 안 만들어진다(422)
3. 규칙이 막은 것은 **422** 다. 500 이면 화면이 「서버 고장」 이라고 말한다
4. 응답이 **저장은 적용이 아니라는 사실**을 들고 온다
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

pytest.importorskip("httpx", reason="fastapi TestClient 가 httpx 를 쓴다")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fastapi.testclient import TestClient
from test_console_api import env, guest, owner  # noqa: F401
from test_supervisor_admin import FakeRepo, _job, _runtime

from tybot.console import app as console_app
from tybot.console import supervisor_admin

BASE = "/api/workspaces/tyit/archiver"
CSRF = {"X-TYBot-CSRF": "1", "Origin": "http://testserver"}


@pytest.fixture
def client(request):
    request.getfixturevalue("env")
    return TestClient(console_app.app)


@pytest.fixture
def repo(monkeypatch) -> FakeRepo:
    """진짜 저장소 대신 가짜를 물린다. **DB 없이 라우트를 전부 본다.**"""
    fake = FakeRepo(_runtime())
    monkeypatch.setattr(supervisor_admin, "default_repo", lambda: fake)
    # 감사도 같은 가짜가 받는다. 진짜 저장소를 부르면 DB 없이 못 돈다.
    monkeypatch.setattr(
        "tybot.console.archiving_repo.default_repo", lambda: fake,
    )
    return fake


# --- 권한 -----------------------------------------------------------------------

def test_a_guest_cannot_see_the_runtime(client, repo):
    assert client.get(f"{BASE}/runtime", headers=guest(client)).status_code == 403


def test_a_guest_cannot_change_the_mode(client, repo):
    headers = {**guest(client), **CSRF}

    response = client.put(f"{BASE}/desired", json={"mode": "off", "reason": "x"},
                          headers=headers)

    assert response.status_code == 403
    assert repo.runtime("tyit")["desired_mode"] == "shadow"


def test_a_guest_cannot_queue_a_backfill(client, repo):
    headers = {**guest(client), **CSRF}

    response = client.post(f"{BASE}/backfill", json={"reason": "x"}, headers=headers)

    assert response.status_code == 403
    assert repo.jobs("tyit") == []


# --- 읽기 -----------------------------------------------------------------------

def test_the_owner_sees_the_runtime(client, repo):
    body = client.get(f"{BASE}/runtime", headers=owner(client)).json()

    assert body["workspace"] == "tyit"
    assert body["desiredMode"] == "shadow"
    assert body["gatedModes"] == ["live"]


def test_every_read_says_saving_is_not_applying(client, repo):
    """화면이 성공처럼 보이는 표시를 하지 않게, 읽기가 그 문장을 들고 온다."""
    body = client.get(f"{BASE}/runtime", headers=owner(client)).json()

    assert "supervisor" in body["runtimeEffect"]


# --- 희망 상태 -------------------------------------------------------------------

def test_a_mode_change_without_a_reason_never_reaches_the_rules(client, repo):
    headers = {**owner(client), **CSRF}

    response = client.put(f"{BASE}/desired", json={"mode": "off"}, headers=headers)

    assert response.status_code == 422
    assert repo.runtime("tyit")["desired_mode"] == "shadow"


def test_live_is_refused_with_422_not_500(client, repo):
    """고칠 수 없는 고장이 아니라 **지금은 안 되는 것**이다."""
    headers = {**owner(client), **CSRF}

    response = client.put(f"{BASE}/desired", json={"mode": "live", "reason": "전환"},
                          headers=headers)

    assert response.status_code == 422
    assert "파일럿" in response.json()["detail"]


def test_pausing_is_recorded(client, repo):
    headers = {**owner(client), **CSRF}

    body = client.put(f"{BASE}/desired", json={"mode": "off", "reason": "소급 검증"},
                      headers=headers).json()

    assert body["desiredMode"] == "off"
    assert body["generation"] == 4


def test_a_write_without_the_origin_header_is_refused(client, repo):
    """CSRF 규약을 이 화면만 예외로 두지 않는다."""
    headers = {**owner(client), "X-TYBot-CSRF": "1"}

    response = client.put(f"{BASE}/desired", json={"mode": "off", "reason": "x"},
                          headers=headers)

    assert response.status_code == 403
    assert repo.runtime("tyit")["desired_mode"] == "shadow"


# --- 소급 -----------------------------------------------------------------------

def test_a_preview_is_queued(client, repo):
    headers = {**owner(client), **CSRF}

    body = client.post(f"{BASE}/backfill", headers=headers, json={
        "channelId": "C1", "fromTs": "1000.000100", "toTs": "2000.000100",
        "reason": "누락 확인",
    }).json()

    assert body["job"]["state"] == "queued"
    assert body["job"]["dry_run"] is True


def test_a_real_run_without_a_preview_is_refused_with_422(client, repo):
    headers = {**owner(client), **CSRF}

    response = client.post(f"{BASE}/backfill", headers=headers, json={
        "channelId": "C1", "fromTs": "1000.000100", "toTs": "2000.000100",
        "dryRun": False, "reason": "복구",
    })

    assert response.status_code == 422
    assert repo.jobs("tyit") == []


def test_a_bad_range_is_refused_with_422(client, repo):
    headers = {**owner(client), **CSRF}

    response = client.post(f"{BASE}/backfill", headers=headers, json={
        "fromTs": "2026-09-01", "reason": "복구",
    })

    assert response.status_code == 422
    assert "Slack ts" in response.json()["detail"]


def test_a_queued_job_can_be_taken_back(client, repo):
    repo._jobs.append(_job(id=7, state="queued"))
    headers = {**owner(client), **CSRF}

    response = client.put(f"{BASE}/backfill/7/cancel", json={"reason": "잘못 걸었음"},
                          headers=headers)

    assert response.status_code == 200
    assert repo.job(7)["state"] == "cancelled"


def test_a_running_job_cancel_is_refused_with_422(client, repo):
    repo._jobs.append(_job(id=8, state="running"))
    headers = {**owner(client), **CSRF}

    response = client.put(f"{BASE}/backfill/8/cancel", json={"reason": "멈춰"},
                          headers=headers)

    assert response.status_code == 422
    assert repo.job(8)["state"] == "running"


# --- 이 화면이 하지 않는 것 -------------------------------------------------------

def test_the_console_never_runs_shell_or_systemctl():
    """FastAPI 프로세스에서 임의 shell 이나 systemctl 을 부르지 않는다(§8).

    라우트가 하는 일은 적는 것이고, supervisor 가 그것을 읽어 움직인다.
    """
    source = (Path(__file__).resolve().parent.parent / "src" / "tybot" / "console"
              / "supervisor_admin.py").read_text(encoding="utf-8")

    for forbidden in ("subprocess", "systemctl", "os.system", "WebClient",
                      "slack_sdk"):
        assert forbidden not in source, forbidden


def test_the_repo_never_writes_the_observed_columns():
    """관측 열은 supervisor 몫이다. 콘솔이 쓰면 사람이 끈 것이 되살아난다."""
    source = (Path(__file__).resolve().parent.parent / "src" / "tybot" / "console"
              / "supervisor_repo.py").read_text(encoding="utf-8")
    # Protocol 선언이 앞에 한 번 더 있다. 구현부를 본다.
    writes = source[source.rindex("def save_desired"):source.rindex("def cursors")]

    assert "desired_mode" in writes
    for column in ("observed_state", "heartbeat_at", "last_event_at", "last_write_at"):
        assert column not in writes, column


def test_every_column_the_repo_reads_exists_in_the_schema():
    """질의와 표가 어긋나면 화면은 500 만 보여 주고 사람은 원인을 못 찾는다.

    격리 DB 검증이 이걸 잡지만, 그건 서버에서 돈다. 여기서 먼저 걸리면 커밋 전에
    안다.
    """
    root = Path(__file__).resolve().parent.parent
    schema = (root / "deploy" / "sql" / "archiver_supervisor_schema.sql").read_text(
        encoding="utf-8"
    )
    source = (root / "src" / "tybot" / "console" / "supervisor_repo.py").read_text(
        encoding="utf-8"
    )
    for table in ("archiver_workspace_runtime", "archive_channel_cursor",
                  "archive_backfill_job"):
        known = _schema_columns(schema, table)
        assert known, table
        for used in _selected_columns(source, table):
            assert used in known, f"{table}.{used}"


SELECTS = re.compile(r"SELECT\s+(.+?)\s+FROM\s+(\w+)", re.S)


def _schema_columns(schema: str, table: str) -> set[str]:
    block = schema[schema.index(f"CREATE TABLE IF NOT EXISTS {table} ("):]
    block = block[:block.index(chr(10) + ");")]
    return {
        line.strip().split()[0]
        for line in block.splitlines()[1:]
        if line.strip() and not line.strip().startswith(("--", "PRIMARY", "CHECK"))
    }


def _selected_columns(source: str, table: str) -> set[str]:
    """`SELECT ... FROM <table>` 의 열 이름. 함수·별칭은 쓰지 않는다."""
    columns: set[str] = set()
    # 질의 하나씩 본다. 원문 전체에 정규식을 걸면 FROM 이 없는 잠금 질의가
    # **다음 질의의 FROM 까지** 삼키고, 그러면 그 질의를 한 번도 안 보게 된다.
    blocks = source.split(chr(34) * 3)
    for body, found in [m for block in blocks for m in SELECTS.findall(block)]:
        if found != table:
            continue
        for name in body.split(","):
            token = name.strip()
            if token and token.replace("_", "").isalnum():
                columns.add(token)
    return columns
