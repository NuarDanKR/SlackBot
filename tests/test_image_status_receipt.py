"""B-100 — Archiver 가 실시간으로 받은 이미지에만 「접수」 상태를 적는다.

- 실시간 이벤트의 PNG/JPEG/WebP 정본 → 접수 기록 1회(이미지마다)
- 소급(`ingest_message` 직접 호출)은 옛 이미지 스레드에 댓글을 달지 않는다
- 접수 기록이 실패해도 원문·첨부 수집은 그대로 끝난다
- 우리 상태 댓글(봇 메시지)은 수집되지 않는다(원칙 1)
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import test_archiving_bot as base

from tybot import archiving_bot
from tybot.archive import image_status_notice as notices
from tybot.archive import writer

TS = "1790070000.000001"


@pytest.fixture
def collected(monkeypatch, tmp_path):
    """첨부 하나가 붙은 메시지를 수집하게 만든다. 정본 문서는 인자로 고른다."""
    monkeypatch.setattr(archiving_bot.ingest_ack, "advance", lambda **_kw: None)
    staged = SimpleNamespace(file_id="F12345678", reference_lines=["[첨부] a"], lines=["[첨부] a"])

    def fake_messages(*_args, staged_out, **_kwargs):
        staged_out.append(staged)
        return [
            writer.IncomingMessage(
                ts=datetime.fromtimestamp(float(TS), tz=UTC),
                speaker="U12345678",
                text="[첨부] a",
                source_ts=TS,
            )
        ]

    monkeypatch.setattr(archiving_bot, "_messages_from", fake_messages)
    monkeypatch.setattr(
        archiving_bot,
        "confirm_archived",
        lambda *_a, **_k: {"F12345678": archiving_bot.ARCHIVE_DONE},
    )

    def build(docs, receipts):
        monkeypatch.setattr(archiving_bot, "write_attachment_docs", lambda *_a, **_k: docs)
        cfg = archiving_bot.load_archiver_workspaces(base._env(tmp_path))[0]
        return archiving_bot.ArchiveCollector(cfg, tmp_path / "archive", image_receipts=receipts)

    return build


def _doc(filetype="png", file_id="F12345678"):
    return SimpleNamespace(
        file_id=file_id,
        filetype=filetype,
        name=f"사진.{filetype}",
        conversion_state="succeeded",
        text="안전제일",
    )


def _event(**over):
    return base._event(text="", subtype="file_share", files=[{"id": "F12345678"}]) | over


def test_a_realtime_image_records_one_receipt(collected):
    calls = []
    collector = collected([_doc()], lambda *args: calls.append(args) or 1)

    collector.ingest_event(base.Client(), _event())

    ((workspace, channel, ts, docs),) = calls
    assert (workspace, channel, ts) == ("tyit", "C12345678", TS)
    assert [d.file_id for d in docs] == ["F12345678"]


def test_backfill_does_not_comment_on_old_image_threads(collected):
    calls = []
    collector = collected([_doc()], lambda *args: calls.append(args) or 1)

    collector.ingest_message(
        base.Client(), _event(), channel="#c", channel_id="C12345678", is_private=False
    )

    assert calls == []


def test_a_failed_receipt_does_not_stop_collection(collected, tmp_path):
    def broken(*_args):
        raise RuntimeError("status table missing")

    collector = collected([_doc()], broken)

    outcome = collector.ingest_event(base.Client(), _event())

    assert outcome in {"written", "partial"}
    assert list((tmp_path / "archive").glob(base._RAW))


def test_only_vision_image_types_are_receipted(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # DB 가 없으면 아무것도 하지 않는다 — 수집은 DB 없이도 돈다.
    assert archiving_bot.record_image_receipts("tyit", "C12345678", TS, [_doc()]) == 0
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    # 이미지가 아니면 DB 를 열지도 않는다.
    monkeypatch.setattr(notices, "connect", lambda *_a: pytest.fail("must not connect"))
    assert archiving_bot.record_image_receipts("tyit", "C12345678", TS, [_doc("pdf")]) == 0


class _Cursor:
    def __init__(self, enabled):
        self.enabled = enabled

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def execute(self, sql, args=None):
        assert "image_notice_vision_enabled" in sql
        assert args == ("tyit",)

    def fetchone(self):
        return {"enabled": self.enabled}


class _Conn:
    def __init__(self, enabled):
        self.enabled = enabled

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def cursor(self):
        return _Cursor(self.enabled)


@pytest.mark.parametrize("enabled", [True, False])
def test_receipts_say_whether_vision_will_run(monkeypatch, enabled):
    monkeypatch.setenv("DATABASE_URL", "postgresql://unused")
    repo = notices.MemoryNoticeRepo()
    monkeypatch.setattr(notices, "connect", lambda *_a: _Conn(enabled))
    monkeypatch.setattr(notices, "PostgresNoticeRepo", lambda _conn: repo)

    recorded = archiving_bot.record_image_receipts(
        "tyit", "C12345678", TS, [_doc(), _doc("jpg", "F99999999"), _doc("pdf", "F88888888")]
    )

    assert recorded == 2
    texts = [notices.render(row.phase, row.payload) for row in repo.rows.values()]
    marker = "- Vision LLM: 처리 대기" if enabled else "- Vision LLM: 사용 안 함"
    assert all(marker in text for text in texts)
    assert all("- 기본 OCR: 보관 완료" in text for text in texts)
    # 같은 이벤트가 다시 와도(재전달) 접수는 한 번이다.
    assert archiving_bot.record_image_receipts("tyit", "C12345678", TS, [_doc()]) == 0


def test_our_status_comment_is_never_collected(tmp_path):
    cfg = archiving_bot.load_archiver_workspaces(base._env(tmp_path))[0]
    collector = archiving_bot.ArchiveCollector(cfg, tmp_path / "archive")
    comment = base._event(
        text="✅ 이미지 접수 완료",
        bot_id="B123",
        thread_ts=TS,
        ts="1790070001.000001",
        metadata={"event_type": notices.METADATA_EVENT},
    )
    assert collector.ingest_event(base.Client(), comment) == "skipped-bot-or-system"
    assert not (tmp_path / "archive").exists()
