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
