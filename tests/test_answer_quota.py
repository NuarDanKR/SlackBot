"""TYBot/VIPBot의 사용자별 정상 답변 횟수 제한."""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

from tybot.answer_quota import AnswerQuota, MemoryQuotaStore, limit_message
from tybot.slack.pilot import WorkspaceBot

NOW = datetime(2026, 11, 10, 1, 0, tzinfo=UTC)  # 10:00 KST


def _reserve(quota, index, *, bot="tybot", user="U1", now=NOW):
    return quota.reserve(
        workspace="tyit",
        bot_name=bot,
        slack_user=user,
        event_key=f"D1:{index}.0",
        now=now,
    )


def test_only_ten_concurrent_success_slots_are_reserved():
    quota = AnswerQuota(MemoryQuotaStore(), limit=10)

    decisions = [_reserve(quota, index) for index in range(11)]

    assert all(item.allowed for item in decisions[:10])
    assert decisions[9].remaining == 0
    assert not decisions[10].allowed
    assert decisions[10].used == 10


def test_a_failed_answer_releases_its_slot():
    quota = AnswerQuota(MemoryQuotaStore(), limit=1)
    failed = _reserve(quota, 1)

    quota.release(failed.reservation_id)

    retry = _reserve(quota, 2)
    assert retry.allowed


def test_a_successful_answer_keeps_its_slot():
    quota = AnswerQuota(MemoryQuotaStore(), limit=1)
    answered = _reserve(quota, 1)

    quota.finalize(answered.reservation_id, now=NOW)

    assert not _reserve(quota, 2).allowed


def test_tybot_and_vipbot_have_separate_limits():
    quota = AnswerQuota(MemoryQuotaStore(), limit=1)
    tybot = _reserve(quota, 1, bot="tybot")
    quota.finalize(tybot.reservation_id, now=NOW)

    vipbot = _reserve(quota, 2, bot="vipbot")
    assert vipbot.allowed


def test_users_have_separate_limits():
    quota = AnswerQuota(MemoryQuotaStore(), limit=1)
    first = _reserve(quota, 1, user="U1")
    quota.finalize(first.reservation_id, now=NOW)

    assert _reserve(quota, 2, user="U2").allowed


def test_the_same_slack_event_is_not_reserved_twice():
    quota = AnswerQuota(MemoryQuotaStore(), limit=10)
    first = _reserve(quota, 1)
    duplicate = _reserve(quota, 1)

    assert first.allowed
    assert not duplicate.allowed
    assert duplicate.duplicate


def test_limit_resets_on_the_kst_day_boundary():
    quota = AnswerQuota(MemoryQuotaStore(), limit=1)
    first = _reserve(quota, 1, now=datetime(2026, 11, 10, 14, 59, tzinfo=UTC))
    quota.finalize(first.reservation_id, now=NOW)

    after_midnight = _reserve(
        quota, 2, now=datetime(2026, 11, 10, 15, 1, tzinfo=UTC)
    )
    assert after_midnight.allowed


def test_user_message_does_not_look_like_a_token_or_cost_failure():
    quota = AnswerQuota(MemoryQuotaStore(), limit=1)
    first = _reserve(quota, 1)
    quota.finalize(first.reservation_id, now=NOW)
    denied = _reserve(quota, 2)

    text = limit_message(denied, "vipbot")
    assert "VIPBot" in text
    assert "10회" not in text
    assert "오류로 끝나거나" in text
    assert "토큰" not in text
    assert "비용" not in text


def _workspace_bot(quota: AnswerQuota) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot.bot_name = "tybot"
    bot.private_answers = False
    bot.reply_in_thread = False
    bot.answer_quota = quota
    return bot


def test_common_runtime_finalizes_only_a_successfully_delivered_answer():
    quota = Mock()
    quota.reserve.return_value = type(
        "Decision",
        (),
        {"allowed": True, "duplicate": False, "reservation_id": "R1"},
    )()
    bot = _workspace_bot(quota)
    bot._handle_request = lambda event, client, say, **kw: event.__setitem__(
        "_tybot_quota_success", True
    )

    bot._handle(
        {"text": "질문", "user": "U1", "channel": "D1", "ts": "1.0"},
        Mock(),
        Mock(return_value={"ts": "2.0"}),
        in_channel=False,
    )

    quota.finalize.assert_called_once_with("R1")
    quota.release.assert_not_called()


def test_common_runtime_refunds_a_request_without_a_successful_answer():
    quota = Mock()
    quota.reserve.return_value = type(
        "Decision",
        (),
        {"allowed": True, "duplicate": False, "reservation_id": "R1"},
    )()
    bot = _workspace_bot(quota)
    bot._handle_request = Mock()

    bot._handle(
        {"text": "질문", "user": "U1", "channel": "D1", "ts": "1.0"},
        Mock(),
        Mock(return_value={"ts": "2.0"}),
        in_channel=False,
    )

    quota.release.assert_called_once_with("R1")
    quota.finalize.assert_not_called()


class _PinnedClockQuota(AnswerQuota):
    """`_handle` 은 시각을 넘기지 않는다. 시험이 벽시계를 두 번 읽으면
    그 사이에 KST 자정이 지날 때 할당량 날짜가 갈라져 실패한다 — 시계를 고정한다."""

    def reserve(self, *, now=None, **kwargs):
        return super().reserve(now=now or NOW, **kwargs)


def test_common_runtime_stops_before_model_work_when_the_limit_is_full():
    quota = _PinnedClockQuota(MemoryQuotaStore(), limit=1)
    counted = _reserve(quota, 99)
    quota.finalize(counted.reservation_id, now=NOW)
    bot = _workspace_bot(quota)
    bot._handle_request = Mock()
    say = Mock(return_value={"ts": "2.0"})

    bot._handle(
        {"text": "질문", "user": "U1", "channel": "D1", "ts": "100.0"},
        Mock(),
        say,
        in_channel=False,
    )

    bot._handle_request.assert_not_called()
    assert "정상 답변 1회" in say.call_args.kwargs["text"]
