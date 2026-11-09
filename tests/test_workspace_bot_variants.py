import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tybot.access import RequestContext
from tybot.bot_access import DENY_MESSAGE
from tybot.slack import pilot
from tybot.slack.pilot import ConfigError, RequestAuthorizationDenied, WorkspaceBot
from tybot.workspaces import WorkspaceConfig


def _bot(**values) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot.cfg = SimpleNamespace(readable=frozenset(), is_root=False)
    bot.exec_users = set()
    bot._chan_cache = {}
    bot._request_context_resolver = None
    for name, value in values.items():
        setattr(bot, name, value)
    return bot


def test_a_variant_context_resolver_replaces_membership_scope() -> None:
    managed = RequestContext(
        workspace="tyit",
        user_id="U1",
        evidence_sources=frozenset({("mgmt", "C1")}),
    )
    resolver = Mock(return_value=managed)
    bot = _bot(_request_context_resolver=resolver)

    assert bot._context(object(), "U1") is managed
    resolver.assert_called_once()


def test_managed_sources_survive_the_common_channel_context_copy() -> None:
    sources = frozenset({("mgmt", "C1"), ("tyit", "C2")})
    preferred = frozenset({"mgmt"})
    bot = _bot(
        _request_context_resolver=lambda _client, user: RequestContext(
            workspace="tyit",
            user_id=user,
            evidence_sources=sources,
            preferred_workspaces=preferred,
        ),
        _channel_name=lambda _client, _channel: "#executive-report",
    )

    context = bot._request_context(
        object(), "U1", channel_id="CASK", in_channel=True
    )

    assert context.evidence_sources == sources
    assert context.preferred_workspaces == preferred
    assert context.channel_id == "CASK"
    assert context.channel == "#executive-report"


def test_managed_scope_label_wins_over_the_calling_channel() -> None:
    context = RequestContext(
        workspace="tyit",
        channel_id="CASK",
        channel="#executive-report",
        evidence_sources=frozenset({("mgmt", "C1"), ("build", "C2")}),
    )

    assert pilot._scope_label(context) == "managed sources 2"


def test_denial_stops_before_planning_or_question_transformation() -> None:
    transformed: list[str] = []
    bot = _bot(
        _request_context_resolver=lambda _client, _user: None,
        _request_context=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RequestAuthorizationDenied("no_grant")
        ),
        _question_transform=lambda text: transformed.append(text) or text,
    )
    say = Mock()
    event = {"user": "U1", "channel": "C1", "text": "secret question"}

    bot._handle_request(event, object(), say, in_channel=True)

    say.assert_called_once_with(DENY_MESSAGE)
    assert transformed == []
    assert "_tybot_qa_record_id" not in event


def test_allowed_managed_request_uses_the_common_request_context_path() -> None:
    managed = RequestContext(
        workspace="tyit",
        user_id="U1",
        evidence_sources=frozenset({("mgmt", "C1")}),
    )
    bot = _bot(
        _request_context_resolver=lambda _client, _user: managed,
        _channel_name=lambda _client, _channel: "#executive-report",
        _question_transform=lambda _text: (_ for _ in ()).throw(
            RuntimeError("stop after authorization")
        ),
    )
    event = {"user": "U1", "channel": "CASK", "text": "question"}

    with pytest.raises(RuntimeError, match="stop after authorization"):
        bot._handle_request(event, object(), Mock(), in_channel=True)

    context = event["_tybot_request_context"]
    assert context.evidence_sources == managed.evidence_sources
    assert context.channel_id == "CASK"
    assert context.channel == "#executive-report"


def test_the_default_context_path_remains_membership_based() -> None:
    client = Mock()
    client.users_conversations.return_value = {
        "channels": [{"name": "team-a"}, {"name": "team-b"}]
    }
    bot = _bot()

    context = bot._context(client, "U1")

    assert context.channels == frozenset({"#team-a", "#team-b"})
    assert context.evidence_sources is None


def test_a_non_owner_cannot_change_collection_or_heartbeat_state() -> None:
    bot = _bot(operational_owner=False)

    assert bot._retire_channel(object(), {}, "deleted") is None
    assert bot._restore_channel(object(), {}) is None
    assert bot.retired_sweep() is None
    assert bot.publish_status(connected=True) is None


def test_the_common_request_path_blocks_write_intents_for_a_non_owner() -> None:
    source = inspect.getsource(WorkspaceBot._handle_request)
    write_branch = source[source.index("if first.kind in WRITE_KINDS:") :]

    assert write_branch.index('getattr(self, "operational_owner", True)') < (
        write_branch.index("self._ingest_all")
    )
    assert write_branch.index("finish(DENY_MESSAGE") < write_branch.index(
        "self._ingest_all"
    )


def test_a_non_owner_cannot_overwrite_the_workspace_runtime_result(
    monkeypatch,
) -> None:
    recorded = Mock()
    monkeypatch.setenv("DATABASE_URL", "postgresql://configured")
    from tybot.console import workspace_store

    monkeypatch.setattr(workspace_store, "record_runtime_result", recorded)

    pilot._record_bot_runtime_result(
        SimpleNamespace(workspace="tyit", operational_owner=False), "failed"
    )
    pilot._record_bot_runtime_result(
        SimpleNamespace(workspace="tyit", operational_owner=True), None
    )

    recorded.assert_called_once_with("tyit", None)


@pytest.mark.parametrize(
    ("bot_token", "app_token"),
    [("master-bot", "vip-app"), ("vip-bot", "master-app")],
)
def test_shared_runtime_refuses_a_duplicate_slack_connection(
    bot_token: str, app_token: str
) -> None:
    configs = [WorkspaceConfig("tyit", "TY IT", "master-bot", "master-app")]

    with pytest.raises(ConfigError, match="duplicate"):
        pilot._require_distinct_runtime_tokens(
            configs, bot_token=bot_token, app_token=app_token
        )


def test_denied_dm_file_stops_before_file_preflight_or_answering() -> None:
    bot = _bot(
        _bot_users={"U1": False},
        _request_context=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RequestAuthorizationDenied("no_grant")
        ),
        _handle_correction=lambda _event, _say: False,
    )
    client = Mock()
    say = Mock()
    event = {
        "user": "U1",
        "channel": "D1",
        "channel_type": "im",
        "text": "read this",
        "files": [{"id": "F1"}],
    }

    assert bot.route_message(event, client, say) == "denied"
    say.assert_called_once_with(DENY_MESSAGE)
    assert "_tybot_question_file_preflight_codes" not in event


def test_a_non_owner_answers_a_dm_without_ingesting_or_mirroring_it() -> None:
    bot = _bot(
        operational_owner=False,
        _bot_users={"U1": False},
        _handle_correction=lambda _event, _say: False,
        _ingest_dm=Mock(side_effect=AssertionError("non-owner must not ingest")),
        _mirror_dm_handoff=Mock(
            side_effect=AssertionError("non-owner must not mirror")
        ),
        _handle=Mock(),
    )
    event = {
        "user": "U1",
        "channel": "D1",
        "channel_type": "im",
        "text": "executive question",
    }

    assert bot.route_message(event, object(), Mock()) == "answered"
    assert event["_tybot_dm_attachments"] == []
    bot._handle.assert_called_once()


def test_a_non_owner_dm_attachment_remains_transient_question_input(
    monkeypatch,
) -> None:
    preflight = Mock(return_value=())
    monkeypatch.setattr(pilot, "validate_question_files", preflight)
    bot = _bot(
        operational_owner=False,
        _bot_users={"U1": False},
        _handle_correction=lambda _event, _say: False,
        _request_context=lambda *_args, **_kwargs: RequestContext(
            workspace="tyit",
            user_id="U1",
            evidence_sources=frozenset({("tyit", "C1")}),
        ),
        _ingest_dm=Mock(side_effect=AssertionError("non-owner must not ingest")),
        _mirror_dm_handoff=Mock(
            side_effect=AssertionError("non-owner must not mirror")
        ),
        _handle=Mock(),
        cfg=SimpleNamespace(bot_token="vip-token", readable=frozenset(), is_root=False),
    )
    files = [{"id": "F1"}]
    event = {
        "user": "U1",
        "channel": "D1",
        "channel_type": "im",
        "text": "read this",
        "files": files,
    }

    assert bot.route_message(event, object(), Mock()) == "answered"
    preflight.assert_called_once_with(files, "vip-token")
    assert event["_tybot_dm_attachments"] == []
    bot._handle.assert_called_once()


def test_private_answer_denial_goes_only_to_the_callers_dm() -> None:
    bot = _bot(private_answers=True)
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "DPRIVATE"}}
    say = Mock()

    bot._deliver_denial(client, say, "U1")

    client.chat_postMessage.assert_called_once_with(
        channel="DPRIVATE", text=DENY_MESSAGE
    )
    say.assert_not_called()


def test_private_answer_denial_never_falls_back_to_a_public_channel() -> None:
    bot = _bot(private_answers=True)
    client = Mock()
    client.conversations_open.return_value = {"channel": {"id": "CPUBLIC"}}
    say = Mock()

    bot._deliver_denial(client, say, "U1")

    client.chat_postMessage.assert_not_called()
    say.assert_not_called()


def test_shared_runtime_builds_vip_as_the_same_workspace_bot(monkeypatch) -> None:
    config = WorkspaceConfig("tyit", "TY IT", "master-bot", "master-app")
    made: list[tuple[WorkspaceConfig, dict]] = []

    class FakeBot:
        def __init__(self, cfg, **kwargs):
            self.cfg = cfg
            self.workspace = cfg.key
            self.kwargs = kwargs
            made.append((cfg, kwargs))

    monkeypatch.setenv("VIPBOT_SHARED_RUNTIME_ENABLED", "1")
    monkeypatch.setenv("VIPBOT_WORKSPACE", "tyit")
    monkeypatch.delenv("VIPBOT_QA_LOG_DIR", raising=False)
    monkeypatch.setattr(pilot, "load_workspaces", lambda: [config])
    monkeypatch.setattr(pilot, "_start_external_v3", lambda _configs: None)
    qa_logs = []

    def fake_qa_log(root, **_kwargs):
        result = SimpleNamespace(root=root)
        qa_logs.append(result)
        return result

    monkeypatch.setattr(pilot, "QALog", fake_qa_log)
    monkeypatch.setattr(pilot, "check_paths", lambda *_args: {})
    monkeypatch.setattr(pilot, "enforce_archive_writable", lambda _problems: None)
    monkeypatch.setattr(pilot, "ArchiveStore", lambda _root: "store")
    from tybot.gateway import router as gateway_router

    monkeypatch.setattr(
        gateway_router.Router, "from_default_registry", lambda **_kwargs: "router"
    )
    monkeypatch.setattr(pilot, "AnswerEngine", lambda *_args, **_kwargs: "engine")
    monkeypatch.setattr(pilot, "specialist_hook", lambda *_args, **_kwargs: "specialist")
    monkeypatch.setattr(pilot, "WorkspaceBot", FakeBot)

    from tybot import vip_profile, vip_workspace
    from tybot.console import bot_identity, workspace_store

    profile = object()
    resolver = object()
    monkeypatch.setattr(
        bot_identity, "runtime_tokens", lambda *_args: ("vip-bot", "vip-app")
    )
    monkeypatch.setattr(
        workspace_store,
        "list_workspaces",
        lambda: [
            {"key": "tyit", "label": "TY IT", "state": "enabled"},
            {"key": "mgmt", "label": "경영본부", "state": "enabled"},
            {"key": "civil", "label": "토목본부", "state": "enabled"},
            {"key": "old", "label": "폐쇄", "state": "disabled"},
        ],
    )
    monkeypatch.setattr(vip_profile, "load_profile", lambda: profile)
    engine_options = []

    def fake_vip_engine(**kwargs):
        engine_options.append(kwargs)
        return "vip-engine"

    monkeypatch.setattr(vip_workspace, "build_engine", fake_vip_engine)
    monkeypatch.setattr(
        vip_workspace, "context_resolver", lambda _workspace: resolver
    )

    bots = pilot.build_bots()

    assert len(bots) == 2
    assert all(isinstance(bot, FakeBot) for bot in bots)
    assert len(made) == 2
    vip_config, vip_options = made[1]
    assert (vip_config.bot_token, vip_config.app_token) == ("vip-bot", "vip-app")
    assert vip_options["engine"] == "vip-engine"
    assert vip_options["qa_log"] is qa_logs[1]
    assert qa_logs[1].root == "/var/lib/tybot-vipbot/qa"
    assert vip_options["request_context_resolver"] is resolver
    assert vip_options["question_transform"] is vip_profile.question_for_engine
    assert vip_options["operational_owner"] is False
    assert vip_options["realtime"] is False
    assert vip_options["autojoin"] is False
    assert vip_options["private_answers"] is True
    assert vip_options["answer_quota"] is made[0][1]["answer_quota"]
    assert engine_options[0]["workspace_labels"] == {
        "tyit": "TY IT",
        "mgmt": "경영본부",
        "civil": "토목본부",
    }
    assert bots[1].workspace_labels == engine_options[0]["workspace_labels"]
