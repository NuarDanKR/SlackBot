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
    capture = canvas_lines(FakeClient(), "C1", "xoxb-t")
    assert capture.lines == [] and capture.warnings == []


def test_canvas_id_from_channel_properties():
    assert canvas_file_id(FakeClient(canvas_id="F123"), "C1") == "F123"


def test_channel_info_failure_is_visible_and_recorded():
    capture = canvas_lines(FakeClient(info_error=True), "C1", "xoxb-t")
    assert capture.lines[0].startswith("[캔버스:미변환] C1")
    assert capture.dedupe_key and capture.warnings


def test_markdown_canvas_is_collected():
    c = FakeClient(canvas_id="F1", file_obj=_file())
    body = "# 주간회의\n- 기성금 3억 청구\n\n- 승인 완료\n".encode()
    with patch("tybot.archive.canvas.download_bytes", return_value=body):
        capture = canvas_lines(c, "C1", "xoxb-t")
    assert capture.warnings == [] and capture.dedupe_key
    assert capture.lines[0].startswith("[캔버스:수집] 회의록 캔버스 [수집키:")
    assert "[캔버스본문:회의록 캔버스] # 주간회의" in capture.lines
    assert "[캔버스본문:회의록 캔버스] - 기성금 3억 청구" in capture.lines


def test_html_canvas_text_is_extracted_without_tags():
    c = FakeClient(canvas_id="F1", file_obj=_file(mime="text/html"))
    body = b"<html><body><h1>\xed\x9a\x8c\xec\x9d\x98</h1><p>3\xec\x96\xb5</p></body></html>"
    with patch("tybot.archive.canvas.download_bytes", return_value=body):
        capture = canvas_lines(c, "C1", "xoxb-t")
    assert capture.warnings == []
    assert any("회의" in ln for ln in capture.lines)
    assert not any("<" in ln for ln in capture.lines)


def test_html_ignores_script_and_style_content():
    c = FakeClient(canvas_id="F1", file_obj=_file(mime="text/html"))
    body = b"<html><style>secret</style><script>bad()</script><p>visible</p></html>"
    with patch("tybot.archive.canvas.download_bytes", return_value=body):
        capture = canvas_lines(c, "C1", "xoxb-t")
    joined = "\n".join(capture.lines)
    assert "visible" in joined and "secret" not in joined and "bad()" not in joined


def test_download_failure_leaves_unconverted_marker():
    """추측해서 넣느니 미변환으로 남긴다 - 되돌릴 수 없는 오염을 막는다."""
    c = FakeClient(canvas_id="F1", file_obj=_file())
    with patch("tybot.archive.canvas.download_bytes", side_effect=RuntimeError("403")):
        capture = canvas_lines(c, "C1", "xoxb-t")
    assert capture.lines[0].startswith("[캔버스:미변환] 회의록 캔버스")
    assert len(capture.warnings) == 1


def test_empty_canvas_is_unconverted_not_silent():
    c = FakeClient(canvas_id="F1", file_obj=_file())
    with patch("tybot.archive.canvas.download_bytes", return_value=b"   "):
        capture = canvas_lines(c, "C1", "xoxb-t")
    assert "[캔버스:미변환]" in capture.lines[0]
    assert capture.warnings and "형식을 알아보지" in capture.warnings[0]


def test_files_info_failure_is_recorded():
    c = FakeClient(canvas_id="F1", files_error=True)
    capture = canvas_lines(c, "C1", "xoxb-t")
    assert capture.lines[0].startswith("[캔버스:미변환] F1")
    assert len(capture.warnings) == 1


def test_no_token_records_marker():
    c = FakeClient(canvas_id="F1", file_obj=_file())
    capture = canvas_lines(c, "C1", None)
    assert "[캔버스:미변환]" in capture.lines[0] and capture.warnings


def test_unknown_or_binary_format_is_never_ingested_as_text():
    for mime, body in [
        ("application/json", b'{"ok":false,"error":"not_allowed"}'),
        ("application/octet-stream", bytes.fromhex("00ff504b0304")),
    ]:
        c = FakeClient(canvas_id="F1", file_obj=_file(mime=mime))
        with patch("tybot.archive.canvas.download_bytes", return_value=body):
            capture = canvas_lines(c, "C1", "xoxb-t")
        assert capture.lines[0].startswith("[캔버스:미변환]")
        assert capture.warnings
        assert "not_allowed" not in "\n".join(capture.lines)


def test_long_canvas_is_truncated():
    c = FakeClient(canvas_id="F1", file_obj=_file())
    body = ("줄\n" * 500).encode()
    with patch("tybot.archive.canvas.download_bytes", return_value=body):
        capture = canvas_lines(c, "C1", "xoxb-t")
    assert len(capture.lines) <= 302
    assert any("이하 생략" in ln for ln in capture.lines)
