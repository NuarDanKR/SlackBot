"""Read-only, content-free impact inventory for mixed PDF image detection.

The metadata reader may omit malformed records. Counts describe only PDF metadata
it returned, not every file in the archive. No conversion or model call occurs.
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from tybot import attachment_review
from tybot.archive import pdf_page_images
from tybot.archive.files import attachment_storage

COUNT_KEYS = (
    "scanned_documents",
    "pages_scanned",
    "pages_with_large_images",
    "pages_with_nested_forms",
    "pages_with_scan_failures",
    "documents_with_large_images",
    "documents_with_nested_forms",
    "documents_with_scan_failures",
    "previously_succeeded_at_risk",
)
SCAN_ERROR_CODES = frozenset({
    pdf_page_images.ERROR_BUDGET,
    pdf_page_images.ERROR_FORM_STREAM,
    pdf_page_images.ERROR_MATRIX,
    pdf_page_images.ERROR_TOO_LARGE,
    pdf_page_images.ERROR_XOBJECT,
})
MAX_CANDIDATES = 100
MAX_PAGES_PER_CANDIDATE = 50
COORDINATE_ID = re.compile(r"[A-Z0-9]{2,32}\Z")


def _expected_object_dir(item, archive: Path) -> Path | None:
    """Bind an original to its metadata coordinate in either supported layout."""
    root = archive.resolve()
    try:
        meta = item.meta_path.resolve(strict=True)
        legacy = attachment_storage(archive, item.workspace, item.channel_id)
        legacy_meta = (legacy.staging_dir / item.file_id / "metadata.json").resolve()
        if meta == legacy_meta:
            allowed_root = (archive.parent / "objects").resolve()
            expected = (legacy.objects_dir / item.file_id).resolve()
        else:
            channel_root = meta.parents[2]
            if (channel_root.parent != root / item.workspace
                    or not channel_root.name.startswith(f"{item.channel_id}__")
                    or meta != channel_root / "staging" / item.file_id / "metadata.json"):
                return None
            allowed_root = root
            expected = (channel_root / "objects" / item.file_id).resolve()
        return expected if expected.is_relative_to(allowed_root) else None
    except (AttributeError, IndexError, OSError, ValueError):
        return None


def _candidate(item, pages: dict) -> dict | None:
    if not (COORDINATE_ID.fullmatch(item.channel_id)
            and COORDINATE_ID.fullmatch(item.file_id)):
        return None
    heavy = sorted(n for n, page in pages.items() if pdf_page_images.image_heavy(page))
    failed = sorted(n for n, page in pages.items() if not page.inspected)
    return {
        "workspace": item.workspace,
        "channel_id": item.channel_id,
        "file_id": item.file_id,
        "heavy_pages": heavy[:MAX_PAGES_PER_CANDIDATE],
        "heavy_page_count": len(heavy),
        "failed_pages": failed[:MAX_PAGES_PER_CANDIDATE],
        "failed_page_count": len(failed),
        "inspection_unavailable": not bool(pages),
    }


def audit_items(
    items, archive: Path, *, limit: int, max_bytes: int, candidate_limit: int = 0,
) -> dict:
    """Count suspect documents; optionally return bounded, content-free coordinates."""
    if limit < 0 or max_bytes <= 0 or not 0 <= candidate_limit <= MAX_CANDIDATES:
        raise ValueError("invalid scan or candidate limit")
    selected = list(items)
    examined = selected[:limit] if limit else selected
    counts: Counter[str] = Counter({key: 0 for key in COUNT_KEYS})
    skipped: Counter[str] = Counter()
    scan_failure_codes: Counter[str] = Counter()
    candidates: list[dict] = []
    candidates_without_safe_ids = 0

    def record_candidate(item, pages: dict) -> None:
        nonlocal candidates_without_safe_ids
        if not candidate_limit:
            return
        candidate = _candidate(item, pages)
        if candidate is None:
            candidates_without_safe_ids += 1
        elif len(candidates) < candidate_limit:
            candidates.append(candidate)

    for item in examined:
        source = item.object_path
        if source is None:
            skipped["no_original_path"] += 1
            continue
        expected = _expected_object_dir(item, archive)
        if expected is None:
            skipped["metadata_outside_known_layout"] += 1
            continue
        try:
            path = Path(source).resolve(strict=True)
            if not path.is_relative_to(expected):
                skipped["outside_expected_objects"] += 1
                continue
            if not path.is_file():
                skipped["unavailable_original"] += 1
                continue
            if path.stat().st_size > max_bytes:
                skipped["over_size_limit"] += 1
                continue
            with path.open("rb") as stream:
                data = stream.read(max_bytes + 1)
            if len(data) > max_bytes:
                skipped["over_size_limit"] += 1
                continue
        except (OSError, ValueError):
            skipped["unavailable_original"] += 1
            continue

        counts["scanned_documents"] += 1
        try:
            pages = pdf_page_images.inspect(data)
        except Exception:  # noqa: BLE001 - malformed PDF must remain unknown, never "no image"
            pages = {}
        if not pages:
            counts["documents_with_scan_failures"] += 1
            counts["previously_succeeded_at_risk"] += item.conversion_state == "succeeded"
            scan_failure_codes["inspection_unavailable"] += 1
            if item.conversion_state == "succeeded":
                record_candidate(item, {})
            continue
        heavy = sum(pdf_page_images.image_heavy(page) for page in pages.values())
        nested = sum(page.max_depth > 0 for page in pages.values())
        failed = sum(not page.inspected for page in pages.values())
        for page in pages.values():
            if not page.inspected:
                code = page.error if page.error in SCAN_ERROR_CODES else "other_inspection_failure"
                scan_failure_codes[code] += 1
        counts["pages_scanned"] += len(pages)
        counts["pages_with_large_images"] += heavy
        counts["pages_with_nested_forms"] += nested
        counts["pages_with_scan_failures"] += failed
        counts["documents_with_large_images"] += int(bool(heavy))
        counts["documents_with_nested_forms"] += int(bool(nested))
        counts["documents_with_scan_failures"] += int(bool(failed))
        counts["previously_succeeded_at_risk"] += (
            item.conversion_state == "succeeded" and bool(heavy or failed)
        )
        if item.conversion_state == "succeeded" and (heavy or failed):
            record_candidate(item, pages)

    result = {
        "read_only": True,
        "scope": "metadata-visible-live-and-legacy-pdf",
        "selected_pdf_metadata": len(selected),
        "attempted": len(examined),
        "truncated_by_limit": len(examined) < len(selected),
        "max_original_bytes": max_bytes,
        "counts": dict(sorted(counts.items())),
        "scan_failure_codes": dict(sorted(scan_failure_codes.items())),
        "skipped": dict(sorted(skipped.items())),
    }
    if candidate_limit:
        result["candidates"] = candidates
        result["candidates_truncated"] = (
            counts["previously_succeeded_at_risk"] > len(candidates)
        )
        result["candidates_without_safe_ids"] = candidates_without_safe_ids
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--limit", type=int, default=100, help="0 scans every selected PDF")
    parser.add_argument("--max-bytes", type=int, default=20_000_000)
    parser.add_argument("--candidate-limit", type=int, default=0,
                        help="opt in to at most 100 at-risk file IDs and page coordinates")
    args = parser.parse_args(argv)
    if args.limit < 0 or args.max_bytes <= 0 or not 0 <= args.candidate_limit <= MAX_CANDIDATES:
        parser.error("--limit >= 0, --max-bytes > 0, --candidate-limit 0..100 required")
    items = [
        item for item in attachment_review.scan(args.archive)
        if item.filetype.lower() == "pdf"
        and (not args.workspace or item.workspace == args.workspace)
    ]
    result = audit_items(
        items, args.archive, limit=args.limit, max_bytes=args.max_bytes,
        candidate_limit=args.candidate_limit,
    )
    result["workspace"] = args.workspace or "all"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
