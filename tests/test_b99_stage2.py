"""B-99 2단계 — DM 인계 거울을 은퇴시킨 뒤의 불변식.

D3: DM 쓰기는 Master 하나(`_ingest_dm`)다. 인계 거울이 다시 생기면 한 파일에 쓰는
프로세스가 둘이 된다. 그래서 환경 변수가 켜져 있어도 DM 이 큐에 들어가면 안 된다.
"""

from types import SimpleNamespace

from cryptography.fernet import Fernet

from tybot import archiving_bot
from tybot.archive.dm_inbox import DmInbox
from tybot.slack.pilot import WorkspaceBot


def _bot():
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "tyit"
    bot.cfg = SimpleNamespace(bot_token="xoxb-test")
    bot._bot_user_id = lambda: "U87654321"
    bot._user_name = lambda client, user: "Alice"
    bot._request_context = lambda client, user, **kwargs: "ctx"
    return bot


class _Client:
    def conversations_info(self, **kwargs):
        return {
            "ok": True,
            "channel": {
                "id": "D12345678",
                "is_im": True,
                "is_mpim": False,
                "user": "U12345678",
                "num_members": 2,
            },
        }


def test_master_has_no_dm_handoff_mirror():
    assert not hasattr(WorkspaceBot, "_mirror_dm_handoff")


def test_archiver_has_no_dm_handoff_consumer():
    for name in ("_dm_handoff_consumer", "_dm_handoff_loop"):
        assert not hasattr(archiving_bot, name)


def test_a_dm_route_writes_through_master_only_and_never_enqueues(monkeypatch, tmp_path):
    cipher = Fernet(Fernet.generate_key())
    # 옛 스위치가 켜져 있어도 거울은 없다 — 남은 설정은 아무것도 하지 않는다.
    monkeypatch.setenv("ARCHIVER_DM_MIRROR_ENABLED", "1")
    monkeypatch.setenv("ARCHIVER_DM_HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr("tybot.console.workspace_store._fernet", lambda: cipher)
    monkeypatch.setattr(WorkspaceBot, "_is_human_request", lambda self, e, c: True)
    monkeypatch.setattr(WorkspaceBot, "_handle_correction", lambda self, e, s: False)
    calls = []
    monkeypatch.setattr(WorkspaceBot, "_ingest_dm", lambda self, c, e: calls.append("master") or [])
    monkeypatch.setattr(
        WorkspaceBot, "_handle", lambda self, e, c, s, *, in_channel: calls.append("answer")
    )
    event = {
        "channel_type": "im",
        "channel": "D12345678",
        "user": "U12345678",
        "ts": "1790800000.000001",
        "text": "private message",
    }

    assert _bot().route_message(event, _Client(), None) == "answered"

    assert calls == ["master", "answer"]
    assert DmInbox(tmp_path, cipher).pending_keys("tyit") == []
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]
