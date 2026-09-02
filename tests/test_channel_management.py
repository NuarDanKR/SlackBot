from __future__ import annotations

from unittest.mock import Mock

import pytest

from tybot.channel_management import (
    ChannelNameError,
    ChannelOwnerStore,
    ChannelRequest,
    build_channel_name,
    create_modal,
    parse_tasks,
    rename_modal,
    request_from_view,
    requests_from_view,
)
from tybot.channels import ChannelSpec
from tybot.orgsearch import OrgHit, option
from tybot.slack.pilot import WorkspaceBot


def test_build_channel_name_normalizes_people_friendly_input():
    assert (
        build_channel_name("본사팀", " 전산 ", "abb110", " 주간 회의 ")
        == "팀-전산_ABB110-주간-회의"
    )


@pytest.mark.parametrize(
    "args,block",
    [
        (("부서", "전산", "ABB110", "회의"), "prefix"),
        (("본사팀", "전산_운영", "ABB110", "회의"), "org_name"),
        (("본사팀", "전산", "ABB-110", "회의"), "org_code"),
        (("본사팀", "전산", "ABB110", ""), "task"),
    ],
)
def test_build_channel_name_rejects_nonstandard_input(args, block):
    with pytest.raises(ChannelNameError) as exc:
        build_channel_name(*args)
    assert exc.value.block_id == block


def test_create_modal_uses_korean_labels_and_private_default():
    modal = create_modal('{"user_id":"U1"}')
    assert modal["callback_id"] == "tybot_create_channel"
    assert modal["title"]["text"] == "업무 채널 만들기"
    visibility = next(b for b in modal["blocks"] if b["block_id"] == "visibility")
    assert visibility["element"]["initial_option"]["value"] == "private"


def test_request_from_view_reads_create_fields():
    view = {
        "state": {
            "values": {
                "prefix": {"prefix": {"selected_option": {"value": "현장"}}},
                "org": {"org": {"selected_option": option(OrgHit("180182", "김해외동"))}},
                "task": {"task": {"value": "채팅방"}},
                "visibility": {
                    "visibility": {"selected_option": {"value": "private"}}
                },
                "members": {"members": {"selected_users": ["U2", "U3"]}},
            }
        }
    }
    request = request_from_view(view, include_channel_options=True)
    assert request.name == "현장-김해외동_180182-채팅방"
    assert request.visibility == "private"
    assert request.members == ("U2", "U3")


def test_owner_store_isolated_by_workspace_and_channel(tmp_path):
    store = ChannelOwnerStore(tmp_path / "channel-owners.json")
    store.record("it", "C1", "U1", "팀-전산_ABB110-회의")

    assert store.is_owner("it", "C1", "U1")
    assert not store.is_owner("finance", "C1", "U1")
    assert not store.is_owner("it", "C1", "U2")


def _bot(tmp_path) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "it"
    bot.channel_admin_users = set()
    bot.channel_owners = ChannelOwnerStore(tmp_path / "channel-owners.json")
    bot._chan_cache = {}
    return bot


def test_create_private_channel_records_owner_and_invites_requester(tmp_path):
    bot = _bot(tmp_path)
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "D1"}}
    client.conversations_create.return_value = {
        "channel": {"id": "C1", "name": "팀-전산_abb110-주간회의"}
    }
    request = ChannelRequest("본사팀", "전산", "ABB110", "주간회의", "private", ("U2",))

    bot._create_channel(client, "U1", request)

    client.conversations_create.assert_called_once_with(
        name="팀-전산_abb110-주간회의", is_private=True
    )
    client.conversations_invite.assert_called_once_with(channel="C1", users="U1,U2")
    client.conversations_open.assert_called_once_with(users="U1")
    assert bot.channel_owners.is_owner("it", "C1", "U1")


def test_rename_rechecks_owner_before_using_bot_permission(tmp_path):
    bot = _bot(tmp_path)
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "D1"}}

    bot._rename_channel(client, "U2", "C1", "팀-전산_ABB110-변경")

    client.conversations_rename.assert_not_called()
    client.chat_postMessage.assert_called_once()


def test_channel_admin_can_rename_without_archive_read_privilege(tmp_path):
    bot = _bot(tmp_path)
    bot.channel_admin_users = {"U9"}
    client = Mock()
    client.conversations_rename.return_value = {
        "channel": {"id": "C1", "name": "팀-전산_abb110-변경"}
    }

    bot._rename_channel(client, "U9", "C1", "팀-전산_ABB110-변경")

    client.conversations_rename.assert_called_once_with(
        channel="C1", name="팀-전산_abb110-변경"
    )
    assert bot._chan_cache["C1"] == "#팀-전산_abb110-변경"


# --- 생성 안내 문구 --------------------------------------------------------
# 자주 나오는 오해: "TYBot 으로 만든 채널만 수집된다". 사실이 아니다 - 수집은 채널 이름이
# 정한다. 참여자 전원이 보는 안내에 사실과 다른 문장을 넣으면 안 된다.
def test_notice_says_the_name_decides_collection():
    from tybot.slack.pilot import CHANNEL_CREATED_NOTICE

    text = CHANNEL_CREATED_NOTICE.format(bot="tybot", visibility="공개")
    assert "채널 이름" in text
    assert "규칙 밖 이름으로 바꾸면" in text


def test_notice_explains_the_private_channel_limitation():
    """비공개는 봇이 자가 참여할 수 없다 - 이 제약이 안내의 실제 알맹이다."""
    from tybot.slack.pilot import CHANNEL_CREATED_NOTICE

    text = CHANNEL_CREATED_NOTICE.format(bot="tybot", visibility="비공개")
    assert "스스로 들어갈 수 없습니다" in text
    assert "/invite @tybot" in text


def test_notice_does_not_claim_only_slash_created_channels_are_collected():
    """사실과 다른 안내를 못 넣게 고정한다."""
    from tybot.slack.pilot import CHANNEL_CREATED_NOTICE

    text = CHANNEL_CREATED_NOTICE.format(bot="tybot", visibility="공개")
    assert "만든 채널만 수집" not in text


def test_notice_warns_about_pii():
    from tybot.slack.pilot import CHANNEL_CREATED_NOTICE

    text = CHANNEL_CREATED_NOTICE.format(bot="tybot", visibility="공개")
    assert "올리지 마세요" in text
    assert "Slack 대화에는 그대로 남습니다" in text

# --- 여러 채널 한 번에 만들기 -------------------------------------------------


def _create_view(task_text: str) -> dict:
    return {
        "state": {
            "values": {
                "prefix": {"prefix": {"selected_option": {"value": "현장"}}},
                "org": {"org": {"selected_option": option(OrgHit("180182", "김해외동"))}},
                "task": {"task": {"value": task_text}},
                "visibility": {"visibility": {"selected_option": {"value": "private"}}},
                "members": {"members": {"selected_users": ["U2"]}},
            }
        }
    }


def test_parse_tasks_splits_on_newlines_and_commas():
    """사람이 붙여넣는 모양을 그대로 받는다 — 줄바꿈이든 쉼표든."""
    assert parse_tasks("주간회의\n안전점검, 기성청구") == ["주간회의", "안전점검", "기성청구"]
    assert parse_tasks("  주간회의  ") == ["주간회의"]


def test_parse_tasks_rejects_empty_input():
    with pytest.raises(ChannelNameError) as e:
        parse_tasks("   \n  ")
    assert e.value.block_id == "task"


def test_parse_tasks_rejects_duplicates_before_creating_anything():
    """같은 이름을 두 번 보내면 Slack 이 두 번째를 거절해 '일부만 생성' 이 된다.

    만들기 전에 막아야 사람이 무엇이 빠졌는지 되짚지 않는다.
    """
    with pytest.raises(ChannelNameError, match="겹칩니다"):
        parse_tasks("주간회의\n주간회의")
    # 앞뒤 공백만 다른 것도 같은 채널이 된다.
    with pytest.raises(ChannelNameError, match="겹칩니다"):
        parse_tasks("주간회의\n  주간회의  ")
    # 반면 가운데 공백은 하이픈이 되어 다른 채널이다. 막지 않는다.
    assert parse_tasks("주간회의\n주간 회의") == ["주간회의", "주간 회의"]


def test_parse_tasks_caps_the_batch():
    """줄바꿈이 잘못 들어간 입력이 그대로 실행되지 않게 한다."""
    with pytest.raises(ChannelNameError, match="10개까지"):
        parse_tasks("\n".join(f"업무{i}" for i in range(11)))


def test_requests_from_view_expands_one_per_task():
    requests = requests_from_view(_create_view("주간회의\n안전점검"))
    assert [r.name for r in requests] == [
        "현장-김해외동_180182-주간회의",
        "현장-김해외동_180182-안전점검",
    ]
    # 조직·공개범위·참여자는 모두 같아야 한다. 그것이 이 기능의 목적이다.
    assert {r.org_code for r in requests} == {"180182"}
    assert {r.visibility for r in requests} == {"private"}
    assert {r.members for r in requests} == {("U2",)}


def test_requests_from_view_still_handles_a_single_task():
    assert len(requests_from_view(_create_view("주간회의"))) == 1


def test_create_modal_takes_several_tasks_but_rename_takes_one():
    """이름 변경은 채널 하나를 고치는 것이다. 여러 줄을 받으면 무엇이 되는지 알 수 없다."""
    create_task = next(
        b for b in create_modal("{}")["blocks"] if b.get("block_id") == "task"
    )
    assert create_task["element"]["multiline"] is True

    spec = ChannelSpec("#현장-김해외동_180182-채팅방", "현장", "김해외동", "180182", "채팅방")
    rename_task = next(
        b for b in rename_modal("{}", spec)["blocks"] if b.get("block_id") == "task"
    )
    assert "multiline" not in rename_task["element"]


def test_section_tip_is_only_for_batches():
    """섹션은 봇이 만들어 줄 수 없다. 한 개 만들 때 붙이면 잔소리만 된다."""
    from tybot.slack.pilot import SECTION_TIP

    assert "섹션으로 이동" in SECTION_TIP
    # 사람마다 따로라는 점을 빠뜨리면, 한 사람이 정리하면 다 되는 줄 안다.
    assert "사람마다 따로" in SECTION_TIP


def test_section_tip_does_not_promise_the_bot_will_do_it():
    """할 수 없는 것을 할 수 있다고 적으면 문의가 늘어난다."""
    from tybot.slack.pilot import SECTION_TIP

    for lie in ("섹션을 만들었", "섹션에 넣었", "자동으로 묶"):
        assert lie not in SECTION_TIP
