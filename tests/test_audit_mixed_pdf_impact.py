"""The operational impact audit is read-only and emits counts, not source details."""

from pathlib import Path
from types import SimpleNamespace

from scripts import audit_mixed_pdf_impact as audit
from tybot.archive.pdf_page_images import PageImages


def _item(path: Path | None, state: str = "succeeded") -> SimpleNamespace:
    return SimpleNamespace(object_path=path, conversion_state=state)


def test_counts_nested_heavy_and_uninspected_pages(tmp_path, monkeypatch):
    source = tmp_path / "private-name.pdf"
    source.write_bytes(b"private document contents")
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: {
        1: PageImages(page_number=1, images=1, area_ratio=0.5, max_depth=2),
        2: PageImages(page_number=2, error="traversal_budget"),
    })

    result = audit.audit_items([_item(source)], tmp_path, limit=10, max_bytes=100)

    assert result["counts"]["scanned_documents"] == 1
    assert result["counts"]["pages_with_large_images"] == 1
    assert result["counts"]["pages_with_nested_forms"] == 1
    assert result["counts"]["pages_with_scan_failures"] == 1
    assert result["counts"]["previously_succeeded_at_risk"] == 1
    assert "private-name" not in str(result)
    assert "private document" not in str(result)


def test_missing_and_external_originals_are_not_clean_pdfs(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    external = tmp_path / "external.pdf"
    external.write_bytes(b"outside")
    missing = archive / "missing.pdf"
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: None)

    result = audit.audit_items(
        [_item(None), _item(external), _item(missing)], archive, limit=0, max_bytes=100
    )

    assert result["counts"]["scanned_documents"] == 0
    assert result["skipped"] == {
        "no_original_path": 1,
        "outside_archive": 1,
        "unavailable_original": 1,
    }


def test_empty_or_failed_inspection_is_unknown_and_at_risk(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")

    def broken(_):
        raise RuntimeError("document contents should not be printed")

    monkeypatch.setattr(audit.pdf_page_images, "inspect", broken)
    result = audit.audit_items([_item(source)], tmp_path, limit=0, max_bytes=100)
    assert result["counts"]["documents_with_scan_failures"] == 1
    assert result["counts"]["previously_succeeded_at_risk"] == 1
    assert "document contents" not in str(result)


def test_limit_and_size_cap_are_reported(tmp_path, monkeypatch):
    large = tmp_path / "large.pdf"
    small = tmp_path / "small.pdf"
    large.write_bytes(b"long content")
    small.write_bytes(b"ok")
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: {
        1: PageImages(page_number=1),
    })

    result = audit.audit_items(
        [_item(large), _item(small), _item(small)], tmp_path, limit=2, max_bytes=5
    )

    assert result["selected_pdf_metadata"] == 3
    assert result["attempted"] == 2
    assert result["truncated_by_limit"] is True
    assert result["skipped"] == {"over_size_limit": 1}
    assert result["counts"]["scanned_documents"] == 1
    assert result["counts"]["previously_succeeded_at_risk"] == 0
