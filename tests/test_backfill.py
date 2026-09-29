"""소급 수집 — **복구 수단이고, 되돌릴 수 없는 종류의 일이다.**

결정: 2026-09-29 supervisor 작업지시서 §4.

여기서 막는 것.

1. **한 페이지만 읽고 끝내는 것.** 오래된 채널의 앞부분만 메워지고, 그건 「했다」 로
   보이는 실패다
2. **thread 를 빼먹는 것.** 부모만 읽으면 스레드 안의 대화가 통째로 빠지는데, 그
   대화가 대개 결론이다
3. **권한 없는 채널을 0건으로 세는 것.** 「없었다」 로 읽히고 사람은 그 채널을 다시
   안 본다
4. **쓰기 전에 cursor 를 옮기는 것.** 안 쓴 구간이 지난 것으로 남는다
5. **dry-run 이 본문을 뱉는 것.** 원문이 아카이브 밖에 생긴다

Slack 도 디스크도 없다. 가짜 client 와 가짜 writer 로 규칙만 본다.
"""

from __future__ import annotations

import pytest

from tybot.archive import backfill
from tybot.archive.backfill import Target
from tybot.archive.supervisor_state import JobState


class FakeSlack:
    """`conversations.history`·`replies` 만. 페이지와 429 를 흉내 낸다."""

    def __init__(self, history: list[dict], replies: dict | None = None) -> None:
        self.history = history
        self.replies = replies or {}
        self.history_calls: list[dict] = []
        self._page = 0
        self.reply_calls: list[dict] = []
        self.rate_limited = 0

    def conversations_history(self, **kwargs):
        self.history_calls.append(kwargs)
        if self.rate_limited > 0:
            self.rate_limited -= 1
            raise _RateLimited()
        # 429 로 실패한 호출은 페이지를 넘기지 않는다. 진짜 Slack 도 그렇다 —
        # 넘겨 버리면 재시도가 다음 페이지를 읽고 그 사이가 빈다.
        page = self.history[self._page]
        self._page += 1
        return page

    def conversations_replies(self, **kwargs):
        self.reply_calls.append(kwargs)
        return self.replies.get(str(kwargs.get("ts")), {"messages": []})


class _RateLimited(Exception):
    def __init__(self) -> None:
        super().__init__("ratelimited")
        self.response = type("R", (), {
            "data": {"error": "ratelimited"}, "headers": {"Retry-After": "0.01"},
        })()


class _Denied(Exception):
    def __init__(self, code: str = "channel_not_found") -> None:
        super().__init__(code)
        self.response = type("R", (), {"data": {"error": code}, "headers": {}})()


def _msg(ts: str, text: str = "사람이 쓴 줄", **over) -> dict:
    return {"ts": ts, "user": "U1", "text": text, **over}


def _page(messages, cursor=""):
    return {"messages": messages, "response_metadata": {"next_cursor": cursor}}


def _target(cursor: str = "") -> Target:
    return Target("tyit", "C1", "#팀_자금(ABB540)_주간보고", cursor)


def _sleep(_seconds: float) -> None:
    return None


# --- 무엇을 모으나 --------------------------------------------------------------

def test_every_page_is_read():
    """한 페이지만 읽으면 오래된 채널의 앞부분만 메워진다."""
    slack = FakeSlack([
        _page([_msg("100.000100")], cursor="next"),
        _page([_msg("200.000200")]),
    ])

    found, exhausted = backfill.collect(slack, _target(), sleeper=_sleep)

    assert [item.ts for item in found] == ["100.000100", "200.000200"]
    assert exhausted is True
    assert slack.history_calls[1]["cursor"] == "next"


def test_thread_replies_are_pulled_in():
    """부모만 읽으면 스레드 안의 대화가 통째로 빠진다 — 그 대화가 대개 결론이다."""
    slack = FakeSlack(
        [_page([_msg("100.000100", thread_ts="100.000100")])],
        replies={"100.000100": {"messages": [
            _msg("100.000100", thread_ts="100.000100"),
            _msg("100.000200", "스레드 답글", thread_ts="100.000100"),
        ]}},
    )

    found, _ = backfill.collect(slack, _target(), sleeper=_sleep)

    assert [item.ts for item in found] == ["100.000100", "100.000200"]
    assert found[1].thread_ts == "100.000100"


def test_a_reply_is_not_counted_twice():
    """`replies` 는 부모도 함께 준다. 두 번 세면 건수가 부풀고 dedupe 를 믿게 된다."""
    slack = FakeSlack(
        [_page([_msg("100.000100", thread_ts="100.000100")])],
        replies={"100.000100": {"messages": [_msg("100.000100", thread_ts="100.000100")]}},
    )

    found, _ = backfill.collect(slack, _target(), sleeper=_sleep)

    assert len(found) == 1


@pytest.mark.parametrize("message", [
    {"ts": "1.1", "bot_id": "B1", "text": "봇"},
    {"ts": "1.1", "app_id": "A1", "user": "U1", "text": "앱"},
    {"ts": "1.1", "user": "U1", "subtype": "channel_join"},
    {"ts": "1.1", "text": "사람 없음"},
])
def test_non_human_messages_are_skipped(message):
    """실시간 경로와 **같은 규칙**이다. 다르면 같은 대화가 경로에 따라 다르게 남는다."""
    slack = FakeSlack([_page([message])])

    found, _ = backfill.collect(slack, _target(), sleeper=_sleep)

    assert found == []


def test_a_file_share_is_a_human_message():
    """첨부가 붙은 메시지는 사람 메시지다. 빼면 그 파일이 영영 안 들어온다."""
    slack = FakeSlack([_page([_msg("100.000100", subtype="file_share", files=[{"id": "F1"}])])])

    found, _ = backfill.collect(slack, _target(), sleeper=_sleep)

    assert len(found) == 1


def test_a_rate_limit_waits_and_retries():
    """429 에 멈추면 그 채널만 영영 안 메워진다."""
    slack = FakeSlack([_page([_msg("100.000100")])])
    slack.rate_limited = 1
    waited: list[float] = []

    found, _ = backfill.collect(slack, _target(), sleeper=waited.append)

    assert [item.ts for item in found] == ["100.000100"]
    assert waited == [0.01]


def test_a_denied_channel_raises_with_only_a_code():
    """오류 문구에 본문·토큰이 섞여 나가지 않게 한다."""
    class Denying(FakeSlack):
        def conversations_history(self, **kwargs):
            raise _Denied("channel_not_found")

    with pytest.raises(backfill.SlackDenied) as caught:
        backfill.collect(Denying([]), _target(), sleeper=_sleep)

    assert caught.value.code == "channel_not_found"
    assert caught.value.channel_id == "C1"


# --- dry-run --------------------------------------------------------------------

def test_the_preview_counts_without_bodies():
    """원문이 콘솔·로그로 흐르면 그때부터 원문이 아카이브 밖에도 있게 된다."""
    slack = FakeSlack([_page([
        _msg("100.000100", "금액은 1,200만원"),
        _msg("100.000200", "다른 줄", subtype="file_share", files=[{"id": "F1"}]),
    ])])

    preview = backfill.plan(slack, [_target()], workspace="tyit", sleeper=_sleep)

    body = str(preview.as_json())
    assert "1,200만원" not in body and "다른 줄" not in body
    assert preview.found == 2
    assert preview.with_attachment == 1


def test_the_preview_reports_denied_channels_separately():
    """권한 없는 채널을 0건으로 세면 「없었다」 로 읽힌다."""
    class Denying(FakeSlack):
        def conversations_history(self, **kwargs):
            raise _Denied("not_in_channel")

    preview = backfill.plan(Denying([]), [_target()], workspace="tyit", sleeper=_sleep)

    assert preview.found == 0
    assert preview.denied == ["C1: not_in_channel"]
    assert preview.exhausted is False


def test_the_preview_never_claims_full_recovery():
    """「완전 복구」 라고 부르면 사람이 그 기간을 다시 안 본다(§4.1)."""
    slack = FakeSlack([_page([])])

    note = backfill.plan(slack, [_target()], workspace="tyit", sleeper=_sleep).as_json()

    assert "완전" not in note["note"]
    assert "수정 전 본문" in note["note"]


# --- 실제 소급 ------------------------------------------------------------------

def _run(slack, ingest, *, cursor: str = "", targets=None):
    saved: list[tuple[str, str]] = []
    counts, state = backfill.run(
        slack, targets or [_target(cursor)], workspace="tyit",
        ingest=ingest, save_cursor=lambda t, ts: saved.append((t.channel_id, ts)),
        sleeper=_sleep,
    )
    return counts, state, saved


def test_a_clean_run_advances_the_cursor_once():
    """메시지마다 옮기면 중간에 끊겼을 때 안 쓴 구간이 지난 것으로 남는다."""
    slack = FakeSlack([_page([_msg("100.000100"), _msg("200.000200")])])

    counts, state, saved = _run(slack, lambda *_: "written")

    assert counts.written == 2
    assert state is JobState.SUCCEEDED
    assert saved == [("C1", "200.000200")]


def test_a_failure_stops_the_channel_and_holds_the_cursor():
    """**이 시험이 cursor 규칙의 이유다.** 실패 뒤에 옮기면 그 구간은 영영 안 메워진다."""
    slack = FakeSlack([_page([_msg("100.000100"), _msg("200.000200")])])
    calls = {"n": 0}

    def ingest(_target, _item):
        calls["n"] += 1
        return "written" if calls["n"] == 1 else "failed"

    counts, state, saved = _run(slack, ingest)

    assert counts.written == 1 and counts.failed == 1
    assert state is JobState.PARTIAL
    assert saved == [], "cursor 는 제자리여야 한다"


def test_an_exception_in_the_writer_is_counted_not_raised():
    """한 건 실패가 채널 전체를 멈추면 나머지 채널까지 못 돈다."""
    slack = FakeSlack([_page([_msg("100.000100")])])

    def boom(_target, _item):
        raise RuntimeError("disk full")

    counts, state, saved = _run(slack, boom)

    assert counts.failed == 1 and state is JobState.FAILED
    assert saved == []


def test_re_running_the_same_range_writes_nothing_new():
    """재실행은 겹치는 구간을 다시 읽고 dedupe 한다. 두 번 쓰면 근거가 늘어난다."""
    slack = FakeSlack([_page([_msg("100.000100"), _msg("200.000200")])])

    counts, state, saved = _run(slack, lambda *_: "duplicate", cursor="200.000200")

    assert counts.written == 0 and counts.duplicate == 2
    assert state is JobState.SUCCEEDED
    assert saved == [], "이미 지난 좌표라 전진할 것이 없다"


def test_refused_messages_are_not_failures():
    """PII 로 거절된 것은 고장이 아니다. 실패로 세면 사람이 원인을 엉뚱한 데서 찾는다."""
    slack = FakeSlack([_page([_msg("100.000100")])])

    counts, state, _ = _run(slack, lambda *_: "refused")

    assert counts.refused == 1 and counts.failed == 0
    assert state is JobState.SUCCEEDED


def test_a_denied_channel_does_not_stop_the_others():
    """한 채널 권한이 없다고 나머지를 안 돌면, 고칠 때까지 전부 멈춘다."""
    class PartlyDenying(FakeSlack):
        def conversations_history(self, **kwargs):
            if kwargs.get("channel") == "C-DENIED":
                raise _Denied("not_in_channel")
            return super().conversations_history(**kwargs)

    slack = PartlyDenying([_page([_msg("100.000100")])])
    targets = [Target("tyit", "C-DENIED"), _target()]

    counts, state, saved = _run(slack, lambda *_: "written", targets=targets)

    assert counts.written == 1
    assert counts.failed == 1
    assert state is JobState.PARTIAL
    assert saved == [("C1", "100.000100")]


def test_nothing_found_is_a_success():
    """아무것도 없었던 것과 못 읽은 것은 사람이 할 일이 다르다."""
    slack = FakeSlack([_page([])])

    counts, state, saved = _run(slack, lambda *_: "written")

    assert counts.found == 0 and state is JobState.SUCCEEDED
    assert saved == []


def test_the_cursor_never_moves_backwards():
    slack = FakeSlack([_page([_msg("100.000100")])])

    _, _, saved = _run(slack, lambda *_: "written", cursor="900.000000")

    assert saved == []
