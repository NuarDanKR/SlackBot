"""Reconversion must preserve original bytes and every previously cited revision."""
from __future__ import annotations

import io
import json

import pytest
from test_archiver_file_recovery import CHANNEL, run
from test_archiver_file_recovery import case as case

from tybot.archive import file_reconversion, file_recovery, files
from tybot.archive.convert import Coverage


@pytest.fixture
def partial(case, monkeypatch):
    monkeypatch.setattr(files, "_converted_state", lambda coverage: "partial")
    assert run(case)["status"] == "retained-not-searchable"
    monkeypatch.setattr(file_reconversion, "convert_with_coverage", lambda *args:
                        (["New OCR body from all pages."], Coverage("page", 3, 3)))
    monkeypatch.setattr(file_reconversion, "conversion_stamp", lambda *args, **kwargs:
                        {"converter_name": "txt:primary", "converter_version": "test-new",
                         "converter_config": {}})
    return case


def reconvert(case, *, apply=True):
    return file_recovery.recover(
        case.client, case.repo, case.owner, case.cfg, ["F123"],
        live_root=case.live, shadow_root=case.shadow, channel_id=CHANNEL,
        apply=apply, actor="operator", reconvert=True,
    )["results"][0]


def test_reconversion_plan_is_read_only(partial, monkeypatch):
    monkeypatch.setattr(file_reconversion, "convert_with_coverage", lambda *args:
                        pytest.fail("plan must not convert"))
    assert reconvert(partial, apply=False)["status"] == "reconvert-planned"
    assert partial.downloads == ["F123"]
    assert not list(partial.base.rglob("reconversions"))


def test_reconversion_preserves_initial_staging_and_uses_retained_original(partial):
    original_meta = partial.base / "staging/F123/metadata.json"
    original_preview = original_meta.with_name("extracted.md")
    before = original_meta.read_bytes(), original_preview.read_bytes()
    assert reconvert(partial)["status"] == "published-index-pending"
    assert (original_meta.read_bytes(), original_preview.read_bytes()) == before
    assert partial.downloads == ["F123"]
    receipt = json.loads((partial.base / "file-inventory/F123.json").read_text())
    meta = json.loads((partial.base / receipt["metadata_relpath"]).read_text())
    assert meta["conversion_state"] == "succeeded"
    assert meta["coverage_converted"] == 3
    assert "origin_message_ts" not in meta
    assert not list(partial.live.rglob("raw"))
    result = file_recovery.index_recovered(partial.live, "mgmt", CHANNEL, ["F123"],
                                           indexer=lambda *args: None)
    assert result["results"][0]["status"] == "recovered"


def test_partial_reconversion_is_not_indexable(partial, monkeypatch):
    monkeypatch.setattr(file_reconversion, "convert_with_coverage", lambda *args:
                        (["Only one page."], Coverage("page", 3, 1, (2, 3))))
    assert reconvert(partial)["status"] == "retained-not-searchable"
    assert not list(partial.base.glob("archive/attachments/*/*.md"))
    result = file_recovery.index_recovered(partial.live, "mgmt", CHANNEL, ["F123"],
                                           indexer=lambda *args: pytest.fail("no index"))
    assert result["results"][0]["status"] == "failed"


def test_unknown_coverage_cannot_become_success(partial, monkeypatch):
    monkeypatch.setattr(file_reconversion, "convert_with_coverage", lambda *args:
                        (["OCR text without page proof."], Coverage()))
    assert reconvert(partial)["conversion_state"] == "partial"


def test_original_tampering_prevents_converter_call(partial, monkeypatch):
    path, = (partial.base / "objects/F123").iterdir()
    path.write_bytes(b"changed")
    monkeypatch.setattr(file_reconversion, "convert_with_coverage", lambda *args:
                        pytest.fail("hash mismatch"))
    assert reconvert(partial)["status"] == "failed"


def test_converter_failure_keeps_previous_metadata_and_preview(partial, monkeypatch):
    meta_path = partial.base / "staging/F123/metadata.json"
    before = meta_path.read_bytes(), meta_path.with_name("extracted.md").read_bytes()

    def fail(*args):
        raise RuntimeError("private converter output")

    monkeypatch.setattr(file_reconversion, "convert_with_coverage", fail)
    result = reconvert(partial)
    assert result == {"file_id": "F123", "status": "failed", "error": "RuntimeError"}
    assert (meta_path.read_bytes(), meta_path.with_name("extracted.md").read_bytes()) == before


def test_new_pii_screening_blocks_reconverted_body(partial, monkeypatch):
    from tybot.pii_screen import ScreenResult

    monkeypatch.setattr(file_reconversion, "screen_document", lambda *args, **kwargs:
                        ScreenResult(True, "fixture", ()))
    assert reconvert(partial)["conversion_state"] == "blocked"
    assert not list(partial.base.glob("archive/attachments/*/*.md"))
    previews = list(partial.base.glob("staging/F123/reconversions/*/extracted.md"))
    assert not previews


def test_resume_after_publish_failure_does_not_repeat_ocr(partial, monkeypatch):
    real = file_recovery.attachment_writer.write_docs

    def fail(*args, **kwargs):
        raise OSError("fixture")

    monkeypatch.setattr(file_recovery.attachment_writer, "write_docs", fail)
    assert reconvert(partial)["status"] == "failed"
    monkeypatch.setattr(file_recovery.attachment_writer, "write_docs", real)
    monkeypatch.setattr(file_reconversion, "convert_with_coverage", lambda *args:
                        pytest.fail("resume must not OCR again"))
    assert run(partial)["status"] == "published-index-pending"


def test_receipt_cannot_redirect_to_other_files_metadata(partial):
    path = partial.base / "file-inventory/F123.json"
    receipt = json.loads(path.read_text())
    receipt["metadata_relpath"] = "staging/F999/metadata.json"
    path.write_text(json.dumps(receipt))
    assert run(partial)["status"] == "failed"


def test_page_ocr_coverage_flows_through_recovery_without_downloading_again(case, monkeypatch):
    from pypdf import PdfReader, PdfWriter

    from tybot.archive import convert as cv

    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    original = buffer.getvalue()
    case.client.file.update(name="minutes.pdf", filetype="pdf")
    calls = []
    monkeypatch.setattr(files, "download_bytes", lambda *args: original)

    def old_convert(*args):
        cv._record("page", total=3, converted=0, missing=(1, 2, 3))
        return ["Legacy merged OCR preview without page coverage."]

    monkeypatch.setattr(files, "convert", old_convert)
    assert run(case)["status"] == "retained-not-searchable"
    monkeypatch.setattr(files, "download_bytes", lambda *args: pytest.fail("no redownload"))

    def ocr(data, suffix, *, force_ocr=False):
        assert len(PdfReader(io.BytesIO(data)).pages) == 1
        assert suffix == "pdf" and force_ocr
        calls.append(1)
        return ["[변환 안내] OCR 사용 - 금액·날짜·조문은 원본 확인 필요",
                "Verified OCR page body."]

    monkeypatch.setattr(cv, "kordoc_lines", ocr)
    result = reconvert(case)
    assert result["status"] == "published-index-pending"
    assert calls == [1, 1, 1]
    receipt = json.loads((case.base / "file-inventory/F123.json").read_text())
    meta = json.loads((case.base / receipt["metadata_relpath"]).read_text())
    assert meta["coverage_total"] == meta["coverage_converted"] == 3
    assert meta["missing_units"] == []
    assert meta["converter_version"] == "3"
