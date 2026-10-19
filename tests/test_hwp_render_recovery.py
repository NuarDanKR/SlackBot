from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot.archive import file_reconversion, hwp_render
from tybot.archive.attachment_doc import revision_for
from tybot.archive.convert import Coverage

HWP = bytes.fromhex('D0CF11E0A1B11AE1') + b'synthetic fixture, not an office document'


def pdf_bytes(pages=3):
    from pypdf import PdfWriter

    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


@pytest.fixture
def renderer(monkeypatch):
    blob = pdf_bytes()
    environment = dict(renderer={'version': 'fixture-1'}, fonts_sha256='fonts-1',
                       ocr={'version': 'fixture-2'}, pypdf='fixture-3')
    calls = []
    monkeypatch.setattr(hwp_render, '_environment', lambda work: ('fixture-soffice', environment.copy()))

    def run(command, *, cwd, home):
        calls.append(command)
        (cwd / 'out/source.pdf').write_bytes(blob)
        return SimpleNamespace(stdout='', stderr='', returncode=0)

    monkeypatch.setattr(hwp_render.external, '_run', run)
    monkeypatch.setattr(hwp_render, 'convert_with_coverage', lambda kind, data:
                        (['[1쪽]', 'Synthetic table | 123 |', '[3쪽]', 'Synthetic body'],
                         Coverage('page', 3, 2, (2,))))
    return SimpleNamespace(blob=blob, calls=calls)


def test_render_uses_pdf_extraction_and_preserves_partial_and_page_basis(renderer, monkeypatch):
    seen = []

    def convert(kind, data):
        seen.append((kind, data))
        return ['Synthetic body'], Coverage('page', 3, 2, (2,))

    monkeypatch.setattr(hwp_render, 'convert_with_coverage', convert)
    result = hwp_render.convert_hwp_rendered(HWP)
    assert seen == [('pdf', renderer.blob)]
    assert result.coverage.state == 'partial' and result.coverage.missing == (2,)
    assert result.stamp['page_basis'] == 'rendered_pdf'
    assert result.stamp['converter_name'] == 'hwp:rendered-pdf'
    assert result.stamp['rendered_sha256'] == hashlib.sha256(renderer.blob).hexdigest()
    assert '--headless' in renderer.calls[0]
    assert any(arg.startswith('-env:UserInstallation=') for arg in renderer.calls[0])


def test_render_failure_never_falls_back_to_direct_hwp(renderer, monkeypatch):
    def fail(*args, **kwargs):
        raise hwp_render.external.ExternalConversionError('fixture', code='converter_crashed')

    monkeypatch.setattr(hwp_render.external, '_run', fail)
    with pytest.raises(hwp_render.external.ExternalConversionError):
        hwp_render.convert_hwp_rendered(HWP)


def test_changed_environment_rejects_unbound_output(renderer, monkeypatch):
    counter = []

    def environment(work):
        counter.append(1)
        return 'fixture-soffice', {'version': str(len(counter))}

    monkeypatch.setattr(hwp_render, '_environment', environment)
    with pytest.raises(hwp_render.external.ExternalConversionError, match='changed'):
        hwp_render.convert_hwp_rendered(HWP)


def test_all_rendered_pages_read_does_not_mean_hwp_layout_verified(renderer, monkeypatch):
    monkeypatch.setattr(hwp_render, 'convert_with_coverage', lambda *args:
                        (['Synthetic complete PDF text'], Coverage('page', 3, 3)))
    result = hwp_render.convert_hwp_rendered(HWP)
    assert result.coverage.converted == result.coverage.total == 3
    assert result.coverage.state == 'partial'
    assert 'rendered_layout_unverified' in result.coverage.flags
    assert result.stamp['render_review_state'] == 'pending'


@pytest.mark.parametrize('blob', [b'', b'PK\x03\x04', b'%PDF-1.7'])
def test_wrong_container_does_not_launch_tools(monkeypatch, blob):
    monkeypatch.setattr(hwp_render, '_environment', lambda work: pytest.fail('no launch'))
    with pytest.raises(ValueError):
        hwp_render.convert_hwp_rendered(blob)


def test_new_identity_cannot_collide_with_direct_hwp_revision(renderer):
    result = hwp_render.convert_hwp_rendered(HWP)
    original = hashlib.sha256(HWP).hexdigest()
    old = revision_for(sha256=original, converter_name='hwp:primary', converter_version='2')
    stamp = result.stamp
    new = revision_for(sha256=original, converter_name=stamp['converter_name'],
                       converter_version=stamp['converter_version'], config=stamp['converter_config'])
    assert old != new


def test_different_extraction_with_same_pdf_and_tools_has_a_new_identity(renderer, monkeypatch):
    first = hwp_render.convert_hwp_rendered(HWP)
    monkeypatch.setattr(hwp_render, 'convert_with_coverage', lambda *args:
                        (['Another extraction result'], Coverage('page', 3, 2, (2,))))
    second = hwp_render.convert_hwp_rendered(HWP)
    assert first.stamp['rendered_sha256'] == second.stamp['rendered_sha256']
    assert first.stamp['renderer_version'] == second.stamp['renderer_version']
    assert first.stamp['converter_config']['extracted_sha256'] != second.stamp['converter_config']['extracted_sha256']


def test_fonts_are_identified_by_contents_not_just_paths(tmp_path, monkeypatch):
    font = tmp_path / 'fixture.font'
    font.write_bytes(b'font-version-1')
    monkeypatch.setattr(hwp_render.external, '_binary', lambda *args: 'fixture-fontconfig')
    monkeypatch.setattr(hwp_render.external, '_run', lambda *args, **kwargs:
                        SimpleNamespace(stdout=str(font) + '\n'))
    before = hwp_render._fonts(tmp_path)
    font.write_bytes(b'font-version-2')
    assert hwp_render._fonts(tmp_path) != before


def test_empty_font_manifest_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(hwp_render.external, '_binary', lambda *args: 'fixture-fontconfig')
    monkeypatch.setattr(hwp_render.external, '_run', lambda *args, **kwargs: SimpleNamespace(stdout=''))
    with pytest.raises(hwp_render.external.ExternalConversionError):
        hwp_render._fonts(tmp_path)


def test_tool_identity_covers_executable_changes(tmp_path, monkeypatch):
    binary = tmp_path / 'tool'
    binary.write_bytes(b'v1')
    monkeypatch.setattr(hwp_render.external, '_run', lambda *args, **kwargs:
                        SimpleNamespace(stdout='constant version banner'))
    before = hwp_render._tool(str(binary), tmp_path)
    binary.write_bytes(b'v2')
    assert hwp_render._tool(str(binary), tmp_path) != before


def prepare_module():
    path = Path(__file__).resolve().parents[1] / 'scripts/prepare_hwp_recovery.py'
    spec = importlib.util.spec_from_file_location('prepare_hwp_recovery', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def staged(tmp_path):
    root = tmp_path / 'archive'
    base = root / 'tyit/C00000001__fixture'
    source = base / 'objects/F00000001/source.hwp'
    source.parent.mkdir(parents=True)
    source.write_bytes(HWP)
    meta = dict(slack_file_id='F00000001', filetype='hwp', name='fixture.hwp',
                sha256=hashlib.sha256(HWP).hexdigest(), object_path=str(source),
                original_state='retained', conversion_state='failed',
                status='download_or_extract_failed', origin_message_ts='123.456')
    path = base / 'staging/F00000001/metadata.json'
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(meta), encoding='utf-8')
    preview = path.with_name('extracted.md')
    preview.write_text('old failed stub', encoding='utf-8')
    return SimpleNamespace(root=root, base=base, source=source, path=path, preview=preview, meta=meta)


def prepare(staged, **kwargs):
    return prepare_module().prepare(staged.root, 'tyit', 'C00000001', 'F00000001', **kwargs)


def test_plan_never_calls_a_renderer_or_writes(staged, monkeypatch):
    monkeypatch.setattr(hwp_render, 'convert_hwp_rendered', lambda data: pytest.fail('no rendering'))
    before = sorted(staged.root.rglob('*'))
    assert prepare(staged)['status'] == 'hwp-render-planned'
    assert sorted(staged.root.rglob('*')) == before


def test_preparation_preserves_message_provenance_and_old_generations(staged, renderer):
    before = staged.path.read_bytes(), staged.preview.read_bytes(), staged.source.read_bytes()
    result = prepare(staged, apply=True, actor='test-operator')
    assert result['status'] == 'prepared-not-published' and result['conversion_state'] == 'partial'
    assert not result['published'] and not result['indexed']
    assert (staged.path.read_bytes(), staged.preview.read_bytes(), staged.source.read_bytes()) == before
    meta = json.loads(Path(result['metadata_path']).read_text(encoding='utf-8'))
    assert meta['origin_message_ts'] == '123.456'
    assert meta['recovery_actor'] == 'test-operator'
    assert Path(meta['rendered_path']).read_bytes() == renderer.blob
    assert not list(staged.base.glob('archive/**/*'))


def test_original_hash_tampering_stops_before_render(staged, monkeypatch):
    staged.source.write_bytes(b'changed')
    monkeypatch.setattr(hwp_render, 'convert_hwp_rendered', lambda data: pytest.fail('hash mismatch'))
    with pytest.raises(ValueError):
        prepare(staged, apply=True, actor='test-operator')


@pytest.mark.parametrize('field,value', [('conversion_state', 'blocked'), ('status', 'pii_refused'),
                                       ('screen_result', 'blocked')])
def test_prior_pii_block_is_not_overridden(staged, field, value):
    staged.path.write_text(json.dumps(staged.meta | {field: value}), encoding='utf-8')
    with pytest.raises(PermissionError):
        prepare(staged, apply=True, actor='test-operator')


def test_blocked_new_extraction_retains_neither_body_nor_pdf(staged, renderer, monkeypatch):
    from tybot.pii_screen import ScreenResult

    monkeypatch.setattr(file_reconversion, 'screen_document', lambda *args, **kwargs:
                        ScreenResult(True, 'fixture', ()))
    result = prepare(staged, apply=True, actor='test-operator')
    generation = Path(result['metadata_path']).parent
    assert result['status'] == 'blocked'
    assert not (generation / 'extracted.md').exists()
    assert not (generation / 'rendered.pdf').exists()


def test_missing_actor_refused_before_render(staged):
    with pytest.raises(ValueError):
        prepare(staged, apply=True)


def test_pdf_ocr_failure_logs_codes_not_converter_body(monkeypatch, caplog):
    from tybot.archive import convert as cv

    monkeypatch.setattr(cv, '_pdf_text_layer', lambda reader: ({1: ['Synthetic content ' * 100]}, [2]))

    def fail(*args):
        raise hwp_render.external.ExternalConversionError('DOCUMENT_BODY_MUST_NOT_LOG',
                                                         code='converter_crashed')

    monkeypatch.setattr(cv, '_ocr_pdf_page', fail)
    _, coverage = cv.convert_with_coverage('pdf', pdf_bytes(2))
    assert coverage.state == 'partial'
    assert 'converter_crashed' in caplog.text
    assert 'DOCUMENT_BODY_MUST_NOT_LOG' not in caplog.text


def test_no_pdf_output_is_a_typed_failure(renderer, monkeypatch):
    monkeypatch.setattr(hwp_render.external, '_run', lambda *args, **kwargs:
                        SimpleNamespace(stdout='', stderr='', returncode=0))
    with pytest.raises(hwp_render.external.ExternalConversionError) as caught:
        hwp_render.convert_hwp_rendered(HWP)
    assert caught.value.code == 'render_output_missing'


def test_renderer_failure_does_not_change_original_generation(staged, monkeypatch):
    before = staged.path.read_bytes(), staged.preview.read_bytes()

    def fail(data):
        raise hwp_render.external.ExternalConversionError('private stderr', code='converter_crashed')

    monkeypatch.setattr(hwp_render, 'convert_hwp_rendered', fail)
    with pytest.raises(hwp_render.external.ExternalConversionError):
        prepare(staged, apply=True, actor='test-operator')
    assert (staged.path.read_bytes(), staged.preview.read_bytes()) == before
    assert not list(staged.path.parent.glob('reconversions/*'))


def test_cli_plan_never_takes_collector_lock(staged, monkeypatch, capsys):
    module = prepare_module()
    monkeypatch.setattr(module, 'load_env_file', lambda: None)
    monkeypatch.setenv('ARCHIVE_DIR', str(staged.root))
    monkeypatch.setattr(module, 'instance_lock', lambda *args: pytest.fail('plan cannot take lock'))
    assert module.main(['--workspace', 'tyit', '--channel', 'C00000001',
                        '--file-id', 'F00000001']) == 0
    assert json.loads(capsys.readouterr().out)['results'][0]['read_only'] is True


def test_cli_apply_is_inside_exclusive_lock(staged, monkeypatch, capsys):
    module = prepare_module()
    monkeypatch.setattr(module, 'load_env_file', lambda: None)
    monkeypatch.setenv('ARCHIVE_DIR', str(staged.root))
    events = []
    lock = SimpleNamespace(acquire=lambda: events.append('acquire'), release=lambda: events.append('release'))
    monkeypatch.setattr(module, 'instance_lock', lambda *args: lock)

    def apply(*args, **kwargs):
        assert events == ['acquire']
        assert kwargs['actor'] == 'operator' and kwargs['apply'] is True
        events.append('prepare')
        return {'status': 'prepared-not-published'}

    monkeypatch.setattr(module, 'prepare', apply)
    assert module.main(['--workspace', 'tyit', '--channel', 'C00000001',
                        '--file-id', 'F00000001', '--apply', '--by', 'operator']) == 0
    assert events == ['acquire', 'prepare', 'release']


def test_cli_failure_does_not_print_converter_text(staged, monkeypatch, capsys):
    module = prepare_module()
    monkeypatch.setattr(module, 'load_env_file', lambda: None)
    monkeypatch.setenv('ARCHIVE_DIR', str(staged.root))

    def fail(*args, **kwargs):
        raise ValueError('DOCUMENT_BODY_MUST_NOT_LOG')

    monkeypatch.setattr(module, 'prepare', fail)
    assert module.main(['--workspace', 'tyit', '--channel', 'C00000001', '--file-id', 'F00000001']) == 1
    assert 'DOCUMENT_BODY_MUST_NOT_LOG' not in capsys.readouterr().out
