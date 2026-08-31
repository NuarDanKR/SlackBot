from __future__ import annotations

from unittest.mock import Mock

import pytest

from tybot.channel_management import (
    ChannelNameError,
    ChannelOwnerStore,
    ChannelRequest,
    build_channel_name,
    create_modal,
    request_from_view,
)
from tybot.slack.pilot import WorkspaceBot


def test_build_channel_name_normalizes_people_friendly_input():
    assert (
        build_channel_name("팀", " 전산 ", "abb110", " 주간 회의 ")
        == "팀-전산_ABB110-주간-회의"
    )


@pytest.mark.parametrize(
    "args,block",
    [
        (("부서", "전산", "ABB110", "회의"), "prefix"),
        (("팀", "전산_운영", "ABB110", "회의"), "org_name"),
        (("팀", "전산", "ABB-110", "회의"), "org_code"),
        (("팀", "전산", "ABB110", ""), "task"),
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
                "org_name": {"org_name": {"value": "김해외동"}},
                "org_code": {"org_code": {"value": "180182"}},
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
    client.conversations_create.return_value = {
        "channel": {"id": "C1", "name": "팀-전산_abb110-주간회의"}
    }
    request = ChannelRequest("팀", "전산", "ABB110", "주간회의", "private", ("U2",))

    bot._create_channel(client, "U1", request)

    client.conversations_create.assert_called_once_with(
        name="팀-전산_abb110-주간회의", is_private=True
    )
    client.conversations_invite.assert_called_once_with(channel="C1", users="U1,U2")
    assert bot.channel_owners.is_owner("it", "C1", "U1")


def test_rename_rechecks_owner_before_using_bot_permission(tmp_path):
    bot = _bot(tmp_path)
    client = Mock()

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
