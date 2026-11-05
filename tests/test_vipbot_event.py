from __future__ import annotations

from dataclasses import dataclass

from tybot import bot_access, vip_access, vipbot_event

WS = "tyit"
USER = "U0VIP0000"
BOT = "U0BOT0000"
CHANNEL = "C0ROOM000"
DM = "D0DIRECT0"


class Slack:
    def __init__(self):
        self.calls = []

    def conversations_open(self, *, users):
        self.calls.append(("conversations_open", users))
        return {"channel": {"id": DM}}

    def chat_postMessage(self, *, channel, text, thread_ts=None):
        self.calls.append(("chat_postMessage", channel, text, thread_ts))


@dataclass
class Generated:
    text: str
    citations: tuple[str, ...] = ("#synthetic, 📄source.md(2026-01-01)",)


def authorization(*, allowed=True, code=vip_access.ALLOWED):
    return vip_access.Authorization(
        allowed,
        code,
        WS,
        USER,
        emp_no="E1",
        grant_id=1,
        sources=frozenset({("mgmt", "C0SOURCE0")}) if allowed else frozenset(),
    )


def event(*, channel=CHANNEL, text=None, **extra):
    direct = channel.startswith("D")
    return {
        "type": "message" if direct else "app_mention",
        "channel_type": "im" if direct else "channel",
        "user": USER,
        "channel": channel,
        "text": text if text is not None else ("status" if direct else f"<@{BOT}> status"),
        **extra,
    }


def handle(payload, client, **overrides):
    values = {
        "workspace": WS,
        "bot_user_id": BOT,
        "authorize": lambda **_kwargs: authorization(),
        "generate": lambda question, _ctx: Generated(question.upper()),
        "render": lambda answer: answer.text,
        "event_id": "Ev0000001",
        "claim_event": lambda _event_id: True,
    }
    values.update(overrides)
    return vipbot_event.handle_event(payload, client, **values)


def test_authorization_precedes_generation_and_supplies_only_managed_sources():
    order = []
    client = Slack()

    def authorize(**coordinates):
        order.append(("authorize", coordinates))
        return authorization()

    def generate(question, ctx):
        order.append(("generate", question, ctx.evidence_sources))
        return Generated("executive answer")

    got = handle(
        event(ts="1.2"),
        client,
        authorize=authorize,
        generate=generate,
    )
    assert order == [
        ("authorize", {"workspace": WS, "slack_user": USER}),
        ("generate", "status", frozenset({("mgmt", "C0SOURCE0")})),
        ("authorize", {"workspace": WS, "slack_user": USER}),
    ]
    assert got.delivery is not None and got.delivery.outcome == "delivered"
    assert client.calls[0][0] == "conversations_open"
    assert client.calls[1][1:3] == (DM, "executive answer")
    assert client.calls[2][1] == CHANNEL


def test_denial_never_calls_generation_and_never_posts_in_the_room():
    client = Slack()

    def forbidden(*_args):
        raise AssertionError("generation ran before authorization")

    got = handle(
        event(),
        client,
        authorize=lambda **_kwargs: authorization(allowed=False, code=bot_access.NO_GRANT),
        generate=forbidden,
    )
    assert got.delivery is not None
    assert got.delivery.outcome == "permission_denied"
    assert all(call[1] != CHANNEL for call in client.calls if call[0] == "chat_postMessage")


def test_dm_request_replies_to_the_existing_dm_without_a_channel_notice():
    client = Slack()
    got = handle(event(channel=DM), client)
    assert got.delivery is not None and got.delivery.notice_posted is False
    assert client.calls == [("chat_postMessage", DM, "STATUS", None)]


def test_bot_events_subtypes_bad_coordinates_and_empty_questions_are_ignored():
    events = [
        event(channel=DM, text="x", bot_id="B1"),
        event(channel=DM, text="x", subtype="message_changed"),
        {**event(channel=DM, text="x"), "user": "bad"},
        event(text="not a mention"),
        event(text=f"<@{BOT}>   "),
        {**event(channel=DM), "type": "app_mention"},
        {**event(channel=DM), "channel_type": "channel"},
        {**event(), "text": {"not": "a string"}},
    ]
    for payload in events:
        got = handle(
            payload,
            Slack(),
            authorize=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
            generate=lambda *_args: (_ for _ in ()).throw(AssertionError()),
        )
        assert got == vipbot_event.EventResult("ignored", "invalid_event")


def test_rendering_happens_only_after_generation_and_empty_output_is_rejected():
    calls = []

    def generate(question, _ctx):
        calls.append(("generate", question))
        return Generated("")

    def render(value):
        calls.append(("render", value.text))
        return value.text

    got = handle(
        event(channel=DM),
        Slack(),
        generate=generate,
        render=render,
    )
    assert got == vipbot_event.EventResult("failed", "generation_failed")
    assert calls == [("generate", "status"), ("render", "")]


def test_missing_citations_are_not_delivered():
    client = Slack()
    got = handle(
        event(channel=DM),
        client,
        generate=lambda _question, _ctx: Generated("answer", ()),
    )
    assert got == vipbot_event.EventResult("failed", "missing_sources")
    assert client.calls == []


def test_authorization_is_rechecked_after_generation_before_delivery():
    decisions = [authorization(), authorization(allowed=False, code=bot_access.NO_GRANT)]
    client = Slack()
    got = handle(
        event(channel=DM),
        client,
        authorize=lambda **_kwargs: decisions.pop(0),
    )
    assert got.delivery is not None
    assert (got.delivery.outcome, got.delivery.reason) == (
        "access_check_failed",
        bot_access.CHECK_FAILED,
    )
    assert client.calls == []


def test_a_changed_source_scope_invalidates_the_generated_answer():
    first = authorization()
    second = vip_access.Authorization(
        True,
        vip_access.ALLOWED,
        WS,
        USER,
        emp_no="E1",
        grant_id=1,
        sources=frozenset({("mgmt", "C0OTHER00")}),
    )
    decisions = [first, second]
    client = Slack()
    got = handle(
        event(channel=DM),
        client,
        authorize=lambda **_kwargs: decisions.pop(0),
    )
    assert got.delivery is not None
    assert got.delivery.outcome == "access_check_failed"
    assert client.calls == []


def test_mismatched_authorization_coordinates_fail_closed_before_generation():
    wrong = vip_access.Authorization(
        True,
        vip_access.ALLOWED,
        "other",
        "U0OTHER00",
        emp_no="E1",
        grant_id=1,
        sources=frozenset({("mgmt", "C0SOURCE0")}),
    )
    got = handle(
        event(channel=DM),
        Slack(),
        authorize=lambda **_kwargs: wrong,
        generate=lambda *_args: (_ for _ in ()).throw(AssertionError()),
    )
    assert got.delivery is not None
    assert got.delivery.outcome == "access_check_failed"


def test_duplicate_event_is_ignored_before_authorization():
    got = handle(
        event(),
        Slack(),
        claim_event=lambda _event_id: False,
        authorize=lambda **_kwargs: (_ for _ in ()).throw(AssertionError()),
    )
    assert got == vipbot_event.EventResult("ignored", "duplicate_event")


def test_generation_exception_body_is_not_returned_or_logged(caplog):
    secret = "synthetic question or model body"

    def broken(*_args):
        raise RuntimeError(secret)

    got = handle(event(channel=DM), Slack(), generate=broken)
    assert got == vipbot_event.EventResult("failed", "generation_failed")
    assert secret not in repr(got)
    assert secret not in caplog.text
