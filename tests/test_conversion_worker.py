import json
import time

import pytest

from tybot.archive import conversion_worker as worker
from tybot.archive import convert as conversion
from tybot.archive.external_convert import ExternalConversionError


@pytest.fixture
def spool(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "out").mkdir()
    return tmp_path


def job(root, *, suffix="pdf", expires=None):
    path = root / "in" / ("a" * 32 + ".json")
    path.with_suffix(".bin").write_bytes(b"document")
    path.write_text(json.dumps({"suffix": suffix, "expires": expires or time.time() + 60}))
    return path


def result(root, manifest):
    return json.loads((root / "out" / manifest.name).read_text(encoding="utf-8"))


def test_worker_does_not_reenter_spool(spool, monkeypatch):
    monkeypatch.setenv("TYBOT_CONVERT_SPOOL", str(spool))
    monkeypatch.setitem(conversion._HANDLERS, "pdf", lambda data: ["converted"])
    manifest = job(spool)
    assert worker.process_one(spool, manifest)
    assert result(spool, manifest) == {"status": "success", "lines": ["converted"]}
    assert not worker.process_one(spool, manifest)


@pytest.mark.parametrize("suffix,expires,code", [
    ("pdf", 1, "worker_expired"),
    ("py", None, "conversion_failed"),
])
def test_invalid_jobs_do_not_convert(spool, monkeypatch, suffix, expires, code):
    def forbidden(*args):
        pytest.fail("converter must not run")
    monkeypatch.setattr(conversion, "convert_local", forbidden)
    manifest = job(spool, suffix=suffix, expires=expires)
    assert worker.process_one(spool, manifest)
    assert result(spool, manifest) == {"status": "failed", "code": code}


def test_error_result_contains_no_document_or_stderr(spool, monkeypatch):
    def fail(*args):
        raise ExternalConversionError("private document native error", code="converter_crashed")
    monkeypatch.setattr(conversion, "convert_local", fail)
    manifest = job(spool)
    worker.process_one(spool, manifest)
    assert result(spool, manifest) == {"status": "failed", "code": "converter_crashed"}


def test_client_round_trip_cleans_inputs(spool, monkeypatch):
    monkeypatch.setitem(conversion._HANDLERS, "pdf", lambda data: ["converted"])
    def tick(_):
        for manifest in (spool / "in").glob("*.json"):
            worker.process_one(spool, manifest)
    monkeypatch.setattr(worker.time, "sleep", tick)
    assert worker.request(spool, "pdf", b"document") == ["converted"]
    assert list((spool / "in").iterdir()) == []


def test_manifest_write_failure_cleans_source(spool, monkeypatch):
    real_write = worker._write
    def fail_manifest(path, data):
        if path.suffix == ".json":
            raise PermissionError("private path")
        real_write(path, data)
    monkeypatch.setattr(worker, "_write", fail_manifest)
    with pytest.raises(ExternalConversionError) as caught:
        worker.request(spool, "pdf", b"document")
    assert caught.value.code == "worker_unavailable"
    assert list((spool / "in").iterdir()) == []


def test_client_timeout_cleans_source(spool):
    with pytest.raises(ExternalConversionError) as caught:
        worker.request(spool, "pdf", b"document", timeout=0)
    assert caught.value.code == "converter_timeout"
    assert caught.value.retryable
    assert list((spool / "in").iterdir()) == []


@pytest.mark.parametrize("payload", [
    [], {"code": []}, {"status": "success", "lines": [4]},
    {"status": "success", "lines": [" "]},
])
def test_client_rejects_invalid_output(spool, monkeypatch, payload):
    def tick(_):
        for manifest in (spool / "in").glob("*.json"):
            (spool / "out" / manifest.name).write_text(json.dumps(payload))
    monkeypatch.setattr(worker.time, "sleep", tick)
    with pytest.raises(ExternalConversionError):
        worker.request(spool, "pdf", b"document")
    assert list((spool / "in").iterdir()) == []
