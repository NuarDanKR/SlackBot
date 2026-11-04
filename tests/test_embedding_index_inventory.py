from __future__ import annotations

import hashlib

import pytest

from tybot.archive.embedding_source import ATTACHMENT, CHANNEL, VectorSource
from tybot.embedding_index_inventory import inventory
from tybot.embedding_index_job import IndexLimits


def _source(
    text: str,
    *,
    workspace: str = "alpha",
    channel: str = "C-ONE",
    path: str = "workspaces/alpha/channels/C-ONE/raw/2026-11-01.md",
    kind: str = CHANNEL,
    revision: str | None = None,
) -> VectorSource:
    return VectorSource(
        workspace=workspace,
        channel_id=channel,
        doc_path=path,
        kind=kind,
        text=text,
        source_revision=revision or hashlib.sha256(text.encode()).hexdigest(),
    )


def test_inventory_reports_only_aggregate_capacity_and_stable_revisions():
    first = _source("# first\n\nbody\n")
    second = _source(
        "attachment body\n",
        workspace="beta",
        channel="C-TWO",
        path="workspaces/beta/channels/C-TWO/attachments/F-ONE/rev.md",
        kind=ATTACHMENT,
    )

    report = inventory([first, second], model_revision="embed-v1")
    payload = report.to_json()

    assert payload["schema"] == "tybot.embedding-index-preview.v1"
    assert payload["readOnly"] is True
    assert payload["modelCalls"] == 0
    assert payload["databaseWrites"] == 0
    assert payload["modelRevision"] == "embed-v1"
    assert payload["chunkerRevision"] == "source-v1:chunk-v1:max=1200:ov=120"
    assert payload["documents"] == 2
    assert payload["plannedDocuments"] == 2
    assert payload["plannedChunks"] == 2
    assert payload["minimumBatches"] == 1
    assert payload["outcomes"] == {"planned": 2}
    assert [row["workspace"] for row in payload["workspaces"]] == ["alpha", "beta"]


def test_report_has_no_body_or_document_coordinates():
    secret_body = "PRIVATE-BODY-MARKER"
    source = _source(secret_body)

    rendered = repr(inventory([source], model_revision="embed-v1").to_json())

    assert secret_body not in rendered
    assert source.channel_id not in rendered
    assert source.doc_path not in rendered
    assert source.source_revision not in rendered


def test_workspace_filter_does_not_plan_other_workspaces(monkeypatch):
    kept = _source("kept\n", workspace="alpha")
    skipped = _source("skipped\n", workspace="beta")
    seen: list[str] = []
    from tybot import embedding_index_inventory as target

    original = target.plan_source

    def recording(source, **kwargs):
        seen.append(source.workspace)
        return original(source, **kwargs)

    monkeypatch.setattr(target, "plan_source", recording)

    report = inventory(
        [skipped, kept], model_revision="embed-v1", workspace="alpha"
    )

    assert seen == ["alpha"]
    assert report.documents == 1
    assert [item.workspace for item in report.workspaces] == ["alpha"]


def test_large_and_invalid_sources_are_counted_without_leaking_reasons():
    large = _source("x" * 11)
    invalid = _source("bad\n", revision="0" * 64)
    limits = IndexLimits(max_chars_per_document=10)

    report = inventory(
        [large, invalid], model_revision="embed-v1", limits=limits
    )

    assert report.documents == 2
    assert report.planned_documents == 0
    assert report.minimum_batches == 0
    assert dict(report.outcomes) == {"plan_rejected": 1, "source_too_large": 1}


def test_minimum_batches_is_the_strictest_runtime_axis():
    sources = [_source(f"body {number}\n", path=f"doc-{number}") for number in range(5)]
    limits = IndexLimits(
        max_documents=4,
        max_calls=2,
        max_total_chars=1_000,
        max_total_chunks=100,
    )

    report = inventory(sources, model_revision="embed-v1", limits=limits)

    assert report.planned_documents == 5
    assert report.minimum_batches == 3


@pytest.mark.parametrize("revision", ["", " model", "model "])
def test_model_revision_must_be_exact(revision):
    with pytest.raises(ValueError, match="model_revision"):
        inventory([], model_revision=revision)


def test_workspace_filter_must_be_exact():
    with pytest.raises(ValueError, match="workspace"):
        inventory([], model_revision="embed-v1", workspace=" alpha")
