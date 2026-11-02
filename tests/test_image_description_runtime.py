from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from tybot.archive.image_description_runtime import (
    SCHEMA,
    ImageDescriptionRuntimeStatus,
    failed_status,
    status_path,
    write_status,
)

NOW = "2026-10-28T12:00:00+00:00"


def _status(**over):
    values = {
        "workspace": "tyit",
        "state": "ok",
        "ran_at": NOW,
        "policy_revision": "global:local",
        "selected": 2,
        "published": 2,
    }
    values.update(over)
    return ImageDescriptionRuntimeStatus(**values)


def test_status_file_contains_aggregate_coordinates_only(tmp_path):
    path = write_status(tmp_path, _status())
    body = json.loads(path.read_text(encoding="utf-8"))

    assert path == tmp_path / "image-description" / "status" / "tyit.json"
    assert body["schema"] == SCHEMA
    assert body["published"] == 2
    assert "description" not in body
    assert "file_id" not in body
    assert "channel" not in body


def test_status_write_replaces_an_existing_file(tmp_path):
    write_status(tmp_path, _status(state="partial", unknown=1))
    path = write_status(tmp_path, _status())

    assert json.loads(path.read_text(encoding="utf-8"))["state"] == "ok"
    assert not list(path.parent.glob("*.tmp-*"))


@pytest.mark.parametrize("workspace", ["", "../tyit", "TYIT", "tyit/x"])
def test_workspace_cannot_escape_the_status_directory(tmp_path, workspace):
    with pytest.raises(ValueError, match="workspace_invalid"):
        status_path(tmp_path, workspace)


@pytest.mark.parametrize("field,value", [("selected", -1), ("unknown", 1.2)])
def test_counts_are_non_negative_integers(field, value):
    with pytest.raises(ValueError, match="count_invalid"):
        _status(**{field: value})


def test_status_timestamp_must_have_a_timezone():
    with pytest.raises(ValueError, match="ran_at_invalid"):
        _status(ran_at="2026-10-28T12:00:00")


def test_failed_status_does_not_accept_exception_text():
    status = failed_status("tyit", "password=/secret")

    assert status.error_code == "runtime_failed"
    assert datetime.fromisoformat(status.ran_at).utcoffset() == UTC.utcoffset(None)
