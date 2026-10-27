from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from tybot.archive import image_description_worker as worker
from tybot.archive.image_description import (
    PROMPT_REVISION,
    STATUS_DESCRIBED,
    ImageDescription,
    ImageInput,
    ImageInputError,
    ImageUsage,
)

IMAGE = ImageInput("tyit", "C12345678", "F12345678", "a" * 64, b"image", "image/png")
RESULT = ImageDescription(
    status=STATUS_DESCRIBED,
    workspace=IMAGE.workspace,
    channel=IMAGE.channel,
    file_id=IMAGE.file_id,
    original_sha256=IMAGE.original_sha256,
    prompt_revision=PROMPT_REVISION,
    description="건설현장 외부에 굴착기가 보인다.",
    tags=("건설현장", "굴착기"),
    usage=ImageUsage(
        model="free-vision-v1",
        provider="approved-provider",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.0,
    ),
)


class Store:
    def __init__(self, *, revision="revision-1", finish_error=None):
        self.revision = revision
        self.finish_error = finish_error
        self.events = []

    def begin_dispatch(self, job_id, image, *, checks):
        self.events.append(("begin", job_id, image, checks))
        return SimpleNamespace(
            model="free-vision-v1",
            provider="approved-provider",
            max_output_tokens=256,
            timeout_seconds=30,
        )

    def finish_and_publish(self, job_id, result):
        self.events.append(("finish", job_id, result))
        if self.finish_error:
            raise self.finish_error
        return self.revision

    def finish_without_call(self, job_id, *, code):
        self.events.append(("without_call", job_id, code))

    def mark_unknown(self, job_id, *, code):
        self.events.append(("unknown", job_id, code))


def test_one_reserved_job_makes_exactly_one_model_call(monkeypatch):
    calls = []

    def describe(image, **kwargs):
        calls.append((image, kwargs))
        return RESULT

    monkeypatch.setattr(worker, "describe_image", describe)
    store = Store()
    run = worker.dispatch_reserved_image(
        store, "job-1", IMAGE, router=object(), checks={"access_verified": True}
    )
    assert len(calls) == 1
    assert calls[0][1]["model"] == "free-vision-v1"
    assert run.state == "published" and run.revision_id == "revision-1"
    assert [event[0] for event in store.events] == ["begin", "finish"]


def test_a_call_preflight_error_is_closed_without_marking_a_transmission(monkeypatch):
    def refuse(*args, **kwargs):
        raise ImageInputError("secret provider detail")

    monkeypatch.setattr(worker, "describe_image", refuse)
    store = Store()
    run = worker.dispatch_reserved_image(store, "job-1", IMAGE, router=object(), checks={})
    assert (run.state, run.code) == ("blocked", "call_preflight_rejected")
    assert store.events[-1] == ("without_call", "job-1", "call_preflight_rejected")
    assert "secret provider detail" not in repr(store.events)


def test_a_publish_failure_becomes_unknown_and_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(
        worker,
        "describe_image",
        lambda *args, **kwargs: calls.append(1) or RESULT,
    )
    store = Store(finish_error=RuntimeError("db failed"))
    with pytest.raises(worker.ImageDescriptionWorkerError) as caught:
        worker.dispatch_reserved_image(store, "job-1", IMAGE, router=object(), checks={})
    assert calls == [1]
    assert store.events[-1] == ("unknown", "job-1", "publish_result_unknown")
    assert caught.value.code == "publish_failed"
    assert caught.value.exception_type == "RuntimeError"
    assert "db failed" not in str(caught.value)


def test_a_blocked_result_closes_without_a_revision(monkeypatch):
    monkeypatch.setattr(worker, "describe_image", lambda *args, **kwargs: RESULT)
    store = Store(revision="")
    run = worker.dispatch_reserved_image(store, "job-1", IMAGE, router=object(), checks={})
    assert run.state == "closed_without_revision"
    assert run.revision_id == ""


def test_worker_does_not_return_the_sensitive_model_result(monkeypatch):
    uncertain = replace(RESULT, status="uncertain", uncertainty=("blurred",))
    monkeypatch.setattr(worker, "describe_image", lambda *args, **kwargs: uncertain)
    store = Store()
    run = worker.dispatch_reserved_image(store, "job-1", IMAGE, router=object(), checks={})
    assert store.events[-1][2] is uncertain
    assert not hasattr(run, "result")
    assert "건설현장" not in repr(run)
