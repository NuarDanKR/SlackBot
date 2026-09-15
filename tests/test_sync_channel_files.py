"""채널 파일 CLI가 원문 반영과 색인까지 닫는지 검증한다."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import sync_channel_files as sync


def test_member_channels_only_returns_joined_rule_matching_channels():
    client = SimpleNamespace(
        conversations_list=lambda **kwargs: {
            "channels": [
                {"id": "C1", "name": "팀-전산_ABB110-자료", "is_member": True},
                {"id": "C2", "name": "점심", "is_member": True},
                {"id": "C3", "name": "팀-전산_ABB110-비가입", "is_member": False},
            ],
            "response_metadata": {},
        }
    )

    assert [row["id"] for row in sync.member_channels(client)] == ["C1"]


def test_publish_writes_source_and_reindexes_the_changed_document(tmp_path, monkeypatch):
    archive = tmp_path / "archive"
    staged = [SimpleNamespace(file_id="F1", lines=["[첨부:자동변환] 보고서.txt"])]
    cfg = SimpleNamespace(key="pilot")
    channel = {"id": "C1", "name": "팀-전산_ABB110-자료"}
    indexed = []
    monkeypatch.setattr(
        sync,
        "confirm_archived",
        lambda store, items, **kwargs: {"F1": "archived"},
    )
    monkeypatch.setattr(
        sync.search_index,
        "reindex",
        lambda docs, root: indexed.extend(docs) or {"docs": len(docs), "lines": 1},
    )

    written, complete = sync._publish(str(archive), cfg, channel, staged)

    assert written == 1 and complete is True
    assert len(indexed) == 1
    assert indexed[0].channel_id == "C1"
    assert indexed[0].raw_lines[0].speaker == "채널 파일"
