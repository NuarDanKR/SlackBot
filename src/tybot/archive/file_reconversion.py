"""Reconvert retained file-only originals into isolated, immutable staging generations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from ..pii_screen import ScreenMetadata, screen_document
from .convert import conversion_stamp, convert_with_coverage


def reconvert(meta: dict, destination: Path) -> Path:
    """Caller verifies hash/ownership and holds the collector lock before calling.

    Never replace the original metadata/extracted pair. A generation becomes
    visible only after the receipt points at its completely written metadata.
    Exceptions leave the previous generation and all canonical revisions intact.
    """
    if meta.get("conversion_state") == "blocked" or meta.get("status") == "pii_refused":
        raise PermissionError("policy-blocked files require separate review")
    raw = Path(meta["object_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
        raise ValueError("original changed before reconversion")
    body, coverage = convert_with_coverage(str(meta.get("filetype") or ""), raw)
    text = "\n".join(body)
    if not text.strip():
        raise ValueError("empty conversion output")
    screen = screen_document(text, filename=str(meta.get("name") or ""),
                             coverage_state=coverage.state)
    state = "blocked" if screen.blocked else (
        "succeeded" if coverage.state == "succeeded" else "partial"
    )
    updated = dict(meta)
    updated.update(coverage.to_json())
    updated.update(ScreenMetadata.of(screen).to_json())
    updated.update(conversion_stamp(str(meta.get("filetype") or ""), coverage,
                                    produced=not screen.blocked))
    updated.update(status="pii_refused" if screen.blocked else "converted",
                   conversion_state=state, extracted=not screen.blocked,
                   error_code="pii_refused" if screen.blocked else "",
                   error=None, retryable=False)
    generation = destination / uuid4().hex
    generation.mkdir(parents=True, exist_ok=False)
    if not screen.blocked:
        (generation / "extracted.md").write_text(
            "<!-- Local reconversion preview; PII screened. -->\n"
            f"# {meta.get('name') or ''}\n\n" + text.rstrip() + "\n", encoding="utf-8",
        )
    path = generation / "metadata.json"
    path.write_text(json.dumps(updated, ensure_ascii=True) + "\n", encoding="utf-8")
    return path
