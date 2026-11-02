from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot.archive import image_description_job as job
from tybot.gateway.base import Sensitivity

NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)


class Connection:
    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None


class Store:
    def __init__(self, _connection, *, enabled=True):
        self.enabled = enabled
        self.reconciled = []

    def reconcile_stale_dispatches(self, *, older_than_seconds):
        self.reconciled.append(older_than_seconds)
        return ()

    def effective_policy(self, workspace):
        return (
            SimpleNamespace(
                workspace=workspace,
                revision="global:local",
                enabled=self.enabled,
                workspace_enabled=self.enabled,
                provider="openai",
                model="free-vision",
                sensitivity=Sensitivity.INTERNAL,
            ),
            {},
            {},
        )


def test_disabled_policy_does_not_read_archive_or_build_router(monkeypatch, tmp_path):
    store = Store(None, enabled=False)
    monkeypatch.setattr(job, "ImageDescriptionStore", lambda _connection: store)

    result = job.run_once(
        "tyit",
        dsn="postgresql://unused",
        archive_root=tmp_path.resolve(),
        runtime_root=tmp_path,
        connect=lambda _dsn: Connection(),
        router_factory=lambda *_a, **_k: pytest.fail("router built while disabled"),
        revisions=lambda _root: pytest.fail("archive read while disabled"),
        now=NOW,
    )

    assert result.state == "disabled"
    assert result.policy_revision == "global:local"
    assert store.reconciled == [job.STALE_DISPATCH_SECONDS]


def test_enabled_policy_runs_the_existing_coordinator(monkeypatch, tmp_path):
    store = Store(None)
    calls = {}
    monkeypatch.setattr(job, "ImageDescriptionStore", lambda _connection: store)

    def run_batch(docs, **kwargs):
        calls.update(kwargs)
        assert docs == ["doc"]
        return SimpleNamespace(
            selected=2,
            skipped=1,
            items=(
                SimpleNamespace(state="published"),
                SimpleNamespace(state="approval_required"),
            ),
        )

    monkeypatch.setattr(job, "run_cleared_batch", run_batch)
    router = object()
    result = job.run_once(
        "tyit",
        dsn="postgresql://unused",
        archive_root=tmp_path.resolve(),
        runtime_root=tmp_path,
        connect=lambda _dsn: Connection(),
        router_factory=lambda policy, **_kwargs: router,
        revisions=lambda _root: ["doc"],
        now=NOW,
    )

    assert result.state == "partial"
    assert result.published == 1
    assert result.pending_approval == 1
    assert calls["store"] is store
    assert calls["router"] is router
    assert calls["dispatch_expires_at"] > NOW


def test_unknown_worker_state_cannot_be_reported_as_ok(monkeypatch, tmp_path):
    monkeypatch.setattr(job, "ImageDescriptionStore", Store)
    monkeypatch.setattr(
        job,
        "run_cleared_batch",
        lambda *_a, **_k: SimpleNamespace(
            selected=1,
            skipped=0,
            items=(SimpleNamespace(state="new_state"),),
        ),
    )

    result = job.run_once(
        "tyit",
        dsn="postgresql://unused",
        archive_root=tmp_path.resolve(),
        runtime_root=tmp_path,
        connect=lambda _dsn: Connection(),
        router_factory=lambda *_a, **_k: object(),
        revisions=lambda _root: [],
        now=NOW,
    )

    assert result.state == "partial"
    assert result.unknown == 1


@pytest.mark.parametrize("workspace", ["", "../tyit", "TYIT"])
def test_run_once_rejects_invalid_workspace(workspace, tmp_path):
    with pytest.raises(RuntimeError, match="runtime_config_invalid"):
        job.run_once(
            workspace,
            dsn="unused",
            archive_root=tmp_path.resolve(),
            runtime_root=tmp_path,
        )


def test_archive_root_must_be_absolute(tmp_path):
    with pytest.raises(RuntimeError, match="runtime_config_invalid"):
        job.run_once(
            "tyit",
            dsn="unused",
            archive_root=Path("archive"),
            runtime_root=tmp_path,
        )


def test_router_has_one_exact_model_and_no_fallback(monkeypatch, tmp_path):
    provider = object()
    monkeypatch.setattr(job, "build_default_providers", lambda: {"openai": provider})
    policy = SimpleNamespace(
        provider="openai",
        model="free-vision",
        sensitivity=Sensitivity.INTERNAL,
    )

    router = job._router(policy, cost_path=tmp_path / "cost.json")

    assert router.default_model == "free-vision"
    assert router.resolve("free-vision", Sensitivity.INTERNAL).provider == "openai"
    with pytest.raises(KeyError):
        router.resolve("gpt-4o", Sensitivity.INTERNAL)


def test_unsupported_provider_fails_before_a_model_call(tmp_path):
    policy = SimpleNamespace(
        provider="other",
        model="free-vision",
        sensitivity=Sensitivity.PUBLIC,
    )

    with pytest.raises(RuntimeError, match="runtime_provider_unsupported"):
        job._router(policy, cost_path=tmp_path / "cost.json")
