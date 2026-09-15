"""B-46 채널 파일 탭 수집의 권한·멱등 경계."""
from __future__ import annotations

import pytest

from tybot.archive import channel_files
from tybot.archive.files import attachment_storage


class FileClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def files_list(self, **kwargs):
        self.calls.append(kwargs)
        page = kwargs["page"]
        return {"files": self.pages[page - 1], "paging": {"pages": len(self.pages)}}


def _file(file_id: str) -> dict:
    return {
        "id": file_id,
        "name": f"{file_id}.txt",
        "filetype": "txt",
        "size": 10,
        "url_private_download": f"https://files.slack.com/{file_id}",
    }


def test_scan_is_always_scoped_to_one_channel(tmp_path, monkeypatch):
    monkeypatch.setattr(channel_files, "_page_pause", lambda: 0)
    client = FileClient([[_file("F1")], [_file("F2")]])
    storage = attachment_storage(tmp_path / "archive", "pilot", "C1")

    result = channel_files.scan(client, "C1", storage)

    assert [item["id"] for item in result.candidates] == ["F1", "F2"]
    assert [call["channel"] for call in client.calls] == ["C1", "C1"]


def test_scan_refuses_a_workspace_wide_files_list(tmp_path):
    storage = attachment_storage(tmp_path / "archive", "pilot", "C1")
    with pytest.raises(ValueError):
        channel_files.scan(FileClient([]), "", storage)


def test_a_file_already_staged_from_a_message_is_not_downloaded_again(tmp_path):
    storage = attachment_storage(tmp_path / "archive", "pilot", "C1")
    staged = storage.staging_dir / "F1"
    staged.mkdir(parents=True)
    (staged / "metadata.json").write_text("{}", encoding="utf-8")

    result = channel_files.scan(FileClient([[_file("F1"), _file("F2")]]), "C1", storage)

    assert result.known == 1
    assert [item["id"] for item in result.candidates] == ["F2"]


def test_collect_reuses_the_existing_attachment_pipeline(tmp_path, monkeypatch):
    storage = attachment_storage(tmp_path / "archive", "pilot", "C1")
    result = channel_files.ChannelFileScan("C1", candidates=[_file("F1")])
    seen = {}

    def fake_stage(files, token, got_storage, *, origin):
        seen.update(files=files, token=token, storage=got_storage, origin=origin)
        return ["staged"]

    monkeypatch.setattr(channel_files, "stage_attachments", fake_stage)

    assert channel_files.collect(result, "xoxb-test", storage, workspace="pilot") == ["staged"]
    assert seen["files"][0]["id"] == "F1"
    assert seen["storage"] == storage
    assert seen["origin"].workspace == "pilot"
    assert seen["origin"].channel_id == "C1"
    assert seen["origin"].message_ts == ""


def test_collect_rechecks_staging_before_download(tmp_path, monkeypatch):
    storage = attachment_storage(tmp_path / "archive", "pilot", "C1")
    staged = storage.staging_dir / "F1"
    staged.mkdir(parents=True)
    (staged / "metadata.json").write_text("{}", encoding="utf-8")
    result = channel_files.ChannelFileScan("C1", candidates=[_file("F1")])
    monkeypatch.setattr(
        channel_files,
        "stage_attachments",
        lambda *args, **kwargs: pytest.fail("must not download an already staged file"),
    )

    assert channel_files.collect(result, "xoxb-test", storage, workspace="pilot") == []


def test_scan_only_never_calls_the_download_pipeline(tmp_path, monkeypatch):
    storage = attachment_storage(tmp_path / "archive", "pilot", "C1")
    monkeypatch.setattr(
        channel_files,
        "stage_attachments",
        lambda *args, **kwargs: pytest.fail("scan must not download"),
    )

    result = channel_files.scan(FileClient([[_file("F1")]]), "C1", storage)

    assert result.missing == 1


def test_canvas_reference_requires_an_explicit_channel_share():
    client = type("Client", (), {
        "files_info": lambda self, file: {"file": {"id": file, "channels": []}}
    })()

    events, warnings = channel_files.referenced_files(client, "C1", ["F1"])

    assert events == []
    assert "공유 여부를 확인할 수 없어" in warnings[0]


def test_canvas_reference_accepts_a_file_shared_to_the_channel():
    raw = {"id": "F1", "channels": ["C1"]}
    client = type("Client", (), {"files_info": lambda self, file: {"file": raw}})()

    events, warnings = channel_files.referenced_files(client, "C1", ["F1"])

    assert events == [raw] and warnings == []
