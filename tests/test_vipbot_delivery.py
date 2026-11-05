"""VIPBot 전달 정책 시험 (B-83). **대역만 쓴다 — 실제 Slack 호출 없음.**

여기 쓰는 글은 전부 합성이다. 실제 질문·답변·사람 이름을 넣지 않는다.

붙들고 있는 것은 넷이다.

1. 상세 답변은 **DM 으로만** 간다. 채널에 남는 글은 고정 안내 한 줄뿐
2. DM 이 실패해도 **채널로 흘리지 않는다**(폴백 없음)
3. 거절은 채널에 안 적는다 — 누가 거절당했는지가 방에 남지 않는다
4. 결과·로그에 질문·답변·근거가 없다
"""
from __future__ import annotations

import logging
from dataclasses import fields

import pytest

from tybot import vipbot_delivery
from tybot.bot_access import CHECK_FAILED, DENY_MESSAGE, NO_GRANT, Decision
from tybot.vipbot_delivery import (
    CHANNEL_NOTICE,
    CHANNEL_NOTICES,
    DeliveryResult,
    deliver_channel_answer,
)

WS = "pilot"
CHANNEL = "C00000000"
USER = "U0VIP0000"
DM = "D0VIP0000"
#: 합성 답변. 채널 쪽 호출 어디에도 나오면 안 된다.
ANSWER = "합성 상세 답변 ZZZANSWERZZZ 입니다."


class Slack:
    """Slack SDK 대역. 부른 메서드와 인자를 그대로 적는다."""

    def __init__(
        self,
        *,
        opened=...,
        open_raises=None,
        dm_post_raises=None,
        notice_raises=None,
    ):
        self.calls: list[tuple[str, dict]] = []
        self.opened = {"channel": {"id": DM}} if opened is ... else opened
        self.open_raises = open_raises
        self.dm_post_raises = dm_post_raises
        self.notice_raises = notice_raises

    def conversations_open(self, *, users):
        self.calls.append(("conversations_open", {"users": users}))
        if self.open_raises is not None:
            raise self.open_raises
        return self.opened

    def chat_postMessage(self, *, channel, text, thread_ts=None):
        self.calls.append(
            ("chat_postMessage", {"channel": channel, "text": text, "thread_ts": thread_ts})
        )
        if channel == DM and self.dm_post_raises is not None:
            raise self.dm_post_raises
        if channel != DM and self.notice_raises is not None:
            raise self.notice_raises
        return {"ok": True}

    # -- 읽기 쉽게 --------------------------------------------------------

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    def posts_to(self, channel: str) -> list[dict]:
        return [
            payload
            for name, payload in self.calls
            if name == "chat_postMessage" and payload["channel"] == channel
        ]


def allow():
    return Decision(True, "allowed", emp_no="12345")


def deny(code=NO_GRANT):
    return Decision(False, code)


def deliver(client, *, decision=None, raises=None, answer=ANSWER, thread_ts=None):
    def check():
        if raises is not None:
            raise raises
        return decision if decision is not None else allow()

    return deliver_channel_answer(
        client,
        workspace=WS,
        channel=CHANNEL,
        slack_user=USER,
        answer=answer,
        check_access=check,
        thread_ts=thread_ts,
    )


# --- 1. 상세는 DM, 채널은 고정 안내 -------------------------------------------


def test_the_detail_goes_to_the_caller_dm_and_the_room_gets_a_fixed_line():
    client = Slack()
    got = deliver(client)
    assert got.outcome == "delivered"
    assert got.dm_sent and got.notice_posted
    assert [payload["text"] for payload in client.posts_to(DM)] == [ANSWER]
    assert [payload["text"] for payload in client.posts_to(CHANNEL)] == [CHANNEL_NOTICE]


def test_nothing_the_room_sees_contains_any_of_the_answer():
    client = Slack()
    deliver(client)
    for payload in client.posts_to(CHANNEL):
        assert payload["text"] in CHANNEL_NOTICES
        assert ANSWER not in payload["text"]
        assert "ZZZANSWER" not in payload["text"]


def test_the_notice_stays_in_the_thread_it_was_called_from():
    client = Slack()
    deliver(client, thread_ts="1767000000.000100")
    assert client.posts_to(CHANNEL)[0]["thread_ts"] == "1767000000.000100"


def test_only_two_slack_methods_are_ever_called():
    client = Slack()
    deliver(client)
    assert set(client.names) <= {"conversations_open", "chat_postMessage"}


# --- 2. DM 이 실패해도 채널로 흘리지 않는다 ------------------------------------


def test_a_failed_dm_is_the_end_of_it_not_a_channel_fallback():
    client = Slack(dm_post_raises=RuntimeError(f"slack said no about {ANSWER!r}"))
    got = deliver(client)
    assert (got.outcome, got.reason) == ("dm_delivery_failed", "dm_post_failed")
    assert not got.dm_sent and not got.notice_posted
    assert client.posts_to(CHANNEL) == []


def test_a_dm_that_could_not_even_be_opened_posts_nothing_at_all():
    client = Slack(open_raises=RuntimeError("boom"))
    got = deliver(client)
    assert (got.outcome, got.reason) == ("dm_delivery_failed", "dm_open_failed")
    assert "chat_postMessage" not in client.names


@pytest.mark.parametrize(
    "opened",
    [
        None,
        {},
        {"channel": {}},
        {"channel": {"id": "C99999999"}},
        {"channel": {"id": ""}},
    ],
)
def test_an_answer_is_never_sent_to_something_that_is_not_a_dm(opened):
    # 응답 하나가 어긋났을 뿐인데 상세 답변이 공개 채널로 나가는 길이다.
    client = Slack(opened=opened)
    got = deliver(client)
    assert (got.outcome, got.reason) == ("dm_delivery_failed", "dm_channel_invalid")
    assert client.posts_to(CHANNEL) == []
    assert "chat_postMessage" not in client.names


def test_a_failed_channel_notice_does_not_undo_the_dm():
    client = Slack(notice_raises=RuntimeError("boom"))
    got = deliver(client)
    assert (got.outcome, got.reason) == ("channel_notice_failed", "notice_post_failed")
    assert got.dm_sent and not got.notice_posted
    assert [payload["text"] for payload in client.posts_to(DM)] == [ANSWER]


# --- 3. 거절은 채널에 안 적는다 ------------------------------------------------


@pytest.mark.parametrize(
    "code",
    ["no_identity", "unknown_employee", "inactive_employee", "no_grant", "expired"],
)
def test_a_refusal_never_appears_in_the_room(code):
    client = Slack()
    got = deliver(client, decision=deny(code))
    assert (got.outcome, got.reason) == ("permission_denied", code)
    assert client.posts_to(CHANNEL) == []
    assert [payload["text"] for payload in client.posts_to(DM)] == [DENY_MESSAGE]


def test_no_registered_source_is_its_own_reason_not_a_missing_grant():
    """조치가 다르다 — 한쪽은 **사람에게 권한**, 다른 한쪽은 **근거 채널 등록**.

    뭉뚱그리면 전산팀은 권한을 다시 주고, 봇은 여전히 답하지 않는다.
    """
    from tybot import vip_access

    client = Slack()
    refusal = vip_access.Authorization(
        False, vip_access.NO_SOURCES, WS, USER, emp_no="E1"
    )
    got = deliver(client, decision=refusal)
    assert (got.outcome, got.reason) == ("permission_denied", "no_sources")
    assert got.reason != NO_GRANT
    assert client.posts_to(CHANNEL) == []
    assert [payload["text"] for payload in client.posts_to(DM)] == [DENY_MESSAGE]


def test_the_delivery_reason_matches_the_word_the_authorizer_uses():
    # 낱말을 베껴 적었으므로, 한쪽만 바뀌면 거절이 조용히 `no_grant` 로 되돌아간다.
    from tybot import vip_access

    assert vipbot_delivery.NO_SOURCES == vip_access.NO_SOURCES
    assert vip_access.NO_SOURCES in vipbot_delivery.DELIVERY_REASONS


def test_the_person_is_not_told_why_they_were_refused():
    client = Slack()
    deliver(client, decision=deny("inactive_employee"))
    text = client.posts_to(DM)[0]["text"]
    assert text == DENY_MESSAGE
    for code in ("inactive", "퇴직", "권한 줄", "grant"):
        assert code not in text


def test_a_refused_caller_gets_no_answer_anywhere():
    client = Slack()
    deliver(client, decision=deny())
    assert all(ANSWER not in payload["text"] for payload in client.posts_to(DM))


@pytest.mark.parametrize(
    ("decision", "raises"),
    [(Decision(False, CHECK_FAILED), None), (None, RuntimeError("db is down"))],
)
def test_a_failed_check_is_told_apart_from_a_refusal_and_sends_nothing(
    decision, raises
):
    client = Slack()
    got = deliver(client, decision=decision, raises=raises)
    assert (got.outcome, got.reason) == ("access_check_failed", CHECK_FAILED)
    assert client.calls == []


# --- 4. 본문은 결과·로그 어디에도 없다 -----------------------------------------


def test_no_result_field_can_hold_a_question_an_answer_or_evidence():
    names = {f.name for f in fields(DeliveryResult)}
    assert names == {"outcome", "reason", "dm_sent", "notice_posted"}
    client = Slack()
    got = deliver(client)
    assert ANSWER not in repr(got)


@pytest.mark.parametrize(
    "client_kwargs",
    [
        {},
        {"dm_post_raises": RuntimeError(f"echoed back: {ANSWER}")},
        {"notice_raises": RuntimeError(f"echoed back: {ANSWER}")},
        {"open_raises": RuntimeError(f"echoed back: {ANSWER}")},
    ],
)
def test_the_log_never_carries_the_answer_or_a_slack_error_body(caplog, client_kwargs):
    with caplog.at_level(logging.DEBUG):
        deliver(Slack(**client_kwargs))
    assert ANSWER not in caplog.text
    assert "echoed back" not in caplog.text


def test_a_refusal_reason_reaches_the_log_but_not_the_person(caplog):
    with caplog.at_level(logging.DEBUG):
        deliver(Slack(), decision=deny("expired"))
    assert "expired" in caplog.text


# --- 5. 결과 객체가 거짓말을 못 한다 -------------------------------------------


def test_a_notice_cannot_claim_a_dm_that_was_never_sent():
    with pytest.raises(ValueError, match="may not claim a DM"):
        DeliveryResult(outcome="delivered", notice_posted=True, dm_sent=False)


@pytest.mark.parametrize(
    ("outcome", "reason", "match"),
    [
        ("그럭저럭", "", "unknown delivery outcome"),
        ("delivered", "slack said: channel_not_found", "unknown delivery reason"),
    ],
)
def test_only_fixed_words_can_be_recorded(outcome, reason, match):
    with pytest.raises(ValueError, match=match):
        DeliveryResult(outcome=outcome, reason=reason)


@pytest.mark.parametrize(
    "overrides",
    [{"answer": ""}, {"answer": "   "}],
)
def test_an_empty_answer_is_a_wiring_bug_not_an_empty_dm(overrides):
    with pytest.raises(ValueError):
        deliver(Slack(), **overrides)


@pytest.mark.parametrize(("channel", "user"), [("", USER), (CHANNEL, ""), (" ", USER)])
def test_a_call_without_a_room_or_a_caller_is_refused_before_slack(channel, user):
    def check():
        raise AssertionError("권한 판정까지 가면 안 된다")

    with pytest.raises(ValueError):
        deliver_channel_answer(
            Slack(),
            workspace=WS,
            channel=channel,
            slack_user=user,
            answer=ANSWER,
            check_access=check,
        )
