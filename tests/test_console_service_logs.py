from __future__ import annotations

from subprocess import CompletedProcess

import pytest

from tybot.console import service_logs


def test_service_logs_redact_secrets(monkeypatch):
    output = "\n".join(
        [
            "INFO token=xoxb-real-secret",
            "ERROR key=sk-ant-api-secret",
            "WARNING postgresql://user:password@db.internal:5432/tyslackai",
            "INFO password=plain-secret",
        ]
    )

    monkeypatch.setattr(
        service_logs.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, stdout=output, stderr=""),
    )

    body = "\n".join(row["message"] for row in service_logs.read(level="info", limit=20))
    assert "real-secret" not in body
    assert "api-secret" not in body
    assert "user:password" not in body
    assert "plain-secret" not in body
    assert "xoxb-***" in body


def test_service_logs_preserve_multiline_traceback(monkeypatch):
    first = "\n".join(
        [
            "2026-09-02 10:00:00,000 tybot.slack ERROR 일정 조회 실패",
            "Traceback (most recent call last):",
            '  File "/opt/tybot/src/tybot/slack/pilot.py", line 10, in handler',
            "    raise RuntimeError('failed')",
            "RuntimeError: failed",
        ]
    )
    second = "2026-09-02 10:01:00,000 tybot.slack ERROR 다음 오류"
    output = f"{first}\n{service_logs.RECORD_SEPARATOR}\n{second}\n{service_logs.RECORD_SEPARATOR}\n"
    monkeypatch.setattr(
        service_logs.subprocess,
        "run",
        lambda *args, **kwargs: CompletedProcess(args[0], 0, stdout=output, stderr=""),
    )

    rows = service_logs.read(level="error", limit=20)

    assert len(rows) == 2
    assert "Traceback (most recent call last):" in rows[0]["message"]
    assert 'File "/opt/tybot/src/tybot/slack/pilot.py"' in rows[0]["message"]
    assert rows[0]["message"].endswith("RuntimeError: failed")
    assert rows[1]["message"] == second


@pytest.mark.parametrize("level", ["debug", "", "INFO ERROR"])
def test_service_logs_reject_invalid_level(level):
    with pytest.raises(service_logs.ServiceLogError):
        service_logs.read(level=level, limit=20)


@pytest.mark.parametrize("limit", [0, 501])
def test_service_logs_reject_invalid_limit(limit):
    with pytest.raises(service_logs.ServiceLogError):
        service_logs.read(level="error", limit=limit)
