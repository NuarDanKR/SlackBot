"""수집 ACK — **「운영 검색에 잡힌다」 를 언제 말해도 되나.**

결정: 2026-09-25 오너 §6, 2026-09-26 보완 지시 1~6.

세 경로를 각각 본다.

| 경로 | 무엇 |
|---|---|
| DB | 앞으로만 밀고, 재전달에 뒤로 안 가고, 못 쓰면 outbox 로 간다 |
| runtime | 수집기가 단계마다 남긴다(`test_archiving_bot.py`) |
| Master | 답하기 전에 ACK 를 보고, 모르면 단언하지 않는다 |

DB 를 요구하지 않는다 — 요구하면 개발 PC 에서 안 돌고, **안 도는 시험은 지켜
주지 않는다.** 연결을 갈아 끼우고 무엇이 저장되는지 본다.
"""

from __future__ import annotations

import json

import pytest

from tybot.archive import ingest_ack
from tybot.archive.archiving_state import IngestProgress, IngestState


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
def db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))

    def make(rows: list[dict | None]):
        cur = FakeCursor(rows)
        monkeypatch.setattr(ingest_ack, "_connect", lambda: FakeConn(cur))
        return cur

    return make


def _row(state: str, total: int = 0, ready: int = 0, written_to: str = "shadow") -> dict:
    return {
        "state": state,
        "attachment_total": total,
        "attachment_ready": ready,
        "written_to": written_to,
    }


def _advance(**over):
    base = {
        "workspace": "tyit", "channel_id": "C1", "message_ts": "1.0001",
        "target": IngestState.RECEIVED,
    }
    return ingest_ack.advance(**(base | over))


# --- DB 경로 -----------------------------------------------------------------

def test_the_first_event_records_received(db):
    cur = db([None])

    assert _advance() == IngestState.RECEIVED
    assert cur.saved[0]["state"] == "received"


def test_the_row_is_locked_before_it_is_read(db):
    """읽고 판정하고 쓰는 사이에 재전달이 끼면 둘 다 같은 값을 보고 쓴다."""
    cur = db([None])

    _advance()

    assert cur.sql[0].startswith("SELECT pg_advisory_xact_lock")


def test_a_redelivered_event_does_not_move_the_state_backwards(db):
    """Slack 재전달은 새 사실이 아니라 **같은 사실의 재방송**이다."""
    cur = db([_row("ready")])

    assert _advance() == IngestState.READY
    assert cur.saved == [], "아무것도 쓰지 않는다"


@pytest.mark.parametrize("terminal", ["ready", "refused", "failed"])
def test_terminal_states_never_move(db, terminal):
    cur = db([_row(terminal)])

    assert _advance(target=IngestState.RAW_WRITTEN) == IngestState(terminal)
    assert cur.saved == []


def test_the_same_state_twice_writes_nothing(db):
    cur = db([_row("raw_written", 2, 0)])

    _advance(target=IngestState.RAW_WRITTEN, attachment_total=2, attachment_ready=0)

    assert cur.saved == []


def test_the_same_state_with_more_attachments_does_write(db):
    """변환이 하나 더 끝난 경우다. 이건 새 사실이다."""
    cur = db([_row("attachment_pending", 3, 1)])

    _advance(
        target=IngestState.ATTACHMENT_PENDING, attachment_total=3, attachment_ready=2
    )

    assert cur.saved[0]["ready"] == 2


def test_ready_is_refused_while_attachments_are_pending(db):
    """표의 CHECK 도 막지만, **여기서 먼저** 막는다."""
    cur = db([_row("attachment_pending", 3, 1)])

    got = _advance(target=IngestState.READY, attachment_total=3, attachment_ready=1)

    assert got == IngestState.ATTACHMENT_PENDING
    assert cur.saved == []


def test_attachment_counts_are_kept_when_not_given(db):
    """본문 경로가 첨부 수를 0 으로 덮으면, 첨부가 아직인데 ready 로 갈 수 있다."""
    cur = db([_row("attachment_pending", 3, 1)])

    _advance(target=IngestState.PARTIAL)

    assert (cur.saved[0]["total"], cur.saved[0]["ready"]) == (3, 1)


def test_ready_count_cannot_exceed_the_total(db):
    cur = db([None])

    _advance(attachment_total=2, attachment_ready=5)

    assert cur.saved[0]["ready"] == 2


# --- outbox — DB 가 죽어도 사실은 남는다 --------------------------------------

@pytest.fixture
def broken_db(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))

    def boom():
        raise RuntimeError("DB 다운")

    monkeypatch.setattr(ingest_ack, "_connect", boom)
    return tmp_path


def test_a_database_failure_spools_the_fact_to_a_file(broken_db):
    """상태가 통째로 사라지면 복구 뒤에도 「모른다」 가 되고 사람은 다시 올린다."""
    assert _advance(target=IngestState.RAW_WRITTEN) is None

    lines = ingest_ack.outbox_path("tyit").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    saved = json.loads(lines[0])
    assert saved["target"] == "raw_written"
    assert saved["message_ts"] == "1.0001"


def test_the_outbox_lives_outside_the_archive(broken_db, monkeypatch):
    """아카이브 안이면 `ArchiveStore` 글롭에 걸려 **답변 근거가 된다.**"""
    monkeypatch.setenv("ARCHIVE_DIR", str(broken_db / "archive"))
    _advance()

    path = ingest_ack.outbox_path("tyit").resolve()

    assert "archive" not in path.parts
    assert path.suffix == ".jsonl", "원문 글롭은 .md 만 본다"


def test_without_a_database_the_fact_still_lands_in_the_outbox(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))

    assert _advance() is None
    assert ingest_ack.outbox_path("tyit").is_file()


def test_draining_applies_the_spooled_facts(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ingest_ack, "_connect", lambda: (_ for _ in ()).throw(RuntimeError()))
    _advance()
    _advance(target=IngestState.RAW_WRITTEN)

    cur = FakeCursor([None, _row("received")])
    monkeypatch.setattr(ingest_ack, "_connect", lambda: FakeConn(cur))
    got = ingest_ack.drain_outbox("tyit")

    assert got == {"applied": 2, "left": 0}
    assert not ingest_ack.outbox_path("tyit").exists()
    assert [row["state"] for row in cur.saved] == ["received", "raw_written"]


def test_draining_twice_is_idempotent(monkeypatch, tmp_path):
    """`advance` 가 멱등이라 두 번 밀어도 같다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ingest_ack, "_connect", lambda: (_ for _ in ()).throw(RuntimeError()))
    _advance()

    cur = FakeCursor([None])
    monkeypatch.setattr(ingest_ack, "_connect", lambda: FakeConn(cur))
    ingest_ack.drain_outbox("tyit")
    second = ingest_ack.drain_outbox("tyit")

    assert second == {"applied": 0, "left": 0}


def test_a_line_that_still_fails_is_kept(monkeypatch, tmp_path):
    """통째로 지우면 한 줄이 실패했을 때 **나머지 사실도 같이 사라진다.**"""
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    monkeypatch.setattr(ingest_ack, "_connect", lambda: (_ for _ in ()).throw(RuntimeError()))
    _advance()

    got = ingest_ack.drain_outbox("tyit")

    assert got == {"applied": 0, "left": 1}
    assert ingest_ack.outbox_path("tyit").is_file()


def test_a_corrupt_line_is_quarantined_instead_of_disappearing(monkeypatch, tmp_path):
    """재시도는 막지 않되 깨졌다는 감사 사실을 없애지 않는다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    path = ingest_ack.outbox_path("tyit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ broken\n", encoding="utf-8")
    monkeypatch.setattr(ingest_ack, "_connect", lambda: FakeConn(FakeCursor([None])))

    assert ingest_ack.drain_outbox("tyit") == {"applied": 0, "left": 0}
    assert not path.exists()
    assert ingest_ack.dead_letter_path("tyit").read_text(encoding="utf-8") == "{ broken\n"


def test_a_successful_write_drains_facts_left_by_an_outage(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/db")
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    path = ingest_ack.outbox_path("tyit")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({
            "workspace": "tyit", "channel_id": "C0", "message_ts": "0.0001",
            "target": "received", "attachment_total": None,
            "attachment_ready": None, "written_to": "shadow", "doc_path": "",
            "error_code": "", "queued_at": "2026-09-26T00:00:00+00:00",
        }) + "\n",
        encoding="utf-8",
    )
    cur = FakeCursor([None, None])
    monkeypatch.setattr(ingest_ack, "_connect", lambda: FakeConn(cur))

    assert _advance() == IngestState.RECEIVED

    assert not path.exists()
    assert [row["state"] for row in cur.saved] == ["received", "received"]


def test_failing_to_spool_raises_an_operational_warning(broken_db, monkeypatch, caplog):
    """파일에도 못 쓰면 **운영이 알아야 한다** — 이 시점부터 ACK 가 통째로 없다."""
    monkeypatch.setattr(ingest_ack, "_spool", lambda _: False)

    with caplog.at_level("ERROR"):
        _advance()

    assert any("outbox" in record.message for record in caplog.records)


def test_spooling_does_not_raise_when_the_disk_refuses(broken_db, monkeypatch):
    def refuse(*_, **__):
        raise OSError("읽기 전용")

    monkeypatch.setattr("pathlib.Path.mkdir", refuse)

    assert _advance() is None, "예외가 수집 루프로 올라가지 않는다"


# --- Master 경로 --------------------------------------------------------------

def _status(state: IngestState, *, written_to="live", total=0, ready=0):
    return ingest_ack.AckStatus(IngestProgress(state, total, ready), written_to)


@pytest.fixture
def state(monkeypatch):
    def given(status):
        monkeypatch.setattr(ingest_ack, "read", lambda *_: status)

    return given


def test_master_says_searchable_only_when_ready_and_live(state):
    state(_status(IngestState.READY, written_to="live"))

    assert "검색할 수 있습니다" in ingest_ack.claim("tyit", "C1", "1.0001")


def test_shadow_ready_is_never_reported_as_operationally_searchable(state):
    """**이게 이번 보완의 핵심이다.**

    그림자 수집도 `ready` 가 된다 — 그림자 경로에서는 본문도 첨부도 다 끝났기
    때문이다. 그런데 그 파일은 운영 아카이브에 없어서 운영 검색이 못 찾는다.
    처음 구현은 `state` 만 보고 「검색할 수 있습니다」 라고 했다.
    """
    state(_status(IngestState.READY, written_to="shadow"))

    claim = ingest_ack.claim("tyit", "C1", "1.0001")

    assert claim == ingest_ack.SHADOW_CLAIM
    assert "검색할 수 있습니다" not in claim
    assert ingest_ack.is_searchable("tyit", "C1", "1.0001") is False


@pytest.mark.parametrize(
    "progress", [IngestState.RECEIVED, IngestState.RAW_WRITTEN, IngestState.PARTIAL]
)
def test_every_shadow_state_names_the_shadow_destination(state, progress):
    state(_status(progress, written_to="shadow"))

    assert ingest_ack.claim("tyit", "C1", "1.0001") == ingest_ack.SHADOW_CLAIM


def test_ready_without_a_destination_is_not_searchable_either(state):
    """빈 `written_to` 는 **모른다** 다. 모르면 단언하지 않는다."""
    state(_status(IngestState.READY, written_to=""))

    assert ingest_ack.claim("tyit", "C1", "1.0001") == ingest_ack.SHADOW_CLAIM
    assert ingest_ack.is_searchable("tyit", "C1", "1.0001") is False


def test_master_does_not_claim_searchable_while_attachments_pend(state):
    """사람이 찾으러 갔다가 못 찾으면 **봇이 거짓말한 것**이 된다."""
    state(_status(IngestState.ATTACHMENT_PENDING, total=3, ready=1))

    claim = ingest_ack.claim("tyit", "C1", "1.0001")

    assert "1/3" in claim
    assert "검색할 수 있습니다" not in claim


def test_master_says_it_does_not_know_when_there_is_no_row(state):
    """없는 것을 「아직 안 됐다」 로도 말하지 않는다 — 모르면서 아는 척이다."""
    state(None)

    assert ingest_ack.claim("tyit", "C1", "1.0001") == ingest_ack.UNKNOWN_CLAIM


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (_status(IngestState.READY, written_to="live"), True),
        (_status(IngestState.READY, written_to="shadow"), False),
        (_status(IngestState.READY, written_to=""), False),
        (_status(IngestState.ATTACHMENT_PENDING, total=2, ready=1), False),
        (_status(IngestState.RAW_WRITTEN), False),
        (_status(IngestState.PARTIAL, total=2, ready=2), False),
        (None, False),
    ],
)
def test_is_searchable_is_false_unless_it_knows(state, status, expected):
    """**모르면 `False`.** 이 함수가 단언에 쓰이므로 기본이 보수적이어야 한다."""
    state(status)

    assert ingest_ack.is_searchable("tyit", "C1", "1.0001") is expected


def test_no_path_reports_shadow_as_operationally_searchable():
    """`searchable` 이 `written_to` 를 안 보는 경로가 하나도 없어야 한다."""
    for state_value in IngestState:
        for destination in ("", "shadow"):
            status = ingest_ack.AckStatus(IngestProgress(state_value), destination)
            assert status.searchable is False, f"{state_value}/{destination}"


def test_the_claim_wording_lives_in_one_place():
    """호출부마다 문구를 쓰면 한 군데가 「올렸습니다」 를 「검색됩니다」 로 적는다."""
    from pathlib import Path

    source = Path(ingest_ack.__file__).read_text(encoding="utf-8")

    assert "searchable_claim" in source
    assert "검색할 수 있습니다" not in source
