from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from tybot.console import app as console_app
from tybot.console import image_description_admin as admin
from tybot.console.auth import ConsoleUser

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


class Cursor:
    def __init__(self, queries):
        self.queries = queries
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))
        if "clock_timestamp() AS generated_at" in sql:
            self.rows = [{"generated_at": NOW}]
        elif "FROM archiver_image_model_catalog" in sql:
            self.rows = [{
                "provider": "free-provider",
                "model": "free-vision",
                "supports_vision": True,
                "zero_cost_verified": True,
                "verified_at": NOW,
                "verification_revision": "catalog-1",
                "max_sensitivity": "confidential",
                "allowed_regions": ["kr"],
                "credential_ready": True,
                "quota_state": "available",
                "quota_remaining": 9,
                "quota_resets_at": NOW,
                "updated_at": NOW,
            }]
        elif "FROM archiver_image_description_job" in sql:
            self.rows = [{
                "id": "job-1",
                "state": "blocked",
                "error_code": "quota_unknown",
                "policy_revision": "g:w",
                "prompt_revision": "prompt-1",
                "channel_id": "C12345678",
                "file_id": "F12345678",
                "original_sha256_mask": "a" * 12,
                "actual_model": None,
                "actual_provider": None,
                "input_tokens": None,
                "output_tokens": None,
                "actual_usd": None,
                "created_at": NOW,
                "dispatched_at": None,
                "finished_at": NOW,
                "expires_at": NOW,
            }]
        elif "active_approvals" in sql:
            self.rows = [{"active_approvals": 2, "active_clearances": 1}]
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class Connection:
    def __init__(self):
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return Cursor(self.queries)


class Store:
    def __init__(self, _connection):
        pass

    def effective_policy(self, workspace):
        assert workspace == "tyit"
        policy = SimpleNamespace(
            revision="g:w",
            enabled=True,
            workspace_enabled=False,
            provider="free-provider",
            model="free-vision",
            region="kr",
            sensitivity=SimpleNamespace(value="confidential"),
            max_image_bytes=1024,
            max_output_tokens=100,
            timeout_seconds=10,
            catalog_verification_ttl_seconds=3600,
        )
        return policy, {"model": "free-vision"}, {"model": "workspace"}


def test_snapshot_contains_status_but_no_body_or_secret_columns(monkeypatch):
    connection = Connection()
    monkeypatch.setattr(admin, "ImageDescriptionStore", Store)

    result = admin.snapshot(" TYIT ", connect=lambda: connection)

    assert result["policy"]["effectiveEnabled"] is False
    assert result["policy"]["workerAppliedRevision"] is None
    assert result["counts"] == {"activeApprovals": 2, "activeClearances": 1}
    assert result["jobs"][0]["originalSha256Mask"] == "a" * 12
    assert result["jobs"][0]["actualUsd"] is None
    assert result["catalog"][0]["zeroCostVerified"] is True
    rendered = repr(result).lower()
    for forbidden in ("description", "policy_snapshot", "catalog_snapshot", "reason"):
        assert forbidden not in rendered

    sql = "\n".join(query for query, _params in connection.queries).lower()
    assert "archiver_image_description_revision" not in sql
    assert "policy_snapshot" not in sql
    assert "catalog_snapshot" not in sql
    assert "approved_by" not in sql
    assert "assessed_by" not in sql


def test_an_untrusted_error_code_is_not_returned(monkeypatch):
    connection = Connection()
    original_execute = Cursor.execute

    def unsafe_execute(self, sql, params=None):
        original_execute(self, sql, params)
        if "FROM archiver_image_description_job" in sql:
            self.rows[0]["error_code"] = "private provider body"

    monkeypatch.setattr(Cursor, "execute", unsafe_execute)
    monkeypatch.setattr(admin, "ImageDescriptionStore", Store)

    result = admin.snapshot("tyit", connect=lambda: connection)

    assert result["jobs"][0]["errorCode"] == "status_unavailable"


def test_missing_dedicated_database_is_not_reported_as_empty(monkeypatch):
    monkeypatch.delenv("TYBOT_VISION_DATABASE_URL", raising=False)

    with pytest.raises(admin.ImageDescriptionAdminError, match="unconfigured"):
        admin.snapshot("tyit")


def _user(*, role="admin", workspaces=("tyit",), all_workspaces=False):
    return ConsoleUser(
        email="operator@taeyoung.com",
        name="운영자",
        role=role,
        workspaces=frozenset(workspaces),
        all_workspaces=all_workspaces,
    )


def test_the_read_route_is_registered_and_admin_scoped(monkeypatch):
    paths = {route.path for route in console_app.app.routes if hasattr(route, "path")}
    assert "/api/archiver/image-description/policy" in paths
    seen = []
    monkeypatch.setattr(
        admin,
        "snapshot",
        lambda workspace, limit: seen.append((workspace, limit)) or {"workspace": workspace},
    )

    result = console_app.get_image_description_policy(" TYIT ", _user(), 25)

    assert result == {"workspace": "tyit"}
    assert seen == [("tyit", 25)]


def test_the_read_route_rejects_non_admin_and_out_of_scope(monkeypatch):
    monkeypatch.setattr(
        admin, "snapshot", lambda *_a, **_k: pytest.fail("must not query")
    )

    with pytest.raises(HTTPException) as guest:
        console_app.get_image_description_policy("tyit", _user(role="guest"), 50)
    assert guest.value.status_code == 403

    with pytest.raises(HTTPException) as scoped:
        console_app.get_image_description_policy(
            "tyit", _user(workspaces=("mgmt",)), 50
        )
    assert scoped.value.status_code == 403


def test_the_read_route_maps_an_unavailable_database_to_503(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise admin.ImageDescriptionAdminError("vision_database_unconfigured")

    monkeypatch.setattr(admin, "snapshot", unavailable)

    with pytest.raises(HTTPException) as error:
        console_app.get_image_description_policy("tyit", _user(), 50)
    assert error.value.status_code == 503
    assert error.value.detail == "vision_database_unconfigured"



# --- B-100 Slack 상태 댓글 -----------------------------------------------------------


def test_snapshot_lists_status_comments_without_names_or_bodies(monkeypatch):
    from tybot.archive import image_status_notice as notices

    repo = notices.MemoryNoticeRepo()
    key = notices.NoticeKey("tyit", "C12345678", "1794000000.000100", "F12345678")
    repo.record(notices.NoticeRow(
        key=key, phase=notices.COMPLETED,
        payload={"file": "현장사진.png", "description": "비밀 본문", "transcript": ["안전제일"]},
    ))
    repo.record(notices.NoticeRow(key=key, phase=notices.ACCEPTED))  # 실시간 접수
    repo.mark_failed(repo.rows[key], "not_member")
    monkeypatch.setattr(admin, "ImageDescriptionStore", Store)
    monkeypatch.setattr(notices, "PostgresNoticeRepo", lambda _conn: repo)

    result = admin.snapshot("tyit", connect=Connection)

    (row,) = result["notices"]
    assert row["phase"] == "completed" and row["deliveryState"] == "failed_delivery"
    assert row["errorCode"] == "not_member" and row["resendable"] is True
    rendered = repr(result["notices"])
    assert "현장사진" not in rendered and "비밀 본문" not in rendered and "안전제일" not in rendered
    assert result["noticesError"] == ""


def test_snapshot_still_works_before_the_status_table_exists(monkeypatch):
    monkeypatch.setattr(admin, "ImageDescriptionStore", Store)

    result = admin.snapshot("tyit", connect=Connection)

    assert result["notices"] == []
    assert result["noticesError"] == "notice_status_unavailable"
    assert result["jobs"]  # 나머지 현황은 그대로 보인다


def test_resend_route_is_admin_scoped_and_audited(monkeypatch):
    paths = {route.path for route in console_app.app.routes if hasattr(route, "path")}
    assert "/api/archiver/image-description/notices/resend" in paths
    calls, audits = [], []
    monkeypatch.setattr(console_app, "_check_write_request", lambda _request: None)
    monkeypatch.setattr(console_app, "_audit_event", lambda **kw: audits.append(kw))
    monkeypatch.setattr(admin, "resend_notice", lambda *a: calls.append(a) or True)
    body = console_app.ImageNoticeResendBody(
        workspace=" TYIT ", channelId="C12345678", messageTs="1794000000.000100",
        fileId="F12345678",
    )

    assert console_app.post_image_notice_resend(body, object(), _user()) == {"queued": True}
    assert calls == [("tyit", "C12345678", "1794000000.000100", "F12345678")]
    assert audits[0]["action"] == "image_notice_resend"

    with pytest.raises(HTTPException) as scoped:
        console_app.post_image_notice_resend(body, object(), _user(workspaces=("mgmt",)))
    assert scoped.value.status_code == 403

    monkeypatch.setattr(admin, "resend_notice", lambda *a: False)
    with pytest.raises(HTTPException) as nothing:
        console_app.post_image_notice_resend(body, object(), _user())
    assert nothing.value.status_code == 409


def test_resend_rejects_malformed_coordinates():
    with pytest.raises(admin.ImageDescriptionAdminError, match="invalid_request"):
        admin.resend_notice("tyit", "C12345678", "not-a-ts", "F12345678", connect=Connection)
