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
