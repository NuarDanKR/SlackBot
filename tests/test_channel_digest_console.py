"""콘솔 「검색 다이제스트」 API (B-93 2단계 §7-5).

거는 것: 본문을 내보내지 않는다 · 분량을 센 뒤에만 실행 · 미리보기 뒤 분량이 늘면 거절 ·
근거 채널 밖은 고를 수 없다 · 전체 관리자만 실행 · 실행은 감사 기록에 남는다.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from tybot import channel_digest_batch as batch
from tybot.console import channel_digest_routes as routes
from tybot.console.auth import ConsoleUser

ADMIN = ConsoleUser(email="a@x", name="a", role="admin", all_workspaces=True)
PART = ConsoleUser(email="p@x", name="p", role="admin", workspaces=frozenset({"tyit"}))


def _client(user, *, calls=3, monkeypatch):
    launched, audits = [], []
    monkeypatch.setattr(batch, "source_channels", lambda conn, bot: [("mgmt", "C0PLAN0001")])
    monkeypatch.setattr(routes, "_archive", lambda: "/archive")

    def fake_plan(conn, root, picked, *, since, until, model=batch.DIGEST_MODEL):
        p = batch.Plan(channels=len(picked), days_seen=5, unchanged=5 - calls)
        p.todo = [object()] * calls
        p.input_chars = 3000
        return p

    monkeypatch.setattr(batch, "plan", fake_plan)

    @contextmanager
    def connect():
        yield object()

    def require_admin(u):
        if u.role != "admin":
            raise HTTPException(status_code=403)

    app = FastAPI()
    app.include_router(routes.build_router(
        current_user=lambda: user, require_admin=require_admin,
        check_write_request=lambda request: None,
        audit_event=lambda **kw: audits.append(kw),
        connect=connect, launch=lambda args: launched.append(args) or 4242,
    ))
    return TestClient(app), launched, audits


BODY = {"channels": ["C0PLAN0001"], "since": "2026-10-01", "until": "2026-11-11"}


def test_preview_counts_without_launching(monkeypatch):
    client, launched, _ = _client(ADMIN, monkeypatch=monkeypatch)
    r = client.post("/api/channel-digest/preview", json=BODY)
    assert r.status_code == 200 and r.json()["calls"] == 3
    assert launched == []


def test_run_launches_the_batch_with_the_exact_scope_and_audits(monkeypatch):
    client, launched, audits = _client(ADMIN, monkeypatch=monkeypatch)
    r = client.post("/api/channel-digest/run", json={**BODY, "expectedCalls": 3, "maxCalls": 3, "reason": "소급"})
    assert r.status_code == 200 and r.json()["started"] is True
    (args,) = launched
    assert args[:6] == ["--apply", "--since", "2026-10-01", "--until", "2026-11-11", "--max-calls"]
    assert args[-2:] == ["--channel", "C0PLAN0001"]
    (audit,) = audits
    assert audit["action"] == "channel_digest_backfill" and audit["metadata"]["calls"] == 3


def test_run_refuses_when_the_count_grew_after_preview(monkeypatch):
    client, launched, _ = _client(ADMIN, calls=5, monkeypatch=monkeypatch)
    r = client.post("/api/channel-digest/run", json={**BODY, "expectedCalls": 3, "maxCalls": 3, "reason": "x"})
    assert r.status_code == 409 and launched == []


def test_channels_outside_the_source_list_are_rejected(monkeypatch):
    client, _, _ = _client(ADMIN, monkeypatch=monkeypatch)
    r = client.post("/api/channel-digest/preview", json={**BODY, "channels": ["C0OTHER001"]})
    assert r.status_code == 422


def test_partial_admins_cannot_run_or_preview_other_workspaces(monkeypatch):
    client, launched, _ = _client(PART, monkeypatch=monkeypatch)
    assert client.post("/api/channel-digest/preview", json=BODY).status_code == 403
    r = client.post("/api/channel-digest/run", json={**BODY, "expectedCalls": 3, "maxCalls": 3, "reason": "x"})
    assert r.status_code == 403 and launched == []


@pytest.mark.parametrize("bad", [
    {"since": "2026-11-12", "until": "2026-11-01"},
    {"since": "2024-01-01", "until": "2026-11-01"},
])
def test_bad_ranges_are_rejected(monkeypatch, bad):
    client, _, _ = _client(ADMIN, monkeypatch=monkeypatch)
    assert client.post("/api/channel-digest/preview", json={**BODY, **bad}).status_code == 422


def test_status_never_returns_digest_text():
    class Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params):
            self.sql = sql

        def fetchall(self):
            return [{"workspace": "mgmt", "channel_id": "C0PLAN0001", "channel_name": "#x",
                     "current": 3, "stale": 1, "latest_day": date(2026, 11, 11), "latest_created": None}]

    cur = Cur()
    rows = routes.status(type("C", (), {"cursor": lambda self: cur})())
    assert "body" not in cur.sql and "terms" not in cur.sql
    assert rows[0]["current"] == 3 and rows[0]["latestDay"] == "2026-11-11"


def test_the_menu_and_route_exist():
    from pathlib import Path

    app_tsx = (Path(__file__).resolve().parents[1] / "console-web/src/App.tsx").read_text(encoding="utf-8")
    assert "'/manage/channel-digest'" in app_tsx and "<ChannelDigestPage" in app_tsx
