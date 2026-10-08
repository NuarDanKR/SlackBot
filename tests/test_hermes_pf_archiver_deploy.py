from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "hermes-archiver@.service"
RUNBOOK = ROOT / "docs" / "deploy" / "hermes-pf-archiver.md"
MANIFEST_SERVICE = ROOT / "deploy" / "hermes-privacy-manifest@.service"
MANIFEST_TIMER = ROOT / "deploy" / "hermes-privacy-manifest@.timer"
INSTALL = ROOT / "deploy" / "install.sh"
IDENTITY = ROOT / "subbots" / "hermes" / "src" / "slack" / "identity.js"
SLACK_MANIFEST = ROOT / "subbots" / "hermes" / "slack-app-manifest.yaml"
NODE = shutil.which("node")


def _directives(path: Path = UNIT) -> list[str]:
    return [
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_archiver_unit_fixes_read_only_mode_and_instance_roots():
    lines = _directives()

    assert "Environment=HERMES_MODE=pf-archiver" in lines
    assert "Environment=HERMES_DATA_ROOT=/var/lib/hermes/%i" in lines
    assert "Environment=HERMES_STATE_DIR=/var/lib/hermes/%i/state" in lines
    assert "Environment=HERMES_ARCHIVER_ROOT=/var/lib/tybot/archive" in lines
    assert (
        "Environment=HERMES_PRIVACY_MANIFEST=/etc/hermes/%i/privacy-manifest.json"
        in lines
    )
    assert "ExecStart=/usr/local/libexec/hermes-archiver-run start %i" in lines
    assert "ExecStartPre=/usr/bin/node scripts/check-archiver-privacy.js" in lines
    assert not any("run-ingest" in line for line in lines)


def test_archiver_instances_have_separate_accounts_config_and_state():
    lines = _directives()

    assert "User=hermes-%i" in lines
    assert "Group=hermes-%i" in lines
    assert "EnvironmentFile=/etc/hermes/%i/hermes.env" in lines
    assert "SyslogIdentifier=hermes-archiver-%i" in lines
    assert "Conflicts=hermes.service" in lines
    assert not any(line.startswith("ConditionPath") for line in lines)
    for required in (
        "/etc/hermes/%i/hermes.env",
        "/etc/hermes/%i/privacy-manifest.json",
        "/var/lib/hermes/%i/config.json",
        "/var/lib/hermes/%i/state",
    ):
        assert any(line.startswith("ExecStartPre=/usr/bin/test") and required in line for line in lines)


def test_archiver_unit_never_opens_evidence_or_code_for_writing():
    lines = _directives()
    writable = next(line for line in lines if line.startswith("ReadWritePaths="))
    readonly = next(line for line in lines if line.startswith("ReadOnlyPaths="))

    assert "/opt/tybot/subbots/hermes" in readonly.split("=", 1)[1].split()
    assert "/etc/hermes/%i" in readonly.split("=", 1)[1].split()
    assert set(writable.split("=", 1)[1].split()) == {
        "/var/lib/hermes/%i/state",
        "/run/hermes-bot-locks",
    }
    assert "TemporaryFileSystem=/var/lib/tybot/archive:ro" in lines
    assert "BindReadOnlyPaths=/var/lib/tybot/archive/%i" in lines
    hidden = next(line for line in lines if line.startswith("InaccessiblePaths="))
    assert "/var/lib/tybot/archive/%i/dm" in hidden


def test_archiver_unit_keeps_secrets_and_domain_out_of_the_template():
    text = UNIT.read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/hermes/%i/hermes.env" in text
    assert "ExecStartPre=/usr/bin/test -f /etc/hermes/%i/privacy-manifest.json" in text
    for name in (
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "ANTHROPIC_API_KEY",
        "HERMES_ARCHIVER_WORKSPACE",
        "HERMES_DOMAIN",
    ):
        assert f"Environment={name}" not in text


def test_archiver_unit_hides_unrelated_tybot_state():
    lines = _directives()
    hidden = next(line for line in lines if line.startswith("InaccessiblePaths="))

    for path in (
        "/var/lib/tybot/state",
        "/var/lib/tybot/archiver-shadow",
        "/var/lib/tybot/qa-log",
    ):
        assert path in hidden
    assert "NoNewPrivileges=true" in lines
    assert "ProtectSystem=strict" in lines
    assert "RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX" in lines


def test_manifest_refresh_is_per_instance_and_publishes_atomically():
    text = MANIFEST_SERVICE.read_text(encoding="utf-8")

    assert "User=tybot" in text
    assert "Environment=TYBOT_ENV_FILE=/etc/tybot/tybot.env" in text
    assert "EnvironmentFile=/etc/hermes/%i/privacy.env" in text
    # systemd PID 1 reads the mandatory EnvironmentFile as root. Rechecking it in
    # ExecStartPre runs as tybot and incorrectly requires traverse access to the
    # sibling directory that also contains Hermes secrets.
    assert "ExecStartPre=/usr/bin/test -f /etc/hermes/%i/privacy.env" not in text
    assert "ExecStartPre=/usr/local/libexec/hermes-archiver-run workspace %i" in text
    assert "--workspace ${HERMES_PRIVACY_WORKSPACE}" in text
    assert "/var/lib/tybot/state/hermes-privacy-%i.json" in text
    assert "/etc/hermes/%i/.privacy-manifest.json.new" in text
    assert "-o root -g hermes-%i -m 0640" in text
    assert "ExecStartPost=+/usr/bin/mv -f" in text
    assert "ExecStartPost=+/usr/local/libexec/hermes-archiver-run recover %i" in text


def test_manifest_refreshes_before_expiry_and_before_hermes_starts():
    timer = MANIFEST_TIMER.read_text(encoding="utf-8")
    lines = _directives()

    assert "OnUnitActiveSec=12h" in timer
    assert "OnBootSec=1min" in timer
    assert "RandomizedDelaySec=1min" in timer
    assert "Unit=hermes-privacy-manifest@%i.service" in timer
    assert "Wants=network-online.target hermes-privacy-manifest@%i.service" in lines
    assert "After=network-online.target hermes-privacy-manifest@%i.service" in lines


def test_install_places_templates_without_enabling_an_unconfigured_instance():
    text = INSTALL.read_text(encoding="utf-8")

    for name in (
        "hermes-archiver@.service",
        "hermes-privacy-manifest@.service",
        "hermes-privacy-manifest@.timer",
    ):
        assert f'deploy/{name}" /etc/systemd/system/{name}' in text
        assert f"enable --now {name}" not in text
    assert 'deploy/hermes-archiver-run" /usr/local/libexec/hermes-archiver-run' in text
    assert 'deploy/hermes-bot-locks.conf" /etc/tmpfiles.d/hermes-bot-locks.conf' in text


def test_runbook_uses_two_isolated_instances_and_stops_legacy_first():
    text = RUNBOOK.read_text(encoding="utf-8")

    assert "hermes-archiver@tyit.service" in text
    assert "hermes-archiver@invest.service" in text
    assert "hermes-tyit" in text
    assert "hermes-invest" in text
    assert "HERMES_DOMAIN=enterprise" in text
    assert "HERMES_DOMAIN=pf-construction" in text
    assert "/var/lib/hermes/tyit/state" in text
    assert "/var/lib/hermes/invest/state" in text
    old_stop = text.index("sudo systemctl stop hermes.service")
    new_start = text.index("sudo systemctl start hermes-archiver@tyit.service")
    assert old_stop < new_start


def test_runbook_requires_a_two_week_internal_pilot_before_pf_instance():
    text = RUNBOOK.read_text(encoding="utf-8")

    pilot = text.index("## 7. 사내 14일 파일럿")
    pf = text.index("## 8. PF 인스턴스 추가")
    rollback = text.index("## 9. 롤백")
    assert pilot < pf < rollback
    assert "14일 연속" in text
    assert "권한 밖 채널 노출 0건" in text
    assert "중복 질문 답변과 중복 검토 DM 0건" in text


def test_start_wrapper_locks_by_expected_slack_bot_not_instance():
    text = (ROOT / "deploy" / "hermes-archiver-run").read_text(encoding="utf-8")
    tmpfiles = (ROOT / "deploy" / "hermes-bot-locks.conf").read_text(
        encoding="utf-8"
    )

    assert "HERMES_SLACK_BOT_USER_ID" in text
    assert 'workspace" != "$instance' in text
    assert '$lock_dir/$bot_user_id.lock' in text
    assert "/usr/bin/flock -n 9" in text
    assert '$lock_dir/$instance.lock' not in text
    assert "^[a-z][a-z0-9]{0,15}$" in text
    assert "2770 root hermes-runtime" in tmpfiles
    writable = next(
        line for line in _directives() if line.startswith("ReadWritePaths=")
    )
    assert "/run/hermes-bot-locks" in writable.split("=", 1)[1].split()


def test_runtime_checks_slack_identity_before_opening_socket_mode():
    source = (ROOT / "subbots" / "hermes" / "src" / "index.js").read_text(
        encoding="utf-8"
    )
    identity = (
        ROOT / "subbots" / "hermes" / "src" / "slack" / "identity.js"
    ).read_text(encoding="utf-8")

    auth = source.index("await app.client.auth.test()")
    check = source.index("assertSlackIdentity(", auth)
    socket = source.index("await app.start()", check)
    assert auth < check < socket
    assert "teamId !== expectedTeamId" in identity
    assert "botUserId !== expectedBotUserId" in identity


@pytest.mark.skipif(NODE is None, reason="node 가 없어 신원 판정 실행 시험을 건너뛴다")
def test_slack_identity_rejects_each_mismatch():
    code = (
        f"const m = await import({json.dumps(IDENTITY.as_uri())});"
        "const cases = ["
        "  [{team_id:'T1',user_id:'U1'},'T1','U1'],"
        "  [{team_id:'T2',user_id:'U1'},'T1','U1'],"
        "  [{team_id:'T1',user_id:'U2'},'T1','U1'],"
        "  [{team_id:'T1',user_id:'U1'},'','U1']"
        "];"
        "const out = cases.map(([a,t,u]) => {"
        "  try { m.assertSlackIdentity(a,t,u); return 'ok'; }"
        "  catch (e) { return e.name; }"
        "});"
        "process.stdout.write(JSON.stringify(out));"
    )
    done = subprocess.run(
        [NODE, "--input-type=module", "-e", code],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [
        "ok",
        "SlackIdentityError",
        "SlackIdentityError",
        "SlackIdentityError",
    ]


def test_archiver_runtime_manifest_does_not_request_ingestion_scopes():
    text = SLACK_MANIFEST.read_text(encoding="utf-8")

    bot_scopes = set(re.findall(r"^\s+-\s+([a-z_]+:[a-z_]+)", text, re.MULTILINE))
    assert "files:read" not in bot_scopes
    assert "channels:join" not in bot_scopes
    for required in (
        "app_mentions:read",
        "channels:history",
        "groups:history",
        "im:history",
        "im:write",
        "chat:write",
    ):
        assert required in bot_scopes
