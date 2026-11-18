from types import SimpleNamespace

import pytest

from scripts import invite_archiver_to_master_channels as invite


def _page(channels, cursor=""):
    return {"channels": channels, "response_metadata": {"next_cursor": cursor}}


class Client:
    def __init__(self, pages, *, user_id="", failures=None):
        self.pages = list(pages)
        self.user_id = user_id
        self.failures = failures or {}
        self.list_calls = []
        self.invites = []

    def users_conversations(self, **kwargs):
        self.list_calls.append(kwargs)
        return self.pages[len(self.list_calls) - 1]

    def auth_test(self):
        return {"user_id": self.user_id}

    def conversations_invite(self, *, channel, users):
        self.invites.append((channel, users))
        error = self.failures.get(channel)
        if error:
            exc = RuntimeError(error)
            exc.response = {"error": error}
            raise exc
        return {"ok": True}


def test_dry_run_finds_master_only_channels_without_inviting():
    master = Client([_page([
        {"id": "C1", "name": "standard"},
        {"id": "C2", "name": "이름규칙밖"},
        {"id": "D1", "is_im": True},
    ])])
    archiver = Client([_page([{"id": "C1", "name": "standard"}])], user_id="U_ARCH")

    result = invite.invite_missing(master, archiver, apply=False)

    assert result.candidates == 1
    assert result.already_joined == 1
    assert master.invites == []


def test_apply_invites_archiver_identity_to_every_missing_channel():
    master = Client([
        _page([{"id": "C1", "name": "public"}], cursor="next"),
        _page([{"id": "G2", "name": "private", "is_private": True}]),
    ])
    archiver = Client([_page([])], user_id="U_ARCH")

    result = invite.invite_missing(master, archiver, apply=True, live_switch_on=True)

    assert result.invited == 2
    assert master.invites == [("C1", "U_ARCH"), ("G2", "U_ARCH")]


def test_one_failure_does_not_prevent_the_remaining_invites():
    master = Client([_page([{"id": "C1"}, {"id": "C2"}])], failures={"C1": "no_permission"})
    archiver = Client([_page([])], user_id="U_ARCH")

    result = invite.invite_missing(master, archiver, apply=True, live_switch_on=True)

    assert result.invited == 1
    assert result.failed == (("C1", "no_permission"),)
    assert master.invites == [("C1", "U_ARCH"), ("C2", "U_ARCH")]


def test_archiver_identity_is_required_before_writing():
    master = Client([_page([{"id": "C1"}])])
    archiver = Client([_page([])])

    try:
        invite.invite_missing(master, archiver, apply=True, live_switch_on=True)
    except RuntimeError as exc:
        assert "user_id" in str(exc)
    else:
        raise AssertionError("missing Archiver identity must stop invitations")


def test_clients_use_console_secret_boundary_not_archiver_runtime_role():
    created = []
    token_calls = []

    def client_factory(*, token):
        created.append(token)
        return token

    def token_loader(workspace, bot_key):
        token_calls.append((workspace, bot_key))
        return "xoxb-archiver"

    clients = invite._clients(
        "tyit",
        client_factory=client_factory,
        master_loader=lambda: [
            SimpleNamespace(key="tyit", bot_token="xoxb-master")
        ],
        token_loader=token_loader,
    )

    assert clients == ("xoxb-master", "xoxb-archiver")
    assert created == ["xoxb-master", "xoxb-archiver"]
    assert token_calls == [("tyit", "archiver")]


def test_invite_script_does_not_use_the_archiver_runtime_database_role():
    source = invite.Path(invite.__file__).read_text(encoding="utf-8")

    assert "archiver_runtime_store" not in source
    assert "load_runtime_config" not in source


def test_operator_held_channels_are_not_invited():
    """사람이 멈춘 채널은 초대해도 paused 로 남는다 — 아예 초대하지 않고 따로 센다."""
    master = Client([_page([{"id": "C1"}, {"id": "C2"}])])
    archiver = Client([_page([])], user_id="U_ARCH")

    result = invite.invite_missing(
        master, archiver, apply=True, held_ids=frozenset({"C2"}), live_switch_on=True,
    )

    assert master.invites == [("C1", "U_ARCH")]
    assert (result.candidates, result.held, result.invited) == (1, 1, 1)


@pytest.mark.parametrize("switch", [False, None])
def test_apply_is_refused_unless_the_live_switch_is_known_on(switch):
    """꺼진 채 초대하면 등록이 보류되고 Master 가 계속 쓴다 — 초대한 의미가 없다."""
    master = Client([_page([{"id": "C1"}])])
    archiver = Client([_page([])], user_id="U_ARCH")

    with pytest.raises(invite.LiveSwitchOff):
        invite.invite_missing(master, archiver, apply=True, live_switch_on=switch)
    assert master.invites == []


def test_dry_run_does_not_need_the_live_switch():
    master = Client([_page([{"id": "C1"}])])
    archiver = Client([_page([])], user_id="U_ARCH")

    result = invite.invite_missing(master, archiver, apply=False, live_switch_on=None)

    assert result.candidates == 1 and master.invites == []


def test_collection_state_reads_holds_and_the_switch_with_the_console_repo():
    from fake_archiving_repo import FakeArchivingRepo

    repo = FakeArchivingRepo()
    repo.given_channel("tyit", "C1", "paused", membership="joined", operator_hold=True)
    repo.given_channel("tyit", "C2", "active", owner="archiver",
                       cutover_ts="1700000000.000000", membership="joined")
    repo.given_flag("archiver_writes_live", True)

    held, live = invite._collection_state("tyit", repo=repo)

    assert held == frozenset({"C1"}) and live is True


def test_invite_script_no_longer_speaks_of_shadow_collection():
    source = invite.Path(invite.__file__).read_text(encoding="utf-8").lower()

    assert "shadow" not in source.replace("shadow 수집 폐기", "")
    assert "그림자" not in source
