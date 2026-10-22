# Mixed PDF image detection: read-only impact audit

This audit counts PDF metadata records whose retained originals are available under
the live archive root. It does not convert, repair, index, call a model, or write to
the archive. It emits aggregate counts only; no document names, IDs, paths, or text.
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

The script skips missing originals and paths outside the live archive root. A high
skip count or `truncated_by_limit=true` means the result must not be presented as a
workspace-wide percentage. Compare representative flagged pages with the original
before deciding whether to re-convert or request Vision approval. The audit itself
does not approve or transmit any page image.
