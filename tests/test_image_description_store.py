from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from tybot.archive.image_description import (
    PROMPT_REVISION,
    STATUS_DESCRIBED,
    STATUS_UNCERTAIN,
    ImageDescription,
    ImageUsage,
)
from tybot.archive.image_description_clearance import SafetyAssessment
from tybot.archive.image_description_policy import ImageDescriptionDispatch
from tybot.archive.image_description_store import (
    ImageDescriptionDenied,
    ImageDescriptionStore,
    _catalog_snapshot,
    dispatch_identity,
)
from tybot.archive.image_model_catalog import FreeQuotaState, ImageModelCatalogEntry
from tybot.gateway.base import Sensitivity

NOW = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)
SCOPE = {
    "workspace": "tyit",
    "channel": "C12345678",
    "file_id": "F12345678",
    "original_sha256": "a" * 64,
    "policy_revision": "global:local",
    "prompt_revision": PROMPT_REVISION,
    "model": "free-vision-v1",
    "provider": "approved-provider",
    "image_sha256": "b" * 64,
}
JOB = {
    "id": "job-1",
    "identity_sha256": dispatch_identity(SCOPE),
    "approval_id": "approval-1",
    "workspace": "tyit",
    "policy_revision": "global:local",
    "prompt_revision": PROMPT_REVISION,
    "scope": SCOPE,
    "catalog_snapshot": {"model": "free-vision-v1", "provider": "approved-provider"},
    "max_output_tokens": 256,
    "state": "dispatching",
    "expires_at": NOW + timedelta(minutes=10),
}
GLOBAL = {"workspace": "", "revision": "global", "enabled": True, "config": {}}


def _assessment(**changes) -> SafetyAssessment:
    values = {
        "workspace": SCOPE["workspace"],
        "channel_id": SCOPE["channel"],
        "file_id": SCOPE["file_id"],
        "original_sha256": SCOPE["original_sha256"],
        "provider": SCOPE["provider"],
        "model": SCOPE["model"],
        "policy_revision": SCOPE["policy_revision"],
        "visibility": "private",
        "acl": frozenset({"U2", "U1"}),
        "assessed_by": "reviewer",
        "assessed_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "pii_cleared": True,
    }
    values.update(changes)
    return SafetyAssessment(**values)


class ScriptedConnection:
    autocommit = True
    info = SimpleNamespace(transaction_status=0)

    def __init__(self, replies):
        self.replies = deque(replies)
        self.events = []

    @contextmanager
    def transaction(self):
        self.events.append("begin")
        try:
            yield
        except Exception:
            self.events.append("rollback")
            raise
        else:
            self.events.append("commit")

    @contextmanager
    def cursor(self):
        yield self

    def execute(self, sql, args=None):
        self.events.append((sql, args))

    def fetchone(self):
        return self.replies.popleft()

    def fetchall(self):
        return self.replies.popleft()


def _result(**changes) -> ImageDescription:
    result = ImageDescription(
        status=STATUS_DESCRIBED,
        workspace=SCOPE["workspace"],
        channel=SCOPE["channel"],
        file_id=SCOPE["file_id"],
        original_sha256=SCOPE["original_sha256"],
        prompt_revision=PROMPT_REVISION,
        description="건설현장 외부에 굴착기와 자재가 보인다.",
        tags=("건설현장", "굴착기"),
        usage=ImageUsage(
            model="free-vision-v1",
            provider="approved-provider",
            input_tokens=20,
            output_tokens=10,
            cost_usd=0.0,
        ),
    )
    return replace(result, **changes)


def _updates(connection: ScriptedConnection):
    return [
        event
        for event in connection.events
        if isinstance(event, tuple) and event[0].lstrip().startswith("UPDATE")
    ]


def _inserts(connection: ScriptedConnection):
    return [
        event
        for event in connection.events
        if isinstance(event, tuple) and event[0].lstrip().startswith("INSERT")
    ]


def test_dispatch_identity_ignores_only_a_policy_resave() -> None:
    assert dispatch_identity(SCOPE) == dispatch_identity(SCOPE | {"policy_revision": "new"})
    for key, value in (
        ("workspace", "mgmt"),
        ("image_sha256", "c" * 64),
        ("prompt_revision", "new-prompt"),
        ("model", "other"),
    ):
        assert dispatch_identity(SCOPE) != dispatch_identity(SCOPE | {key: value})


def test_missing_fresh_checks_are_fail_closed() -> None:
    assert ImageDescriptionStore._checks({}) == {
        "source_verified": False,
        "access_verified": False,
        "pii_blocked": True,
        "bot_generated": True,
    }


def test_indexed_sources_include_withheld_revisions_and_can_be_channel_scoped() -> None:
    rows = [
        {"workspace": "tyit", "channel_id": "C1", "original_sha256": "a" * 64},
        {"workspace": "tyit", "channel_id": "C1", "original_sha256": "b" * 64},
    ]
    connection = ScriptedConnection([rows])

    got = ImageDescriptionStore(connection).indexed_sources("tyit", channel_id="C1")

    assert got == frozenset(
        {("tyit", "C1", "a" * 64), ("tyit", "C1", "b" * 64)}
    )
    query = next(event for event in connection.events if isinstance(event, tuple))
    assert "archiver_image_description_revision" in query[0]
    assert "status" not in query[0]
    assert query[1] == ("tyit", "C1")


def test_indexed_sources_require_a_workspace() -> None:
    with pytest.raises(ImageDescriptionDenied, match="workspace_required"):
        ImageDescriptionStore(ScriptedConnection([])).indexed_sources("")


def test_catalog_snapshot_keeps_regions_and_times_structured() -> None:
    entry = ImageModelCatalogEntry(
        model="free-vision-v1",
        provider="approved-provider",
        supports_vision=True,
        zero_cost_verified=True,
        verified_at=NOW,
        verification_revision="catalog-1",
        max_sensitivity=Sensitivity.INTERNAL,
        allowed_regions=frozenset({"kr", "jp"}),
        credential_ready=True,
        quota_state=FreeQuotaState.AVAILABLE,
        quota_remaining=10,
        quota_resets_at=NOW + timedelta(hours=1),
    )
    snapshot = _catalog_snapshot(entry)
    assert snapshot["allowed_regions"] == ["jp", "kr"]
    assert snapshot["verified_at"] == NOW.isoformat()
    assert snapshot["quota_state"] == "available"


def test_receipt_is_bound_to_the_saved_scope() -> None:
    receipt = ImageDescriptionStore._receipt(JOB | {"state": "reserved"})
    assert receipt == ImageDescriptionDispatch(
        dispatch_id="job-1",
        approval_id="approval-1",
        max_output_tokens=256,
        expires_at=JOB["expires_at"],
        active=True,
        **SCOPE,
    )


def test_clearance_is_appended_with_a_stable_acl_and_authenticated_actor() -> None:
    connection = ScriptedConnection([GLOBAL])
    ImageDescriptionStore(connection).save_clearance(
        "clearance-1",
        _assessment(),
        reason="사진을 직접 확인함",
        actor="reviewer",
    )

    insert = next(
        event for event in _inserts(connection)
        if "archiver_image_description_clearance" in event[0]
    )
    assert insert[1][0] == "clearance-1"
    assert insert[1][9] == ["U1", "U2"]
    assert insert[1][11] == "reviewer"
    assert insert[1][14] == "사진을 직접 확인함"


@pytest.mark.parametrize(
    "assessment,actor,reason",
    [
        (_assessment(assessed_by="someone-else"), "reviewer", "확인"),
        (_assessment(acl=frozenset()), "reviewer", "확인"),
        (_assessment(original_sha256="A" * 64), "reviewer", "확인"),
        (_assessment(), "", "확인"),
        (_assessment(), "reviewer", " "),
    ],
)
def test_invalid_clearance_never_reaches_the_database(assessment, actor, reason) -> None:
    connection = ScriptedConnection([])
    with pytest.raises(ImageDescriptionDenied, match="clearance_invalid"):
        ImageDescriptionStore(connection).save_clearance(
            "clearance-1", assessment, reason=reason, actor=actor
        )
    assert connection.events == []


def test_only_an_exact_active_clearance_is_loaded() -> None:
    row = {
        "workspace": SCOPE["workspace"],
        "channel_id": SCOPE["channel"],
        "file_id": SCOPE["file_id"],
        "original_sha256": SCOPE["original_sha256"],
        "provider": SCOPE["provider"],
        "model": SCOPE["model"],
        "policy_revision": SCOPE["policy_revision"],
        "visibility": "private",
        "acl": ["U1", "U2"],
        "assessed_by": "reviewer",
        "assessed_at": NOW,
        "expires_at": NOW + timedelta(hours=1),
        "pii_cleared": True,
    }
    connection = ScriptedConnection([GLOBAL, row])
    found = ImageDescriptionStore(connection).active_clearance(
        workspace=SCOPE["workspace"],
        channel_id=SCOPE["channel"],
        file_id=SCOPE["file_id"],
        original_sha256=SCOPE["original_sha256"],
        provider=SCOPE["provider"],
        model=SCOPE["model"],
        policy_revision=SCOPE["policy_revision"],
    )

    assert found == _assessment()
    select = next(
        event for event in connection.events
        if isinstance(event, tuple) and "image_description_clearance" in event[0]
    )
    assert "revoked_at IS NULL" in select[0]
    assert "expires_at > clock_timestamp()" in select[0]
    assert select[1] == (
        SCOPE["workspace"], SCOPE["channel"], SCOPE["file_id"],
        SCOPE["original_sha256"], SCOPE["provider"], SCOPE["model"],
        SCOPE["policy_revision"],
    )


def test_missing_active_clearance_is_not_guessed() -> None:
    connection = ScriptedConnection([GLOBAL, None])
    assert ImageDescriptionStore(connection).active_clearance(
        workspace="tyit", channel_id="C12345678", file_id="F12345678",
        original_sha256="a" * 64, provider="p", model="m", policy_revision="g:l",
    ) is None


def test_clearance_revocation_needs_an_actor_and_reason() -> None:
    connection = ScriptedConnection([GLOBAL, {"id": "clearance-1"}])
    ImageDescriptionStore(connection).revoke_clearance(
        "clearance-1", actor="reviewer", reason="승인 철회"
    )
    update = _updates(connection)[0]
    assert update[1] == ("reviewer", "승인 철회", "clearance-1")
    assert "revoked_at IS NULL" in update[0]


def test_an_inactive_clearance_cannot_be_revoked_twice() -> None:
    connection = ScriptedConnection([GLOBAL, None])
    with pytest.raises(ImageDescriptionDenied, match="clearance_not_active"):
        ImageDescriptionStore(connection).revoke_clearance(
            "clearance-1", actor="reviewer", reason="승인 철회"
        )


def test_safe_description_is_published_only_after_the_guard() -> None:
    connection = ScriptedConnection([GLOBAL, JOB])
    revision = ImageDescriptionStore(connection).finish_and_publish("job-1", _result())
    assert revision
    assert connection.events[-1] == "commit"
    insert = _inserts(connection)[0]
    assert "archiver_image_description_revision" in insert[0]
    assert insert[1][12] == "allow"
    assert insert[1][14].startswith("건설현장")
    update = _updates(connection)[0]
    assert update[1][0] == "described"
    assert update[1][1] == ""


def test_sensitive_uncertainty_keeps_tags_but_withholds_description() -> None:
    connection = ScriptedConnection([GLOBAL, JOB])
    result = _result(
        status=STATUS_UNCERTAIN,
        uncertainty=("person_visible",),
    )
    revision = ImageDescriptionStore(connection).finish_and_publish("job-1", result)
    assert revision
    insert = _inserts(connection)[0]
    assert insert[1][12] == "withhold_description"
    assert insert[1][14] == ""
    assert insert[1][15] == ["건설현장", "굴착기"]
    assert _updates(connection)[0][1][0] == "uncertain"


def test_unsafe_numeric_output_never_reaches_the_revision_table() -> None:
    connection = ScriptedConnection([GLOBAL, JOB])
    revision = ImageDescriptionStore(connection).finish_and_publish(
        "job-1", _result(description="굴착기 2대가 보인다.")
    )
    assert revision == ""
    assert not _inserts(connection)
    update = _updates(connection)[0]
    assert update[1][0:2] == ("blocked", "digits_present")


def test_a_nonzero_charge_blocks_a_free_derived_revision_but_records_the_charge() -> None:
    connection = ScriptedConnection([GLOBAL, JOB])
    result = _result(usage=replace(_result().usage, cost_usd=0.01))
    assert ImageDescriptionStore(connection).finish_and_publish("job-1", result) == ""
    assert not _inserts(connection)
    update = next(event for event in _updates(connection) if "description_job" in event[0])
    assert update[1][0:2] == ("blocked", "unexpected_charge")
    assert str(update[1][6]) == "0.01"
    catalog_update = next(event for event in _updates(connection) if "model_catalog" in event[0])
    assert "zero_cost_verified=false" in catalog_update[0]


def test_an_unknown_cost_blocks_publication_and_disables_the_free_claim() -> None:
    connection = ScriptedConnection([GLOBAL, JOB])
    result = _result(usage=replace(_result().usage, cost_usd=None))
    assert ImageDescriptionStore(connection).finish_and_publish("job-1", result) == ""
    assert not _inserts(connection)
    job_update = next(event for event in _updates(connection) if "description_job" in event[0])
    assert job_update[1][0:2] == ("blocked", "cost_unverified")
    catalog_update = next(event for event in _updates(connection) if "model_catalog" in event[0])
    assert catalog_update[1] == ("approved-provider", "free-vision-v1")


def test_a_result_from_an_unapproved_identity_is_never_published() -> None:
    connection = ScriptedConnection([GLOBAL, JOB])
    result = _result(usage=replace(_result().usage, model="other-model"))
    with pytest.raises(ImageDescriptionDenied, match="result_identity_invalid"):
        ImageDescriptionStore(connection).finish_and_publish("job-1", result)
    assert connection.events[-1] == "rollback"
    assert not _inserts(connection)


def test_raw_provider_output_and_image_bytes_have_no_store_columns() -> None:
    source = (
        __import__("pathlib").Path(__file__).resolve().parents[1]
        / "src/tybot/archive/image_description_store.py"
    ).read_text(encoding="utf-8")
    inserts = "\n".join(
        part.split("VALUES", 1)[0] for part in source.split("INSERT INTO")[1:]
    )
    assert "raw" not in inserts
    assert "image bytes" not in inserts.lower()
    assert "provider_response" not in inserts


def test_stale_dispatch_reconciliation_closes_without_making_it_retryable() -> None:
    connection = ScriptedConnection([GLOBAL, [{"id": "job-1"}, {"id": "job-2"}]])
    found = ImageDescriptionStore(connection).reconcile_stale_dispatches(
        older_than_seconds=600
    )
    assert found == ("job-1", "job-2")
    update = _updates(connection)[0]
    assert "state='unknown'" in update[0]
    assert "dispatch_stale" in update[0]
    assert "state='reserved'" not in update[0]


@pytest.mark.parametrize("age", [0, -1, 1.5, True])
def test_invalid_reconciliation_age_is_rejected(age) -> None:
    with pytest.raises(ImageDescriptionDenied, match="invalid_reconcile_age"):
        ImageDescriptionStore(None).reconcile_stale_dispatches(older_than_seconds=age)
