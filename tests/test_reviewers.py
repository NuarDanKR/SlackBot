"""채널 요약 검토자 (B-37).

설계: `docs/design/summary-review.md`
"""
from __future__ import annotations

from datetime import time

import pytest

from tybot import reviewers
from tybot.slack.pilot import _asks_to_clear, _mentioned_users, _time_token


# --- 인수 파싱 --------------------------------------------------------------
def test_users_come_from_ids_not_names():
    """이름으로 받으면 동명이인에서 갈리고, 이름이 바뀌면 검토자가 사라진다."""
    assert _mentioned_users("<@U0123ABC|dan> <@W9XY> 09:00") == ["U0123ABC", "W9XY"]


def test_duplicate_mentions_collapse_but_order_holds():
    assert _mentioned_users("<@U1> <@U2> <@U1>") == ["U1", "U2"]


def test_time_is_not_read_out_of_a_mention():
    """`<@U0123ABC>` 의 0123 을 시각으로 읽으면 엉뚱한 시각에 DM 이 간다."""
    assert _time_token("<@U0123ABC>") == ""
    assert _time_token("<@U0123ABC|dan> 09:00") == "09:00"
    assert _time_token("<@U1> 9시") == "9"


def test_no_argument_is_not_a_request_to_clear():
    """실수로 `/채널 검토자` 만 쳐서 검토가 멈추면 아무 표시 없이 요약이 안 된다.

    인수가 없으면 현재 상태를 보여주는 것이 맞고, 해제는 명시해야 한다.
    """
    assert not _asks_to_clear("")
    assert not _asks_to_clear("<@U1>")
    assert _asks_to_clear("없음")
    assert _asks_to_clear("해제")


# --- 시각 --------------------------------------------------------------------
@pytest.mark.parametrize(
    ("raw", "expected"),
    [("09:00", time(9, 0)), ("9", time(9, 0)), ("0930", time(9, 30)), ("", time(8, 0))],
)
def test_send_at_accepts_the_shapes_people_type(raw, expected):
    assert reviewers.parse_send_at(raw) == expected


@pytest.mark.parametrize("raw", ["25:00", "09:99", "아침", "1x:00"])
def test_a_bad_time_is_refused_not_defaulted(raw):
    """기본값으로 넘어가면 09:00 로 적었는데 08:00 에 와서 설정이 안 된 것처럼 보인다."""
    with pytest.raises(reviewers.ReviewerError):
        reviewers.parse_send_at(raw)


# --- 반영 가능 여부 ----------------------------------------------------------
def test_reading_failure_counts_as_no_reviewer(monkeypatch):
    """막는 쪽이 기본값이다.

    DB 를 못 읽었을 때 반영해 버리면 **장애가 곧 무단 반영**이 된다.
    """
    def explode(workspace, channel_id):
        raise reviewers.ReviewerError("DB 없음")

    monkeypatch.setattr(reviewers, "reviewers_for", explode)

    assert reviewers.has_reviewer("pilot", "C1") is False


def test_missing_database_url_is_a_clear_message(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(reviewers.ReviewerError, match="DATABASE_URL"):
        reviewers.reviewers_for("pilot", "C1")


def test_channels_without_reviewer_does_not_call_slack(monkeypatch):
    """채널 목록은 Slack 이 알고 이 모듈은 모른다.

    여기서 Slack 을 부르면 검토자 조회가 Slack 장애에 묶인다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)

    # DB 가 없어도 예외를 올리지 않는다 — 헬스 체크가 이걸로 멈추면 안 된다.
    assert reviewers.channels_without_reviewer({"pilot": [("C1", "#팀_전산(ABB155)_주간보고")]}) == []


def test_duplicate_reviewers_are_refused(monkeypatch):
    """같은 사람이 두 번 들어가면 DM 이 두 번 간다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/none")

    with pytest.raises(reviewers.ReviewerError, match="두 번"):
        reviewers.set_reviewers(
            workspace="pilot",
            channel_id="C1",
            channel_name="#x",
            reviewer_users=["U1", "U1"],
            send_at=time(9, 0),
            set_by="U9",
        )


# --- 스키마가 판정에 이름을 쓰지 않는지 -------------------------------------
def test_schema_keys_on_channel_id_not_name():
    """이름으로 키를 잡으면 `/채널 이름변경` 이 검토자를 조용히 지운다."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "deploy" / "sql" / "reviewer_schema.sql").read_text(
        encoding="utf-8"
    )

    assert "PRIMARY KEY (workspace, channel_id, reviewer_user)" in sql
    assert "channel_name" in sql, "표시용 이름은 함께 두되 판정에 쓰지 않는다"
