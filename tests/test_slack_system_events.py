"""Slack 이 만든 메시지를 질문으로 처리하지 않는다(2026-09-16 장애 §6).

사고: Canvas 접근 요청 시스템 메시지가 일반 DM 질문으로 처리돼
`restricted_action_read_only_channel` 로 전달이 실패했고, 그 본문
(`requested access to <Canvas URL>`)이 QA 기록에 질문으로 남았다.

**막는 쪽이 기본값이다.** 시스템 메시지를 사람 질문으로 올리면 되돌릴 수 없고,
사람 메시지를 한 번 놓치면 사람이 다시 묻는다.
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from tybot.slack.pilot import WorkspaceBot


@pytest.fixture
def bot() -> WorkspaceBot:
    made = WorkspaceBot.__new__(WorkspaceBot)
    made.workspace = "tyit"
    made._bot_users = {}
    return made


def _client(**user) -> Mock:
    made = Mock()
    made.users_info.return_value = {"user": {"id": "U1", **user}}
    return made


def _human(**kw) -> dict:
    event = {"user": "U1", "text": "기성금 얼마야?", "channel_type": "im"}
    event.update(kw)
    return event


# --- 막아야 하는 것 ------------------------------------------------------------
def test_a_canvas_access_request_is_not_a_question(bot):
    """§7.4 · 사고를 낸 그 이벤트."""
    event = _human(
        subtype="canvas_access_requested",
        text="requested access to https://tyit.slack.com/docs/T1/F1",
    )

    assert bot._is_human_request(event, _client()) is False


def test_a_bot_message_never_reaches_the_answer_path(bot):
    assert bot._is_human_request(_human(bot_id="B1"), _client()) is False
    assert bot._is_human_request(_human(app_id="A1"), _client()) is False


def test_slackbot_itself_is_not_a_person(bot):
    assert bot._is_human_request(_human(user="USLACKBOT"), _client()) is False


def test_a_message_without_a_sender_is_not_a_person(bot):
    assert bot._is_human_request(_human(user=""), _client()) is False
    assert bot._is_human_request({"channel_type": "im"}, _client()) is False


def test_a_hidden_event_is_not_a_question(bot):
    assert bot._is_human_request(_human(hidden=True), _client()) is False


def test_an_app_user_is_not_a_person(bot):
    """`users.info` 가 봇이라고 하면 막는다 — subtype 이 없어도."""
    assert bot._is_human_request(_human(), _client(is_bot=True)) is False


@pytest.mark.parametrize(
    "subtype",
    ["channel_join", "channel_leave", "message_changed", "tombstone", "bot_message"],
)
def test_known_system_subtypes_stay_blocked(bot, subtype):
    assert bot._is_human_request(_human(subtype=subtype), _client()) is False


# --- 통과해야 하는 것 ----------------------------------------------------------
def test_a_normal_human_dm_still_works(bot):
    """§7.4 · 정상 사람 DM 은 기존처럼 처리된다."""
    assert bot._is_human_request(_human(), _client()) is True


def test_a_human_file_share_keeps_working(bot):
    """첨부만 올린 DM 은 `file_share` 로 온다 — 사람이 올린 것이다."""
    event = _human(subtype="file_share", files=[{"id": "F1"}])

    assert bot._is_human_request(event, _client()) is True


def test_a_channel_message_is_still_ingested(bot):
    assert bot._is_human_request(_human(channel_type="channel"), _client()) is True


# --- 조회 실패 -----------------------------------------------------------------
def test_a_failed_user_lookup_fails_closed(bot):
    """조회 실패를 시스템 메시지의 사람 승격 근거로 쓰지 않는다."""
    client = Mock()
    client.users_info.side_effect = RuntimeError("rate limited")

    assert bot._is_human_request(_human(), client) is False


def test_a_failed_lookup_is_not_cached(bot):
    """실패를 캐시하면 일시 장애 뒤에도 영영 다시 묻지 않는다."""
    client = Mock()
    client.users_info.side_effect = RuntimeError("rate limited")

    bot._is_human_request(_human(), client)

    assert bot._bot_users == {}


def test_a_known_bot_user_is_only_looked_up_once(bot):
    client = _client(is_bot=True)

    bot._is_human_request(_human(), client)
    bot._is_human_request(_human(), client)

    assert client.users_info.call_count == 1


# --- 배선 ----------------------------------------------------------------------
#
# 판정 함수만 테스트하면 **배선을 통째로 빼도** 아무 테스트가 안 걸린다.
# 되돌리기 실험에서 실제로 그랬다 — 그래서 여기서 경로를 지난다.
def _routing_bot(**kw) -> WorkspaceBot:
    made = WorkspaceBot.__new__(WorkspaceBot)
    made.workspace = "tyit"
    made._bot_users = {}
    made.realtime = True
    made.handled = []
    made.ingested = []
    made._handle_correction = lambda event, say: False
    made._handle = lambda event, client, say, **k: made.handled.append(event)
    made._ingest_live = lambda client, event: made.ingested.append(event)
    for name, value in kw.items():
        setattr(made, name, value)
    return made


def test_a_system_event_never_reaches_the_answer_path():
    """§7.4 · `_handle()` 과 `chat.postMessage` 가 호출되지 않는다."""
    bot = _routing_bot()
    event = _human(
        subtype="canvas_access_requested",
        text="requested access to https://tyit.slack.com/docs/T1/F1",
    )
    say = Mock()

    assert bot.route_message(event, _client(), say) == "ignored"
    assert bot.handled == []
    assert bot.ingested == []
    say.assert_not_called()


def test_a_human_dm_still_reaches_the_answer_path():
    bot = _routing_bot()

    assert bot.route_message(_human(), _client(), Mock()) == "answered"
    assert len(bot.handled) == 1


def test_a_human_file_share_dm_still_reaches_the_answer_path():
    bot = _routing_bot()
    event = _human(subtype="file_share", files=[{"id": "F1"}])

    assert bot.route_message(event, _client(), Mock()) == "answered"
    assert len(bot.handled) == 1


def test_a_channel_message_is_ingested_not_answered():
    bot = _routing_bot()

    assert bot.route_message(_human(channel_type="channel"), _client(), Mock()) == "ingested"
    assert bot.handled == []
    assert len(bot.ingested) == 1


def test_a_correction_short_circuits_before_the_answer_path():
    bot = _routing_bot(_handle_correction=lambda event, say: True)

    assert bot.route_message(_human(), _client(), Mock()) == "correction"
    assert bot.handled == []


def test_the_message_handler_is_actually_wired_to_the_router():
    """배선을 빼도 안 걸리면 **테스트가 거기를 지나지 않는 것**이다.

    되돌리기 실험에서 `on_message` 안의 호출을 통째로 지워도 전부 통과했다.
    Bolt 가 등록한 핸들러를 직접 불러 그 갈래를 지난다.
    """
    handlers: dict[str, object] = {}

    class FakeApp:
        def middleware(self, fn):
            return fn

        def event(self, name):
            def register(fn):
                handlers.setdefault(name, fn)
                return fn
            return register

        def __getattr__(self, _name):
            # `action`·`view`·`command` 등 나머지 데코레이터는 그냥 통과시킨다.
            def register(*_a, **_kw):
                def wrap(fn):
                    return fn
                return wrap
            return register

    bot = _routing_bot()
    bot.app = FakeApp()
    routed: list[dict] = []
    bot.route_message = lambda event, client, say: routed.append(event)
    # 등록만 하면 된다. 등록 중 다른 초기화가 필요하면 그건 이 테스트의 관심 밖이다.
    try:
        WorkspaceBot._register(bot)
    except AttributeError as exc:  # pragma: no cover - 등록에 다른 값이 필요할 때
        pytest.skip(f"핸들러 등록에 다른 초기화가 필요합니다: {exc}")

    assert "message" in handlers, "message 핸들러가 등록되지 않았습니다"
    handlers["message"](_human(), _client(), Mock())

    assert routed, "message 핸들러가 route_message 를 부르지 않습니다"
