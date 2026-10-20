from scripts.inventory_companion_channels import inventory


class Client:
    def __init__(self, pages):
        self.pages = iter(pages)

    def users_conversations(self, **_kwargs):
        return next(self.pages)


def page(*channels, cursor=""):
    return {
        "channels": [{"id": channel, "is_im": channel.startswith("D")} for channel in channels],
        "response_metadata": {"next_cursor": cursor},
    }


def connection(workspace, key, *, state="enabled", identity_ok=True):
    return {"workspace": workspace, "bot_key": key, "state": state, "identity_ok": identity_ok}


def test_union_deduplicates_and_excludes_tec_and_dms():
    rows = [
        connection("tyit", "master"),
        connection("tyit", "archiver"),
        connection("tec", "archiver"),
    ]
    clients = {
        "tyit/master": Client([page("C1", "D1", cursor="next"), page("C2")]),
        "tyit/archiver": Client([page("C2", "C3")]),
    }
    loaded = []

    def load(workspace, key):
        loaded.append(f"{workspace}/{key}")
        return f"{workspace}/{key}"

    result = inventory(rows, lambda *, token: clients[token], load)

    assert loaded == ["tyit/archiver", "tyit/master"]
    assert result == [
        {
            "workspace": "tyit",
            "source_bots": ["archiver", "master"],
            "channels": [
                {"channel_id": "C1", "seen_by": ["master"]},
                {"channel_id": "C2", "seen_by": ["archiver", "master"]},
                {"channel_id": "C3", "seen_by": ["archiver"]},
            ],
        }
    ]


def test_unverified_or_failed_source_aborts_instead_of_undercounting():
    rows = [connection("mgmt", "archiver", identity_ok=False)]
    try:
        inventory(rows, lambda **_: Client([page("C1")]), lambda *_: "token")
    except RuntimeError as exc:
        assert "unverified source" in str(exc)
    else:
        raise AssertionError("unverified source must abort")

    rows = [connection("mgmt", "archiver")]

    def failing_client(*, token):
        raise ConnectionError(token)

    try:
        inventory(rows, failing_client, lambda *_: "secret-token")
    except ConnectionError:
        pass
    else:
        raise AssertionError("failed membership read must abort")


def test_disabled_sources_and_non_collection_bots_are_ignored():
    rows = [
        connection("pilot", "master", state="disabled"),
        connection("pilot", "hermes"),
        connection("archi", "archiver"),
    ]
    result = inventory(rows, lambda **_: Client([page("C1")]), lambda *_: "token")
    assert [row["workspace"] for row in result] == ["archi"]


def test_archiver_only_workspace_remains_in_scope_when_master_is_disabled():
    rows = [
        connection("pilot", "master", state="disabled", identity_ok=False),
        connection("pilot", "archiver"),
    ]
    loaded = []

    def load(workspace, key):
        loaded.append((workspace, key))
        return "archiver-token"

    result = inventory(rows, lambda **_: Client([page("C1")]), load)

    assert loaded == [("pilot", "archiver")]
    assert result == [
        {
            "workspace": "pilot",
            "source_bots": ["archiver"],
            "channels": [{"channel_id": "C1", "seen_by": ["archiver"]}],
        }
    ]
