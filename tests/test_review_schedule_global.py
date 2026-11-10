"""요약 검토 일정은 전역 하나 — **Slack 명령 경로** (B-88).

모달은 `test_channel_health.py` 가 본다. 여기서는 텍스트 명령
`/채널 검토자 @사람 [매주 수요일] [09:00]` 을 본다.

모달만 고치고 명령을 두면, 같은 사람이 명령으로 「매주 수요일 09:00」 을 치고
그때 DM 을 기다린다. 입력 경로가 둘이면 고장도 둘이다.
"""

from __future__ import annotations

from datetime import time
from types import SimpleNamespace

import pytest

from tybot import reviewers
from tybot.slack.pilot import WorkspaceBot

GLOBAL = "매주 화·금 17:00 KST"


def _bot(monkeypatch, *, rows=None):
    """`set_reviewers` 가 받은 값을 기록하는 봇."""
    saved: list[dict] = []

    def fake_set(**kwargs):
        saved.append(kwargs)
        users = kwargs["reviewer_users"]
        return [
            SimpleNamespace(
                reviewer_user=u,
                schedule_kind=kwargs["schedule_kind"],
                weekday=kwargs["weekday"],
                send_at=kwargs["send_at"],
            )
            for u in users
        ]

    monkeypatch.setattr(reviewers, "set_reviewers", fake_set)
    if rows is not None:
        monkeypatch.setattr(reviewers, "reviewers_for", lambda ws, ch: rows)

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot._can_manage_channel = lambda channel_id, user_id: True
    return bot, saved


def _run(bot, args: str) -> str:
    out: list[str] = []
    bot._handle_reviewer_command(
        {"channel_id": "C1", "user_id": "U0", "channel_name": "팀_전산"},
        args,
        lambda text, **kw: out.append(text),
    )
    return "\n".join(out)


@pytest.mark.parametrize(
    "args",
    ["<@U1> 매주 수요일 09:00", "<@U1> 매일 06:30", "<@U1> 10:00", "<@U1>"],
)
def test_a_typed_schedule_is_never_stored(monkeypatch, args):
    """친 주기·시각이 DB 에 들어가지 않는다.

    들어가면 「이 채널은 수요일 09시」 가 쌓이고, 다음 사람이 그것이 실행된다고
    믿는다. 실행 판단은 전역 정책이 한다.
    """
    bot, saved = _bot(monkeypatch)

    _run(bot, args)

    (call,) = saved
    assert call["schedule_kind"] == reviewers.DAILY
    assert call["weekday"] is None
    assert call["send_at"] == reviewers.DEFAULT_SEND_AT


def test_the_confirmation_names_the_global_round(monkeypatch):
    bot, _ = _bot(monkeypatch)

    text = _run(bot, "<@U1>")

    assert GLOBAL in text
    assert "매일" not in text


def test_a_typed_schedule_is_reported_as_ignored(monkeypatch):
    """조용히 무시하면 사람은 그 시각에 DM 을 기다린다."""
    bot, _ = _bot(monkeypatch)

    text = _run(bot, "<@U1> 매주 수요일 09:00")

    assert "쓰지 않습니다" in text
    assert GLOBAL in text


def test_no_ignored_notice_when_nothing_was_typed(monkeypatch):
    # 아무 말도 안 했는데 「무시했다」 고 하면 사람은 자기가 뭘 쳤는지 되짚는다.
    bot, _ = _bot(monkeypatch)

    assert "쓰지 않습니다" not in _run(bot, "<@U1>")


def test_the_confirmation_no_longer_claims_only_reviewed_items_are_used(monkeypatch):
    """B-88 이후 미검토 후보는 **자동으로 봇 요약이 된다.**

    「검토자가 확인한 것만 요약에 반영됩니다」 는 이제 사실이 아니다. 그 문장을
    믿은 검토자는 안 봐도 반영이 안 되는 줄 알고 넘긴다.
    """
    bot, _ = _bot(monkeypatch)

    text = _run(bot, "<@U1>")

    assert "확인한 것만" not in text
    assert "자동으로 봇 요약" in text


def test_showing_reviewers_uses_the_global_label_not_the_db_one(monkeypatch):
    """DB 에 「매주 수요일 09:00」 이 남아 있어도 화면은 전역 일정을 말한다."""
    rows = [SimpleNamespace(
        reviewer_user="U1", schedule_kind="weekly", weekday=2, send_at=time(9, 0)
    )]
    bot, _ = _bot(monkeypatch, rows=rows)

    text = _run(bot, "")

    assert GLOBAL in text
    assert "수요일" not in text
    assert "09:00" not in text


def test_the_help_no_longer_teaches_the_old_syntax(monkeypatch):
    bot, _ = _bot(monkeypatch, rows=[])

    text = _run(bot, "")

    assert "매주 수요일" not in text
    assert "매일 09:00" not in text
    assert GLOBAL in text
