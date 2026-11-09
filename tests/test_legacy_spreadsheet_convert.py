"""구형 엑셀(xls·xlsb) 변환 — LibreOffice 로 xlsx 로 옮겨 xlsx 변환기로 읽는다.

2026-11-06 첨부 진단에서 자금팀 연간자금수지(.xlsb)·수금계획(.xls) 등이 「미지원」 으로
남아 답변 근거가 되지 못했다. 원본은 서버에 보관돼 있으므로 변환기만 생기면
소급 변환 도구가 다시 읽을 수 있다.
"""
from __future__ import annotations

import pytest

from tybot.archive import convert, external_convert
from tybot.archive.files import SlackFile


@pytest.mark.parametrize("kind", ["xls", "xlsb"])
def test_legacy_spreadsheets_are_convertible(kind):
    assert kind in convert.CONVERTIBLE
    assert convert.can_convert(kind)
    assert convert.can_convert(kind.upper())


@pytest.mark.parametrize("kind", ["xls", "xlsb"])
def test_converted_through_xlsx_renderer_not_pdf(monkeypatch, kind):
    calls = {}

    def to_xlsx(data, suffix):
        calls["to_xlsx"] = (data, suffix)
        return b"XLSX-BYTES"

    def xlsx(data):
        calls["xlsx"] = data
        return ["| 사업장 | 수금 |", "|---|---|", "| 강릉 | 1,200 |"]

    def pdf(*_a, **_k):
        raise AssertionError("엑셀을 PDF 로 렌더하면 열이 쪽 경계에서 잘린다")

    monkeypatch.setattr(convert, "office_to_xlsx", to_xlsx)
    monkeypatch.setattr(convert, "_xlsx", xlsx)
    monkeypatch.setattr(convert, "office_pdf_lines", pdf)

    lines = convert.convert(kind, b"RAW")

    assert calls["to_xlsx"] == (b"RAW", kind)
    assert calls["xlsx"] == b"XLSX-BYTES"
    assert lines[0].startswith("[변환 안내]")
    assert "| 강릉 | 1,200 |" in lines


def test_missing_libreoffice_is_an_explicit_failure(monkeypatch):
    def unavailable(*_a, **_k):
        raise external_convert.ExternalConverterUnavailable("LIBREOFFICE_BIN 없음")

    monkeypatch.setattr(convert, "office_to_xlsx", unavailable)
    with pytest.raises(convert.ConvertError, match="XLSB 변환 실패"):
        convert.convert("xlsb", b"RAW")


def test_office_to_xlsx_refuses_other_formats():
    with pytest.raises(external_convert.ExternalConversionError):
        external_convert.office_to_xlsx(b"RAW", "docx")


def test_slack_binary_report_uses_file_extension():
    """Slack 은 모르는 형식을 `binary` 로 보고한다. 확장자가 처리 형식이면 그것을 쓴다."""
    f = SlackFile.from_event({"id": "F1", "name": "(자금팀)연간자금수지_260901.xlsb",
                              "filetype": "binary"})
    assert f.filetype == "xlsb" and f.is_convertible
