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


@pytest.mark.parametrize("level", ["debug", "", "INFO ERROR"])
def test_service_logs_reject_invalid_level(level):
    with pytest.raises(service_logs.ServiceLogError):
        service_logs.read(level=level, limit=20)


@pytest.mark.parametrize("limit", [0, 501])
def test_service_logs_reject_invalid_limit(limit):
    with pytest.raises(service_logs.ServiceLogError):
        service_logs.read(level="error", limit=limit)
