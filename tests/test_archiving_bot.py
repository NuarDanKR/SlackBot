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


# --- ACK (수집 상태) ----------------------------------------------------------
#
# 수집기가 **단계마다** 상태를 남긴다. 안 남기면 Master 가 「방금 올린 것이
# 검색되나」 에 답할 근거가 없고, 근거가 없으면 사람은 추측으로 답하는 봇을 본다.


@pytest.fixture
def acked(monkeypatch):
    """`ingest_ack.advance` 가 받은 것을 모은다. DB 를 요구하지 않는다."""
    calls: list[dict] = []
    monkeypatch.setattr(
        archiving_bot.ingest_ack, "advance", lambda **kw: calls.append(kw) or None
    )
    return calls


def _event(**over) -> dict:
    return {
        "channel_type": "channel",
        "channel": "C12345678",
        "user": "U12345678",
        "ts": "1790070000.000001",
        "text": "회의 일정은 10시입니다.",
    } | over


def _targets(calls: list[dict]) -> list[str]:
    return [str(call["target"]) for call in calls]


def test_a_plain_message_goes_received_then_raw_written_then_ready(tmp_path, acked):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    assert collector.ingest_event(Client(), _event()) == "written"

    assert _targets(acked) == ["received", "raw_written", "ready"]


def test_ready_is_recorded_last_after_every_check(tmp_path, acked):
    """앞에서 찍으면 그 뒤 단계가 실패해도 이미 「됐다」 고 적힌 채로 남는다.

    그리고 `ready` 는 terminal 이라 되돌려지지 않는다.
    """
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    collector.ingest_event(Client(), _event())

    assert _targets(acked)[-1] == "ready"


def test_a_screened_message_never_reaches_ready(tmp_path, acked, monkeypatch):
    """PII 로 거부된 것은 원문에 없다. 「됐다」 고 하면 찾으러 간 사람이 못 찾는다."""
    import dataclasses

    real = archiving_bot.writer.ingest

    def refuse(*args, **kwargs):
        got = real(*args, **kwargs)
        return dataclasses.replace(got, written=0, refused=[("U1", "rrn")])

    monkeypatch.setattr(archiving_bot.writer, "ingest", refuse)
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    assert collector.ingest_event(Client(), _event()) == "refused"

    assert "ready" not in _targets(acked)
    assert _targets(acked)[-1] == "refused"


def test_a_failed_revision_record_never_reaches_ready(tmp_path, acked, monkeypatch):
    """revision 기록이 빠지면 나중에 수정·삭제를 못 잇는다. 그건 완료가 아니다."""
    monkeypatch.setattr(
        archiving_bot.ShadowCollector, "_record_revision", lambda *a, **k: False
    )
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    assert collector.ingest_event(Client(), _event()) == "metadata-unconfirmed"

    assert "ready" not in _targets(acked)
    assert _targets(acked)[-1] == "partial"


def test_the_shadow_collector_never_claims_it_wrote_live(tmp_path, acked):
    """인수 전까지 이 기록이 운영 아카이브를 가리킨다고 읽히면 안 된다."""
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    collector.ingest_event(Client(), _event())

    assert {call["written_to"] for call in acked} == {"shadow"}


def test_two_channels_do_not_mix_their_ack_coordinates(tmp_path, acked):
    """**두 채널을 번갈아 처리해도 좌표가 섞이지 않는다.**

    전에는 채널을 인스턴스에 들고 있어서, 뒤에 온 이벤트가 앞의 좌표를 덮고
    엉뚱한 채널에 기록했다. 한 프로세스가 여러 채널을 보는 것이 정상 동작이다.
    """
    env = _env(tmp_path)
    env["ARCHIVER_CHANNEL_IDS_TYIT"] = "C12345678,C87654321"
    cfg = archiving_bot.load_archiver_workspaces(env)[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    collector.ingest_event(Client(), _event(channel="C12345678", ts="1.0001"))
    collector.ingest_event(Client(), _event(channel="C87654321", ts="2.0002"))

    pairs = {(call["channel_id"], call["message_ts"]) for call in acked}
    assert pairs == {("C12345678", "1.0001"), ("C87654321", "2.0002")}


def test_events_outside_our_scope_leave_no_state(tmp_path, acked):
    """범위 밖까지 남기면 「받았는데 안 됐다」 가 쌓여 진짜 미완료를 덮는다."""
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    collector.ingest_event(Client(), _event(channel_type="im"))
    collector.ingest_event(Client(), _event(channel="C99999999"))
    collector.ingest_event(Client(), _event(bot_id="B123"))

    assert acked == []


def test_a_redelivered_event_records_the_same_states_again(tmp_path, acked):
    """수집기는 멱등을 판단하지 않는다 — **저장소가** 앞으로만 민다."""
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    collector.ingest_event(Client(), _event())
    acked.clear()
    assert collector.ingest_event(Client(), _event()) == "duplicate"

    # 재전달에서도 **파일 확인 결과로** 사실을 다시 남긴다. 그래야 DB 가 죽어
    # 있던 동안의 빈 구간이 복구된다.
    assert _targets(acked) == ["received", "raw_written", "ready"]


def test_a_failed_ack_does_not_stop_collection(tmp_path, monkeypatch):
    """놓친 원본은 되돌릴 수 없다. ACK 는 나중에 다시 만들 수 있다."""

    def boom(**_):
        raise RuntimeError("DB 다운")

    monkeypatch.setattr(archiving_bot.ingest_ack, "advance", boom)
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    assert collector.ingest_event(Client(), _event()) == "written"
    files = list((tmp_path / "shadow").glob("workspaces/*/channels/*/raw/*.md"))
    assert files and "회의 일정은 10시입니다." in files[0].read_text(encoding="utf-8")


# --- 수정·삭제 revision 의 ACK ------------------------------------------------


def _changed(**over) -> dict:
    return {
        "channel_type": "channel",
        "channel": "C12345678",
        "subtype": "message_changed",
        "ts": "1790070100.000001",
        "event_ts": "1790070100.000001",
        "message": {"ts": "1790070000.000001", "user": "U12345678", "text": "11시입니다"},
        "previous_message": {
            "ts": "1790070000.000001",
            "user": "U12345678",
            "text": "10시입니다",
        },
    } | over


def _deleted(**over) -> dict:
    return {
        "channel_type": "channel",
        "channel": "C12345678",
        "subtype": "message_deleted",
        "ts": "1790070200.000001",
        "event_ts": "1790070200.000001",
        "deleted_ts": "1790070000.000001",
        "previous_message": {
            "ts": "1790070000.000001",
            "user": "U12345678",
            "text": "10시입니다",
        },
    } | over


def test_a_changed_message_stops_at_raw_written(tmp_path, acked):
    """revision reader 전에는 `ready` 가 아니다.

    지금 검색은 `[수정 전]` 줄을 그대로 집는다. 「검색 가능」 이라고 말하면
    **고치기 전 문장을 찾아 주겠다고 약속**하는 셈이다.
    """
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    collector.ingest_event(Client(), _changed())

    assert _targets(acked) == ["received", "raw_written"]


def test_a_deleted_message_is_never_marked_ready(tmp_path, acked):
    """지워진 것을 「검색 가능」 이라고 하면 지운 사람의 뜻을 뒤집는다."""
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    collector.ingest_event(Client(), _deleted())

    assert "ready" not in _targets(acked)


def test_revision_acks_use_the_original_message_coordinate(tmp_path, acked):
    """수정본의 `ts` 가 아니라 **원본 메시지의 `ts`** 로 기록한다.

    아니면 같은 메시지의 상태가 수정할 때마다 새로 생겨서, 「그 메시지가
    검색되나」 에 답할 수 없다.
    """
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    collector.ingest_event(Client(), _changed())

    assert {call["message_ts"] for call in acked} == {"1790070000.000001"}


def test_revision_acks_are_shadow_only(tmp_path, acked):
    cfg = archiving_bot.load_archiver_workspaces(_env(tmp_path))[0]
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    collector.ingest_event(Client(), _deleted())

    assert {call["written_to"] for call in acked} == {"shadow"}
