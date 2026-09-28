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

    result = invite.invite_missing(master, archiver, apply=True)

    assert result.invited == 2
    assert master.invites == [("C1", "U_ARCH"), ("G2", "U_ARCH")]


def test_one_failure_does_not_prevent_the_remaining_invites():
    master = Client([_page([{"id": "C1"}, {"id": "C2"}])], failures={"C1": "no_permission"})
    archiver = Client([_page([])], user_id="U_ARCH")

    result = invite.invite_missing(master, archiver, apply=True)

    assert result.invited == 1
    assert result.failed == (("C1", "no_permission"),)
    assert master.invites == [("C1", "U_ARCH"), ("C2", "U_ARCH")]


def test_archiver_identity_is_required_before_writing():
    master = Client([_page([{"id": "C1"}])])
    archiver = Client([_page([])])

    try:
        invite.invite_missing(master, archiver, apply=True)
    except RuntimeError as exc:
        assert "user_id" in str(exc)
    else:
        raise AssertionError("missing Archiver identity must stop invitations")
