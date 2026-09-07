"""검토자에게 하루치를 밀어 준다 (2026-09-08).

첨부 승인을 `/첨부` 로만 열어 뒀더니 **대기 31건·승인 0건**이 됐다. 사람은
모르는 일을 하러 찾아오지 않는다. 당겨 가는 방식은 게이트가 아니라 정체였다.

여기서 지키는 것은 「보냈다」 가 아니라 **사람이 실제로 볼 수 있는 목록**이다.
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from tybot import daily_review as dr
from tybot.attachment_review import APPROVED, PENDING, REJECTED

KST = timezone(timedelta(hours=9))


def _stage(tmp_path, *, name, file_id, status=PENDING, staged_at="2026-09-08T09:00:00+09:00",
           ws="tyit", ch="C1"):
    """`stage_files` 가 만드는 것과 같은 모양."""
    archive = tmp_path / "archive"
    archive.mkdir(exist_ok=True)
    suffix = f"workspaces/{ws}/channels/{ch}/attachments/{file_id}"
    staged = tmp_path / "staging" / suffix
    staged.mkdir(parents=True, exist_ok=True)
    obj = tmp_path / "objects" / suffix / name
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(b"x")
    meta = {
        "schema_version": 1, "status": status, "slack_file_id": file_id,
        "name": name, "filetype": name.rsplit(".", 1)[-1], "mimetype": "",
        "declared_size": 4096, "object_path": str(obj),
    }
    if staged_at is not None:
        meta["staged_at"] = staged_at
    (staged / "metadata.json").write_text(json.dumps(meta, ensure_ascii=False),
                                          encoding="utf-8")
    return archive


def _digest(archive, *, today=None, backlog=0, name="스캔본.png", ch="C1"):
    return dr.Digest(
        workspace="tyit", channel_id=ch, channel_name="#팀_전산(ABB155)_공지",
        recipient="U1", on=date(2026, 9, 8),
        today=today if today is not None else [],
        backlog=backlog,
    )


# --- 무엇이 목록에 오르는가 ---------------------------------------------------
def test_a_converted_file_is_not_asked_about(tmp_path):
    """이미 답변에 쓰이고 있는 파일을 목록에 넣으면, 사람이 그 목록을 믿지 않게 된다."""
    archive = _stage(tmp_path, name="가정산서.xlsx", file_id="F1")

    got = dr.blocked(archive, workspace="tyit", channel_id="C1",
                     extracted={"가정산서.xlsx"})

    assert got == []


def test_a_scan_is_asked_about(tmp_path):
    """글자가 없어 PII 검사가 아예 돌지 않은 것 — 사람이 봐야 하는 유일한 경우다."""
    archive = _stage(tmp_path, name="스캔본.png", file_id="F1")

    got = dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set())

    assert [i.name for i in got] == ["스캔본.png"]


@pytest.mark.parametrize("status", [APPROVED, REJECTED])
def test_a_decided_file_is_not_asked_again(tmp_path, status):
    """사람이 이미 판단한 것을 매일 다시 물으면 그 판단이 무시된다."""
    archive = _stage(tmp_path, name="스캔본.png", file_id="F1", status=status)

    assert dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set()) == []


def test_another_channel_is_not_mixed_in(tmp_path):
    """검토자는 자기 채널만 본다. 섞이면 권한이 조용히 넓어진다."""
    _stage(tmp_path, name="남의것.png", file_id="F9", ch="C2")
    archive = _stage(tmp_path, name="내것.png", file_id="F1", ch="C1")

    got = dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set())

    assert [i.name for i in got] == ["내것.png"]


# --- 오늘 것과 밀린 것 --------------------------------------------------------
def test_todays_items_are_listed_and_the_backlog_is_counted(tmp_path):
    """밀린 31건 사이에 오늘의 2건을 끼워 넣으면 오늘 것이 묻힌다."""
    _stage(tmp_path, name="옛날것.png", file_id="F1", staged_at="2026-08-01T09:00:00+09:00")
    archive = _stage(tmp_path, name="오늘것.png", file_id="F2",
                     staged_at="2026-09-08T10:00:00+09:00")

    items = dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set())
    today, backlog = dr.split(items, date(2026, 9, 8))

    assert [i.name for i in today] == ["오늘것.png"]
    assert backlog == 1


def test_an_unknown_date_is_not_counted_as_today(tmp_path):
    """모르는 것을 오늘로 치면 밀린 것이 **매일** 오늘 자리에 올라온다."""
    archive = _stage(tmp_path, name="시각없음.png", file_id="F1", staged_at=None)

    items = dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set())
    today, backlog = dr.split(items, date(2026, 9, 8))

    assert today == []
    assert backlog == 1


def test_a_broken_timestamp_does_not_raise(tmp_path):
    """메타데이터 한 줄이 깨졌다고 그 채널 전체 발송이 멈추면 안 된다."""
    archive = _stage(tmp_path, name="깨짐.png", file_id="F1", staged_at="어제쯤")

    items = dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set())

    assert dr.split(items, date(2026, 9, 8)) == ([], 1)


def test_a_utc_timestamp_lands_on_the_kst_day(tmp_path):
    """`staged_at` 은 UTC 로 적힌다(`datetime.now(UTC)`).

    변환 없이 날짜만 떼면 **UTC 15시 이후에 올라온 것이 하루 전으로 밀린다** —
    한국 시간으로 자정 넘어 올라온 파일이 오늘 목록에서 빠지고, 다음 날에는
    「밀린 것」 으로 잡혀 건별로는 영영 안 보인다.
    """
    archive = _stage(tmp_path, name="새벽.png", file_id="F1",
                     staged_at="2026-09-07T22:00:00+00:00")  # KST 2026-09-08 07:00

    items = dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set())
    today, _ = dr.split(items, date(2026, 9, 8))

    assert [i.name for i in today] == ["새벽.png"]


# --- 화면 --------------------------------------------------------------------
def test_every_button_carries_the_channel(tmp_path):
    """**DM 에서 누르면** `body["channel"]["id"]` 는 DM 채널(`D…`)이다.

    그 값으로 원본을 찾으면 0건이 되고, 오류가 아니라 「대상을 특정하지 못했습니다」
    로 나타난다. 그래서 버튼 값이 원 채널을 함께 싣는다.
    """
    from tybot import attachment_view

    archive = _stage(tmp_path, name="스캔본.png", file_id="F1")
    items = dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set())

    blocks = dr.blocks(_digest(archive, today=items))

    buttons = [e for b in blocks if b["type"] == "actions" for e in b["elements"]]
    assert buttons, "버튼이 없다"
    for button in buttons:
        assert button["value"], "value 가 비면 Slack 이 메시지를 통째로 거부한다"
        channel, file_id = attachment_view.unpack_value(button["value"])
        assert channel == "C1"
        assert file_id == "F1"


def test_the_backlog_is_one_line_not_a_list(tmp_path):
    """밀린 것을 늘어놓으면 오늘 것이 묻힌다."""
    archive = _stage(tmp_path, name="스캔본.png", file_id="F1")
    items = dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set())

    blocks = dr.blocks(_digest(archive, today=items, backlog=30))

    tail = blocks[-1]
    assert tail["type"] == "context"
    assert "30건" in tail["elements"][0]["text"]


def test_the_dm_says_what_approval_means(tmp_path):
    """무엇을 하는 버튼인지 화면이 말해야 한다."""
    blocks = dr.blocks(_digest(tmp_path / "archive"))

    head = blocks[0]["text"]["text"]
    assert "LLM 제공자에게 전달" in head
    assert "개인정보" in head


def test_the_notification_preview_has_no_filename(tmp_path):
    """알림 미리보기는 잠금화면에 뜬다. 파일명에는 사업장·문서 종류가 들어 있다."""
    archive = _stage(tmp_path, name="김해외동_계약자명단.png", file_id="F1")
    items = dr.blocked(archive, workspace="tyit", channel_id="C1", extracted=set())

    assert "김해외동" not in dr.text_fallback(_digest(archive, today=items))


# --- 언제 · 누구에게 ----------------------------------------------------------
def test_it_waits_for_the_configured_time():
    assert not dr.due(time(8, 0), datetime(2026, 9, 8, 7, 59, tzinfo=KST))
    assert dr.due(time(8, 0), datetime(2026, 9, 8, 8, 0, tzinfo=KST))


def test_a_late_run_still_sends_the_same_day():
    """지난 회의 알림과 다르다 — 어제 올라온 스캔본은 오후에 확인해도 쓸모가 있다."""
    assert dr.due(time(8, 0), datetime(2026, 9, 8, 17, 30, tzinfo=KST))


def test_the_owner_gets_it_when_no_reviewer_is_set(monkeypatch):
    """검토자를 안 정한 채널이 조용히 빠지면, 그 채널 첨부는 영영 안 읽힌다."""
    from tybot import reviewers

    monkeypatch.setattr(reviewers, "reviewers_for", lambda ws, ch: [])

    assert dr.recipients("tyit", "C1", owner="UOWNER") == ["UOWNER"]


def test_a_db_failure_does_not_hand_it_to_the_owner(monkeypatch):
    """장애가 곧 권한 이동이 되면 안 된다 — 검토자가 있는데 개설자에게 갈 수 있다."""
    from tybot import reviewers

    def boom(ws, ch):
        raise reviewers.ReviewerError("DB 없음")

    monkeypatch.setattr(reviewers, "reviewers_for", boom)

    assert dr.recipients("tyit", "C1", owner="UOWNER") == []


# --- 하루에 한 번 · 보낼 것이 없으면 안 보낸다 --------------------------------
class FakeConn:
    """`already_sent` / `mark_sent` 만 흉내 낸다."""

    def __init__(self):
        self.rows: set[tuple] = set()
        self.committed = 0

    class _Cur:
        def __init__(self, outer):
            self.outer = outer
            self.result = None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params):
            if "SELECT" in sql:
                self.result = (1,) if tuple(params) in self.outer.rows else None
            else:
                self.outer.rows.add(tuple(params[:5]))

        def fetchone(self):
            return self.result

    def cursor(self):
        return self._Cur(self)

    def commit(self):
        self.committed += 1


class FakeClient:
    def __init__(self):
        self.sent: list[dict] = []

    def conversations_open(self, users):
        return {"channel": {"id": f"D-{users}"}}

    def chat_postMessage(self, **kwargs):
        self.sent.append(kwargs)
        return {"ok": True}


def _run(tmp_path, archive, conn, client, *, extracted=(), now=None, owners=None):
    return dr.run(
        conn,
        {"tyit": client},
        archive_dir=archive,
        channels=[("tyit", "C1", "#팀_전산(ABB155)_공지", time(8, 0))],
        extracted_for=lambda ws, ch: set(extracted),
        owners=owners or {},
        now=now or datetime(2026, 9, 8, 9, 0, tzinfo=KST),
    )


def test_it_sends_once_a_day(tmp_path, monkeypatch):
    """타이머는 1분마다 돈다. 매번 보내면 사람이 그 DM 을 끈다."""
    from tybot import reviewers

    monkeypatch.setattr(reviewers, "reviewers_for", lambda ws, ch: [
        type("R", (), {"reviewer_user": "U1"})()
    ])
    archive = _stage(tmp_path, name="스캔본.png", file_id="F1")
    conn, client = FakeConn(), FakeClient()

    first = _run(tmp_path, archive, conn, client)
    second = _run(tmp_path, archive, conn, client)

    assert first.sent == 1
    assert second.sent == 0 and second.skipped == 1
    assert len(client.sent) == 1


def test_nothing_to_review_means_no_dm(tmp_path, monkeypatch):
    """매일 "없습니다" 가 오면 사람이 이 DM 을 끈다. 침묵이 「없음」 이어야 한다."""
    from tybot import reviewers

    monkeypatch.setattr(reviewers, "reviewers_for", lambda ws, ch: [
        type("R", (), {"reviewer_user": "U1"})()
    ])
    archive = _stage(tmp_path, name="가정산서.xlsx", file_id="F1")
    conn, client = FakeConn(), FakeClient()

    result = _run(tmp_path, archive, conn, client, extracted={"가정산서.xlsx"})

    assert result.sent == 0
    assert client.sent == []


def test_no_recipient_is_reported_not_swallowed(tmp_path, monkeypatch, caplog):
    """조용히 아무 일도 안 일어나는 것이 우리가 가장 자주 겪은 고장이다."""
    from tybot import reviewers

    monkeypatch.setattr(reviewers, "reviewers_for", lambda ws, ch: [])
    archive = _stage(tmp_path, name="스캔본.png", file_id="F1")

    with caplog.at_level("WARNING"):
        result = _run(tmp_path, archive, FakeConn(), FakeClient())

    assert result.no_recipient == 1
    assert "검토자도 개설자도 없어" in caplog.text


def test_a_failed_send_is_not_recorded_as_sent(tmp_path, monkeypatch):
    """보내기 전에 이력을 남기면 발송 실패가 「보냈음」 이 되어 영영 다시 안 간다."""
    from tybot import reviewers

    monkeypatch.setattr(reviewers, "reviewers_for", lambda ws, ch: [
        type("R", (), {"reviewer_user": "U1"})()
    ])
    archive = _stage(tmp_path, name="스캔본.png", file_id="F1")

    class Broken(FakeClient):
        def chat_postMessage(self, **kwargs):
            raise RuntimeError("slack down")

    conn = FakeConn()
    failed = _run(tmp_path, archive, conn, Broken())

    assert failed.failed == 1
    assert conn.rows == set(), "실패했는데 보낸 것으로 남았다"

    # 다음 회차에 다시 간다.
    again = _run(tmp_path, archive, conn, FakeClient())
    assert again.sent == 1


def test_it_waits_until_the_configured_hour(tmp_path, monkeypatch):
    """08:00 로 정했는데 07:00 에 오면 설정이 안 된 것으로 보인다."""
    from tybot import reviewers

    monkeypatch.setattr(reviewers, "reviewers_for", lambda ws, ch: [
        type("R", (), {"reviewer_user": "U1"})()
    ])
    archive = _stage(tmp_path, name="스캔본.png", file_id="F1")
    client = FakeClient()

    _run(tmp_path, archive, FakeConn(), client,
         now=datetime(2026, 9, 8, 7, 0, tzinfo=KST))

    assert client.sent == []


def test_the_dm_never_carries_file_content():
    """검토 DM 은 이름과 상태만 보인다. 본문을 실으면 그것이 Slack 에 남는다."""
    import inspect

    source = inspect.getsource(dr)

    for leaked in ("read_bytes", "read_text", "object_path"):
        assert leaked not in source, f"하루치가 파일 내용을 만진다: {leaked}"


def test_a_missing_schema_says_what_to_run():
    """스키마를 안 적용하고 타이머를 켜면 회차마다 채널 수만큼 트레이스백이 쌓인다.

    그 로그 어디에도 무엇을 해야 하는지는 안 적힌다.
    """
    assert "review_digest_schema.sql" in dr.SCHEMA_MISSING
    assert "-p 55432" in dr.SCHEMA_MISSING, "포트를 빠뜨리면 psql 이 소켓을 찾는다"


def test_schema_check_reads_the_catalog():
    class Conn:
        def __init__(self, value):
            self.value = value

        class _Cur:
            def __init__(self, outer):
                self.outer = outer

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, sql):
                assert "review_digest_sent" in sql

            def fetchone(self):
                return {"t": self.outer.value}

        def cursor(self):
            return self._Cur(self)

    assert dr.schema_ready(Conn("review_digest_sent"))
    assert not dr.schema_ready(Conn(None))
