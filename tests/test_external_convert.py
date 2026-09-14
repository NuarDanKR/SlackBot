from __future__ import annotations

import subprocess

import pytest

from tybot.archive import external_convert as ext


def test_kordoc_requires_exactly_one_output(monkeypatch):
    monkeypatch.setattr(ext, "_binary", lambda *_args: "/usr/bin/kordoc")
    monkeypatch.setattr(
        ext,
        "_run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess([], 0, "", ""),
    )

    with pytest.raises(ext.ExternalConversionError, match="출력 문서 없음"):
        ext.kordoc_lines(b"document", "hwp")


def test_kordoc_marks_ocr_output(monkeypatch, tmp_path):
    monkeypatch.setattr(ext, "_binary", lambda *_args: "/usr/bin/kordoc")

    def fake_run(_command, *, cwd, home):
        assert home == tmp_path
        output = cwd / "out" / "source.md"
        output.write_text("금액 3억 원", encoding="utf-8")
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(ext, "_run", fake_run)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))

    lines = ext.kordoc_lines(b"scan", "pdf", force_ocr=True)

    assert lines[0].startswith("[변환 안내] OCR 사용")
    assert "금액 3억 원" in lines


def test_converter_binary_is_not_downloaded_at_runtime(monkeypatch):
    monkeypatch.delenv("KORDOC_BIN", raising=False)
    monkeypatch.setattr(ext.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ext, "SYSTEM_BINARY_DIRS", ())

    with pytest.raises(ext.ExternalConverterUnavailable):
        ext.kordoc_lines(b"document", "hwp")


def test_converter_finds_standard_path_when_process_path_is_narrow(monkeypatch, tmp_path):
    binary = tmp_path / "kordoc"
    binary.write_text("#!/bin/sh\n", encoding="ascii")
    binary.chmod(binary.stat().st_mode | 0o111)
    monkeypatch.delenv("KORDOC_BIN", raising=False)
    monkeypatch.setattr(ext.shutil, "which", lambda _name: None)
    monkeypatch.setattr(ext, "SYSTEM_BINARY_DIRS", (tmp_path,))

    assert ext._binary("KORDOC_BIN", "kordoc") == str(binary)


def test_configured_converter_must_be_executable(monkeypatch, tmp_path):
    binary = tmp_path / "kordoc"
    binary.write_text("not executable", encoding="ascii")
    monkeypatch.setenv("KORDOC_BIN", str(binary))
    monkeypatch.setattr(ext.os, "access", lambda *_args: False)
    monkeypatch.setattr(ext.shutil, "which", lambda _name: None)

    with pytest.raises(ext.ExternalConverterUnavailable, match="KORDOC_BIN"):
        ext._binary("KORDOC_BIN", "kordoc")


def test_run_overrides_inherited_home(monkeypatch, tmp_path):
    seen = {}

    def fake_subprocess_run(command, **kwargs):
        seen.update(kwargs)
        from unittest.mock import Mock

        return Mock(returncode=0, communicate=Mock(return_value=("", "")))

    monkeypatch.setattr(ext.subprocess, "Popen", fake_subprocess_run)
    monkeypatch.setenv("HOME", "/root")

    ext._run(["soffice", "--version"], cwd=tmp_path, home=tmp_path)

    assert seen["env"]["HOME"] == str(tmp_path)


def test_native_crash_is_not_misreported_as_oom(monkeypatch, tmp_path):
    from unittest.mock import Mock

    process = Mock(returncode=-6, communicate=Mock(return_value=("", "V8_Fatal secret")))
    monkeypatch.setattr(ext.subprocess, "Popen", lambda *a, **kw: process)
    with pytest.raises(ext.ExternalConversionError) as got:
        ext._run(["kordoc"], cwd=tmp_path)
    assert got.value.code == "converter_crashed"
    assert not got.value.retryable
    assert "secret" not in str(got.value)


def test_timeout_reaps_converter(monkeypatch, tmp_path):
    from unittest.mock import Mock

    process = Mock(pid=12345)
    process.communicate.side_effect = [subprocess.TimeoutExpired("converter", 120), ("", "")]
    monkeypatch.setattr(ext.subprocess, "Popen", lambda *a, **kw: process)
    killpg = Mock()
    monkeypatch.setattr(ext.os, "killpg", killpg, raising=False)
    with pytest.raises(ext.ExternalConversionError) as got:
        ext._run(["kordoc"], cwd=tmp_path)
    assert got.value.code == "converter_timeout"
    assert got.value.retryable
    assert process.communicate.call_count == 2
    if ext.os.name == "posix":
        killpg.assert_called_once_with(12345, ext.signal.SIGKILL)
    else:
        process.kill.assert_called_once()
