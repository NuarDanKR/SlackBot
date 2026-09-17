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
    # 2026-06-01 09:00 KST 에 올라온 파일. 수집은 오늘 해도 원문 시각은 그날이다.
    staged = [SimpleNamespace(file_id="F1", lines=["[첨부:자동변환] 보고서.txt"],
                              created=1780272000)]
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
    # **수집 시각이 아니라 파일이 올라온 시각**이다. 이게 오늘로 찍히면 몇 년 전
    # 문서가 그날 요약에 들어간다(2026-09-17 실측).
    assert indexed[0].raw_lines[0].ts.startswith("2026-06-01")


def test_publish_says_so_when_the_upload_time_is_unknown(tmp_path, monkeypatch):
    """모르는 시각을 수집 시각으로 채우면 사람은 오늘 올라온 것으로 읽는다."""
    archive = tmp_path / "archive"
    staged = [SimpleNamespace(file_id="F1", lines=["[첨부:자동변환] 보고서.txt"],
                              created=0)]
    cfg = SimpleNamespace(key="pilot")
    channel = {"id": "C1", "name": "팀-전산_ABB110-자료"}
    indexed = []
    monkeypatch.setattr(
        sync, "confirm_archived", lambda store, items, **kwargs: {"F1": "archived"},
    )
    monkeypatch.setattr(
        sync.search_index,
        "reindex",
        lambda docs, root: indexed.extend(docs) or {"docs": len(docs), "lines": 1},
    )

    sync._publish(str(archive), cfg, channel, staged)

    assert indexed[0].raw_lines[0].speaker == "채널 파일(올린 시각 미상)"


def test_apply_publishes_each_file_before_starting_the_next(tmp_path, monkeypatch):
    cfg = SimpleNamespace(key="pilot", bot_token="token")
    channel = {"id": "C1", "name": "팀-전산_abb155-업무", "is_member": True}
    candidates = [{"id": "F1", "name": "one.txt"}, {"id": "F2", "name": "two.txt"}]
    events = []
    monkeypatch.setattr(sync, "member_channels", lambda client: [channel])
    monkeypatch.setattr(
        sync,
        "scan",
        lambda client, channel_id, storage: sync.ChannelFileScan(
            channel_id=channel_id, total=2, candidates=candidates
        ),
    )

    def fake_collect(result, token, storage, *, workspace):
        file_id = result.candidates[0]["id"]
        events.append(("collect", file_id))
        return [SimpleNamespace(file_id=file_id, warnings=[], lines=[file_id])]

    def fake_publish(archive, got_cfg, got_channel, staged):
        events.append(("publish", staged[0].file_id))
        return 1, True

    monkeypatch.setattr(sync, "collect", fake_collect)
    monkeypatch.setattr(sync, "_publish", fake_publish)
    monkeypatch.setitem(
        sys.modules,
        "slack_sdk",
        SimpleNamespace(WebClient=lambda **kwargs: SimpleNamespace()),
    )

    stats = sync.sync_workspace(cfg, str(tmp_path), apply=True, pace=0)

    assert events == [
        ("collect", "F1"),
        ("publish", "F1"),
        ("collect", "F2"),
        ("publish", "F2"),
    ]
    assert stats["collected"] == 2
    assert stats["written"] == 2
