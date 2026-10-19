"""Reconvert retained file-only originals into isolated, immutable staging generations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4

from ..pii_screen import ScreenMetadata, screen_document
from .convert import conversion_stamp, convert_with_coverage


def reconvert(meta: dict, destination: Path, *, render_hwp: bool = False, actor: str = '') -> Path:
    """Caller verifies hash/ownership and holds the collector lock before calling.

    Never replace the original metadata/extracted pair. A generation becomes
    visible only after the receipt points at its completely written metadata.
    Exceptions leave the previous generation and all canonical revisions intact.
    """
    if (meta.get("conversion_state") == "blocked" or meta.get("status") == "pii_refused"
            or meta.get('screen_result') == 'blocked'):
        raise PermissionError("policy-blocked files require separate review")
    raw = Path(meta["object_path"]).read_bytes()
    if hashlib.sha256(raw).hexdigest() != meta.get("sha256"):
        raise ValueError("original changed before reconversion")
    rendered = None
    if render_hwp:
        if str(meta.get('filetype') or '').lower() != 'hwp':
            raise ValueError('rendered HWP recovery cannot process another format')
        from .hwp_render import convert_hwp_rendered

        rendered = convert_hwp_rendered(raw)
        body, coverage = rendered.lines, rendered.coverage
    else:
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
    updated.update(rendered.stamp if rendered else conversion_stamp(
        str(meta.get("filetype") or ""), coverage, produced=not screen.blocked,
    ))
    updated.update(status="pii_refused" if screen.blocked else "converted",
                   conversion_state=state, extracted=not screen.blocked,
                   error_code="pii_refused" if screen.blocked else "",
                   error=None, retryable=False)
    generation = destination / uuid4().hex
    generation.mkdir(parents=True, exist_ok=False)
    if rendered:
        generation.chmod(0o700)
        updated['recovery_actor'] = actor
    if rendered and not screen.blocked:
        # A PDF may contain PII even when text screening passes. Keep it in
        # private staging, never under the searchable archive/attachments tree.
        pdf_path = generation / 'rendered.pdf'
        with pdf_path.open('xb') as stream:
            stream.write(rendered.pdf)
        pdf_path.chmod(0o600)
        updated['rendered_path'] = str(pdf_path.resolve())
    if rendered and screen.blocked:
        updated['converted_at'] = None
        updated.pop('rendered_path', None)
    if not screen.blocked:
        (generation / "extracted.md").write_text(
            "<!-- Local reconversion preview; PII screened. -->\n"
            f"# {meta.get('name') or ''}\n\n" + text.rstrip() + "\n", encoding="utf-8",
        )
    path = generation / "metadata.json"
    path.write_text(json.dumps(updated, ensure_ascii=True) + "\n", encoding="utf-8")
    return path
