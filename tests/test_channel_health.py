"""`/채널 상태` · `/채널 수정` (2026-09-08).

채널이 제대로 물려 있는지 알려면 명령을 세 개 쳐야 했다 — 그래서 아무도 다
확인하지 않았다. 넷 다 **틀려도 오류가 안 난다**: 이름이 규칙 밖이면 조용히 수집이
안 되고, 봇이 초대되지 않으면 조용히 비어 있고, 검토자가 없으면 조용히 요약이
반영되지 않고, 스캔 첨부는 조용히 안 읽힌다.
"""
from __future__ import annotations

import pytest

from tybot import channel_health as ch
from tybot.channel_management import (
    ChannelNameError,
    edit_from_view,
    edit_modal,
)
from tybot.channels import parse

GOOD = "#팀-전산_ABB110-주간회의"


def _facts(**kw) -> ch.HealthFacts:
    base = {
        "channel": GOOD,
        "channel_id": "C1",
        "is_member": True,
        "raw_lines": 120,
        "last_ingested": "2026-09-08T17:00+09:00",
        "reviewers": ["U1"],
        "send_at": "09:00",
        "waiting_attachments": 0,
        "last_digest": "2026-09-10",
    }
    base.update(kw)
    return ch.HealthFacts(**base)


def _find(facts, label: str) -> ch.Check:
    return next(c for c in ch.checks(facts) if c.label == label)


# --- 조용한 고장 넷 -----------------------------------------------------------
def test_a_healthy_channel_says_so():
    assert all(c.healthy for c in ch.checks(_facts()))
    assert "모두 정상" in ch.report(_facts())


def test_a_nonstandard_name_is_reported_as_blocking():
    """이름이 규칙 밖이면 **아무 일도 일어나지 않는다.** 오류도 안 난다."""
    check = _find(_facts(channel="#잡담방"), "이름 규칙")

    assert check.mark == ch.BAD
    assert "수집되지 않습니다" in check.detail
    assert "/채널 수정" in check.fix, "무엇을 하면 되는지 말해야 한다"


def test_a_private_channel_without_the_bot_says_slack_cannot_help():
    """비공개 채널에는 봇이 스스로 못 들어간다 — 기다려도 안 된다."""
    check = _find(_facts(is_member=False, is_private=True), "봇 참여")

    assert check.mark == ch.BAD
    assert "/invite" in check.fix


def test_a_public_channel_without_the_bot_is_only_a_warning():
    """공개 채널은 자동으로 들어간다. 빨강으로 칠하면 진짜 빨강이 묻힌다."""
    assert _find(_facts(is_member=False), "봇 참여").mark == ch.WARN


def test_a_blocked_write_path_is_reported_even_while_collecting():
    """이름·참여가 다 맞아도 **쓰기가 막히면 대화가 남지 않는다.**"""
    check = _find(_facts(write_problems={"아카이브": "권한 없음"}), "수집")

    assert check.mark == ch.BAD
    assert "권한 없음" in check.detail


def test_no_reviewer_is_reported_as_blocking():
    """검토자가 없으면 요약이 반영되지 않고, 읽지 못한 첨부도 아무에게도 안 간다."""
    check = _find(_facts(reviewers=[]), "검토자")

    assert check.mark == ch.BAD
    assert "/채널 수정" in check.fix


def test_an_unreadable_reviewer_db_is_not_reported_as_none():
    """모르는 것을 「없음」 이라 하면, 사람이 멀쩡한 설정을 다시 만든다."""
    check = _find(_facts(reviewers=None), "검토자")

    assert check.mark == ch.UNKNOWN
    assert "확인하지 못했습니다" in check.detail


# --- 첨부는 검토자 유무로 뜻이 달라진다 --------------------------------------
def test_waiting_attachments_with_a_reviewer_are_on_their_way():
    check = _find(_facts(waiting_attachments=3), "첨부")

    assert check.mark == ch.WARN
    assert "09:00" in check.fix, "언제 가는지 말해야 사람이 기다릴 수 있다"


def test_waiting_attachments_without_a_reviewer_go_nowhere():
    """받을 사람이 없으면 그 파일들은 **영영 안 읽힌다.** 그건 경고가 아니라 고장이다."""
    check = _find(_facts(waiting_attachments=3, reviewers=[]), "첨부")

    assert check.mark == ch.BAD
    assert "받을 사람이 없습니다" in check.detail


# --- 화면 --------------------------------------------------------------------
def test_the_headline_states_the_conclusion():
    """목록만 주면 사람이 초록·빨강을 세어야 하는데, 바쁠 때는 안 센다."""
    text = ch.report(_facts(channel="#잡담방", reviewers=[]))

    assert text.splitlines()[0].startswith(ch.BAD)
    assert "가지가 막혀" in text.splitlines()[0]


def test_a_dm_says_where_to_run_it():
    assert ch.report(_facts(is_dm=True)) == ch.DM_NOTICE


def test_the_report_names_the_command_that_fixes_things():
    assert "/채널 수정" in ch.report(_facts())


# --- 수정 화면 ---------------------------------------------------------------
def test_the_edit_modal_opens_for_a_nonstandard_name():
    """전에는 여기서 막았다 — 정확히 **이름을 고쳐야 하는 채널**을 못 고치게 했다."""
    modal = edit_modal("{}", spec=None, current_name="#잡담방")

    head = modal["blocks"][0]["text"]["text"]
    assert "수집되지 않습니다" in head, "왜 고쳐야 하는지 말해야 한다"


def test_the_name_fields_are_optional():
    """검토자만 바꾸려는 사람에게 조직 검색을 강요하면 그 사람은 지정을 포기한다."""
    modal = edit_modal("{}", spec=parse(GOOD), current_name=GOOD)

    inputs = {b["block_id"]: b.get("optional") for b in modal["blocks"]
              if b["type"] == "input"}
    assert inputs["task"] is True
    assert inputs["reviewers"] is True
    assert inputs["send_at"] is True


def test_current_reviewers_are_prefilled():
    """비어 있는 채로 열면 저장할 때 기존 검토자가 지워진다."""
    modal = edit_modal("{}", spec=parse(GOOD), reviewers=("U1", "U2"), send_at="10:30")

    blocks = {b["block_id"]: b for b in modal["blocks"] if b["type"] == "input"}
    assert blocks["reviewers"]["element"]["initial_users"] == ["U1", "U2"]
    assert blocks["send_at"]["element"]["initial_time"] == "10:30"


def test_channel_managers_are_only_shown_to_people_who_can_delegate():
    hidden = edit_modal("{}", spec=parse(GOOD))
    shown = edit_modal("{}", spec=parse(GOOD), managers=("U2", "U3"))

    assert not any(b.get("block_id") == "channel_managers" for b in hidden["blocks"])
    block = next(b for b in shown["blocks"] if b.get("block_id") == "channel_managers")
    assert block["element"]["initial_users"] == ["U2", "U3"]


# --- 제출 읽기 ---------------------------------------------------------------
def _view(
    *, org="", task="", reviewers=None, send_at="", with_reviewer_block=True,
    managers=None, with_manager_block=False,
):
    from tybot.orgsearch import OrgHit, option

    state: dict = {
        "prefix": {"prefix": {"selected_option": {"value": "본사팀"}}},
        "task": {"task": {"value": task}},
    }
    if org:
        state["org_team"] = {"org": {"selected_option": option(OrgHit("ABB110", org))}}
    else:
        state["org_team"] = {"org": {}}
    if with_reviewer_block:
        state["reviewers"] = {"reviewers": {"selected_users": list(reviewers or [])}}
    if send_at:
        state["send_at"] = {"send_at": {"selected_time": send_at}}
    if with_manager_block:
        state["channel_managers"] = {
            "channel_managers": {"selected_users": list(managers or [])}
        }
    return {"state": {"values": state}}


def test_only_reviewers_changed_means_no_rename():
    edit = edit_from_view(_view(reviewers=["U1"], send_at="09:00"))

    assert not edit.renames
    assert edit.reviewers == ("U1",)
    assert edit.send_at == "09:00"


def test_a_full_name_is_assembled():
    edit = edit_from_view(_view(org="전산", task="주간회의", reviewers=["U1"]))

    assert edit.renames
    assert "전산" in edit.name and "ABB110" in edit.name


def test_half_a_name_is_refused_not_ignored():
    """조용히 무시하면 사람은 바꿨다고 믿고 나간다."""
    with pytest.raises(ChannelNameError):
        edit_from_view(_view(org="전산"))
    with pytest.raises(ChannelNameError):
        edit_from_view(_view(task="주간회의"))


def test_an_empty_reviewer_field_clears_them():
    """비우고 저장하는 것은 「전부 해제」 다. 화면 힌트가 그렇게 말한다."""
    edit = edit_from_view(_view(reviewers=[]))

    assert edit.clear_reviewers
    assert edit.reviewers == ()


def test_a_screen_without_the_reviewer_block_does_not_clear_them():
    """옛 화면의 제출을 「전부 해제」 로 읽으면 검토가 조용히 멈춘다."""
    edit = edit_from_view(_view(task="주간회의", org="전산", with_reviewer_block=False))

    assert not edit.clear_reviewers


def test_channel_managers_are_read_only_when_the_block_was_shown():
    changed = edit_from_view(_view(managers=["U2"], with_manager_block=True))
    hidden = edit_from_view(_view())
    cleared = edit_from_view(_view(managers=[], with_manager_block=True))

    assert changed.managers == ("U2",)
    assert not changed.clear_managers
    assert hidden.managers == () and not hidden.clear_managers
    assert cleared.clear_managers


# --- 자기가 만든 채널을 자기가 못 고쳤다 -------------------------------------
#
# A 가 Slack 에서 직접 만든 채널을 A 가 `/채널 이름변경` 하면 「TYBot 이 만든 게
# 아니라서 안 된다」 고 막혔다. `channel_owners` 는 **우리 생성 기록**이라 우리가
# 만들지 않은 채널에는 아무 줄도 없다. 권한이 아니라 고장이다.
class _Client:
    def __init__(self, creator="", boom=False):
        self.creator = creator
        self.boom = boom
        self.calls = 0

    def conversations_info(self, channel):
        self.calls += 1
        if self.boom:
            raise RuntimeError("slack down")
        return {"channel": {"id": channel, "creator": self.creator}}


def _bot(*, creator="", admins=(), owner_of="", boom=False):
    """`WorkspaceBot` 을 세우지 않고 판정 함수만 부른다(Bolt App 은 토큰을 검증한다)."""
    from types import SimpleNamespace

    client = _Client(creator, boom=boom)
    return SimpleNamespace(
        workspace="tyit",
        channel_admin_users=set(admins),
        channel_owners=SimpleNamespace(
            # `is_manager` = 개설자 또는 위임된 수정 담당자. 권한 판정이 보는
            # 것은 이쪽이다 — `is_owner` 만 두면 위임된 사람이 빠진다.
            is_manager=lambda ws, ch, uid: uid == owner_of,
            is_owner=lambda ws, ch, uid: uid == owner_of,
            managers_of=lambda ws, ch: (),
            owner_of=lambda ws, ch: owner_of,
        ),
        app=SimpleNamespace(client=client),
        _is_workspace_admin=lambda uid: False,
        _client=client,
    )


def _may(bot, user_id, channel_id="C1"):
    from tybot.slack.pilot import WorkspaceBot

    bot._slack_creator = lambda ch: WorkspaceBot._slack_creator(bot, ch)
    return WorkspaceBot._can_manage_channel(bot, channel_id, user_id)


def test_the_slack_creator_can_edit_a_channel_tybot_did_not_create():
    """실제로 막혔던 경우 — 우리 기록에는 없지만 Slack 은 만든 사람을 안다."""
    bot = _bot(creator="UA")

    assert _may(bot, "UA"), "자기가 만든 채널을 자기가 못 고쳤다"


def test_someone_else_still_cannot_edit():
    """생성자를 되묻는 것은 권한을 넓히는 것이 아니다."""
    assert not _may(_bot(creator="UA"), "UB")


def test_our_own_owner_record_still_counts():
    """TYBot 이 만든 채널은 Slack 을 묻지 않고도 통과해야 한다."""
    bot = _bot(creator="", owner_of="UA")

    assert _may(bot, "UA")
    assert bot._client.calls == 0, "필요 없는 Slack 호출을 했다"


def test_a_delegated_tybot_manager_can_edit():
    bot = _bot(creator="UA")
    bot.channel_owners.is_manager = lambda ws, ch, uid: uid == "UMANAGER"

    assert _may(bot, "UMANAGER")
    assert bot._client.calls == 0


def test_a_workspace_admin_can_edit_without_environment_allowlist():
    bot = _bot(creator="UA")
    bot._is_workspace_admin = lambda uid: uid == "UADMIN"

    assert _may(bot, "UADMIN")


def test_a_delegated_manager_cannot_delegate_again():
    from tybot.slack.pilot import WorkspaceBot

    bot = _bot(creator="UCREATOR")
    bot.channel_owners.is_manager = lambda ws, ch, uid: uid == "UMANAGER"
    bot.channel_owners.is_owner = lambda ws, ch, uid: False

    assert _may(bot, "UMANAGER")
    assert not WorkspaceBot._can_delegate_channel_manager(bot, "C1", "UMANAGER")


def test_workspace_admin_can_delegate_for_any_channel():
    from tybot.slack.pilot import WorkspaceBot

    bot = _bot(creator="UCREATOR")
    bot._slack_creator = lambda channel_id: "UCREATOR"
    bot._is_workspace_admin = lambda uid: uid == "UADMIN"

    assert WorkspaceBot._can_delegate_channel_manager(bot, "C1", "UADMIN")


def test_a_regular_member_is_not_treated_as_workspace_admin():
    bot = _bot(creator="UA")
    bot._is_workspace_admin = lambda uid: False

    assert not _may(bot, "UMEMBER")


def test_workspace_admin_fields_are_read_from_slack_and_cached():
    from tybot.slack.pilot import WorkspaceBot

    bot = _bot(creator="UA")
    bot.app.client.users_info = lambda user: {
        "user": {"id": user, "is_admin": user == "UADMIN"}
    }

    assert WorkspaceBot._is_workspace_admin(bot, "UADMIN")
    assert not WorkspaceBot._is_workspace_admin(bot, "UMEMBER")
    assert bot._workspace_admin_cache == {"UADMIN": True, "UMEMBER": False}


def test_a_slack_failure_does_not_widen_permission():
    assert not _may(_bot(creator="UA", boom=True), "UA")


def test_a_slack_failure_is_not_cached():
    """일시 오류를 캐시하면 그 채널이 **영구히** 잠긴다."""
    bot = _bot(creator="UA", boom=True)

    _may(bot, "UA")
    _may(bot, "UA")

    assert bot._client.calls == 2, "실패를 캐시해 다시 묻지 않았다"


def test_the_creator_is_looked_up_once():
    """권한 판정마다 API 를 부르면 버튼 하나에 호출이 여러 번 나간다."""
    bot = _bot(creator="UA")

    _may(bot, "UB")
    _may(bot, "UB")

    assert bot._client.calls == 1


# --- 화면이 자기 모순이던 것 (2026-09-08 실측) --------------------------------
#
# `/채널 수정` 은 「생성자 또는 TYBot 채널 관리자만」 이라고 거절하는데, 같은
# 화면의 관리 항목은 🟢 「개설자 또는 채널 관리자가 수정할 수 있습니다」 로 떴다.
# 「누군가는 고칠 수 있다」 를 보였기 때문이다. 화면이 자기 모순이면 사람은
# 화면을 안 믿는다.
def test_a_viewer_without_permission_is_told_so():
    check = _find(_facts(viewer_can_edit=False, owner="UA"), "관리")

    assert check.mark != ch.OK
    assert "권한이 없습니다" in check.detail
    assert "UA" in check.detail, "누구에게 요청할지 말해야 한다"


def test_a_channel_nobody_can_edit_is_a_failure_not_a_warning():
    """개설 기록도 없고 관리자도 없으면 그 채널은 **영영 못 고친다.**"""
    check = _find(_facts(viewer_can_edit=False, owner="", admin_exists=False), "관리")

    assert check.mark == ch.BAD
    assert "CHANNEL_ADMIN_USERS" in check.fix, "서버에서 무엇을 해야 하는지 말해야 한다"


def test_an_admin_exists_is_only_a_warning():
    check = _find(_facts(viewer_can_edit=False, admin_exists=True), "관리")

    assert check.mark == ch.WARN


def test_a_powerless_viewer_is_not_sent_to_a_command_that_will_refuse():
    """막다른 안내는 「이 봇은 안 된다」 로 읽힌다."""
    check = _find(
        _facts(waiting_attachments=3, reviewers=[], viewer_can_edit=False), "첨부"
    )

    assert "/채널 수정" not in check.fix
    assert "요청" in check.fix


def test_the_screen_and_the_edit_command_share_one_verdict():
    """따로 판정하면 갈린다 — 갈린 것이 실제로 화면에 나갔다."""
    import inspect

    from tybot.slack.pilot import WorkspaceBot

    source = inspect.getsource(WorkspaceBot._health_facts)

    assert "_can_manage_channel" in source, "화면이 권한을 따로 센다"


def test_the_bot_is_never_shown_as_the_owner():
    """TYBot 이 만든 채널은 Slack 상 생성자가 **봇 자신**이다.

    그 값을 개설자로 보이면 「<@TYBot> 에게 요청하세요」 가 된다.
    """
    from types import SimpleNamespace

    from tybot.slack.pilot import WorkspaceBot

    bot = _bot(creator="UBOT")
    bot.channel_owners = SimpleNamespace(
        is_manager=lambda ws, ch, uid: False,
        is_owner=lambda ws, ch, uid: False,
        managers_of=lambda ws, ch: (),
        owner_of=lambda ws, ch: "",
    )
    bot._bot_uid = "UBOT"
    bot._bot_user_id = lambda: "UBOT"
    bot._slack_creator = lambda ch: WorkspaceBot._slack_creator(bot, ch)

    assert WorkspaceBot._human_owner(bot, "C1") == ""


def test_our_own_record_wins_over_slack():
    """우리 생성 기록이 먼저다 — 봇이 만든 채널의 실제 요청자가 거기 있다."""
    from types import SimpleNamespace

    from tybot.slack.pilot import WorkspaceBot

    bot = _bot(creator="UBOT")
    bot.channel_owners = SimpleNamespace(
        is_manager=lambda ws, ch, uid: False,
        is_owner=lambda ws, ch, uid: False,
        managers_of=lambda ws, ch: (),
        owner_of=lambda ws, ch: "UA",
    )
    bot._bot_uid = "UBOT"
    bot._bot_user_id = lambda: "UBOT"
    bot._slack_creator = lambda ch: WorkspaceBot._slack_creator(bot, ch)

    assert WorkspaceBot._human_owner(bot, "C1") == "UA"
    assert bot._client.calls == 0, "기록이 있는데 Slack 을 물었다"


# --- 생성 시점에 검토자를 정한다 (2026-09-08) --------------------------------
#
# `/채널 생성` 에 검토자 칸이 없어서, 만들어진 채널은 모두 검토자 없는 상태로
# 시작했다. 그 채널은 요약이 반영되지 않고 읽지 못한 첨부도 아무에게도 가지
# 않는다 — 둘 다 오류 없이 조용하다.
def _create_view(*, reviewers=("U1",), send_at="09:00", with_block=True):
    from tybot.orgsearch import OrgHit, option

    state = {
        "prefix": {"prefix": {"selected_option": {"value": "본사팀"}}},
        "org_team": {"org": {"selected_option": option(OrgHit("ABB110", "전산"))}},
        "task": {"task": {"value": "주간회의"}},
        "visibility": {"visibility": {"selected_option": {"value": "private"}}},
        "members": {"members": {"selected_users": []}},
    }
    if with_block:
        state["reviewers"] = {"reviewers": {"selected_users": list(reviewers)}}
        state["send_at"] = {"send_at": {"selected_time": send_at}}
    return {"state": {"values": state}}


def test_the_create_modal_asks_for_a_reviewer():
    from tybot.channel_management import create_modal

    modal = create_modal("{}", default_reviewer="UME")

    block = next(
        b for b in modal["blocks"]
        if b.get("block_id") == "reviewers"
    )
    assert not block.get("optional"), "선택으로 두면 안 정한 채널이 쌓인다"
    assert block["element"]["initial_users"] == ["UME"], "기본값은 만든 사람 자신"


def test_creating_without_a_reviewer_is_refused():
    """빈 칸으로 만들어지면 그 채널은 조용히 검토 밖에 놓인다."""
    from tybot.channel_management import request_from_view

    with pytest.raises(ChannelNameError) as got:
        request_from_view(_create_view(reviewers=()), include_channel_options=True)

    assert got.value.block_id == "reviewers"


def test_the_reviewer_rides_along_to_every_channel():
    """업무명을 여러 줄 적으면 그만큼 만든다 — 검토자도 다 붙어야 한다."""
    from tybot.channel_management import requests_from_view

    view = _create_view(reviewers=("U1", "U2"))
    view["state"]["values"]["task"]["task"]["value"] = "주간회의\n안전점검"

    made = requests_from_view(view)

    assert len(made) == 2
    for request in made:
        assert request.reviewers == ("U1", "U2")
        assert request.send_at == "09:00"


def test_the_rename_path_does_not_break_on_the_new_fields():
    """검토자 칸이 없는 화면의 제출도 이름은 읽혀야 한다.

    생성 화면에만 있는 값을 `if` 안에서만 만들면 여기서 UnboundLocalError 로
    터진다 — 실제로 그렇게 터졌다.
    """
    from tybot.channel_management import request_from_view

    request = request_from_view(
        _create_view(with_block=False), include_channel_options=False
    )

    assert request.name == "팀-전산_ABB110-주간회의"
    assert request.reviewers == ()


def test_creation_stores_the_reviewer_and_says_when_it_could_not():
    """채널은 만들어졌는데 검토가 안 물린 상태를 삼키면 안 된다."""
    import inspect

    from tybot.slack.pilot import WorkspaceBot

    source = inspect.getsource(WorkspaceBot._create_channel)

    assert "set_reviewers" in source, "생성 시점에 저장하지 않는다"
    assert "reviewer_error" in source, "저장 실패를 사용자에게 말하지 않는다"


# --- 검토 DM 이 실제로 나가는가 ------------------------------------------------
#
# 2026-09-11: 검토자는 지정돼 있고 화면은 초록이었는데 DM 이 한 건도 안 갔다. 원인이
# 둘이었다 — 이력 표에 봇 권한이 없었고, 타이머가 enable 조차 안 돼 있었다.
#
# 검토자 지정은 사람이 하는 일이고 발송은 서버가 하는 일이다. 둘은 따로 고장나므로
# 초록 하나로 둘을 다 말하게 두지 않는다.
def test_never_sent_is_reported_even_with_a_reviewer():
    check = _find(_facts(last_digest=""), "검토 DM")
    assert check.mark == ch.BAD
    assert "한 번도 나가지 않았습니다" in check.detail
    # 원인은 봇이 알 수 없다. 어디를 볼지만 말한다.
    assert "tybot-review-dm" in check.fix


def test_sent_recently_is_healthy():
    assert _find(_facts(last_digest="2026-09-10"), "검토 DM").healthy


def test_unknown_history_is_not_green():
    """못 읽은 것과 안 간 것은 다르다. 조치도 다르다."""
    check = _find(_facts(last_digest=None), "검토 DM")
    assert check.mark == ch.UNKNOWN
    assert not check.healthy


def test_no_reviewer_does_not_double_report():
    """검토자가 없으면 안 가는 게 당연하다. `검토자` 항목이 이미 빨강이다."""
    facts = _facts(reviewers=[], last_digest="")
    assert _find(facts, "검토자").mark == ch.BAD
    assert _find(facts, "검토 DM").healthy


def test_reviewer_set_today_is_not_yet_a_failure():
    """방금 정했으면 아직 안 간 것이 정상이다."""
    from datetime import datetime

    today = datetime.now(ch.KST).date().isoformat()
    facts = _facts(last_digest="", reviewer_since=today)
    assert _find(facts, "검토 DM").healthy


def test_reviewer_set_long_ago_and_never_sent_is_a_failure():
    facts = _facts(last_digest="", reviewer_since="2026-01-01")
    assert _find(facts, "검토 DM").mark == ch.BAD
