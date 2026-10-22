# Mixed PDF image detection: read-only impact audit

This audit counts PDF metadata records whose retained originals are available in
the live per-channel object layout or the legacy sibling `objects/workspaces` layout.
It does not convert, repair, index, call a model, or write to
the archive. By default it emits aggregate counts only; no document names, IDs,
paths, or text. An explicit candidate option emits bounded safe file IDs and page
numbers. IDs outside the safe coordinate pattern are replaced by opaque references;
names, paths, and extracted content are never printed.
Run it after the nested-Form scanner's fail-closed parsing checks are deployed.

## Server pilot

Start with one workspace and a small limit. Use the deployed Python environment:

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env \
  timeout 300s /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/audit_mixed_pdf_impact.py \
  --archive /var/lib/tybot/archive --workspace tyit \
  --limit 20 --max-bytes 5000000
```

Increase `--limit` only after confirming the pilot finishes normally. `--limit 0`
means all selected metadata, not all PDFs on disk. The default size cap is 20 MB
per original; larger originals are reported in `skipped.over_size_limit`.

`selected_pdf_metadata` is the number of matching metadata records returned by
`attachment_review.scan`, not a distinct-file count or a completeness guarantee.
`attempted` is the prefix selected by the limit. `scanned_documents` means image
inspection returned; a nonzero `documents_with_scan_failures` means the inspection
was incomplete or failed, not that there were no images. `previously_succeeded_at_risk`
counts prior `conversion_state=succeeded` records with large image regions or an
inspection failure. It is a review queue estimate, not proof of unread content.
`scan_failure_codes` counts known page-level reasons; `inspection_unavailable`
counts whole documents that could not be inspected. Unexpected error strings are
collapsed into `other_inspection_failure` and never printed verbatim.

The first server run over 153 TYIT metadata records scanned only 30 originals:
122 were reported as `outside_archive` by the old script because it excluded the
legitimate legacy sibling object store, and one exceeded the 5 MB cap. Those 30
results must not be treated as the workspace-wide rate. Rerun after deploying the
layout fix; `scope` should be `metadata-visible-live-and-legacy-pdf` and the 122
previously excluded records should be scanned or have a specific remaining skip reason.

The script now binds each source path to its metadata workspace, channel, and file ID.
It skips missing originals and paths outside the corresponding object directory.
A high
skip count or `truncated_by_limit=true` means the result must not be presented as a
workspace-wide percentage. Compare representative flagged pages with the original
before deciding whether to re-convert or request Vision approval. The audit itself
does not approve or transmit any page image.

## TYIT full scan and review coordinates

The 2026-10-22 run with `--limit 0 --max-bytes 20000000` attempted all 153
metadata records. It inspected 152 originals and 1,456 pages; one original was
over the size cap. It flagged 85 documents with large images, including 26
previously marked `succeeded`. Nine documents contained nested Forms. One page
reported `content_too_large`, so it remains uninspected. These are metadata
records, not a distinct-file count, and the 26 are review candidates rather
than confirmed missing content.

After deploying the coordinate option, rerun the same read-only audit to find
the 26 candidate records. Do not paste its output into tickets or public logs;
file IDs and page coordinates can still identify internal documents.

```bash
sudo -u tybot env TYBOT_ENV_FILE=/etc/tybot/archiver.env \
  timeout 300s /opt/tybot/.venv/bin/python \
  /opt/tybot/scripts/audit_mixed_pdf_impact.py \
  --archive /var/lib/tybot/archive --workspace tyit \
  --limit 0 --max-bytes 20000000 --candidate-limit 30
```

`candidates_truncated=false` is required before treating the list as complete.
`candidates_without_safe_ids` counts entries that have null IDs and a
`candidate_ref` instead; they remain in the list but require private coordinate
resolution before targeted action. The first candidate run returned 24 entries
and omitted two unsafe-ID records; rerun with this version to account for all 26.
Each entry has `heavy_pages` and
`failed_pages`, capped at 50 numbers each; compare the count fields to detect
truncation. A whole-document inspection failure has `inspection_unavailable=true`
and no page coordinates. Compare representative flagged pages with the retained
original before re-conversion or Vision approval. The one original over 20 MB
and the `content_too_large` page need separate bounded inspection; do not
silently classify either as image-free.
