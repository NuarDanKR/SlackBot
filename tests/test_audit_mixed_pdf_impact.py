"""The operational impact audit is read-only and emits counts, not source details."""

from pathlib import Path
from types import SimpleNamespace

from scripts import audit_mixed_pdf_impact as audit
from tybot.archive.files import attachment_storage
from tybot.archive.pdf_page_images import PageImages


def _item(archive: Path, file_id: str, data: bytes, state: str = "succeeded") -> SimpleNamespace:
    channel = archive / "pilot" / "C1__test"
    meta = channel / "staging" / file_id / "metadata.json"
    source = channel / "objects" / file_id / "source.pdf"
    meta.parent.mkdir(parents=True, exist_ok=True)
    source.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text("{}", encoding="utf-8")
    source.write_bytes(data)
    return SimpleNamespace(
        object_path=source, meta_path=meta, workspace="pilot", channel_id="C1",
        file_id=file_id, conversion_state=state,
    )


def test_counts_nested_heavy_and_uninspected_pages(tmp_path, monkeypatch):
    item = _item(tmp_path, "F1", b"private document contents")
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: {
        1: PageImages(page_number=1, images=1, area_ratio=0.5, max_depth=2),
        2: PageImages(page_number=2, error="traversal_budget"),
    })

    result = audit.audit_items([item], tmp_path, limit=10, max_bytes=100)

    assert result["counts"]["scanned_documents"] == 1
    assert result["counts"]["pages_with_large_images"] == 1
    assert result["counts"]["pages_with_nested_forms"] == 1
    assert result["counts"]["pages_with_scan_failures"] == 1
    assert result["counts"]["previously_succeeded_at_risk"] == 1
    assert result["scan_failure_codes"] == {"traversal_budget": 1}
    assert "private-name" not in str(result)
    assert "private document" not in str(result)


def test_missing_and_external_originals_are_not_clean_pdfs(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    external = tmp_path / "external.pdf"
    external.write_bytes(b"outside")
    no_path = _item(archive, "F1", b"one")
    no_path.object_path = None
    outside = _item(archive, "F2", b"two")
    outside.object_path = external
    missing = _item(archive, "F3", b"three")
    missing.object_path.unlink()
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: None)

    result = audit.audit_items(
        [no_path, outside, missing], archive, limit=0, max_bytes=100
    )

    assert result["counts"]["scanned_documents"] == 0
    assert result["skipped"] == {
        "no_original_path": 1,
        "outside_expected_objects": 1,
        "unavailable_original": 1,
    }


def test_empty_or_failed_inspection_is_unknown_and_at_risk(tmp_path, monkeypatch):
    item = _item(tmp_path, "F1", b"pdf")

    def broken(_):
        raise RuntimeError("document contents should not be printed")

    monkeypatch.setattr(audit.pdf_page_images, "inspect", broken)
    result = audit.audit_items([item], tmp_path, limit=0, max_bytes=100)
    assert result["counts"]["documents_with_scan_failures"] == 1
    assert result["counts"]["previously_succeeded_at_risk"] == 1
    assert result["scan_failure_codes"] == {"inspection_unavailable": 1}
    assert "document contents" not in str(result)


def test_limit_and_size_cap_are_reported(tmp_path, monkeypatch):
    large = _item(tmp_path, "F1", b"long content")
    small = _item(tmp_path, "F2", b"ok")
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: {
        1: PageImages(page_number=1),
    })

    result = audit.audit_items(
        [large, small, small], tmp_path, limit=2, max_bytes=5
    )

    assert result["selected_pdf_metadata"] == 3
    assert result["attempted"] == 2
    assert result["truncated_by_limit"] is True
    assert result["skipped"] == {"over_size_limit": 1}
    assert result["counts"]["scanned_documents"] == 1
    assert result["counts"]["previously_succeeded_at_risk"] == 0


def test_unknown_failure_text_is_not_exposed(tmp_path, monkeypatch):
    item = _item(tmp_path, "F1", b"pdf")
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: {
        1: PageImages(page_number=1, error="secret document text"),
        2: PageImages(page_number=2, error="matrix_unreadable"),
    })

    result = audit.audit_items([item], tmp_path, limit=0, max_bytes=100)

    assert result["scan_failure_codes"] == {
        "matrix_unreadable": 1,
        "other_inspection_failure": 1,
    }
    assert "secret document text" not in str(result)


def test_legacy_sibling_objects_are_in_scope(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    archive.mkdir()
    storage = attachment_storage(archive, "pilot", "C1")
    meta = storage.staging_dir / "F1" / "metadata.json"
    source = storage.objects_dir / "F1" / "source.pdf"
    meta.parent.mkdir(parents=True)
    source.parent.mkdir(parents=True)
    meta.write_text("{}", encoding="utf-8")
    source.write_bytes(b"pdf")
    item = SimpleNamespace(
        object_path=source, meta_path=meta, workspace="pilot", channel_id="C1",
        file_id="F1", conversion_state="succeeded",
    )
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: {
        1: PageImages(page_number=1, images=1, area_ratio=0.5),
    })

    result = audit.audit_items([item], archive, limit=0, max_bytes=100)

    assert result["counts"]["scanned_documents"] == 1
    assert result["counts"]["previously_succeeded_at_risk"] == 1
    assert result["skipped"] == {}


def test_metadata_cannot_read_another_file_id_original(tmp_path, monkeypatch):
    item = _item(tmp_path, "F1", b"one")
    other = _item(tmp_path, "F2", b"two")
    item.object_path = other.object_path
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: None)

    result = audit.audit_items([item], tmp_path, limit=0, max_bytes=100)

    assert result["counts"]["scanned_documents"] == 0
    assert result["skipped"] == {"outside_expected_objects": 1}


def test_candidate_coordinates_are_opt_in_and_bounded(tmp_path, monkeypatch):
    items = [_item(tmp_path, f"F{i}", b"private document") for i in range(4)]
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: {
        4: PageImages(page_number=4, images=1, area_ratio=0.5),
        9: PageImages(page_number=9, error="content_too_large"),
    })

    default = audit.audit_items(items, tmp_path, limit=0, max_bytes=100)
    result = audit.audit_items(
        items, tmp_path, limit=0, max_bytes=100, candidate_limit=2,
    )

    assert "candidates" not in default
    assert result["counts"]["previously_succeeded_at_risk"] == 4
    assert result["candidates_truncated"] is True
    assert result["candidates_without_safe_ids"] == 0
    assert result["candidates"] == [
        {
            "workspace": "pilot", "channel_id": "C1", "file_id": f"F{i}",
            "heavy_pages": [4], "heavy_page_count": 1,
            "failed_pages": [9], "failed_page_count": 1,
            "inspection_unavailable": False,
        }
        for i in range(2)
    ]
    assert "private document" not in str(result)
    assert "source.pdf" not in str(result)


def test_candidate_includes_whole_inspection_failure_but_not_prior_failure(
    tmp_path, monkeypatch,
):
    succeeded = _item(tmp_path, "F1", b"one")
    failed = _item(tmp_path, "F2", b"two", state="failed")
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: {})

    result = audit.audit_items(
        [succeeded, failed], tmp_path, limit=0, max_bytes=100, candidate_limit=10,
    )

    assert result["counts"]["documents_with_scan_failures"] == 2
    assert result["counts"]["previously_succeeded_at_risk"] == 1
    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["inspection_unavailable"] is True
    assert result["candidates"][0]["file_id"] == "F1"


def test_candidate_masks_unsafe_coordinate_and_caps_page_lists(tmp_path, monkeypatch):
    item = _item(tmp_path, "F-1", b"pdf")
    monkeypatch.setattr(audit.pdf_page_images, "inspect", lambda _: {
        n: PageImages(page_number=n, images=1, area_ratio=0.5)
        for n in range(1, 60)
    })

    masked = audit.audit_items(
        [item], tmp_path, limit=0, max_bytes=100, candidate_limit=10,
    )
    assert len(masked["candidates"]) == 1
    assert masked["candidates_without_safe_ids"] == 1
    assert masked["candidates_truncated"] is False
    assert masked["candidates"][0]["channel_id"] is None
    assert masked["candidates"][0]["file_id"] is None
    assert len(masked["candidates"][0]["candidate_ref"]) == 16
    assert "F-1" not in str(masked)

    item = _item(tmp_path, "F1", b"pdf")
    accepted = audit.audit_items(
        [item], tmp_path, limit=0, max_bytes=100, candidate_limit=10,
    )
    candidate = accepted["candidates"][0]
    assert candidate["heavy_page_count"] == 59
    assert len(candidate["heavy_pages"]) == audit.MAX_PAGES_PER_CANDIDATE


def test_candidate_limit_validation(tmp_path):
    for limit in (-1, 101):
        try:
            audit.audit_items([], tmp_path, limit=0, max_bytes=100, candidate_limit=limit)
        except ValueError:
            pass
        else:
            raise AssertionError("candidate limit must be bounded")
