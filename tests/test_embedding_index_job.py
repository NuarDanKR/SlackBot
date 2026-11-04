from __future__ import annotations

import hashlib

import pytest

from tybot.archive.embedding_source import CHANNEL, VectorSource
from tybot.embedding_index_job import (
    EXTERNAL,
    LOCAL,
    EmbeddingBatch,
    IndexAuthorization,
    IndexLimits,
    run_index_batch,
)
from tybot.embedding_index_store import PublishResult


def source(name: str = "one", text: str = "# title\nbody\n") -> VectorSource:
    return VectorSource(
        workspace="sample",
        channel_id="C1",
        doc_path=f"workspaces/sample/channels/C1/raw/{name}.md",
        kind=CHANNEL,
        text=text,
        source_revision=hashlib.sha256(text.encode()).hexdigest(),
    )


class Embedder:
    def __init__(self, *, cost=0.0, revision="model-v1", fail=False):
        self.cost = cost
        self.revision = revision
        self.fail = fail
        self.calls = []

    def embed(self, inputs, *, model_revision, policy_revision):
        self.calls.append((inputs, model_revision, policy_revision))
        if self.fail:
            raise RuntimeError("private provider response")
        return EmbeddingBatch.checked(
            self.revision,
            [[float(item.chunk_no), 1.0] for item in inputs],
            cost_usd=self.cost,
        )


class Writer:
    def __init__(self, *, current=False, fail=False):
        self.current = current
        self.fail = fail
        self.calls = []

    def publish(self, key, text, chunks, vectors):
        self.calls.append((key, text, chunks, vectors))
        if self.fail:
            raise RuntimeError("private DB row")
        return PublishResult(1, len(chunks), self.current)


def allow(_request, *, transport=LOCAL, external=False):
    return IndexAuthorization(True, "model-v1", "policy-7", transport, external)


def outcomes(result):
    return dict(result.outcomes)


def test_approved_local_sources_are_embedded_once_and_published_atomically():
    embedder, writer = Embedder(cost=0.25), Writer()
    result = run_index_batch(
        [source("a"), source("b")],
        model_revision="model-v1",
        authorize=allow,
        embedder=embedder,
        writer=writer,
    )
    assert result.scanned == result.planned == result.published == 2
    assert result.embed_calls == 2
    assert result.cost_usd == 0.5
    assert outcomes(result) == {"published": 2}
    assert len(embedder.calls) == len(writer.calls) == 2


def test_external_transport_without_explicit_transfer_approval_calls_no_model():
    embedder, writer = Embedder(), Writer()
    result = run_index_batch(
        [source()],
        model_revision="model-v1",
        authorize=lambda request: allow(request, transport=EXTERNAL, external=False),
        embedder=embedder,
        writer=writer,
    )
    assert outcomes(result) == {"authorization_rejected": 1}
    assert embedder.calls == writer.calls == []


def test_policy_denial_calls_no_model():
    embedder, writer = Embedder(), Writer()
    result = run_index_batch(
        [source()],
        model_revision="model-v1",
        authorize=lambda _request: IndexAuthorization(
            False, "model-v1", "policy-7", LOCAL
        ),
        embedder=embedder,
        writer=writer,
    )
    assert outcomes(result) == {"authorization_denied": 1}
    assert embedder.calls == writer.calls == []


def test_authorizer_receives_coordinates_but_not_in_result_repr():
    requests = []
    item = source()
    result = run_index_batch(
        [item],
        model_revision="model-v1",
        authorize=lambda request: requests.append(request) or allow(request),
        embedder=Embedder(),
        writer=Writer(),
    )
    assert requests[0].doc_path == item.doc_path
    assert item.doc_path not in repr(requests[0])
    assert item.doc_path not in repr(result)
    assert item.text not in repr(result)


def test_model_identity_mismatch_is_not_published():
    writer = Writer()
    result = run_index_batch(
        [source()],
        model_revision="model-v1",
        authorize=allow,
        embedder=Embedder(revision="other-model"),
        writer=writer,
    )
    assert outcomes(result) == {"embedding_invalid_result": 1}
    assert writer.calls == []


def test_provider_and_database_errors_are_fixed_codes_without_bodies():
    provider = run_index_batch(
        [source()],
        model_revision="model-v1",
        authorize=allow,
        embedder=Embedder(fail=True),
        writer=Writer(),
    )
    database = run_index_batch(
        [source()],
        model_revision="model-v1",
        authorize=allow,
        embedder=Embedder(),
        writer=Writer(fail=True),
    )
    assert outcomes(provider) == {"embedding_failed": 1}
    assert outcomes(database) == {"publish_failed": 1}
    assert "private provider response" not in repr(provider)
    assert "private DB row" not in repr(database)


def test_unknown_cost_makes_batch_cost_unknown_not_zero():
    result = run_index_batch(
        [source("known"), source("unknown")],
        model_revision="model-v1",
        authorize=allow,
        embedder=Embedder(cost=None),
        writer=Writer(),
    )
    assert result.cost_usd is None


def test_existing_current_revision_is_counted_separately():
    result = run_index_batch(
        [source()],
        model_revision="model-v1",
        authorize=allow,
        embedder=Embedder(),
        writer=Writer(current=True),
    )
    assert result.published == 0
    assert result.already_current == 1
    assert outcomes(result) == {"already_current": 1}


def test_per_document_size_is_rejected_before_authorization_and_model():
    called = False
    embedder = Embedder()

    def authorize(_request):
        nonlocal called
        called = True

    result = run_index_batch(
        [source(text="x" * 65)],
        model_revision="model-v1",
        authorize=authorize,
        embedder=embedder,
        writer=Writer(),
        limits=IndexLimits(max_chars_per_document=64),
    )
    assert outcomes(result) == {"source_too_large": 1}
    assert called is False
    assert embedder.calls == []


def test_total_character_budget_stops_before_the_next_model_call():
    text = "x" * 64
    embedder = Embedder()
    result = run_index_batch(
        [source("a", text), source("b", text)],
        model_revision="model-v1",
        authorize=allow,
        embedder=embedder,
        writer=Writer(),
        limits=IndexLimits(max_total_chars=100),
    )
    assert result.published == 1
    assert result.stopped_reason == "character_limit_reached"
    assert len(embedder.calls) == 1


def test_call_budget_stops_without_silently_skipping_the_remainder():
    embedder = Embedder()
    result = run_index_batch(
        [source("a"), source("b")],
        model_revision="model-v1",
        authorize=allow,
        embedder=embedder,
        writer=Writer(),
        limits=IndexLimits(max_calls=1),
    )
    assert result.stopped_reason == "call_limit_reached"
    assert result.scanned == 2
    assert len(embedder.calls) == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"model_revision": ""},
        {"model_revision": " model-v1"},
    ],
)
def test_invalid_model_revision_is_rejected_before_source_iteration(kwargs):
    with pytest.raises(ValueError, match="model_revision"):
        run_index_batch(
            [], authorize=allow, embedder=Embedder(), writer=Writer(), **kwargs
        )


@pytest.mark.parametrize("cost", [-1, float("nan"), True])
def test_invalid_cost_is_rejected(cost):
    with pytest.raises(ValueError, match="cost_usd"):
        EmbeddingBatch.checked("model-v1", [[0.1]], cost_usd=cost)


@pytest.mark.parametrize(
    "vectors",
    [
        [],
        [[]],
        [[float("nan")]],
        [[0.1], [0.1, 0.2]],
        [[True]],
        [[0.0] * 16_001],
    ],
)
def test_invalid_vectors_are_rejected_before_publication(vectors):
    with pytest.raises(ValueError, match="embedding_vectors_invalid"):
        EmbeddingBatch.checked("model-v1", vectors, cost_usd=0)


def test_direct_batch_constructor_cannot_bypass_validation():
    with pytest.raises(ValueError, match="embedding_model_revision_invalid"):
        EmbeddingBatch("", ((0.1,),), 0)
