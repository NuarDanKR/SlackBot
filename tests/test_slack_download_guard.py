"""B-97 0단계 — 봇 토큰은 `files.slack.com` 으로만 간다.

설계: docs/design/google-drive-evidence.md §5
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import Request

import pytest

from tybot.archive import canvas as canvas_mod
from tybot.archive import files
from tybot.archive.files import (
    EXTERNAL_SKIPPED_CODE,
    DownloadError,
    ExternalFileRefused,
    SlackFile,
    _SlackOnlyRedirect,
    attachment_storage,
    download_bytes,
    download_text,
    file_lines,
    slack_file_url,
    stage_attachments,
)

TOKEN = "xoxb-secret"


def _no_network(*_a, **_k):
    raise AssertionError("요청이 만들어지면 안 된다")


def _gdrive(**extra):
    row = {
        "id": "F0GDRIVE01",
        "name": "공정표",
        "filetype": "gdoc",
        "mode": "external",
        "is_external": True,
        "external_type": "gdrive",
        "url_private": "https://docs.google.com/document/d/abc/edit",
        "size": 0,
    }
    row.update(extra)
    return row


def test_remote_gdrive_file_never_builds_a_request(monkeypatch):
    calls = []
    monkeypatch.setattr(files, "urlopen", lambda *a, **k: calls.append(a) or _no_network())
    f = SlackFile.from_event(_gdrive())
    assert f.is_external and f.external_type == "gdrive"
    # 원격 파일은 `url_private` 으로 내려가지 않는다.
    assert f.url_private_download is None
    with pytest.raises(ExternalFileRefused):
        download_bytes(f, TOKEN)
    with pytest.raises(ExternalFileRefused):
        download_text(f, TOKEN)
    assert calls == []


@pytest.mark.parametrize(
    "row",
    [
        {"mode": "external"},
        {"is_external": True},
        {"filetype": "remote"},
        {"external_type": "gdrive"},
    ],
)
def test_any_external_marker_refuses_even_with_slack_url(monkeypatch, row):
    monkeypatch.setattr(files, "urlopen", _no_network)
    base = {
        "id": "F1",
        "name": "x.pdf",
        "filetype": "pdf",
        "size": 10,
        "url_private_download": "https://files.slack.com/files-pri/T-F1/x.pdf",
    }
    with pytest.raises(ExternalFileRefused):
        download_bytes(SlackFile.from_event({**base, **row}), TOKEN)


@pytest.mark.parametrize(
    "url",
    [
        "https://files.slack.com.evil.test/x",
        "https://evilfiles.slack.com/x",
        "http://files.slack.com/x",
        "https://files.slack.com:8443/x",
        "https://user@files.slack.com/x",
        "https://docs.google.com/document/d/abc",
        "ftp://files.slack.com/x",
    ],
)
def test_non_slack_file_hosts_are_refused(monkeypatch, url):
    monkeypatch.setattr(files, "urlopen", _no_network)
    f = SlackFile.from_event(
        {"id": "F1", "name": "a.txt", "filetype": "txt", "size": 3, "url_private_download": url}
    )
    assert slack_file_url(url) == ""
    with pytest.raises(ExternalFileRefused):
        download_bytes(f, TOKEN)
    with pytest.raises(ExternalFileRefused):
        download_text(f, TOKEN)


class _Resp:
    def __init__(self, data: bytes):
        self._data = data
        self.headers = {"Content-Type": "text/plain"}

    def read(self, n=-1):
        return self._data if n is None or n < 0 else self._data[:n]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_hosted_slack_file_is_unchanged(monkeypatch):
    seen: list[Request] = []

    def fake(req, timeout=None):
        seen.append(req)
        return _Resp(b"hello")

    monkeypatch.setattr(files, "urlopen", fake)
    url = "https://files.slack.com/files-pri/T1-F1/download/a.txt"
    f = SlackFile.from_event(
        {
            "id": "F1",
            "name": "a.txt",
            "filetype": "txt",
            "size": 5,
            "mode": "hosted",
            "url_private_download": url,
        }
    )
    assert download_bytes(f, TOKEN) == b"hello"
    assert seen[0].full_url == url
    assert seen[0].get_header("Authorization") == f"Bearer {TOKEN}"


def test_hosted_file_may_still_fall_back_to_url_private(monkeypatch):
    f = SlackFile.from_event(
        {
            "id": "F1",
            "name": "a.txt",
            "filetype": "txt",
            "size": 5,
            "url_private": "https://files.slack.com/files-pri/T1-F1/a.txt",
        }
    )
    assert f.url_private_download == "https://files.slack.com/files-pri/T1-F1/a.txt"


def test_redirect_off_the_slack_file_host_is_refused():
    handler = _SlackOnlyRedirect()
    req = Request("https://files.slack.com/x", headers={"Authorization": f"Bearer {TOKEN}"})
    with pytest.raises(DownloadError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://evil.test/steal")
    with pytest.raises(DownloadError):
        handler.redirect_request(req, None, 302, "Found", {}, "https://acme.slack.com/?redir=x")
    # 같은 호스트 안의 리다이렉트는 따라간다.
    nxt = handler.redirect_request(req, None, 302, "Found", {}, "https://files.slack.com/y")
    assert nxt is not None and nxt.full_url == "https://files.slack.com/y"


def test_module_urlopen_uses_the_guarded_opener():
    assert any(isinstance(h, _SlackOnlyRedirect) for h in files._OPENER.handlers)


def test_stage_attachments_marks_remote_file_as_external_skipped(monkeypatch, tmp_path):
    monkeypatch.setattr(files, "urlopen", _no_network)
    queued = []
    monkeypatch.setattr(files, "queue_retry", lambda *a, **k: queued.append(k))
    storage = attachment_storage(tmp_path / "archive", "ws1", "C01234567")
    [result] = stage_attachments([_gdrive()], TOKEN, storage)
    assert result.reference_lines == ["[첨부:외부파일] 공정표 (gdrive) · id:F0GDRIVE01"]
    assert result.body_lines == []
    meta = json.loads(result.metadata_path.read_text(encoding="utf-8"))
    assert meta["status"] == EXTERNAL_SKIPPED_CODE
    assert meta["error_code"] == EXTERNAL_SKIPPED_CODE
    assert meta["retryable"] is False
    assert meta["original_state"] == "missing"
    # 다시 해도 같은 판정이다 — 재처리 대상이 아니다.
    assert [q["retryable"] for q in queued] == [False]
    # 외부 URL 을 metadata 에 남기지 않는다.
    assert "docs.google.com" not in result.metadata_path.read_text(encoding="utf-8")


def test_file_lines_leaves_a_trace_for_remote_file(monkeypatch):
    monkeypatch.setattr(files, "urlopen", _no_network)
    lines, warnings = file_lines([{**_gdrive(), "filetype": "pdf", "name": "a.pdf"}], TOKEN)
    assert lines == ["[첨부:외부파일] a.pdf (gdrive)"]
    assert warnings


def test_canvas_and_dm_handoff_share_the_guarded_downloader():
    """세 호출부(수집·Canvas·DM 인계)가 같은 함수를 지나야 같이 막힌다."""
    assert canvas_mod.download_bytes is files.download_bytes
    source = files.__file__.replace("archive\\files.py", "slack\\pilot.py").replace(
        "archive/files.py", "slack/pilot.py"
    )
    text = Path(source).read_text(encoding="utf-8")
    assert "from ..archive.files import SlackFile, download_bytes" in text


def test_trace_reports_remote_file_as_skipped_not_failed(tmp_path):
    from tybot.attachment_trace import SKIPPED, _converted

    meta_path = tmp_path / "metadata.json"
    result = _converted({"status": EXTERNAL_SKIPPED_CODE}, meta_path)
    assert result.status == SKIPPED and "external-file" in result.detail
