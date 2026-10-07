from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "deploy" / "hermes-pf-archiver.service"
RUNBOOK = ROOT / "docs" / "deploy" / "hermes-pf-archiver.md"


def _directives() -> list[str]:
    return [
        line.strip()
        for line in UNIT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_pf_archiver_unit_fixes_read_only_mode_and_roots():
    lines = _directives()

    assert "Environment=HERMES_MODE=pf-archiver" in lines
    assert "Environment=HERMES_DATA_ROOT=/var/lib/hermes-pf" in lines
    assert "Environment=HERMES_ARCHIVER_ROOT=/var/lib/tybot/archive" in lines
    assert (
        "Environment=HERMES_PRIVACY_MANIFEST=/etc/hermes-pf/privacy-manifest.json"
        in lines
    )
    assert "ExecStart=/usr/bin/node scripts/run-server.js" in lines
    assert "ExecStartPre=/usr/bin/node scripts/check-archiver-privacy.js" in lines
    assert not any("run-ingest" in line for line in lines)


def test_pf_archiver_unit_never_opens_the_canonical_archive_for_writing():
    lines = _directives()
    writable = next(line for line in lines if line.startswith("ReadWritePaths="))
    readonly = next(line for line in lines if line.startswith("ReadOnlyPaths="))

    assert "/var/lib/tybot/archive" in readonly.split("=", 1)[1].split()
    assert "/etc/hermes-pf/privacy-manifest.json" in readonly.split("=", 1)[1].split()
    assert "/var/lib/tybot/archive" not in writable.split("=", 1)[1].split()
    assert set(writable.split("=", 1)[1].split()) == {
        "/var/lib/hermes-pf",
        "/opt/tybot/subbots/hermes/logs",
    }


def test_pf_archiver_unit_keeps_secrets_out_of_the_repository():
    text = UNIT.read_text(encoding="utf-8")

    assert "EnvironmentFile=/etc/hermes-pf/hermes.env" in text
    assert "ConditionPathExists=/etc/hermes-pf/privacy-manifest.json" in text
    for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "ANTHROPIC_API_KEY"):
        assert f"Environment={name}" not in text
    assert "HERMES_ARCHIVER_WORKSPACE=" not in text


def test_pf_archiver_unit_hides_unrelated_tybot_state():
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


def test_runbook_stops_the_old_instance_before_starting_the_new_one():
    text = RUNBOOK.read_text(encoding="utf-8")
    old_stop = text.index("sudo systemctl stop hermes.service")
    new_start = text.index("sudo systemctl start hermes-pf-archiver.service")

    assert old_stop < new_start
    assert "node scripts/run-ingest.js" in text
    assert "종료 코드 2" in text
    assert "setfacl" in text
    assert "전체 Python·Hermes 시험" in text


def test_runbook_requires_a_two_week_internal_pilot_before_pf_rollout():
    text = RUNBOOK.read_text(encoding="utf-8")

    pilot = text.index("## 7. 사내 14일 파일럿")
    pf_gate = text.index("### 7.3 PF 승격 조건")
    rollback = text.index("## 8. 롤백")

    assert pilot < pf_gate < rollback
    assert "14일 연속" in text
    assert "권한 밖 채널 노출 0건" in text
    assert "중복 검토 DM 0건" in text
    assert "원문 쓰기 및 Git 아카이브 쓰기 0건" in text
