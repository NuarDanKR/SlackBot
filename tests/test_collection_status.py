"""`/수집상태` — 왜 수집이 안 되는지 그 자리에서 답한다.

수집 여부는 채널 **이름**이 정하고(`channels.should_collect`), 비공개 채널은 봇이 스스로
들어갈 수 없다. 두 조건이 겹쳐 "왜 우리 채널은 수집이 안 되지?" 가 가장 흔한 질문이 된다.
여기 테스트는 상태별 판정과 **조치 안내가 빠지지 않는 것**을 고정한다.
"""
from __future__ import annotations

import pytest

from tybot.collection_status import (
    AUTOJOIN_OFF,
    COLLECTING,
    DM,
    NAME_MISMATCH,
    NOT_MEMBER_PRIVATE,
    NOT_MEMBER_PUBLIC,
    ChannelFacts,
    diagnose,
    report,
)

GOOD = "#팀-전산_ABB110-회의"


def test_name_mismatch_wins_over_membership():
    """이름이 규칙 밖이면 봇이 멤버여도 수집하지 않는다 - 실제 동작과 같아야 한다."""
    assert diagnose(ChannelFacts(channel="#점심메뉴", is_member=True)) == NAME_MISMATCH


def test_private_without_membership():
    f = ChannelFacts(channel=GOOD, is_private=True)
    assert diagnose(f) == NOT_MEMBER_PRIVATE


def test_public_without_membership_is_pending_not_broken():
    assert diagnose(ChannelFacts(channel=GOOD)) == NOT_MEMBER_PUBLIC


def test_autojoin_off_is_distinct_from_pending():
    """자동 참여가 꺼져 있으면 '기다리면 된다' 가 거짓말이 된다."""
    f = ChannelFacts(channel=GOOD, autojoin_enabled=False)
    assert diagnose(f) == AUTOJOIN_OFF
    assert "자동 참여가 꺼져" in report(f)


def test_member_and_valid_name_is_collecting():
    assert diagnose(ChannelFacts(channel=GOOD, is_member=True)) == COLLECTING


def test_dm_is_not_a_failure():
    assert diagnose(ChannelFacts(channel="@사용자", is_dm=True)) == DM
    assert "DM" in report(ChannelFacts(channel="@사용자", is_dm=True))


@pytest.mark.parametrize(
    "facts",
    [
        ChannelFacts(channel="#점심메뉴"),
        ChannelFacts(channel=GOOD, is_private=True),
        ChannelFacts(channel=GOOD),
        ChannelFacts(channel=GOOD, autojoin_enabled=False),
    ],
)
def test_every_failure_state_tells_the_user_what_to_do(facts):
    """원인만 알려주고 방법을 안 알려주면 결국 담당자에게 다시 묻게 된다."""
    text = report(facts)
    assert "/invite" in text or "/채널 이름변경" in text


def test_private_report_explains_the_slack_limitation():
    text = report(ChannelFacts(channel=GOOD, is_private=True, bot_name="tybot"))
    assert "스스로 들어갈 수 없습니다" in text
    assert "/invite @tybot" in text


def test_mismatch_report_shows_the_expected_format():
    text = report(ChannelFacts(channel="#점심메뉴"))
    assert "조직코드" in text
    assert "#본사팀-전산_ABB110-주간회의" in text
    assert "소급 수집되지 않습니다" in text  # 기대치를 미리 낮춘다


def test_collecting_report_shows_archive_stats():
    text = report(
        ChannelFacts(channel=GOOD, is_member=True, raw_lines=412, last_ingested="2026-08-27T15:40")
    )
    assert "412줄" in text
    assert "2026-08-27T15:40" in text
    assert "봇 자신의 발언은 제외" in text


def test_collecting_but_empty_says_so():
    text = report(ChannelFacts(channel=GOOD, is_member=True))
    assert "아직 쌓인 원문이 없습니다" in text


def test_write_problem_is_surfaced_here_too():
    """수집 중이라고만 말하면 저장이 안 되는 상태를 사람이 모른다."""
    text = report(
        ChannelFacts(
            channel=GOOD,
            is_member=True,
            raw_lines=10,
            write_problems={"아카이브": "/opt/tybot/archive (Read-only file system)"},
        )
    )
    assert "쓰기 불가" in text
    assert "저장되지 않습니다" in text


def test_realtime_off_is_warned():
    text = report(ChannelFacts(channel=GOOD, is_member=True, raw_lines=1, realtime_enabled=False))
    assert "실시간 수집이 꺼져" in text
