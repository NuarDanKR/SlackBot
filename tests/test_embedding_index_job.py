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
from tybot.embedding_index_store import IndexPublishError, PublishResult


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
    def __init__(self, *, current=False, fail=False, superseded=False):
        self.current = current
        self.fail = fail
        self.superseded = superseded
        self.calls = []

    def publish(self, key, text, chunks, vectors, *, expected_current):
        self.calls.append((key, text, chunks, vectors, expected_current))
        if self.fail:
            raise RuntimeError("private DB row")
        if self.superseded:
            raise IndexPublishError("embedding_source_superseded")
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
        expected_current={},
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
        expected_current={},
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
        expected_current={},
    )
    assert outcomes(result) == {"authorization_denied": 1}
    assert embedder.calls == writer.calls == []


def test_unapproved_external_policy_can_be_reported_as_a_denial():
    denied = IndexAuthorization(False, "model-v1", "policy-7", EXTERNAL)
    assert denied.checked("model-v1") is denied
    with pytest.raises(ValueError, match="external_transfer_not_approved"):
        IndexAuthorization(True, "model-v1", "policy-7", EXTERNAL).checked(
            "model-v1"
        )


def test_authorizer_receives_coordinates_but_not_in_result_repr():
    requests = []
    item = source()
    result = run_index_batch(
        [item],
        model_revision="model-v1",
        authorize=lambda request: requests.append(request) or allow(request),
        embedder=Embedder(),
        writer=Writer(),
        expected_current={},
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
        expected_current={},
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
        expected_current={},
    )
    database = run_index_batch(
        [source()],
        model_revision="model-v1",
        authorize=allow,
        embedder=Embedder(),
        writer=Writer(fail=True),
        expected_current={},
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
        expected_current={},
    )
    assert result.cost_usd is None


def test_existing_current_revision_is_counted_separately():
    result = run_index_batch(
        [source()],
        model_revision="model-v1",
        authorize=allow,
        embedder=Embedder(),
        writer=Writer(current=True),
        expected_current={},
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
        expected_current={},
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
        expected_current={},
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
        expected_current={},
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
            [],
            authorize=allow,
            embedder=Embedder(),
            writer=Writer(),
            expected_current={},
            **kwargs,
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


def test_a_superseded_publication_is_counted_apart_from_a_failure():
    """A newer source won the document while we embedded. Not a fault; its own number.

    Folded into ``publish_failed`` it would read as a broken writer, and the
    real cause -- a reconciliation snapshot older than the run it feeds -- would
    stay invisible.
    """
    embedder, writer = Embedder(), Writer(superseded=True)
    result = run_index_batch(
        [source("a")],
        model_revision="model-v1",
        authorize=allow,
        embedder=embedder,
        writer=writer,
        expected_current={},
    )

    assert outcomes(result) == {"publish_superseded": 1}
    assert result.published == 0 and result.already_current == 0
    # Exactly one attempt. These vectors describe text that is no longer current,
    # so republishing them -- here or anywhere -- would index text that is gone.
    assert len(writer.calls) == 1


def test_each_document_is_published_against_the_id_observed_for_it():
    embedder, writer = Embedder(), Writer()
    first, second = source("a"), source("b")
    run_index_batch(
        [first, second],
        model_revision="model-v1",
        authorize=allow,
        embedder=embedder,
        writer=writer,
        expected_current={
            (first.workspace, first.channel_id, first.doc_path): 11,
        },
    )

    sent = {call[0].doc_path: call[4] for call in writer.calls}
    assert sent[first.doc_path] == 11
    # Absent from the map means "this document has no current revision", which
    # the writer accepts only as a first indexing. An empty map can therefore
    # create documents but never replace one -- the closed direction.
    assert sent[second.doc_path] is None


def test_an_unusable_expectation_map_is_rejected_before_any_source_is_read():
    embedder, writer = Embedder(), Writer()
    with pytest.raises(ValueError, match="expected_current"):
        run_index_batch(
            [source("a")],
            model_revision="model-v1",
            authorize=allow,
            embedder=embedder,
            writer=writer,
            expected_current=[("tyit", "C1", "a.md", 1)],
        )
    assert embedder.calls == writer.calls == []


@pytest.mark.parametrize(
    "expected",
    [
        {("tyit", "C1", "a.md"): True},
        {("tyit", "C1", "a.md"): "7"},
        {("tyit", "C1", "a.md"): 0},
        {(" tyit", "C1", "a.md"): 7},
        {("", "C1", "a.md"): 7},
        {("tyit", "C1"): 7},
        {"tyit/C1/a.md": 7},
    ],
)
def test_expectation_coordinates_and_ids_are_not_coerced(expected):
    embedder, writer = Embedder(), Writer()
    with pytest.raises(ValueError, match="expected_current"):
        run_index_batch(
            [source("a")],
            model_revision="model-v1",
            authorize=allow,
            embedder=embedder,
            writer=writer,
            expected_current=expected,
        )
    assert embedder.calls == writer.calls == []


def test_publishing_a_batch_without_an_expectation_map_is_a_type_error():
    with pytest.raises(TypeError):
        run_index_batch(
            [],
            model_revision="model-v1",
            authorize=allow,
            embedder=Embedder(),
            writer=Writer(),
        )


@pytest.mark.parametrize(
    "bad",
    [
        {("tyit", "C1"): 1},
        {("tyit", "C1", "a.md", "extra"): 1},
        {("tyit", "C1", ""): 1},
        {("tyit", "C1", " a.md"): 1},
        {("tyit", "C1", b"a.md"): 1},
        {"tyit/C1/a.md": 1},
    ],
)
def test_an_inexact_coordinate_is_refused_rather_than_coerced(bad):
    """A coerced key would miss its document and read as a first indexing.

    That is the one direction this check exists to close: a document that
    already has a current revision must never look like a new one.
    """
    embedder, writer = Embedder(), Writer()
    with pytest.raises(ValueError, match="expected_current"):
        run_index_batch(
            [source("a")],
            model_revision="model-v1",
            authorize=allow,
            embedder=embedder,
            writer=writer,
            expected_current=bad,
        )
    assert embedder.calls == writer.calls == []


@pytest.mark.parametrize("bad", [0, -1, True, "4", 4.0, None])
def test_an_unusable_id_in_the_map_is_refused(bad):
    embedder, writer = Embedder(), Writer()
    with pytest.raises(ValueError, match="expected_current"):
        run_index_batch(
            [source("a")],
            model_revision="model-v1",
            authorize=allow,
            embedder=embedder,
            writer=writer,
            expected_current={("sample", "C1", "workspaces/sample/raw/a.md"): bad},
        )
    assert embedder.calls == writer.calls == []
