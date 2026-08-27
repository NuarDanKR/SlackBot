"""캔버스 수집 — 확인 못 한 형식은 추측하지 않고 미변환으로 남긴다."""
from __future__ import annotations

from unittest.mock import patch

from tybot.archive.canvas import canvas_file_id, canvas_lines


class FakeClient:
    def __init__(self, *, canvas_id=None, file_obj=None, info_error=False, files_error=False):
        self._canvas_id = canvas_id
        self._file = file_obj
        self._info_error = info_error
        self._files_error = files_error

    def conversations_info(self, channel):
        if self._info_error:
            raise RuntimeError("channel_not_found")
        props = {"canvas": {"file_id": self._canvas_id}} if self._canvas_id else {}
        return {"channel": {"id": channel, "properties": props}}

    def files_info(self, file):
        if self._files_error:
            raise RuntimeError("file_not_found")
        return {"file": self._file or {}}


def _file(name="회의록 캔버스", mime="text/markdown"):
    return {
        "id": "F1",
        "name": name,
        "filetype": "canvas",
        "size": 1024,
        "mimetype": mime,
        "url_private_download": "https://files.slack.com/canvas",
    }


def test_no_canvas_is_normal():
    lines, warns = canvas_lines(FakeClient(), "C1", "xoxb-t")
    assert lines == [] and warns == []


def test_canvas_id_from_channel_properties():
    assert canvas_file_id(FakeClient(canvas_id="F123"), "C1") == "F123"


def test_channel_info_failure_is_not_fatal():
    assert canvas_file_id(FakeClient(info_error=True), "C1") is None


def test_markdown_canvas_is_collected():
    c = FakeClient(canvas_id="F1", file_obj=_file())
    body = "# 주간회의\n- 기성금 3억 청구\n\n- 승인 완료\n".encode()
    with patch("tybot.archive.canvas.download_bytes", return_value=body):
        lines, warns = canvas_lines(c, "C1", "xoxb-t")
    assert warns == []
    assert lines[0] == "[캔버스:수집] 회의록 캔버스"
    assert "[캔버스본문:회의록 캔버스] # 주간회의" in lines
    assert "[캔버스본문:회의록 캔버스] - 기성금 3억 청구" in lines


def test_html_canvas_text_is_extracted_without_tags():
    c = FakeClient(canvas_id="F1", file_obj=_file(mime="text/html"))
    body = b"<html><body><h1>\xed\x9a\x8c\xec\x9d\x98</h1><p>3\xec\x96\xb5</p></body></html>"
    with patch("tybot.archive.canvas.download_bytes", return_value=body):
        lines, warns = canvas_lines(c, "C1", "xoxb-t")
    assert warns == []
    assert any("회의" in ln for ln in lines)
    assert not any("<" in ln for ln in lines)


def test_download_failure_leaves_unconverted_marker():
    """추측해서 넣느니 미변환으로 남긴다 - 되돌릴 수 없는 오염을 막는다."""
    c = FakeClient(canvas_id="F1", file_obj=_file())
    with patch("tybot.archive.canvas.download_bytes", side_effect=RuntimeError("403")):
        lines, warns = canvas_lines(c, "C1", "xoxb-t")
    assert lines == ["[캔버스:미변환] 회의록 캔버스"]
    assert len(warns) == 1


def test_empty_canvas_is_unconverted_not_silent():
    c = FakeClient(canvas_id="F1", file_obj=_file())
    with patch("tybot.archive.canvas.download_bytes", return_value=b"   "):
        lines, warns = canvas_lines(c, "C1", "xoxb-t")
    assert "[캔버스:미변환]" in lines[0]
    assert warns and "형식을 알아보지" in warns[0]


def test_files_info_failure_is_recorded():
    c = FakeClient(canvas_id="F1", files_error=True)
    lines, warns = canvas_lines(c, "C1", "xoxb-t")
    assert lines == ["[캔버스:미변환] F1"] and len(warns) == 1


def test_no_token_records_marker():
    c = FakeClient(canvas_id="F1", file_obj=_file())
    lines, warns = canvas_lines(c, "C1", None)
    assert "[캔버스:미변환]" in lines[0] and warns


def test_long_canvas_is_truncated():
    c = FakeClient(canvas_id="F1", file_obj=_file())
    body = ("줄\n" * 500).encode()
    with patch("tybot.archive.canvas.download_bytes", return_value=body):
        lines, _ = canvas_lines(c, "C1", "xoxb-t")
    assert len(lines) <= 302
    assert any("이하 생략" in ln for ln in lines)
