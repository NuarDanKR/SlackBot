"""B-99 — Master 가 DM 원문을 **옛 `workspaces/` 배치에 쓰지 않는다**.

2026-11-20: Master 가 DM 한 건을 `ARCHIVE_DIR/workspaces/tyit/dm/<user>/raw` 에 썼고,
Archiver 는 그 디렉터리를 보고 기동을 거부해 tyit 수집·이미지 알림이 멈췄다.
DM 은 운영 루트의 개인별 배치(`<ws>/dm/<user>/archive`)에 쌓여야 하고, 검색은 그
배치를 그대로 읽어 본인 DM 에서만 연다.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tybot.access import RequestContext
from tybot.archive import writer
from tybot.archive.store import ArchiveStore

USER = "U0BQUJ60N5U"
DM = "D0ABCDEF12"


def _bot(root, monkeypatch):
    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot.archive_dir = root
    bot.cfg = SimpleNamespace(bot_token="xoxb-fixture")
    bot._ingested = 0
    monkeypatch.setattr(
        WorkspaceBot,
        "_messages_from",
        lambda self, client, event: [
            writer.IncomingMessage(
                ts=datetime(2026, 11, 19, 6, 16, tzinfo=UTC),
                speaker="사람",
                text="개인 메모 — 내일 현장 점검",
                source_ts=event["ts"],
            )
        ],
    )
    monkeypatch.setattr(WorkspaceBot, "_confirm_attachments", lambda self, ch, staged: None)
    return bot


def test_master_dm_lands_in_the_per_user_layout_not_legacy_workspaces(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)

    bot._ingest_dm(None, {"user": USER, "channel": DM, "ts": "1795000000.000100"})

    assert not (tmp_path / "workspaces").exists()
    raws = list((tmp_path / "tyit" / "dm" / USER / "archive" / "raw").glob("*.md"))
    assert len(raws) == 1
    assert "개인 메모" in raws[0].read_text(encoding="utf-8")


def test_the_dm_is_readable_only_by_its_owner_in_dm(tmp_path, monkeypatch):
    bot = _bot(tmp_path, monkeypatch)
    bot._ingest_dm(None, {"user": USER, "channel": DM, "ts": "1795000000.000100"})

    store = ArchiveStore(tmp_path)
    mine = RequestContext(workspace="tyit", user_id=USER)
    other = RequestContext(workspace="tyit", user_id="U0OTHER0001")
    assert any(d.dm_user == USER for d in store.visible_docs(mine))
    assert not any(d.dm_user == USER for d in store.visible_docs(other))


def test_a_non_dm_conversation_is_not_written_anywhere(tmp_path, monkeypatch):
    """그룹 DM·채널 ID 는 개인 배치가 받지 않는다 — 옛 배치로 새지도 않는다."""
    bot = _bot(tmp_path, monkeypatch)

    bot._ingest_dm(None, {"user": USER, "channel": "C0ABCDEF12", "ts": "1795000000.000100"})

    assert not (tmp_path / "workspaces").exists()
    assert not list(tmp_path.rglob("*.md"))


# --- 2026-11-25: DM 첨부 정본도 옛 배치에 쓰지 않는다 ---------------------------------


def _capture_attachment_writes(monkeypatch):
    from tybot.slack import pilot

    calls: list[dict] = []
    monkeypatch.setattr(
        pilot, "write_attachment_docs", lambda root, staged, **kw: calls.append(kw) or []
    )
    monkeypatch.setattr(pilot.WorkspaceBot, "_channel_name", lambda self, client, ch: ch)
    return calls


def test_master_dm_attachment_documents_go_to_the_per_user_layout(tmp_path, monkeypatch):
    """2026-11-25 DM 첨부 한 건이 `workspaces/tyit/channels/<D…>` 를 만들어 Archiver 가 멈췄다."""
    calls = _capture_attachment_writes(monkeypatch)
    bot = _bot(tmp_path, monkeypatch)

    bot._write_attachment_docs(None, [object()], DM, dm_user=USER)

    (call,) = calls
    assert call["channel_archive"] == tmp_path / "tyit" / "dm" / USER / "archive"
    assert not (tmp_path / "workspaces").exists()


def test_a_channel_attachment_without_a_destination_is_refused_on_the_live_root(
    tmp_path, monkeypatch
):
    """Master 가 위치 없이 쓰면 운영 루트에서는 writer 가 거절한다 — 옛 배치가 안 생긴다."""
    import pytest

    from tybot.archive import attachment_writer

    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path))
    with pytest.raises(writer.LegacyLayoutRefused):
        attachment_writer.write_docs(
            tmp_path, [], workspace="tyit", channel_id="C0ABCDEF12",
            channel="#팀_전산", visibility="private", acl=frozenset({"#팀_전산"}),
        )
    assert not (tmp_path / "workspaces").exists()


def test_a_dm_attachment_write_failure_never_falls_back_to_the_legacy_layout(
    tmp_path, monkeypatch
):
    """1:1 DM 이 아닌 ID 면 개인 배치가 거절하고, 첨부 정본은 아무 데도 쓰지 않는다."""
    calls = _capture_attachment_writes(monkeypatch)
    bot = _bot(tmp_path, monkeypatch)

    bot._write_attachment_docs(None, [object()], "C0ABCDEF12", dm_user=USER)

    assert calls == []
    assert not (tmp_path / "workspaces").exists()

