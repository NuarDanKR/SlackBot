"""첨부 처리 — 텍스트만 본문 수집, 나머지는 목록만."""
from __future__ import annotations

import io
from unittest.mock import patch

from tybot.archive.files import DownloadError, SlackFile, download_text, file_lines


def _f(name, filetype, size=2048, mime=""):
    return {
        "id": "F1",
        "name": name,
        "filetype": filetype,
        "size": size,
        "mimetype": mime,
        "url_private_download": "https://files.slack.com/x",
    }


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, ctype: str = "text/plain"):
        super().__init__(data)
        self.headers = {"Content-Type": ctype}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_unconvertible_types_are_listed_only():
    """도면·이미지·구형 바이너리는 다운로드조차 하지 않고 목록만 남긴다."""
    lines, warns = file_lines(
        [_f("도면.dwg", "dwg"), _f("사진.jpg", "jpg"), _f("구형.hwp", "hwp")], "xoxb-t"
    )
    assert len(lines) == 3 and warns == []
    assert all("[첨부:미변환]" in ln for ln in lines)


def test_text_file_body_is_collected():
    payload = "첫 줄\n두 번째 줄\n".encode()
    with patch("tybot.archive.files.urlopen", return_value=_Resp(payload)):
        lines, warns = file_lines([_f("메모.txt", "txt")], "xoxb-t")
    assert warns == []
    assert lines[0].startswith("[첨부:본문 수집] 메모.txt")
    assert "[첨부본문:메모.txt] 첫 줄" in lines
    assert "[첨부본문:메모.txt] 두 번째 줄" in lines


def test_login_html_is_detected_as_failure():
    """files:read 가 없으면 Slack 은 로그인 페이지를 200 으로 준다 - 조용히 넘기면 안 된다."""
    html = b"<!DOCTYPE html>\n<html>login</html>"
    with patch("tybot.archive.files.urlopen", return_value=_Resp(html, "text/html")):
        lines, warns = file_lines([_f("메모.txt", "txt")], "xoxb-t")
    assert len(warns) == 1 and "files:read" in warns[0]
    assert lines == [lines[0]]  # 목록 줄은 남고 본문은 안 들어간다


def test_no_token_warns_without_crashing():
    lines, warns = file_lines([_f("메모.txt", "txt")], None)
    assert len(lines) == 1 and "토큰이 없어" in warns[0]


def test_document_is_converted_and_tagged():
    """업무 문서는 변환해서 넣고, 자동 변환본임을 표시한다."""
    from openpyxl import Workbook

    wb = Workbook()
    wb.active.title = "기성"
    wb.active.append(["항목", "금액"])
    wb.active.append(["김해외동 기성금", "3억 2천만원"])
    buf = io.BytesIO()
    wb.save(buf)

    with patch("tybot.archive.files.download_bytes", return_value=buf.getvalue()):
        lines, warns = file_lines([_f("원가.xlsx", "xlsx")], "xoxb-t")
    assert warns == []
    assert "[첨부:변환]" in lines[0]
    assert any("[첨부추출:원가.xlsx] [시트] 기성" == ln for ln in lines)
    assert any("김해외동 기성금 | 3억 2천만원" in ln for ln in lines)


def test_failed_conversion_still_leaves_a_trace():
    """변환 실패해도 목록 줄은 남는다 - 「변환하지 못한 것」이 색인에 보이게."""
    with patch("tybot.archive.files.download_bytes", return_value=b"not a real xlsx"):
        lines, warns = file_lines([_f("깨진.xlsx", "xlsx")], "xoxb-t")
    assert len(lines) == 1 and "[첨부:미변환]" in lines[0]
    assert len(warns) == 1


def test_oversized_document_is_not_converted():
    huge = _f("대용량.xlsx", "xlsx", size=50 * 1024 * 1024)
    lines, warns = file_lines([huge], "xoxb-t")
    assert "[첨부:미변환]" in lines[0] and warns == []


def test_long_text_is_truncated():
    payload = ("줄\n" * 500).encode()
    with patch("tybot.archive.files.urlopen", return_value=_Resp(payload)):
        body = download_text(SlackFile.from_event(_f("큰파일.txt", "txt")), "xoxb-t")
    assert body.count("\n") <= 201 and "이하 생략" in body


def test_missing_url_raises():
    f = SlackFile.from_event({"id": "F", "name": "x.txt", "filetype": "txt", "size": 1})
    try:
        download_text(f, "xoxb-t")
    except DownloadError as e:
        assert "다운로드 URL 없음" in str(e)
    else:
        raise AssertionError("예외가 나야 한다")


def test_one_bad_attachment_does_not_block_others():
    def side_effect(req, timeout=0):
        if "bad" in req.full_url:
            raise OSError("네트워크 오류")
        return _Resp(b"ok line\n")

    good = _f("좋음.txt", "txt")
    bad = _f("나쁨.txt", "txt")
    bad["url_private_download"] = "https://files.slack.com/bad"
    with patch("tybot.archive.files.urlopen", side_effect=side_effect):
        lines, warns = file_lines([bad, good], "xoxb-t")
    assert len(warns) == 1
    assert any("[첨부본문:좋음.txt] ok line" == ln for ln in lines)
