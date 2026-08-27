"""정기 백필 잡 — 페이싱·멱등·실패 격리."""
from __future__ import annotations

from unittest.mock import patch

from tybot.collect import collect_workspace
from tybot.workspaces import WorkspaceConfig


class FakeClient:
    def __init__(self, channels, history, fail_history=(), canvas=None, file_obj=None):
        self._channels = channels
        self._history = history
        self._fail = set(fail_history)
        self._canvas = canvas or {}
        self._file = file_obj or {}
        self.history_calls = []

    def conversations_list(self, **kw):
        return {"channels": self._channels, "response_metadata": {}}

    def conversations_history(self, channel, limit=15):
        self.history_calls.append(channel)
        if channel in self._fail:
            raise RuntimeError("rate limited")
        return {"messages": self._history.get(channel, [])}

    def users_info(self, user):
        return {"user": {"profile": {"real_name": f"사용자{user}"}}}

    def conversations_info(self, channel):
        file_id = self._canvas.get(channel)
        props = {"canvas": {"file_id": file_id}} if file_id else {}
        return {"channel": {"id": channel, "properties": props}}

    def files_info(self, file):
        return {"file": self._file}


def _cfg():
    return WorkspaceConfig(
        key="pilot", label="파일럿", bot_token="xoxb-fake_1", app_token="xapp-fake_1"
    )


def _msg(ts, text, **extra):
    return {"ts": ts, "user": "U1", "text": text, **extra}


def _run(tmp_path, client, pace=0.0):
    with patch("tybot.collect.WebClient", return_value=client, create=True), patch(
        "slack_sdk.WebClient", return_value=client, create=True
    ):
        return collect_workspace(_cfg(), str(tmp_path), pace=pace)


def test_collects_member_channels_only(tmp_path):
    client = FakeClient(
        channels=[
            {"id": "C1", "name": "팀-전산_ABB110-가입채널", "is_member": True},
            {"id": "C2", "name": "팀-전산_ABB110-미가입채널", "is_member": False},
        ],
        history={"C1": [_msg("1755000000.0", "기성금 3억")]},
    )
    stats = _run(tmp_path, client)
    assert client.history_calls == ["C1"]  # 멤버가 아닌 채널은 조회조차 안 한다
    assert stats["written"] == 1


def test_rule_mismatching_member_channel_is_not_read(tmp_path):
    client = FakeClient(
        channels=[
            {"id": "C1", "name": "팀-전산_ABB110-회의", "is_member": True},
            {"id": "C2", "name": "점심메뉴", "is_member": True},
        ],
        history={"C1": [_msg("1755000000.0", "업무 대화")], "C2": [_msg("1755000000.0", "잡담")]},
    )
    stats = _run(tmp_path, client)
    assert client.history_calls == ["C1"]
    assert stats["written"] == 1 and stats["skipped_rule"] == 1


def test_second_run_is_idempotent(tmp_path):
    client = FakeClient(
        channels=[{"id": "C1", "name": "팀-전산_ABB110-채널", "is_member": True}],
        history={"C1": [_msg("1755000000.0", "같은 메시지")]},
    )
    first = _run(tmp_path, client)
    second = _run(tmp_path, client)
    assert first["written"] == 1
    assert second["written"] == 0 and second["skipped"] == 1


def test_one_channel_failure_does_not_stop_others(tmp_path):
    client = FakeClient(
        channels=[
            {"id": "C1", "name": "팀-전산_ABB110-실패채널", "is_member": True},
            {"id": "C2", "name": "팀-전산_ABB110-정상채널", "is_member": True},
        ],
        history={"C2": [_msg("1755000000.0", "정상 수집")]},
        fail_history=("C1",),
    )
    stats = _run(tmp_path, client)
    assert stats["failed"] == 1 and stats["written"] == 1


def test_bot_and_system_messages_excluded(tmp_path):
    client = FakeClient(
        channels=[{"id": "C1", "name": "팀-전산_ABB110-채널", "is_member": True}],
        history={
            "C1": [
                _msg("1755000000.0", "사람 발언"),
                _msg("1755000001.0", "봇 답변", bot_id="B1"),
                _msg("1755000002.0", "입장", subtype="channel_join"),
                _msg("1755000003.0", "파일 올림", subtype="file_share"),
            ]
        },
    )
    stats = _run(tmp_path, client)
    assert stats["written"] == 2  # 사람 발언 + file_share 만


def test_attachments_recorded(tmp_path):
    client = FakeClient(
        channels=[{"id": "C1", "name": "팀-전산_ABB110-채널", "is_member": True}],
        history={
            "C1": [
                _msg(
                    "1755000000.0", "도면 공유",
                    subtype="file_share",
                    files=[{"id": "F1", "name": "도면.dwg", "filetype": "dwg", "size": 1024}],
                )
            ]
        },
    )
    _run(tmp_path, client)
    md = next((tmp_path / "channels" / "pilot").glob("*.md")).read_text(encoding="utf-8")
    assert "[첨부:미변환] 도면.dwg" in md
    assert "도면 공유" in md


def test_periodic_collect_captures_canvas_idempotently(tmp_path):
    client = FakeClient(
        channels=[{"id": "C1", "name": "팀-전산_ABB110-회의", "is_member": True}],
        history={"C1": []},
        canvas={"C1": "F1"},
        file_obj={
            "id": "F1",
            "name": "회의록",
            "filetype": "canvas",
            "size": 100,
            "mimetype": "text/markdown",
            "url_private_download": "https://files.slack.com/canvas",
        },
    )
    with patch("tybot.archive.canvas.download_bytes", return_value=b"same canvas"):
        first = _run(tmp_path, client)
        second = _run(tmp_path, client)
    assert first["written"] == 2
    assert second["written"] == 0
    md = next((tmp_path / "channels" / "pilot").glob("*.md")).read_text(encoding="utf-8")
    assert md.count("[캔버스본문:회의록] same canvas") == 1
