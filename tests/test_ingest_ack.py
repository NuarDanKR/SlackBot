"""수집 ACK — **「검색됩니다」 를 언제 말해도 되나.**

결정: 2026-09-25 오너 §6, 후속 지시 1~8.

세 경로를 각각 본다.

| 경로 | 무엇 |
|---|---|
| DB | 상태를 앞으로만 밀고, 재전달에 뒤로 안 간다 |
| runtime | 수집기가 단계마다 남긴다 |
| Master | 답하기 전에 ACK 를 보고, 모르면 단언하지 않는다 |

DB 를 요구하지 않는다 — 요구하면 개발 PC 에서 안 돌고, **안 도는 시험은 지켜
주지 않는다.** 연결을 갈아 끼우고 무엇이 저장되는지 본다.
"""

from __future__ import annotations

import pytest

from tybot.archive import ingest_ack
from tybot.archive.archiving_state import IngestProgress, IngestState

# --- DB 경로 (연결은 가짜) ----------------------------------------------------

class FakeCursor:
    def __init__(self, rows: list[dict | None]) -> None:
        self.rows = list(rows)
        self.saved: list[dict] = []
        self.sql: list[str] = []

    def execute(self, sql, params=()):
        text = " ".join(str(sql).split())
        self.sql.append(text)
        if text.startswith("INSERT INTO archive_ingest_state"):
            self.saved.append(dict(params))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeConn:
    def __init__(self, cur: FakeCursor) -> None:
        self._cur = cur

    def cursor(self):
        return self._cur

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

    def make(rows: list[dict | None]):
        cur = FakeCursor(rows)
        monkeypatch.setattr(ingest_ack, "_connect", lambda: FakeConn(cur))
        return cur

    return make


def _row(state: str, total: int = 0, ready: int = 0) -> dict:
    return {"state": state, "attachment_total": total, "attachment_ready": ready}


def test_the_first_event_records_received(db):
    cur = db([None])

    got = ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.RECEIVED,
    )

    assert got == IngestState.RECEIVED
    assert cur.saved[0]["state"] == "received"


def test_the_row_is_locked_before_it_is_read(db):
    """읽고 판정하고 쓰는 사이에 재전달이 끼면 둘 다 같은 값을 보고 쓴다."""
    cur = db([None])

    ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.RECEIVED,
    )

    assert cur.sql[0].startswith("SELECT pg_advisory_xact_lock")


def test_a_redelivered_event_does_not_move_the_state_backwards(db):
    """**이게 이 파일의 이유다.**

    Slack 은 재전달을 한다. `ready` 인 메시지에 `received` 가 다시 오면 그것은 새
    사실이 아니라 같은 사실의 재방송이다. 뒤로 보내면 그 순간 「검색된다」 가
    「아직」 으로 바뀌고, 사람은 방금 본 것이 사라졌다고 읽는다.
    """
    cur = db([_row("ready")])

    got = ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.RECEIVED,
    )

    assert got == IngestState.READY
    assert cur.saved == [], "아무것도 쓰지 않는다"


@pytest.mark.parametrize("terminal", ["ready", "refused", "failed"])
def test_terminal_states_never_move(db, terminal):
    cur = db([_row(terminal)])

    got = ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.RAW_WRITTEN,
    )

    assert got == IngestState(terminal)
    assert cur.saved == []


def test_the_same_state_twice_writes_nothing(db):
    """재전달이 같은 단계를 다시 보내도 갱신 시각만 흔들리지 않게 한다."""
    cur = db([_row("raw_written", 2, 0)])

    ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.RAW_WRITTEN, attachment_total=2, attachment_ready=0,
    )

    assert cur.saved == []


def test_the_same_state_with_more_attachments_does_write(db):
    """변환이 하나 더 끝난 경우다. 이건 새 사실이다."""
    cur = db([_row("attachment_pending", 3, 1)])

    ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.ATTACHMENT_PENDING, attachment_total=3, attachment_ready=2,
    )

    assert cur.saved[0]["ready"] == 2


def test_ready_is_refused_while_attachments_are_pending(db):
    """표의 CHECK 도 막지만, **여기서 먼저** 막는다."""
    cur = db([_row("attachment_pending", 3, 1)])

    got = ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.READY, attachment_total=3, attachment_ready=1,
    )

    assert got == IngestState.ATTACHMENT_PENDING
    assert cur.saved == []


def test_attachment_counts_are_kept_when_not_given(db):
    """본문 경로가 첨부 수를 0 으로 덮으면, 첨부가 아직인데 ready 로 갈 수 있다."""
    cur = db([_row("attachment_pending", 3, 1)])

    ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.PARTIAL,
    )

    assert (cur.saved[0]["total"], cur.saved[0]["ready"]) == (3, 1)


def test_ready_count_cannot_exceed_the_total(db):
    cur = db([None])

    ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.RECEIVED, attachment_total=2, attachment_ready=5,
    )

    assert cur.saved[0]["ready"] == 2


def test_without_a_database_nothing_is_recorded(monkeypatch):
    """기록을 못 한다고 수집을 멈추지 않는다. **놓친 원본은 되돌릴 수 없다.**"""
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.RECEIVED,
    ) is None


def test_a_database_error_does_not_raise(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")

    def boom():
        raise RuntimeError("DB 다운")

    monkeypatch.setattr(ingest_ack, "_connect", boom)

    assert ingest_ack.advance(
        workspace="tyit", channel_id="C1", message_ts="1.0001",
        target=IngestState.RECEIVED,
    ) is None


# --- Master 경로 --------------------------------------------------------------

@pytest.fixture
def state(monkeypatch):
    def given(progress: IngestProgress | None):
        monkeypatch.setattr(ingest_ack, "read", lambda *_: progress)

    return given


def test_master_says_searchable_only_when_ready(state):
    state(IngestProgress(IngestState.READY))

    assert "검색할 수 있습니다" in ingest_ack.claim("tyit", "C1", "1.0001")


def test_master_does_not_claim_searchable_while_attachments_pend(state):
    """사람이 찾으러 갔다가 못 찾으면 **봇이 거짓말한 것**이 된다."""
    state(IngestProgress(IngestState.ATTACHMENT_PENDING, 3, 1))

    claim = ingest_ack.claim("tyit", "C1", "1.0001")

    assert "1/3" in claim
    assert "안 잡힙니다" in claim
    assert "검색할 수 있습니다" not in claim


def test_master_says_it_does_not_know_when_there_is_no_row(state):
    """없는 것을 「아직 안 됐다」 로도 말하지 않는다 — 모르면서 아는 척이다."""
    state(None)

    assert ingest_ack.claim("tyit", "C1", "1.0001") == ingest_ack.UNKNOWN_CLAIM


def test_unknown_never_claims_searchable(state):
    state(None)

    assert "검색" in ingest_ack.UNKNOWN_CLAIM
    assert "검색할 수 있습니다" not in ingest_ack.UNKNOWN_CLAIM


@pytest.mark.parametrize(
    ("progress", "expected"),
    [
        (IngestProgress(IngestState.READY), True),
        (IngestProgress(IngestState.ATTACHMENT_PENDING, 2, 1), False),
        (IngestProgress(IngestState.RAW_WRITTEN), False),
        (IngestProgress(IngestState.PARTIAL, 2, 2), False),
        (None, False),
    ],
)
def test_is_searchable_is_false_unless_it_knows(state, progress, expected):
    """**모르면 `False`.** 이 함수가 단언에 쓰이므로 기본이 보수적이어야 한다."""
    state(progress)

    assert ingest_ack.is_searchable("tyit", "C1", "1.0001") is expected


def test_the_claim_wording_lives_in_one_place():
    """호출부마다 문구를 쓰면 한 군데가 「올렸습니다」 를 「검색됩니다」 로 적는다."""
    from pathlib import Path

    source = Path(ingest_ack.__file__).read_text(encoding="utf-8")

    assert "searchable_claim" in source
    # 이 모듈이 직접 「검색할 수 있습니다」 를 만들지 않는다
    assert "검색할 수 있습니다" not in source
