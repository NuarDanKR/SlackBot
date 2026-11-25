"""운영 루트 아래에 옛 배치 `workspaces/` 를 **누구도 만들지 않는다**(B-99).

2026-11-20 장애: Master 의 DM 한 건과 채널 수집이 운영 루트에
`workspaces/<ws>/{dm,channels}` 를 만들었고, Archiver 는 그 디렉터리를 보면 기동을
거부한다(`archiving_bot.archive_root`). 재시작 한 번에 그 워크스페이스 수집이
통째로 멈췄다.

막는 자리는 둘이다.

1. `write_owner` — 주인 기록이 없거나 DB 를 못 읽으면 Master 는 쓰지 않는다(fail-closed)
2. `writer.ingest` — 그래도 옛 기본 경로로 떨어지면 운영 루트에서는 거절한다

이 파일은 2 와, 2 를 부르는 수집 잡·수동 스크립트가 **조용히 넘어가지 않는지**를 본다.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tybot import collect
from tybot.archive import writer
from tybot.archive.archiving_state import ChannelMode, ChannelState, WriterOwner
from tybot.archive.shadow_paths import archive_dir, dm_archive_dir
from tybot.archive.write_owner import OwnerLookup, decide

WS = "pilot"
CH = "C0ABCDEF12"
CHANNEL = "#팀-전산_ABB110-회의"
USER = "U0ABCDEF12"
DM = "D0ABCDEF12"


def _msg(text: str = "원문") -> writer.IncomingMessage:
    return writer.IncomingMessage(
        ts=datetime(2026, 11, 20, 3, 0, tzinfo=UTC),
        speaker="사용자",
        text=text,
    )


@pytest.fixture
def live(tmp_path, monkeypatch) -> Path:
    """`ARCHIVE_DIR` 이 가리키는 운영 루트."""
    root = tmp_path / "archive"
    root.mkdir()
    monkeypatch.setenv("ARCHIVE_DIR", str(root))
    return root


@pytest.fixture
def offline(tmp_path, monkeypatch) -> Path:
    """운영 루트가 아닌 별도 루트(시험·오프라인 도구)."""
    monkeypatch.setenv("ARCHIVE_DIR", str(tmp_path / "elsewhere"))
    root = tmp_path / "offline"
    root.mkdir()
    return root


# --- 판정 -------------------------------------------------------------------------


def test_archive_dir_env_marks_the_live_root(live):
    assert writer.is_live_archive_root(live, WS)
    assert writer.legacy_layout_refusal(live, WS) == "legacy-layout-live-root"


def test_relative_spelling_of_the_same_root_is_still_live(live, monkeypatch):
    monkeypatch.chdir(live.parent)
    assert writer.is_live_archive_root(Path("archive"), WS)
    assert writer.is_live_archive_root(str(live / "." / ""), WS)


def test_archiver_layout_marks_the_live_root_without_env(tmp_path, monkeypatch):
    """`--archive` 로 운영 경로를 직접 준 수동 실행도 잡힌다."""
    monkeypatch.delenv("ARCHIVE_DIR", raising=False)
    (tmp_path / WS).mkdir()

    assert writer.is_live_archive_root(tmp_path, WS)
    assert not writer.is_live_archive_root(tmp_path, "other")


def test_a_separate_root_is_not_live(offline):
    assert not writer.is_live_archive_root(offline, WS)
    assert writer.legacy_layout_refusal(offline, WS) is None


# --- writer 경비 -----------------------------------------------------------------


def test_writer_refuses_the_legacy_channel_path_under_the_live_root(live):
    with pytest.raises(writer.LegacyLayoutRefused) as caught:
        writer.ingest(
            live, workspace=WS, channel=CHANNEL, channel_id=CH, messages=[_msg()], acl=[CHANNEL]
        )

    assert "legacy-layout-live-root" in str(caught.value)
    # 사유에 채널 이름·본문이 섞이지 않는다.
    assert CHANNEL.lstrip("#") not in str(caught.value)
    assert not (live / "workspaces").exists()


def test_writer_refuses_the_legacy_dm_path_under_the_live_root(live):
    with pytest.raises(writer.LegacyLayoutRefused):
        writer.ingest(
            live,
            workspace=WS,
            channel=writer.dm_channel(USER),
            channel_id=DM,
            messages=[_msg()],
            acl=[],
            dm_user=USER,
        )

    assert not (live / "workspaces").exists()


def test_the_refusal_is_an_archive_refusal(live):
    """부르는 쪽이 `ArchiveRefused`·`ValueError` 로 잡던 자리에서도 잡힌다."""
    assert issubclass(writer.LegacyLayoutRefused, writer.ArchiveRefused)
    assert issubclass(writer.LegacyLayoutRefused, ValueError)


def test_archiver_channel_directory_still_writes_under_the_live_root(live):
    target = archive_dir(live, WS, CH, CHANNEL)

    result = writer.ingest(
        live,
        workspace=WS,
        channel=CHANNEL,
        channel_id=CH,
        messages=[_msg()],
        acl=[CHANNEL],
        channel_directory=target,
    )

    assert result.written == 1
    assert result.path.is_relative_to(live / WS)
    assert not (live / "workspaces").exists()


def test_archiver_dm_directory_still_writes_under_the_live_root(live):
    target = dm_archive_dir(live, WS, USER, DM)

    result = writer.ingest(
        live,
        workspace=WS,
        channel=writer.dm_channel(USER),
        channel_id=DM,
        messages=[_msg()],
        acl=[],
        dm_user=USER,
        dm_directory=target,
    )

    assert result.written == 1
    assert not (live / "workspaces").exists()


def test_legacy_layout_stays_allowed_in_a_separate_root(offline):
    """시험·오프라인 도구가 별도 루트에 옛 배치로 쓰는 것은 막지 않는다."""
    result = writer.ingest(
        offline, workspace=WS, channel=CHANNEL, channel_id=CH, messages=[_msg()], acl=[CHANNEL]
    )

    assert result.written == 1
    assert (offline / "workspaces" / WS / "channels").is_dir()


# --- Master 문지기 ----------------------------------------------------------------


def test_master_gate_without_a_database_denies():
    def down():
        raise RuntimeError("down")

    verdict = OwnerLookup(WS, connect=down).master_may_write(CH)
    assert not verdict.allowed
    assert verdict.degraded is True


def test_master_gate_without_a_row_denies():
    assert not decide(None, WriterOwner.MASTER)


# --- 수집 잡 ----------------------------------------------------------------------


class _Allow:
    """주인 표가 Master 몫이라고 답한다 — 경비가 두 번째 자리에서 막는지 본다."""

    def __init__(self, workspace=None) -> None:
        self.state = ChannelState(WS, "C1", ChannelMode.OFF, WriterOwner.MASTER)

    def master_may_write(self, channel_id):
        return decide(self.state, WriterOwner.MASTER)


class _Slack:
    def __init__(self) -> None:
        self.history_calls: list[str] = []

    def conversations_list(self, **_kw):
        return {
            "channels": [{"id": "C1", "name": "팀-전산_ABB110-회의", "is_member": True}],
            "response_metadata": {},
        }

    def conversations_history(self, channel, **_kw):
        self.history_calls.append(channel)
        return {"messages": [{"ts": "1763600000.000100", "user": "U1", "text": "원문"}]}


def test_the_hourly_job_refuses_the_live_root_before_reading_slack(live, monkeypatch):
    slack = _Slack()
    monkeypatch.setattr("slack_sdk.WebClient", lambda token: slack)
    monkeypatch.setattr(collect, "OwnerLookup", _Allow)

    stats = collect.collect_workspace(Mock(key=WS, bot_token="xoxb-테스트"), str(live), pace=0)

    assert stats["refused_layout"] == 1
    assert stats["failed"] == 1
    assert slack.history_calls == []
    assert not (live / "workspaces").exists()


def test_the_hourly_job_exits_nonzero_on_a_layout_refusal(monkeypatch):
    monkeypatch.setattr(collect, "load_workspaces", lambda: [Mock(key=WS)])
    monkeypatch.setattr(
        collect,
        "collect_workspace",
        lambda *a, **k: {
            "channels": 1,
            "written": 3,
            "skipped": 0,
            "failed": 1,
            "refused_layout": 1,
        },
    )

    assert collect._collect_all() == 1


# --- 수동 스크립트 ----------------------------------------------------------------


@pytest.fixture
def scripts_path():
    root = Path(__file__).resolve().parent.parent / "scripts"
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def _exploding_slack(monkeypatch):
    class Exploding:
        def __init__(self, token=None, **_kw):
            pass

        def __getattr__(self, name):
            raise AssertionError(f"운영 루트인데 Slack 을 불렀다: {name}")

    monkeypatch.setattr("slack_sdk.WebClient", Exploding)


def test_history_backfill_refuses_the_live_root(live, monkeypatch, scripts_path):
    import backfill_channel_history as script

    _exploding_slack(monkeypatch)
    checkpoints = script.Checkpoints(live.parent / "state.json")

    stats = script.backfill_channel(
        SimpleNamespace(key=WS, bot_token="xoxb-테스트"),
        {"id": "C1", "name": "팀-전산_ABB110-회의"},
        str(live),
        checkpoints,
        pace=0,
        owner=_Allow(),
    )

    assert stats.refused_layout == 1
    assert stats.failures == 1
    assert not (live / "workspaces").exists()


def test_file_sync_apply_refuses_the_live_root(live, monkeypatch, scripts_path):
    import sync_channel_files as script

    monkeypatch.setattr("slack_sdk.WebClient", lambda token=None, **_kw: object())
    monkeypatch.setattr(
        script,
        "member_channels",
        lambda _c: [
            {"id": "C1", "name": "팀-전산_ABB110-회의"},
        ],
    )
    monkeypatch.setattr(script, "scan", lambda *a, **k: pytest.fail("운영 루트인데 훑었다"))

    stats = script.sync_workspace(
        SimpleNamespace(key=WS, bot_token="xoxb-테스트"),
        str(live),
        apply=True,
        pace=0,
        owner=_Allow(),
    )

    assert stats["refused_layout"] == 1
    assert stats["failed"] == 1
    assert not (live / "workspaces").exists()


def test_file_sync_dry_run_is_not_refused(live, monkeypatch, scripts_path):
    """조회만 하는 실행은 쓰지 않으므로 막지 않는다."""
    import sync_channel_files as script

    monkeypatch.setattr("slack_sdk.WebClient", lambda token=None, **_kw: object())
    monkeypatch.setattr(
        script,
        "member_channels",
        lambda _c: [
            {"id": "C1", "name": "팀-전산_ABB110-회의"},
        ],
    )
    monkeypatch.setattr(
        script,
        "scan",
        lambda *a, **k: SimpleNamespace(
            total=0,
            known=0,
            missing=0,
            candidates=[],
            warnings=[],
            truncated=False,
            summary=lambda: "파일 0건",
        ),
    )

    stats = script.sync_workspace(
        SimpleNamespace(key=WS, bot_token="xoxb-테스트"),
        str(live),
        apply=False,
        pace=0,
        owner=_Allow(),
    )

    assert stats["refused_layout"] == 0
    assert stats["channels"] == 1
    assert not (live / "workspaces").exists()
