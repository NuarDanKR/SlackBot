from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from tybot.archive.attachment_doc import AttachmentDoc
from tybot.archive.image_description import PROMPT_REVISION, ImageInput
from tybot.archive.image_description_clearance import SafetyAssessment
from tybot.archive.image_description_coordinator import (
    run_approved_batch,
    run_cleared_batch,
)
from tybot.archive.image_description_policy import ImageDescriptionPolicy
from tybot.archive.image_description_store import ImageDescriptionDenied
from tybot.archive.image_description_worker import ImageDescriptionRun

NOW = datetime(2026, 10, 27, 12, tzinfo=UTC)
SHA = "a" * 64


def _doc(**over):
    values = {
        "workspace": "tyit",
        "channel_id": "C12345678",
        "channel": "#현장",
        "file_id": "F12345678",
        "name": "현장.png",
        "revision": "r1",
        "visibility": "private",
        "acl": frozenset({"#현장"}),
        "filetype": "png",
        "sha256": SHA,
        "conversion_state": "failed",
        "human_upload_confirmed": True,
    }
    values.update(over)
    return AttachmentDoc(**values)


def _image(candidate, _doc):
    return ImageInput(
        workspace=candidate.workspace,
        channel=candidate.channel_id,
        file_id=candidate.file_id,
        original_sha256=candidate.original_sha256,
        mime=candidate.mime,
        image=b"safe synthetic image",
    )


class Store:
    def __init__(self, indexed=()):
        self.indexed = frozenset(indexed)
        self.reserved = []

    def indexed_sources(self, workspace):
        assert workspace == "tyit"
        return self.indexed

    def reserve(self, image, **kwargs):
        self.reserved.append((image, kwargs))
        return SimpleNamespace(dispatch_id="job-1")


class ManagedStore(Store):
    def __init__(self, *, enabled=True, assessment=True, approval=True):
        super().__init__()
        self.policy = ImageDescriptionPolicy(
            workspace="tyit",
            revision="policy-1",
            enabled=enabled,
            workspace_enabled=enabled,
            model="free-vision",
            provider="provider",
            region="kr",
            max_image_bytes=1024,
            max_output_tokens=100,
            timeout_seconds=10,
            catalog_verification_ttl_seconds=60,
        )
        self.assessment = (
            SafetyAssessment(
                workspace="tyit",
                channel_id="C12345678",
                file_id="F12345678",
                original_sha256=SHA,
                provider="provider",
                model="free-vision",
                policy_revision="policy-1",
                visibility="private",
                acl=frozenset({"#현장"}),
                assessed_by="admin",
                assessed_at=NOW - timedelta(minutes=1),
                expires_at=NOW + timedelta(hours=1),
                pii_cleared=True,
            )
            if assessment
            else None
        )
        self.clearance_queries = []
        self.approval_queries = []
        self.approval = approval

    def effective_policy(self, workspace):
        assert workspace == "tyit"
        return self.policy, {}, {}

    def active_clearance(self, **scope):
        self.clearance_queries.append(scope)
        return self.assessment

    def active_approval_id(self, **scope):
        self.approval_queries.append(scope)
        return "approval-1" if self.approval else ""


def _run(monkeypatch, docs=None, store=None, **over):
    monkeypatch.setattr(
        "tybot.archive.image_description_coordinator.dispatch_reserved_image",
        lambda *_args, **_kwargs: ImageDescriptionRun(
            "job-1", "published", revision_id="revision-1"
        ),
    )
    args = {
        "workspace": "tyit",
        "store": store or Store(),
        "router": object(),
        "load_image": _image,
        "approval_id_for": lambda _candidate: "approval-1",
        "checks_for": lambda *_args: {
            "source_verified": True,
            "access_verified": True,
            "pii_blocked": False,
            "bot_generated": False,
        },
        "dispatch_expires_at": NOW + timedelta(minutes=5),
    }
    args.update(over)
    return run_approved_batch([_doc()] if docs is None else docs, **args)


def test_an_approved_candidate_is_reserved_then_dispatched(monkeypatch):
    store = Store()
    batch = _run(monkeypatch, store=store)

    assert batch.selected == 1
    assert batch.items[0].state == "published"
    assert batch.items[0].revision_id == "revision-1"
    assert len(store.reserved) == 1


def test_an_existing_revision_is_not_loaded_or_reserved(monkeypatch):
    loaded = []
    store = Store({("tyit", "C12345678", SHA)})
    batch = _run(
        monkeypatch,
        store=store,
        load_image=lambda *_args: loaded.append(True),
    )

    assert batch.selected == 0
    assert batch.skip_counts == {"already_indexed": 1}
    assert not loaded
    assert not store.reserved


def test_missing_human_provenance_never_reaches_the_loader(monkeypatch):
    loaded = []
    batch = _run(
        monkeypatch,
        docs=[_doc(human_upload_confirmed=False)],
        load_image=lambda *_args: loaded.append(True),
    )

    assert batch.selected == 0
    assert batch.skip_counts == {"not_known_human_upload": 1}
    assert not loaded


def test_missing_approval_does_not_open_the_original(monkeypatch):
    loaded = []
    batch = _run(
        monkeypatch,
        approval_id_for=lambda _candidate: "",
        load_image=lambda *_args: loaded.append(True),
    )

    assert batch.items[0].state == "approval_required"
    assert batch.items[0].code == "approval_missing"
    assert not loaded


def test_failed_approval_lookup_does_not_open_the_original_or_leak(monkeypatch):
    loaded = []
    batch = _run(
        monkeypatch,
        approval_id_for=lambda _candidate: (_ for _ in ()).throw(
            RuntimeError("private approval row")
        ),
        load_image=lambda *_args: loaded.append(True),
    )

    assert batch.items[0].code == "approval_lookup_failed"
    assert "private" not in repr(batch)
    assert not loaded


def test_unverified_access_does_not_open_the_original(monkeypatch):
    loaded = []
    batch = _run(
        monkeypatch,
        checks_for=lambda *_args: {
            "access_verified": False,
            "pii_blocked": False,
            "bot_generated": False,
        },
        load_image=lambda *_args: loaded.append(True),
    )

    assert batch.items[0].code == "access_unverified"
    assert not loaded


def test_failed_current_checks_do_not_expose_the_exception(monkeypatch):
    batch = _run(
        monkeypatch,
        checks_for=lambda *_args: (_ for _ in ()).throw(
            RuntimeError("private ACL response")
        ),
    )

    assert batch.items[0].code == "checks_failed"
    assert "private" not in repr(batch)


def test_loader_failure_exposes_only_a_safe_code(monkeypatch):
    class UnsafeError(RuntimeError):
        pass

    batch = _run(
        monkeypatch,
        load_image=lambda *_args: (_ for _ in ()).throw(
            UnsafeError("C:/secret/path and provider body")
        ),
    )

    assert batch.items[0].code == "source_load_failed"
    assert "secret" not in repr(batch)


def test_loader_control_code_is_preserved(monkeypatch):
    class SourceError(RuntimeError):
        code = "original_hash_mismatch"

    batch = _run(
        monkeypatch,
        load_image=lambda *_args: (_ for _ in ()).throw(SourceError("private")),
    )

    assert batch.items[0].code == "original_hash_mismatch"


def test_a_non_image_loader_result_is_blocked(monkeypatch):
    batch = _run(monkeypatch, load_image=lambda *_args: b"raw bytes")

    assert batch.items[0].code == "source_load_invalid"


def test_checks_are_recomputed_for_reserve_and_dispatch(monkeypatch):
    checks = {
        "source_verified": True,
        "access_verified": True,
        "pii_blocked": False,
        "bot_generated": False,
    }
    seen = []

    def dispatch(_store, _job, _image, *, router, checks):
        seen.append((router, checks))
        return ImageDescriptionRun("job-1", "published", revision_id="revision-1")

    monkeypatch.setattr(
        "tybot.archive.image_description_coordinator.dispatch_reserved_image", dispatch
    )
    store = Store()
    batch = run_approved_batch(
        [_doc()],
        workspace="tyit",
        store=store,
        router="router",
        load_image=_image,
        approval_id_for=lambda _candidate: "approval-1",
        checks_for=lambda *_args: checks,
        dispatch_expires_at=NOW + timedelta(minutes=5),
    )

    assert batch.items[0].state == "published"
    expected = checks | {"source_verified": True}
    assert store.reserved[0][1]["checks"] == expected
    assert seen == [("router", expected)]


def test_reserve_refusal_does_not_call_the_model(monkeypatch):
    class RefusingStore(Store):
        def reserve(self, image, **kwargs):
            raise ImageDescriptionDenied("quota_unknown")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("model must not run")

    monkeypatch.setattr(
        "tybot.archive.image_description_coordinator.dispatch_reserved_image", forbidden
    )
    batch = run_approved_batch(
        [_doc()],
        workspace="tyit",
        store=RefusingStore(),
        router=object(),
        load_image=_image,
        approval_id_for=lambda _candidate: "approval-1",
        checks_for=lambda *_args: {
            "access_verified": True,
            "pii_blocked": False,
            "bot_generated": False,
        },
        dispatch_expires_at=NOW + timedelta(minutes=5),
    )

    assert batch.items[0].state == "blocked"
    assert batch.items[0].code == "quota_unknown"


def test_dispatch_failure_is_unknown_and_does_not_leak(monkeypatch):
    monkeypatch.setattr(
        "tybot.archive.image_description_coordinator.dispatch_reserved_image",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("provider response with private text")
        ),
    )
    batch = run_approved_batch(
        [_doc()],
        workspace="tyit",
        store=Store(),
        router=object(),
        load_image=_image,
        approval_id_for=lambda _candidate: "approval-1",
        checks_for=lambda *_args: {
            "access_verified": True,
            "pii_blocked": False,
            "bot_generated": False,
        },
        dispatch_expires_at=NOW + timedelta(minutes=5),
    )

    assert batch.items[0].state == "unknown"
    assert batch.items[0].code == "dispatch_failed"
    assert "private" not in repr(batch)


def test_other_workspaces_are_not_selected(monkeypatch):
    batch = _run(
        monkeypatch,
        docs=[_doc(workspace="mgmt"), replace(_doc(), file_id="F99999999")],
    )

    assert batch.selected == 1
    assert batch.items[0].file_id == "F99999999"


def _run_cleared(monkeypatch, *, store=None, doc=None, loaded=None):
    store = store or ManagedStore()
    opened = []
    result = loaded or SimpleNamespace(ok=True, image=_image(SimpleNamespace(
        workspace="tyit",
        channel_id="C12345678",
        file_id="F12345678",
        original_sha256=SHA,
        mime="image/png",
    ), None), error_code="")

    def fake_load(candidate, **kwargs):
        opened.append((candidate, kwargs))
        return result

    monkeypatch.setattr(
        "tybot.archive.image_description_coordinator.load_original", fake_load
    )
    monkeypatch.setattr(
        "tybot.archive.image_description_coordinator.dispatch_reserved_image",
        lambda *_args, **_kwargs: ImageDescriptionRun(
            "job-1", "published", revision_id="revision-1"
        ),
    )
    batch = run_cleared_batch(
        [doc or _doc()],
        workspace="tyit",
        store=store,
        router=object(),
        archive_root="archive-root",
        dispatch_expires_at=NOW + timedelta(minutes=5),
        now=NOW,
    )
    return batch, store, opened


def test_managed_batch_binds_current_policy_clearance_and_original(monkeypatch):
    batch, store, opened = _run_cleared(monkeypatch)

    assert batch.items[0].state == "published"
    assert store.approval_queries == [{
        "workspace": "tyit",
        "channel": "C12345678",
        "file_id": "F12345678",
        "original_sha256": SHA,
        "provider": "provider",
        "model": "free-vision",
        "policy_revision": "policy-1",
        "prompt_revision": PROMPT_REVISION,
    }]
    assert store.clearance_queries == [{
        "workspace": "tyit",
        "channel_id": "C12345678",
        "file_id": "F12345678",
        "original_sha256": SHA,
        "provider": "provider",
        "model": "free-vision",
        "policy_revision": "policy-1",
    }]
    assert opened[0][1]["max_image_bytes"] == 1024
    assert store.reserved[0][1]["checks"] == {
        "access_verified": True,
        "pii_blocked": False,
        "bot_generated": False,
        "source_verified": True,
    }


def test_unknown_fresh_check_is_rejected_instead_of_silently_dropped(monkeypatch):
    store = Store()
    batch = _run(
        monkeypatch,
        store=store,
        checks_for=lambda *_args: {
            "access_verified": True,
            "pii_blocked": False,
            "bot_generated": False,
            "unexpected": True,
        },
    )

    assert batch.items[0].code == "checks_invalid"
    assert store.reserved == []


def test_missing_clearance_never_opens_the_original(monkeypatch):
    batch, _store, opened = _run_cleared(
        monkeypatch, store=ManagedStore(assessment=False)
    )

    assert batch.items[0].code == "assessment_missing"
    assert not opened


def test_missing_exact_approval_never_opens_the_original(monkeypatch):
    batch, _store, opened = _run_cleared(
        monkeypatch, store=ManagedStore(approval=False)
    )

    assert batch.items[0].state == "approval_required"
    assert batch.items[0].code == "approval_missing"
    assert not opened


def test_disabled_policy_does_not_query_clearance_or_open_original(monkeypatch):
    batch, store, opened = _run_cleared(
        monkeypatch, store=ManagedStore(enabled=False)
    )

    assert batch.items[0].code == "image_description_disabled"
    assert not store.clearance_queries
    assert not opened


def test_changed_acl_never_opens_the_original(monkeypatch):
    batch, _store, opened = _run_cleared(
        monkeypatch, doc=_doc(acl=frozenset({"#다른채널"}))
    )

    assert batch.items[0].code == "acl_changed"
    assert not opened


def test_managed_original_failure_keeps_only_the_safe_code(monkeypatch):
    batch, _store, _opened = _run_cleared(
        monkeypatch,
        loaded=SimpleNamespace(
            ok=False, image=None, error_code="original_hash_mismatch"
        ),
    )

    assert batch.items[0].code == "original_hash_mismatch"
