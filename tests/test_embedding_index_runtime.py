from __future__ import annotations

import pytest

from tybot.embedding_index_job import IndexLimits
from tybot.embedding_index_runtime import run_embedding_index


def test_runtime_assembles_only_injected_boundaries(monkeypatch):
    connection = object()
    store = object()
    provider = object()
    limits = IndexLimits(max_documents=1)
    sources = (object(),)
    binding_lookup = object()
    embedder = object()
    writer = object()
    result = object()
    seen = {}

    monkeypatch.setattr(
        "tybot.embedding_index_runtime.iter_index_sources", lambda got: sources
    )
    monkeypatch.setattr(
        "tybot.embedding_index_runtime.db_bindings",
        lambda got: binding_lookup if got is connection else None,
    )

    def build_embedder(**kwargs):
        seen["embedder"] = kwargs
        return embedder

    monkeypatch.setattr("tybot.embedding_index_runtime.GatewayEmbedder", build_embedder)
    monkeypatch.setattr(
        "tybot.embedding_index_runtime.EmbeddingIndexWriter",
        lambda got: writer if got is connection else None,
    )

    def reconcile(got_connection, got_sources, **kwargs):
        seen["reconcile"] = (got_connection, got_sources, kwargs)
        return result

    monkeypatch.setattr(
        "tybot.embedding_index_runtime.run_reconciled_index", reconcile
    )

    got = run_embedding_index(
        connection,
        store,
        model_revision="embed-v1",
        providers={"fake": provider},
        scan_complete=True,
        limits=limits,
        timeout_seconds=7,
    )

    assert got is result
    assert seen["embedder"] == {
        "bindings": binding_lookup,
        "providers": {"fake": provider},
        "timeout_seconds": 7,
    }
    got_connection, got_sources, kwargs = seen["reconcile"]
    assert got_connection is connection and callable(got_sources)
    assert got_sources() is sources
    assert kwargs["scan_complete"] is True
    assert kwargs["model_revision"] == "embed-v1"
    assert kwargs["embedder"] is embedder
    assert kwargs["writer"] is writer
    assert kwargs["limits"] is limits

    request = object()
    monkeypatch.setattr(
        "tybot.embedding_index_runtime.authorize_index_request",
        lambda got_connection, got_request: (got_connection, got_request),
    )
    assert kwargs["authorize"](request) == (connection, request)


@pytest.mark.parametrize("scan_complete", [None, 0, 1, "true"])
def test_inventory_completeness_cannot_be_coerced(monkeypatch, scan_complete):
    monkeypatch.setattr(
        "tybot.embedding_index_runtime.iter_index_sources",
        lambda _store: pytest.fail("inventory must not open"),
    )
    with pytest.raises(TypeError, match="scan_complete"):
        run_embedding_index(
            object(),
            object(),
            model_revision="embed-v1",
            providers={},
            scan_complete=scan_complete,
        )


def test_provider_discovery_is_never_implicit(monkeypatch):
    monkeypatch.setattr(
        "tybot.embedding_index_runtime.iter_index_sources",
        lambda _store: pytest.fail("inventory must not open"),
    )
    with pytest.raises(TypeError, match="providers"):
        run_embedding_index(
            object(),
            object(),
            model_revision="embed-v1",
            providers=None,
            scan_complete=True,
        )
