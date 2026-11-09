from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "manage_vipbot_access.py"
SPEC = importlib.util.spec_from_file_location("manage_vipbot_access", SCRIPT)
assert SPEC and SPEC.loader
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def parse(*args: str):
    return cli.build_parser().parse_args(args)


def test_the_cli_is_fixed_to_vipbot_and_accepts_no_token_argument():
    source = SCRIPT.read_text(encoding="utf-8")
    assert cli.BOT == "vipbot"
    assert "--bot" not in source
    assert "--token" not in source


def test_grant_uses_the_console_store_contract(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(
        cli.directory,
        "grant",
        lambda request: seen.append(request) or {"id": 7, "state": "active"},
    )
    args = parse(
        "grant", "--emp-no", "E1", "--reason", "임원 승인", "--actor", "a@x"
    )
    assert cli.run(args) == 0
    assert seen == [cli.directory.GrantRequest(
        bot="vipbot", emp_no="E1", reason="임원 승인", actor="a@x",
        expires_at=None,
    )]
    assert "grant_id=7" in capsys.readouterr().out


def test_grant_requires_a_reason_and_actor():
    with pytest.raises(SystemExit):
        parse("grant", "--emp-no", "E1")


def test_expiry_requires_a_timezone():
    with pytest.raises(SystemExit):
        parse(
            "grant", "--emp-no", "E1", "--reason", "r", "--actor", "a",
            "--expires-at", "2026-12-01T09:00:00",
        )
    args = parse(
        "grant", "--emp-no", "E1", "--reason", "r", "--actor", "a",
        "--expires-at", "2026-12-01T09:00:00+09:00",
    )
    assert args.expires_at.hour == 9
    assert args.expires_at.utcoffset() == timedelta(hours=9)


def test_add_source_uses_the_console_store_contract(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(
        cli.directory,
        "add_sources",
        lambda **kwargs: seen.append(kwargs) or {"added": ["tyit:C123456"], "already": []},
    )
    args = parse(
        "add-source", "--workspace", "tyit", "--channel-id", "C123456",
        "--name", "경영", "--reason", "근거 승인", "--actor", "a@x",
    )
    assert cli.run(args) == 0
    assert seen == [{
        "bot": "vipbot",
        "channels": [{"workspace": "tyit", "channelId": "C123456", "name": "경영"}],
        "reason": "근거 승인",
        "actor": "a@x",
    }]
    assert "added=1" in capsys.readouterr().out


def test_status_checks_the_actual_user_and_lists_sources(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.directory,
        "check",
        lambda **kwargs: {"allowed": True, "code": "allowed"},
    )
    monkeypatch.setattr(
        cli.directory,
        "list_sources",
        lambda **kwargs: {"channels": [{
            "workspace": "mgmt", "channelId": "C7654321", "name": "임원",
        }]},
    )
    assert cli.run(parse("status", "--workspace", "tyit", "--slack-user", "U123456")) == 0
    output = capsys.readouterr().out
    assert "allowed=true code=allowed" in output
    assert "mgmt:C7654321" in output


@pytest.mark.parametrize(
    ("allowed", "sources"),
    [
        (False, [{"workspace": "mgmt", "channelId": "C7654321", "name": "임원"}]),
        (True, []),
    ],
)
def test_status_fails_closed_without_permission_or_sources(monkeypatch, allowed, sources):
    monkeypatch.setattr(
        cli.directory,
        "check",
        lambda **kwargs: {"allowed": allowed, "code": "allowed" if allowed else "no_grant"},
    )
    monkeypatch.setattr(
        cli.directory, "list_sources", lambda **kwargs: {"channels": sources}
    )
    assert cli.run(parse("status", "--workspace", "tyit", "--slack-user", "U123456")) == 1


def test_store_errors_return_a_fixed_code_without_the_database_exception(monkeypatch, capsys):
    monkeypatch.setattr(
        cli.directory,
        "grant",
        lambda request: (_ for _ in ()).throw(
            cli.directory.UserDirectoryError("database_unavailable")
        ),
    )
    monkeypatch.setattr(cli, "load_env_file", lambda: "test")
    result = cli.main([
        "grant", "--emp-no", "E1", "--reason", "r", "--actor", "a",
    ])
    assert result == 2
    error = capsys.readouterr().err
    assert error.strip() == "VIPBot 관리 실패: database_unavailable"


def test_env_file_is_loaded_without_putting_a_dsn_on_the_command_line(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "load_env_file", lambda: seen.append("loaded"))
    monkeypatch.setattr(
        cli.directory,
        "check",
        lambda **kwargs: {"allowed": True, "code": "allowed"},
    )
    monkeypatch.setattr(
        cli.directory,
        "list_sources",
        lambda **kwargs: {"channels": [{
            "workspace": "mgmt", "channelId": "C7654321", "name": "임원",
        }]},
    )
    assert cli.main([
        "--env-file", "/etc/tybot/tybot.env", "status",
        "--workspace", "tyit", "--slack-user", "U123456",
    ]) == 0
    assert seen == ["loaded"]
