"""필수 앱 넷의 참여 진단 (B-87).

이 파일이 지키는 것은 하나다 — **모르는 것을 「없음」 이라 하지 않는다.**

Slack API 가 실패했을 때 「미초대」 라고 쓰면 사람은 이미 들어와 있는 앱을
초대하러 가서 아무 일도 못 한다. 더 나쁘게는 **정말로 빠진 앱이 있을 때도 같은
화면**이라, 다음부터 이 화면을 안 믿는다. 조용한 고장을 잡으려고 만든 화면이
스스로 조용한 고장이 되는 셈이다.

그리고 **이름으로 추측하지 않는다.** 「아카이버」 라는 이름의 다른 앱이 들어와
있으면 이름 대조는 있다고 말한다. 정체성은 검증된 `bot_user_id` 와 콘솔이
관리하는 Slack 앱 ID 뿐이다.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tybot import channel_app_probe as probe
from tybot import channel_apps as ca
from tybot import channel_health as ch

ARCHIVER_USER = "U-ARCHIVER"
TYBOT_USER = "U-TYBOT"
HERMES_USER = "U-HERMES"
RHWP_APP = "A0RHWP0001"
RHWP_USER = "U-RHWP"


def _row(bot_user_id: str, *, state: str = "enabled", identity_ok: bool = True) -> dict:
    return {"state": state, "bot_user_id": bot_user_id, "identity_ok": identity_ok}


def _bindings(*, hermes_row=..., hermes_route="disabled", rhwp_on=True) -> tuple:
    """기본은 **넷 다 정상 등록**이다. 시험마다 한 조각씩만 망가뜨린다."""
    if hermes_row is ...:
        hermes_row = _row(HERMES_USER)
    return (
        ca.binding_from_connection(ca.ARCHIVER, _row(ARCHIVER_USER)),
        ca.binding_from_connection(ca.TYBOT, _row(TYBOT_USER)),
        ca.converter_binding([RHWP_APP], delegated=rhwp_on),
        ca.hermes_binding(hermes_row, route_mode=hermes_route),
    )


def _facts(**kw) -> ca.ChannelAppFacts:
    base = {
        "bindings": _bindings(),
        "member_ids": frozenset(
            {ARCHIVER_USER, TYBOT_USER, HERMES_USER, RHWP_USER, "U-HUMAN"}
        ),
        "member_app_ids": frozenset({RHWP_APP}),
    }
    base.update(kw)
    return ca.ChannelAppFacts(**base)


def _by_role(statuses) -> dict[str, ca.AppStatus]:
    return {status.role: status for status in statuses}


# --- 1. 네 역할이 **항상** 나온다 ---------------------------------------------


def test_every_role_is_reported_even_when_nothing_is_registered():
    """등록 안 된 역할이 목록에서 빠지면 「검사했는데 괜찮다」 로 읽힌다."""
    got = ca.evaluate(ca.ChannelAppFacts())

    assert [status.role for status in got] == list(ca.ROLES)
    assert all(status.state in ca.STATES for status in got)
    assert all(status.code in ca.CODES for status in got)


def test_vipbot_is_not_one_of_the_four_roles():
    """VIPBot 은 필수 앱이 아니다. 섞으면 「VIP 가 없어서 빨강」 인 채널이 생긴다."""
    assert "vipbot" not in ca.ROLES
    assert "vipbot" not in ca.CATALOG_KEYS.values()
    assert ca.CATALOG_KEYS[ca.TYBOT] == "master"


# --- 2. 참여 / 미초대 ---------------------------------------------------------


def test_all_four_present_is_all_joined():
    got = _by_role(ca.evaluate(_facts()))

    assert {status.state for status in got.values()} == {ca.JOINED}
    assert all(status.code == ca.MEMBER for status in got.values())
    assert ca.summary(got.values()) == "필수 앱이 모두 참여 중입니다."
    assert ca.missing(got.values()) == ()


def test_a_missing_archiver_is_not_invited_with_an_invite_hint():
    got = _by_role(ca.evaluate(_facts(
        member_ids=frozenset({TYBOT_USER, HERMES_USER, RHWP_USER}),
    )))

    assert got[ca.ARCHIVER].state == ca.NOT_INVITED
    assert got[ca.ARCHIVER].code == ca.ABSENT
    # 봇 핸들을 지어내지 않는다 — `@Archiver` 는 화면용 역할 이름이지 Slack
    # 핸들이 아니다. 그 명령을 그대로 친 사람은 `user_not_found` 만 본다.
    assert "@" not in got[ca.ARCHIVER].fix
    assert "/invite" not in got[ca.ARCHIVER].fix
    assert "앱 추가" in got[ca.ARCHIVER].fix
    assert got[ca.TYBOT].state == ca.JOINED
    assert "Archiver" in ca.summary(got.values())


def test_a_missing_converter_app_is_not_invited():
    """rhwp 는 **앱 ID** 로만 대조한다. 사용자 ID 가 있어도 앱이 다르면 아니다."""
    got = _by_role(ca.evaluate(_facts(member_app_ids=frozenset({"A0OTHER001"}))))

    assert got[ca.RHWP].state == ca.NOT_INVITED
    assert got[ca.ARCHIVER].state == ca.JOINED


def test_a_name_lookalike_does_not_count_as_the_real_app():
    """이름은 보지 않는다 — 이 시험이 그것을 고정한다.

    「Archiving Bot」 이라는 이름의 다른 앱이 들어와 있어도, 검증된 사용자 ID 가
    아니면 참여가 아니다. 이름 대조였다면 여기서 초록이 떴을 것이다.
    """
    got = _by_role(ca.evaluate(_facts(
        member_ids=frozenset(
            {"U-FAKE-ARCHIVING-BOT", TYBOT_USER, HERMES_USER, RHWP_USER}
        ),
    )))

    assert got[ca.ARCHIVER].state == ca.NOT_INVITED


# --- 3. 확인 불가 — Slack API 오류 --------------------------------------------


def test_an_api_failure_is_never_reported_as_not_invited():
    """**이 파일의 존재 이유.** 못 읽은 것을 없다고 말하지 않는다."""
    got = ca.evaluate(_facts(member_ids=None, member_app_ids=None,
                             probe_error="ratelimited"))

    assert {status.state for status in got} == {ca.UNKNOWN}
    assert {status.code for status in got} == {ca.MEMBERS_UNREADABLE}
    assert ca.missing(got) == ()
    assert "확인하지 못했습니다" in ca.summary(got)


def test_a_private_channel_without_the_bot_explains_why_it_cannot_look():
    """비공개 채널에서는 멤버 목록 자체를 못 읽는다(Slack 제약).

    그 사실을 적어야 사람이 다음 행동을 안다 — 「초대하세요」 가 아니라
    「TYBot 을 먼저 넣으세요」 다.
    """
    got = _by_role(ca.evaluate(_facts(
        member_ids=None, member_app_ids=None, is_private=True,
        probe_error="channel_not_found",
    )))

    assert got[ca.ARCHIVER].state == ca.UNKNOWN
    assert "비공개" in got[ca.ARCHIVER].detail
    assert "자료가 없다" not in got[ca.ARCHIVER].detail


def test_unresolved_app_ids_do_not_make_the_converter_absent():
    """멤버는 읽었지만 어느 앱인지 못 풀었을 때.

    사용자 ID 로 판정하는 셋은 그대로 답이 나오고, 앱 ID 가 필요한 rhwp 만
    「확인 불가」 다. 한 조각 실패가 나머지 판정을 덮지 않는다.
    """
    got = _by_role(ca.evaluate(_facts(member_app_ids=None)))

    assert got[ca.RHWP].state == ca.UNKNOWN
    assert got[ca.RHWP].code == ca.APP_ID_UNRESOLVED
    assert got[ca.ARCHIVER].state == ca.JOINED
    assert got[ca.TYBOT].state == ca.JOINED


# --- 4. 확인 불가 — binding 누락 ----------------------------------------------


def test_a_missing_binding_is_unknown_not_absent():
    """대조할 ID 가 없는데 「없다」 고 말할 수 없다."""
    got = _by_role(ca.evaluate(_facts(
        bindings=(
            ca.binding_from_connection(ca.ARCHIVER, None),
            ca.binding_from_connection(ca.TYBOT, _row(TYBOT_USER)),
            ca.converter_binding([RHWP_APP], delegated=True),
            ca.hermes_binding(None, route_mode="disabled"),
        ),
    )))

    assert got[ca.ARCHIVER].state == ca.UNKNOWN
    assert got[ca.ARCHIVER].code == ca.BINDING_MISSING
    assert "콘솔" in got[ca.ARCHIVER].fix


@pytest.mark.parametrize("state", ["draft", "disabled", "error", "retired"])
def test_a_connection_that_is_not_enabled_is_unknown(state):
    """꺼 둔 연결의 `bot_user_id` 로 「참여 중」 이라고 쓰지 않는다.

    그렇게 쓰면 꺼 둔 앱이 살아 있는 것처럼 보이고, 수집이 멈춘 이유를
    이 화면에서 찾을 수 없게 된다.
    """
    binding = ca.binding_from_connection(ca.ARCHIVER, _row(ARCHIVER_USER, state=state))

    assert not binding.identified
    assert binding.missing_code == ca.BINDING_DISABLED

    got = _by_role(ca.evaluate(_facts(bindings=(binding, *_bindings()[1:]))))
    assert got[ca.ARCHIVER].state == ca.UNKNOWN
    assert got[ca.ARCHIVER].code == ca.BINDING_DISABLED


@pytest.mark.parametrize("identity_ok", [None, False])
def test_an_unverified_identity_is_unknown(identity_ok):
    binding = ca.binding_from_connection(
        ca.ARCHIVER, _row(ARCHIVER_USER, identity_ok=identity_ok)
    )

    assert binding.missing_code == ca.IDENTITY_UNVERIFIED
    got = _by_role(ca.evaluate(_facts(bindings=(binding, *_bindings()[1:]))))
    assert got[ca.ARCHIVER].state == ca.UNKNOWN


def test_an_enabled_connection_without_a_user_id_is_unknown():
    # 스키마가 막고 있지만, 막는 것과 화면이 믿는 것은 다른 문제다.
    binding = ca.binding_from_connection(ca.ARCHIVER, _row("", identity_ok=True))

    assert binding.missing_code == ca.IDENTITY_UNVERIFIED
    assert not binding.identified


# --- 5. 해당 없음 -------------------------------------------------------------


def test_hermes_on_external_rpc_is_not_applicable_not_missing():
    """외부 RPC 구성이면 Slack 앱이 없는 것이 **정상**이다(B-87).

    「미초대」 라고 쓰면 사람은 있지도 않은 앱을 찾으러 간다.
    """
    got = _by_role(ca.evaluate(_facts(
        bindings=_bindings(hermes_row=None, hermes_route="active"),
    )))

    assert got[ca.HERMES].state == ca.NOT_APPLICABLE
    assert got[ca.HERMES].code == ca.EXTERNAL_RPC
    assert "외부 전문봇" in got[ca.HERMES].detail
    assert got[ca.HERMES].fix == ""
    assert ca.missing(got.values()) == ()
    assert ca.summary(got.values()) == "필수 앱이 모두 참여 중입니다."


@pytest.mark.parametrize("mode", ["shadow", "active"])
def test_external_rpc_covers_every_non_disabled_route_mode(mode):
    binding = ca.hermes_binding(None, route_mode=mode)

    assert binding.required is False
    assert binding.missing_code == ca.EXTERNAL_RPC


def test_a_slack_connected_hermes_wins_over_the_external_mode():
    """둘 다 구성된 공존 기간에는 **채널 참여가 실제로 필요하다.**

    여기서 외부 모드를 먼저 보면, 켜 둔 Slack 연결이 빠져 있어도 「해당 없음」
    으로 덮인다.
    """
    binding = ca.hermes_binding(_row(HERMES_USER), route_mode="active")

    assert binding.required is True
    assert binding.bot_user_ids == frozenset({HERMES_USER})

    got = _by_role(ca.evaluate(_facts(
        bindings=_bindings(hermes_row=_row(HERMES_USER), hermes_route="active"),
        member_ids=frozenset({ARCHIVER_USER, TYBOT_USER, RHWP_USER}),  # Hermes 없음
    )))
    assert got[ca.HERMES].state == ca.NOT_INVITED


def test_hermes_with_neither_a_connection_nor_a_route_is_unknown():
    """구성이 아예 없으면 「해당 없음」 이 아니라 「모른다」 다.

    쓰지 않기로 한 것과 설정을 빠뜨린 것은 사람이 할 일이 다르다.
    """
    got = _by_role(ca.evaluate(_facts(
        bindings=_bindings(hermes_row=None, hermes_route="disabled"),
    )))

    assert got[ca.HERMES].state == ca.UNKNOWN
    assert got[ca.HERMES].code == ca.BINDING_MISSING


@pytest.mark.parametrize(
    ("ids", "delegated"),
    [((RHWP_APP,), False), ((), False)],
)
def test_the_converter_is_not_applicable_when_delegation_is_off(ids, delegated):
    """HWP 를 넘기지 않는 설정이면 변환 앱 부재는 고장이 아니다.

    고칠 것이 없는 경고가 매번 보이면, 몇 번 지난 뒤 옆의 진짜 경고까지
    같이 무시된다.
    """
    binding = ca.converter_binding(ids, delegated=delegated)

    assert binding.required is False
    assert binding.missing_code == ca.DELEGATION_OFF

    got = _by_role(ca.evaluate(_facts(bindings=(*_bindings()[:2], binding, _bindings()[3]))))
    assert got[ca.RHWP].state == ca.NOT_APPLICABLE
    assert got[ca.RHWP].fix == ""


def test_delegation_on_without_an_app_id_is_unknown_not_not_applicable():
    """설정이 **반쯤** 된 상태. 가장 조용한 고장이다.

    위임이 켜져 있으면 TYBot 은 HWP 를 직접 변환하지 않는다
    (`conversion_delegated`). 그런데 받을 앱이 지정돼 있지 않으면 한글 문서가
    통째로 근거에서 빠진다 — 변환 실패도 안 나고, 화면도 조용하다.

    「해당 없음」 으로 적으면 그 조용함이 화면으로 옮겨올 뿐이다.
    """
    binding = ca.converter_binding([], delegated=True)

    assert binding.required is True
    assert binding.missing_code == ca.BINDING_MISSING
    assert "앱 ID" in binding.invite_hint

    got = _by_role(ca.evaluate(_facts(
        bindings=(*_bindings()[:2], binding, _bindings()[3]),
    )))
    assert got[ca.RHWP].state == ca.UNKNOWN
    assert got[ca.RHWP].code == ca.BINDING_MISSING


def test_converter_app_ids_are_compared_case_insensitively():
    binding = ca.converter_binding(["a0rhwp0001"], delegated=True)

    assert binding.app_ids == frozenset({RHWP_APP})


# --- 6. 화면 ------------------------------------------------------------------


def test_every_role_gets_one_line_with_its_state_word():
    rows = ca.evaluate(_facts(
        bindings=_bindings(hermes_row=None, hermes_route="active", rhwp_on=False),
        member_ids=frozenset({TYBOT_USER}),
    ))
    text = "\n".join(ca.lines(rows))

    assert "Archiver: 미초대" in text
    assert "TYBot: 참여" in text
    assert "rhwp: 해당 없음" in text
    assert "Hermes: 해당 없음" in text


def test_the_health_check_is_red_when_a_required_app_is_missing():
    facts = ch.HealthFacts(
        channel="#팀-전산_ABB110-주간회의",
        required_apps=ca.evaluate(_facts(member_ids=frozenset({TYBOT_USER}))),
    )

    check = next(c for c in ch.checks(facts) if c.label == "필수 앱")

    assert check.mark == ch.BAD
    assert "Archiver" in check.detail


def test_the_health_check_is_amber_when_only_unknowns_remain():
    """확인 불가는 **빨강이 아니다.** 고장일 수도 아닐 수도 있다."""
    facts = ch.HealthFacts(
        channel="#팀-전산_ABB110-주간회의",
        required_apps=ca.evaluate(_facts(member_ids=None, member_app_ids=None)),
    )

    check = next(c for c in ch.checks(facts) if c.label == "필수 앱")

    assert check.mark == ch.WARN


def test_an_unchecked_channel_is_not_reported_as_healthy():
    """검사하지 못한 것을 정상이라고 말하지 않는다.

    기본값이 `None` 인 이유다. 비어 있을 때 초록으로 두면 B-87 이 고치려던
    바로 그 상태(「확인 안 했는데 모두 정상」)가 되돌아온다.
    """
    facts = ch.HealthFacts(channel="#팀-전산_ABB110-주간회의", required_apps=None)

    check = next(c for c in ch.checks(facts) if c.label == "필수 앱")

    assert check.mark == ch.UNKNOWN
    assert not check.healthy


def test_the_status_report_lists_the_missing_app_and_how_to_invite_it():
    facts = ch.HealthFacts(
        channel="#팀-전산_ABB110-주간회의",
        channel_id="C1",
        is_member=True,
        required_apps=ca.evaluate(_facts(member_ids=frozenset({TYBOT_USER}))),
    )

    text = ch.report(facts)

    assert "필수 앱" in text
    assert "Archiver: 미초대" in text
    assert ca.INVITE_HINT in text
    assert "@Archiver" not in text, "지어낸 봇 핸들이 화면에 나갔다"


# --- 7. 수집 경로 — Slack 호출 --------------------------------------------------


def test_members_are_read_to_the_last_page():
    """반쪽 목록으로 「없다」 고 말하지 않는다."""
    client = Mock()
    client.conversations_members.side_effect = [
        {"members": ["U1", "U2"], "response_metadata": {"next_cursor": "c2"}},
        {"members": ["U3"], "response_metadata": {"next_cursor": ""}},
    ]

    found, error = probe.members(client, "C1")

    assert found == frozenset({"U1", "U2", "U3"})
    assert error == ""
    assert client.conversations_members.call_count == 2


def test_an_unfinished_member_listing_is_dropped_entirely():
    """끝까지 못 읽으면 통째로 버린다 — 뒤 페이지의 앱이 미초대가 되지 않게."""
    client = Mock()
    client.conversations_members.return_value = {
        "members": ["U1"], "response_metadata": {"next_cursor": "always-more"}
    }

    found, error = probe.members(client, "C1")

    assert found is None
    assert error == "too_many_members"
    assert client.conversations_members.call_count == probe.MAX_PAGES


def test_a_slack_error_returns_its_reason_and_no_members():
    client = Mock()
    client.conversations_members.side_effect = RuntimeError("boom")
    client.conversations_members.side_effect.response = SimpleNamespace(
        data={"error": "channel_not_found"}
    )

    found, error = probe.members(client, "C1")

    assert found is None
    assert error == "channel_not_found"


def test_app_ids_come_from_the_bot_profile_not_the_name():
    """이름이 아니라 `profile.api_app_id` 로 대조한다.

    사람 사용자에 `rhwp` 라는 이름을 붙여도 앱 지도에 들어오지 않는다.
    """
    probe.clear_cache()
    client = Mock()
    client.users_list.return_value = {
        "members": [
            {"id": "U-HUMAN", "is_bot": False, "name": "rhwp"},
            {"id": RHWP_USER, "is_bot": True,
             "profile": {"api_app_id": "a0rhwp0001"}},
        ],
        "response_metadata": {"next_cursor": ""},
    }

    assert probe.app_ids(client, "tyit", [RHWP_USER, "U-HUMAN"]) == frozenset({RHWP_APP})


def test_the_user_list_is_read_to_the_last_page():
    """한 쪽만 보면 뒤 페이지의 앱이 「없는」 것이 된다."""
    probe.clear_cache()
    client = Mock()
    client.users_list.side_effect = [
        {"members": [{"id": "U-A", "is_bot": True, "profile": {"api_app_id": "A0AAA"}}],
         "response_metadata": {"next_cursor": "p2"}},
        {"members": [{"id": RHWP_USER, "is_bot": True,
                      "profile": {"api_app_id": RHWP_APP}}],
         "response_metadata": {"next_cursor": ""}},
    ]

    assert probe.app_ids(client, "tyit", [RHWP_USER]) == frozenset({RHWP_APP})
    assert client.users_list.call_count == 2


def test_an_unfinished_user_listing_is_dropped_entirely():
    probe.clear_cache()
    client = Mock()
    client.users_list.return_value = {
        "members": [], "response_metadata": {"next_cursor": "always-more"}
    }

    assert probe.bot_app_map(client, "tyit") is None
    assert client.users_list.call_count == probe.MAX_PAGES


def test_one_failed_listing_call_drops_the_whole_app_id_set():
    """일부만 읽고 「없다」 고 말하지 않는다."""
    probe.clear_cache()
    client = Mock()
    client.users_list.side_effect = RuntimeError("ratelimited")

    assert probe.app_ids(client, "tyit", ["U1"]) is None


def test_a_human_only_channel_resolves_to_an_empty_app_set():
    # `None`(못 읽음)과 `frozenset()`(읽었고 봇이 없음)은 다른 사실이다.
    probe.clear_cache()
    client = Mock()
    client.users_list.return_value = {
        "members": [{"id": "U-HUMAN", "is_bot": False}],
        "response_metadata": {"next_cursor": ""},
    }

    assert probe.app_ids(client, "tyit", ["U-HUMAN"]) == frozenset()


def test_the_bot_app_map_is_cached_so_one_status_call_is_not_n_lookups():
    """멤버마다 `users.info` 를 부르던 것을 목록 한 번 + 캐시로 바꿨다.

    `users.info` 는 Tier 4 지만 그 한도는 **워크스페이스 전체가 나눠 쓴다** —
    상태 화면 하나가 수집기와 답변 경로의 호출까지 같이 굶겼다.
    """
    probe.clear_cache()
    client = Mock()
    client.users_list.return_value = {
        "members": [{"id": RHWP_USER, "is_bot": True,
                     "profile": {"api_app_id": RHWP_APP}}],
        "response_metadata": {"next_cursor": ""},
    }

    for _ in range(5):
        assert probe.app_ids(client, "tyit", [RHWP_USER]) == frozenset({RHWP_APP})

    assert client.users_list.call_count == 1
    assert client.users_info.call_count == 0, "멤버별 조회가 남아 있다"


def test_the_cache_expires_so_a_newly_installed_app_shows_up():
    """TTL 이 없으면 방금 설치한 앱이 프로세스가 사는 동안 계속 「없음」 이다.

    그 화면을 보는 사람은 **방금 넣은 앱을 확인하러 온 사람**이다.
    """
    probe.clear_cache()
    client = Mock()
    client.users_list.return_value = {
        "members": [], "response_metadata": {"next_cursor": ""}
    }

    assert probe.bot_app_map(client, "tyit", now=0.0) == {}
    assert probe.bot_app_map(client, "tyit", now=probe.CACHE_SECONDS - 1) == {}
    assert client.users_list.call_count == 1

    probe.bot_app_map(client, "tyit", now=probe.CACHE_SECONDS + 1)
    assert client.users_list.call_count == 2


def test_a_failed_listing_is_not_cached():
    """실패를 캐시하면 한 번 꼬인 조회가 TTL 동안 「확인 불가」 를 고정한다."""
    probe.clear_cache()
    client = Mock()
    client.users_list.side_effect = [
        RuntimeError("ratelimited"),
        {"members": [], "response_metadata": {"next_cursor": ""}},
    ]

    assert probe.bot_app_map(client, "tyit", now=0.0) is None
    # **다시 물어야 한다.** 값만 보면 캐시된 빈 지도와 구분되지 않는다 —
    # 실패 직후 빈 지도를 캐시해도 두 번째 호출이 `{}` 를 돌려주기 때문이다.
    assert probe.bot_app_map(client, "tyit", now=1.0) == {}
    assert client.users_list.call_count == 2


def test_two_workspaces_do_not_share_one_app_map():
    probe.clear_cache()
    client = Mock()
    client.users_list.return_value = {
        "members": [], "response_metadata": {"next_cursor": ""}
    }

    probe.bot_app_map(client, "tyit", now=0.0)
    probe.bot_app_map(client, "mgmt", now=0.0)

    assert client.users_list.call_count == 2


def test_an_unreadable_console_db_is_not_reported_as_unregistered(monkeypatch):
    """DB 장애가 「콘솔에 등록하세요」 안내로 둔갑하지 않는다.

    그 안내를 받은 사람은 이미 등록된 것을 또 등록하러 가고, 거기서 멈춘다.
    """
    monkeypatch.setattr(
        probe, "_connections",
        lambda workspace: (_ for _ in ()).throw(RuntimeError("db down")),
    )
    monkeypatch.setattr(probe, "_hermes_route_mode", lambda workspace: "disabled")

    found = {b.role: b for b in probe.bindings("tyit", converter_ids=[], delegated=False)}

    assert found[ca.ARCHIVER].missing_code == ca.DB_UNREADABLE
    assert found[ca.TYBOT].missing_code == ca.DB_UNREADABLE
    assert "등록" not in found[ca.ARCHIVER].invite_hint

    got = _by_role(ca.evaluate(ca.ChannelAppFacts(
        bindings=found_tuple(found), member_ids=frozenset({ARCHIVER_USER}),
    )))
    assert got[ca.ARCHIVER].state == ca.UNKNOWN
    assert got[ca.ARCHIVER].code == ca.DB_UNREADABLE


def found_tuple(mapping) -> tuple:
    return tuple(mapping[role] for role in ca.ROLES)


def test_an_unreadable_hermes_route_defaults_to_requiring_a_slack_app(monkeypatch):
    """모르는 상태에서 「외부 RPC 라 해당 없음」 이라고 적지 않는다.

    그렇게 적으면 정말로 빠진 Slack 연결이 그 문장 뒤에 숨는다. 막는 쪽이
    기본값이다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert probe._hermes_route_mode("tyit") == "disabled"
    assert ca.hermes_binding(None, route_mode="disabled").required is True


def test_the_probe_marks_the_app_id_lookup_as_unresolved_on_failure():
    probe.clear_cache()
    client = Mock()
    client.conversations_members.return_value = {
        "members": [RHWP_USER], "response_metadata": {"next_cursor": ""}
    }
    client.users_list.side_effect = RuntimeError("ratelimited")

    facts = probe.facts(client, "tyit", "C1")

    assert facts.member_ids == frozenset({RHWP_USER})
    assert facts.member_app_ids is None
