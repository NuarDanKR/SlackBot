"""Explicit local HWP recovery. No model calls or canonical publication.

Keep the rendered PDF: its page coordinates are NOT original HWP print pages.
Only the opt-in recovery preparation path uses this module.
"""
from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

from . import external_convert as external
from .convert import Coverage, config_snapshot, convert_with_coverage, converter_version


@dataclass(frozen=True)
class RenderedHwp:
    lines: list[str]
    coverage: Coverage
    pdf: bytes
    stamp: dict


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _tool(binary: str, work: Path) -> dict:
    result = external._run([binary, '--version'], cwd=work, home=work)
    if not result.stdout.strip():
        raise external.ExternalConversionError('Converter identity unavailable', code='converter_identity_unverified')
    return dict(executable_sha256=_hash_file(Path(binary)),
                version_sha256=hashlib.sha256(result.stdout.encode()).hexdigest())


def _fonts(work: Path) -> str:
    binary = external._binary('FONTCONFIG_BIN', 'fc-list')
    result = external._run([binary, '--format=%{file}\n'], cwd=work, home=work)
    paths = sorted(set(line.strip() for line in result.stdout.splitlines() if line.strip()))
    if not paths:
        raise external.ExternalConversionError('Font identity unavailable', code='converter_identity_unverified')
    digest = hashlib.sha256()
    for path in paths:
        digest.update(json.dumps([path, _hash_file(Path(path))]).encode())
    return digest.hexdigest()


def _environment(work: Path) -> tuple[str, dict]:
    soffice = external._binary('LIBREOFFICE_BIN', 'soffice', 'libreoffice')
    try:
        kordoc = external._binary('KORDOC_BIN', 'kordoc')
    except external.ExternalConverterUnavailable:
        ocr = {'availability': 'missing'}
    else:
        ocr = _tool(kordoc, work)
    return soffice, dict(renderer=_tool(soffice, work), fonts_sha256=_fonts(work),
                         ocr=ocr, pypdf=version('pypdf'))


def convert_hwp_rendered(data: bytes) -> RenderedHwp:
    """Render once, retain those exact bytes, then use page-aware PDF extraction.

    Missing local tools/identity fail closed. Never silently use the old short
    direct-HWP extraction result if rendering fails.
    """
    if not data.startswith(bytes.fromhex('D0CF11E0A1B11AE1')):
        raise ValueError('HWP recovery requires an OLE2 container')
    with tempfile.TemporaryDirectory(prefix='tybot-hwp-render-') as raw:
        work = Path(raw)
        binary, environment = _environment(work)
        source = work / 'source.hwp'
        output, profile = work / 'out', work / 'profile'
        output.mkdir()
        profile.mkdir()
        source.write_bytes(data)
        external._run([
            binary, f'-env:UserInstallation={profile.resolve().as_uri()}',
            '--headless', '--norestore', '--convert-to', 'pdf', '--outdir', str(output), str(source),
        ], cwd=work, home=work)
        path = output / 'source.pdf'
        if not path.is_file():
            raise external.ExternalConversionError('Rendered PDF missing', code='render_output_missing')
        pdf = path.read_bytes()
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(pdf))
        if reader.is_encrypted or not reader.pages:
            raise external.ExternalConversionError('Rendered PDF invalid', code='render_output_invalid')
        lines, coverage = convert_with_coverage('pdf', pdf)
        # PDF page coverage cannot prove that LibreOffice retained the HWP's
        # original layout/content. Do not label an unreviewed render complete.
        coverage.note('rendered_layout_unverified')
        _, after = _environment(work)
        if after != environment:
            raise external.ExternalConversionError('Converter changed during recovery', code='converter_identity_changed')
        rendered_sha256 = hashlib.sha256(pdf).hexdigest()
        renderer_version = hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
        # Output hashing also separates differing OCR results when a native
        # dependency changes without changing its launcher's version banner.
        lines = ['[Conversion: rendered PDF page numbers, not original HWP print pages]', *lines]
        config = dict(pdf_config=config_snapshot('pdf'), pdf_converter_version=converter_version('pdf'),
                      tools=environment, page_basis='rendered_pdf', rendered_sha256=rendered_sha256,
                      extracted_sha256=hashlib.sha256('\n'.join(lines).encode()).hexdigest())
        stamp = dict(
            converter_name='hwp:rendered-pdf', converter_version='1', converter_config=config,
            page_basis='rendered_pdf', rendered_sha256=rendered_sha256,
            renderer_version=renderer_version, render_review_state='pending',
            converted_at=datetime.now(UTC).isoformat(timespec='seconds'),
        )
        return RenderedHwp(lines, coverage, pdf, stamp)
