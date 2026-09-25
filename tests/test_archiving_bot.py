from pathlib import Path

import pytest

from tybot import archiving_bot


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ARCHIVER_CONFIG_SOURCE": "env",
        "ARCHIVER_WORKSPACES": "tyit",
        "ARCHIVER_BOT_TOKEN_TYIT": "archiver-bot",
        "ARCHIVER_APP_TOKEN_TYIT": "archiver-app",
        "ARCHIVER_TEAM_ID_TYIT": "T12345678",
        "SLACK_BOT_TOKEN_TYIT": "master-bot",
        "SLACK_APP_TOKEN_TYIT": "master-app",
        "ARCHIVER_MASTER_BOT_USER_TYIT": "U_MASTER",
        "ARCHIVER_CHANNEL_IDS_TYIT": "C12345678",
        "ARCHIVE_DIR": str(tmp_path / "live"),
        "ARCHIVER_SHADOW_DIR": str(tmp_path / "shadow" / "archive"),
    }


def test_separate_tokens_required(tmp_path):
    env = _env(tmp_path)
    assert archiving_bot.load_archiver_workspaces(env)[0].key == "tyit"
    env["ARCHIVER_APP_TOKEN_TYIT"] = env["SLACK_APP_TOKEN_TYIT"]
    with pytest.raises(archiving_bot.ArchiverConfigError, match="must differ"):
        archiving_bot.load_archiver_workspaces(env)


def test_db_configuration_uses_console_managed_service(monkeypatch, tmp_path):
    row = {
        "bot_token": "xoxb-db",
        "app_token": "xapp-db",
        "team_id": "T12345678",
        "master_bot_user_id": "U_MASTER",
        "channel_ids": ["C12345678"],
        "separate_attachments": True,
    }
    monkeypatch.setattr(
        "tybot.archiver_runtime_store.load_runtime_config", lambda key: row | {"key": key}
    )

    configs = archiving_bot.load_archiver_workspaces({
        "ARCHIVER_CONFIG_SOURCE": "db",
        "ARCHIVER_WORKSPACE": "tyit",
    })

    assert configs[0].bot_token == "xoxb-db"
    assert configs[0].allowed_channels == frozenset({"C12345678"})
    assert configs[0].separate_attachments is True
    env = _env(tmp_path)
    del env["ARCHIVER_CHANNEL_IDS_TYIT"]
    with pytest.raises(archiving_bot.ArchiverConfigError, match="channel IDs"):
        archiving_bot.load_archiver_workspaces(env)


def test_slack_identity_must_match_workspace_and_not_master(tmp_path):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    archiving_bot.validate_slack_identity(
        cfg, {"team_id": "T12345678", "user_id": "U_ARCHIVER"}
    )
    with pytest.raises(archiving_bot.ArchiverConfigError, match="wrong workspace"):
        archiving_bot.validate_slack_identity(
            cfg, {"team_id": "T_OTHER", "user_id": "U_ARCHIVER"}
        )
    with pytest.raises(archiving_bot.ArchiverConfigError, match="TYBot app"):
        archiving_bot.validate_slack_identity(
            cfg, {"team_id": "T12345678", "user_id": "U_MASTER"}
        )


def test_shadow_must_not_overlap_live_archive(tmp_path):
    env = _env(tmp_path)
    assert archiving_bot.shadow_archive_dir(env) == Path(env["ARCHIVER_SHADOW_DIR"]).resolve()
    env["ARCHIVER_SHADOW_DIR"] = str(tmp_path / "live" / "workspaces")
    with pytest.raises(archiving_bot.ArchiverConfigError, match="must not overlap"):
        archiving_bot.shadow_archive_dir(env)
    del env["ARCHIVE_DIR"]
    with pytest.raises(archiving_bot.ArchiverConfigError, match="ARCHIVE_DIR"):
        archiving_bot.shadow_archive_dir(env)


class Client:
    def conversations_info(self, *, channel):
        return {"channel": {"id": channel, "name": "팀-전산_abb155-공지", "is_member": True}}

    def users_info(self, *, user):
        return {"user": {"name": user}}


def test_shadow_collector_writes_only_shadow_root(tmp_path):
    env = _env(tmp_path)
    cfg = archiving_bot.load_archiver_workspaces(env)[0]
    collector = archiving_bot.ShadowCollector(cfg, archiving_bot.shadow_archive_dir(env))
    event = {
        "channel_type": "channel",
        "channel": "C12345678",
        "user": "U12345678",
        "ts": "1790070000.000001",
        "text": "회의 일정은 10시입니다.",
    }
    assert collector.ingest_event(Client(), event) == "written"
    assert collector.ingest_event(Client(), event) == "duplicate"
    files = list(Path(env["ARCHIVER_SHADOW_DIR"]).glob("workspaces/*/channels/*/raw/*.md"))
    assert len(files) == 1
    assert "회의 일정은 10시입니다." in files[0].read_text(encoding="utf-8")
    assert not Path(env["ARCHIVE_DIR"]).exists()


def test_shadow_collector_rejects_bot_and_dm(tmp_path):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")
    event = {
        "channel_type": "channel",
        "channel": "C12345678",
        "user": "U12345678",
        "ts": "1790070000.000001",
        "text": "ignore",
    }
    assert collector.ingest_event(Client(), {**event, "bot_id": "B123"}) == "skipped-bot-or-system"
    assert collector.ingest_event(Client(), {**event, "channel_type": "im"}) == "skipped-scope"
    assert collector.ingest_event(Client(), {**event, "channel": "C99999999"}) == "skipped-allowlist"
    assert not (tmp_path / "shadow").exists()


def test_changed_message_appends_before_and_after_with_one_revision(tmp_path):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")
    recorded = []
    collector._record_revision = lambda **values: recorded.append(values) or True
    event = {
        "channel_type": "channel",
        "channel": "C12345678",
        "subtype": "message_changed",
        "event_ts": "1790070100.000001",
        "message": {
            "user": "U12345678",
            "ts": "1790070000.000001",
            "text": "회의는 11시입니다.",
            "edited": {"ts": "1790070100.000001"},
        },
        "previous_message": {
            "user": "U12345678",
            "ts": "1790070000.000001",
            "text": "회의는 10시입니다.",
        },
    }

    assert collector.ingest_event(Client(), event) == "revision-written"
    text = next((tmp_path / "shadow").glob("workspaces/*/channels/*/raw/*.md")).read_text(
        encoding="utf-8"
    )
    assert "[수정 전] 회의는 10시입니다." in text
    assert "[수정 후] 회의는 11시입니다." in text
    assert recorded[0]["kind"] == "change"
    assert recorded[0]["previous_body"] == "회의는 10시입니다."


def test_deleted_message_appends_tombstone_and_revision(tmp_path):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")
    recorded = []
    collector._record_revision = lambda **values: recorded.append(values) or True
    event = {
        "channel_type": "channel",
        "channel": "C12345678",
        "subtype": "message_deleted",
        "event_ts": "1790070100.000001",
        "deleted_ts": "1790070000.000001",
        "previous_message": {
            "user": "U12345678",
            "ts": "1790070000.000001",
            "text": "삭제할 문장",
        },
    }

    assert collector.ingest_event(Client(), event) == "revision-written"
    text = next((tmp_path / "shadow").glob("workspaces/*/channels/*/raw/*.md")).read_text(
        encoding="utf-8"
    )
    assert "[삭제 전] 삭제할 문장" in text
    assert "[삭제됨] Slack에서 삭제된 메시지" in text
    assert recorded[0]["kind"] == "delete"


def test_revision_metadata_failure_is_not_reported_as_success(monkeypatch, tmp_path):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr(archiving_bot, "record_revision", lambda **_values: 1 / 0)
    event = {
        "channel_type": "channel",
        "channel": "C12345678",
        "user": "U12345678",
        "ts": "1790070000.000001",
        "text": "회의 일정은 10시입니다.",
    }

    assert collector.ingest_event(Client(), event) == "metadata-unconfirmed"


def test_duplicate_event_retries_missing_revision_metadata(tmp_path):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")
    attempts = []
    collector._record_revision = lambda **values: attempts.append(values) or True
    event = {
        "channel_type": "channel",
        "channel": "C12345678",
        "user": "U12345678",
        "ts": "1790070000.000001",
        "text": "회의 일정은 10시입니다.",
    }

    assert collector.ingest_event(Client(), event) == "written"
    assert collector.ingest_event(Client(), event) == "duplicate"
    assert len(attempts) == 2


def test_revision_doc_path_is_relative_to_archive_root(tmp_path):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    root = tmp_path / "shadow"
    collector = archiving_bot.ShadowCollector(cfg, root)

    assert collector._relative_doc_path(root / "workspaces" / "tyit" / "raw.md") == (
        "workspaces/tyit/raw.md"
    )
    with pytest.raises(archiving_bot.ArchiverConfigError, match="escaped"):
        collector._relative_doc_path(tmp_path / "outside.md")
