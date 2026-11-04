from __future__ import annotations

import hashlib

import pytest

from tybot.archive import chunking
from tybot.archive.embedding_source import ATTACHMENT, CHANNEL, DM, VectorSource
from tybot.embedding_index_plan import IndexPlanError, plan_source
from tybot.embedding_index_store import PublishResult


def source(
    text: str = "# 합성 문서\n\n합성 본문입니다.\n",
    *,
    kind: str = CHANNEL,
    revision: str | None = None,
) -> VectorSource:
    return VectorSource(
        workspace="sample",
        channel_id="C-SAMPLE",
        doc_path="workspaces/sample/channels/C-SAMPLE/raw/2026-01-01.md",
        kind=kind,
        text=text,
        source_revision=revision or hashlib.sha256(text.encode()).hexdigest(),
    )


def test_plan_binds_source_chunker_and_model_revisions():
    plan = plan_source(source(), model_revision="local-test-v1")

    assert plan.key.workspace == "sample"
    assert plan.key.source_revision == hashlib.sha256(source().text.encode()).hexdigest()
    assert plan.key.chunker_revision == "source-v1:chunk-v1:max=1200:ov=120"
    assert plan.key.model_revision == "local-test-v1"
    assert plan.kind == CHANNEL
    assert plan.chunk_count == len(plan.inputs()) > 0
    assert plan.char_count == len(source().text)


def test_inputs_are_exact_slices_with_stable_hashes_and_hidden_repr():
    text = "# 제목\n\n" + ("긴 합성 문장입니다. " * 100)
    plan = plan_source(
        source(text),
        model_revision="model-v1",
        spec=chunking.ChunkSpec(max_chars=200, overlap=40),
    )

    inputs = plan.inputs()
    assert len(inputs) > 1
    assert [item.chunk_no for item in inputs] == list(range(len(inputs)))
    assert all(item.text_sha256 == hashlib.sha256(item.text.encode()).hexdigest() for item in inputs)
    assert text not in repr(plan)
    assert inputs[0].text not in repr(inputs[0])


@pytest.mark.parametrize("kind", [DM, "unknown", ""])
def test_dm_and_unknown_source_kinds_are_rejected(kind):
    with pytest.raises(IndexPlanError, match="kind_not_enabled"):
        plan_source(source(kind=kind), model_revision="model-v1")


@pytest.mark.parametrize("text", ["", "  \n\t", "nul\x00body\n", "crlf\r\n"])
def test_noncanonical_or_empty_sources_are_rejected(text):
    with pytest.raises(IndexPlanError, match=r"empty|not_canonical"):
        plan_source(source(text), model_revision="model-v1")


def test_source_revision_and_document_identity_are_checked_before_chunking(monkeypatch):
    called = False

    def fail_if_called(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError

    monkeypatch.setattr(chunking, "split", fail_if_called)
    with pytest.raises(IndexPlanError, match="revision_mismatch"):
        plan_source(source(revision="0" * 64), model_revision="model-v1")
    with pytest.raises(IndexPlanError, match="identity_invalid"):
        bad = source()
        plan_source(
            VectorSource(**{**bad.__dict__, "workspace": " sample"}),
            model_revision="model-v1",
        )
    assert not called


def test_a_gap_in_chunk_output_is_rejected(monkeypatch):
    text = "first body second"
    bad = [
        chunking.Chunk("first", 0, 5, "text", 1, 1),
        chunking.Chunk("second", 11, 17, "text", 1, 1),
    ]
    monkeypatch.setattr(chunking, "split", lambda *_args, **_kwargs: bad)

    with pytest.raises(IndexPlanError, match="coverage_incomplete"):
        plan_source(source(text), model_revision="model-v1")


def test_mutated_chunk_text_and_oversize_chunks_are_rejected(monkeypatch):
    text = "a" * 100
    mutated = [chunking.Chunk("b" * 100, 0, 100, "text", 1, 1)]
    monkeypatch.setattr(chunking, "split", lambda *_args, **_kwargs: mutated)
    with pytest.raises(IndexPlanError, match="contract_broken"):
        plan_source(source(text), model_revision="model-v1")

    oversized = [chunking.Chunk(text, 0, 100, "text", 1, 1)]
    monkeypatch.setattr(chunking, "split", lambda *_args, **_kwargs: oversized)
    with pytest.raises(IndexPlanError, match="contract_broken"):
        plan_source(
            source(text),
            model_revision="model-v1",
            spec=chunking.ChunkSpec(max_chars=64, overlap=8),
        )


def test_attachment_sources_use_the_same_plan_contract():
    plan = plan_source(source(kind=ATTACHMENT), model_revision="model-v1")
    assert plan.kind == ATTACHMENT


def test_publish_hands_the_writer_exactly_the_planned_source_and_chunks():
    plan = plan_source(source(), model_revision="model-v1")

    class Writer:
        def __init__(self):
            self.calls = []

        def publish(self, key, source_text, chunks, embeddings):
            self.calls.append((key, source_text, chunks, embeddings))
            return PublishResult(17, len(chunks), False)

    writer = Writer()
    embeddings = [[0.1, 0.2] for _ in plan.inputs()]
    result = plan.publish(writer, embeddings)

    assert result == PublishResult(17, plan.chunk_count, False)
    assert len(writer.calls) == 1
    key, body, chunks, sent_embeddings = writer.calls[0]
    assert key is plan.key
    assert body == source().text
    assert [chunk.text for chunk in chunks] == [item.text for item in plan.inputs()]
    assert sent_embeddings is embeddings
