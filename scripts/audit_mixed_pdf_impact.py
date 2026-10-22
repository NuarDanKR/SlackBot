"""Read-only, content-free impact inventory for mixed PDF image detection.

The metadata reader may omit malformed records. Counts describe only PDF metadata
it returned, not every file in the archive. No conversion or model call occurs.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from tybot import attachment_review
from tybot.archive import pdf_page_images

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


def audit_items(items, archive: Path, *, limit: int, max_bytes: int) -> dict:
    """Count suspect documents without returning paths, names, or document text."""
    if limit < 0 or max_bytes <= 0:
        raise ValueError("limit must be nonnegative and max_bytes must be positive")
    root = archive.resolve()
    selected = list(items)
    examined = selected[:limit] if limit else selected
    counts: Counter[str] = Counter({key: 0 for key in COUNT_KEYS})
    skipped: Counter[str] = Counter()
    scan_failure_codes: Counter[str] = Counter()

    for item in examined:
        source = item.object_path
        if source is None:
            skipped["no_original_path"] += 1
            continue
        try:
            path = Path(source).resolve(strict=True)
            if not path.is_relative_to(root):
                skipped["outside_archive"] += 1
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

    return {
        "read_only": True,
        "scope": "metadata-visible-live-pdf",
        "selected_pdf_metadata": len(selected),
        "attempted": len(examined),
        "truncated_by_limit": len(examined) < len(selected),
        "max_original_bytes": max_bytes,
        "counts": dict(sorted(counts.items())),
        "scan_failure_codes": dict(sorted(scan_failure_codes.items())),
        "skipped": dict(sorted(skipped.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--workspace", default="")
    parser.add_argument("--limit", type=int, default=100, help="0 scans every selected PDF")
    parser.add_argument("--max-bytes", type=int, default=20_000_000)
    args = parser.parse_args(argv)
    if args.limit < 0 or args.max_bytes <= 0:
        parser.error("--limit must be >= 0 and --max-bytes must be > 0")
    items = [
        item for item in attachment_review.scan(args.archive)
        if item.filetype.lower() == "pdf"
        and (not args.workspace or item.workspace == args.workspace)
    ]
    result = audit_items(items, args.archive, limit=args.limit, max_bytes=args.max_bytes)
    result["workspace"] = args.workspace or "all"
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
