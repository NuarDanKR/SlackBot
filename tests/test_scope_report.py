"""`/권한` — 내 질문이 근거로 삼는 범위.

사내 피드백: "슬랙 내 어떤 콘텐츠 한도 안에서 답변되는지 알 수 없다."
`/수집상태` 는 채널 하나를 답한다. 이건 **사람 기준**의 범위다.
"""
from __future__ import annotations

from unittest.mock import Mock

from tybot.scope_report import ScopeFacts, report


def _facts(**kw) -> ScopeFacts:
    base = {
        "workspace_label": "경영본부",
        "workspace_key": "mgmt",
        "collected_channels": 7,
        "uncollected_channels": 3,
        "visible_docs": 6,
        "visible_lines": 101,
    }
    base.update(kw)
    return ScopeFacts(**base)


def test_report_states_the_workspace_and_counts():
    text = report(_facts())
    assert "경영본부" in text and "`mgmt`" in text
    assert "7개" in text
    assert "문서 6건 · 원문 101줄" in text


def test_channel_names_are_never_listed():
    """채널명에 조직·업무·현장이 들어 있어 그 자체가 노출이다."""
    text = report(_facts())
    assert "#" not in text


def test_role_is_spelled_out():
    assert "일반" in report(_facts())
    assert "상위(root)" in report(_facts(is_root=True))
    assert "임원" in report(_facts(is_exec=True))


def test_exec_beats_root_in_the_label():
    assert "임원" in report(_facts(is_root=True, is_exec=True))


def test_cross_workspace_is_explicit_either_way():
    assert "pilot, tyit" in report(_facts(readable=["pilot", "tyit"]))
    assert "없음 (이 워크스페이스 자료만)" in report(_facts(readable=[]))


def test_root_scope_says_all_workspaces_without_readable_list():
    text = report(_facts(is_root=True, readable=[]))
    assert "모든 등록 워크스페이스" in text
    assert "멤버가 아닌 채널" not in text


def test_uncollected_channels_explain_why():
    text = report(_facts(uncollected_channels=3))
    assert "표준 규칙과 달라" in text


def test_uncollected_line_is_omitted_when_zero():
    assert "수집되지 않는 채널" not in report(_facts(uncollected_channels=0))


def test_empty_scope_says_why_it_is_empty():
    text = report(_facts(visible_docs=0, visible_lines=0))
    assert "없음" in text
    assert "멤버가 아닙니다" in text


def test_footer_denies_learning():
    """'학습하는 것 아니냐' 가 가장 흔한 오해다. 매번 원문을 다시 찾는다는 사실을 못 박는다."""
    text = report(_facts())
    assert "매번 아카이브 원문을 다시 찾아" in text
    assert "학습하거나 근거로 쓰지 않습니다" in text


def test_footer_points_to_the_per_channel_command():
    assert "/수집상태" in report(_facts())


def test_footer_uses_slack_bold_not_markdown():
    """`**굵게**` 는 Slack 에서 글자 그대로 보인다."""
    assert "**" not in report(_facts())


def test_lookup_failure_still_answers():
    """조회 실패로 명령이 침묵하면 사용자는 고장으로 오해한다."""
    text = report(_facts(lookup_failed=True))
    assert "조회하지 못해" in text
    assert "학습하거나 근거로 쓰지 않습니다" in text  # 안내는 그대로 준다


# --- 봇 연결 -----------------------------------------------------------------
def _bot(*, channels=(), docs=(), exec_users=()):
    from tybot.slack.pilot import WorkspaceBot

    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "mgmt"
    bot.cfg = Mock(label="경영본부", readable=frozenset({"pilot"}), is_root=True)
    bot.exec_users = set(exec_users)
    bot.store = Mock(visible_docs=lambda ctx: list(docs))
    bot._context = lambda client, uid: Mock(
        workspace="mgmt", role="member", channels=frozenset(channels), is_root=True
    )
    client = Mock()
    client.users_conversations.return_value = {
        "channels": [{"name": n.lstrip("#")} for n in channels]
    }
    return bot, client


def test_bot_counts_only_rule_matching_channels_as_collected():
    bot, client = _bot(channels=["#본사팀-전산_ABB110-회의", "#점심메뉴", "#잡담"])
    text = bot._scope_report(client, "U1")
    assert "수집 대상 채널*: 1개" in text
    assert "수집되지 않는 채널*: 2개" in text


def test_bot_uses_the_same_permission_filter_as_answers():
    """여기 숫자와 실제 답변의 근거가 다르면 이 화면이 거짓말이 된다."""
    docs = [Mock(raw_lines=[0] * 12), Mock(raw_lines=[0] * 3)]
    bot, client = _bot(channels=["#본사팀-전산_ABB110-회의"], docs=docs)
    text = bot._scope_report(client, "U1")
    assert "문서 2건 · 원문 15줄" in text


def test_bot_survives_slack_failure():
    bot, client = _bot()
    client.users_conversations.side_effect = RuntimeError("missing_scope")
    assert "조회하지 못해" in bot._scope_report(client, "U1")


def test_exec_user_is_labelled():
    bot, client = _bot(channels=["#본사팀-전산_ABB110-회의"], exec_users=("U1",))
    assert "임원" in bot._scope_report(client, "U1")
