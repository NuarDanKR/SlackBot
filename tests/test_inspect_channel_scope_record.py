"""The offline QA inspector reports coordinate scope, never authorization success."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


@pytest.fixture
def cli():
    path = Path(__file__).resolve().parents[1] / "scripts/inspect_channel_scope_record.py"
    spec = importlib.util.spec_from_file_location("inspect_channel_scope_record", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def row(**kwargs):
    return {"workspace": "tyit", "channel_id": "C0BQYAC28F3",
            "request_ts": "1792116000.123456", "reason": "answered",
            "decision_id": "1234567890abcdef", "final_responder": "hermes",
            "attempted_specialists": ["hermes"],
            "question": "PRIVATE_QUESTION", "answer": "PRIVATE_ANSWER",
            "evidence_refs": [{"workspace": "tyit", "channel_id": "C0BQYAC28F3",
                               "document_path": "PRIVATE_PATH", "text": "PRIVATE_TEXT"}],
            **kwargs}


def check(cli, tmp_path, rows, **kwargs):
    path = tmp_path / "qa.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in rows), encoding="utf-8")
    before = path.read_bytes()
    result = cli.inspect(path, workspace="tyit", channel_id="C0BQYAC28F3", **kwargs)
    assert path.read_bytes() == before
    return result


def test_only_coordinates_are_exposed(cli, tmp_path):
    result, code = check(cli, tmp_path, [row()])
    assert code == 0 and result["status"] == "recorded_refs_in_channel"
    assert "PRIVATE" not in json.dumps(result)
    assert result["rpc_verified"] is False
    assert result["all_tool_reads_verified"] is False


@pytest.mark.parametrize("field", ["evidence_refs", "attachment_refs"])
def test_foreign_reference_is_not_a_success(cli, tmp_path, field):
    result, code = check(cli, tmp_path, [row(**{
        field: [{"workspace": "mgmt", "channel_id": "C0SECRET001"}],
    })])
    assert code == 1 and result["status"] == "outside_channel_reference"
    assert result["outside_scopes"] == [{"workspace": "mgmt", "channel_id": "C0SECRET001"}]


@pytest.mark.parametrize("refs", [[], None, [{}], "not-a-list"])
def test_missing_or_invalid_references_cannot_prove_scope(cli, tmp_path, refs):
    result, code = check(cli, tmp_path, [row(evidence_refs=refs)])
    assert code == 2
    assert result["status"] in {"references_unavailable", "record_incomplete_or_ambiguous"}


def test_duplicate_requests_are_ambiguous(cli, tmp_path):
    result, code = check(cli, tmp_path, [row(), row()])
    assert code == 2 and result["status"] == "record_incomplete_or_ambiguous"


def test_latest_uses_request_time_not_append_order(cli, tmp_path):
    result, code = check(cli, tmp_path, [row(), row(request_ts="1792115000.000001", reason="no_access")])
    assert code == 0 and result["reason"] == "answered"


def test_exact_request_can_select_the_failed_baseline(cli, tmp_path):
    result, code = check(cli, tmp_path, [row(), row(request_ts="1792115000.000001", reason="no_access")],
                         request_ts="1792115000.000001")
    assert code == 2 and result["status"] == "not_answered"
    assert result["reason"] == "no_access"


def test_wrong_channel_record_is_not_selected(cli, tmp_path):
    result, code = check(cli, tmp_path, [row(channel_id="C0SECRET001")])
    assert code == 2 and result["status"] == "record_not_found"


def test_corrupt_log_is_not_silently_clean(cli, tmp_path):
    path = tmp_path / "qa.jsonl"
    path.write_text(json.dumps(row()) + "\ninvalid\n", encoding="utf-8")
    result, code = cli.inspect(path, workspace="tyit", channel_id="C0BQYAC28F3")
    assert code == 2 and result["unreadable_records"] == 1


def test_dm_channel_is_refused_without_current_membership_verification(cli, tmp_path):
    with pytest.raises(ValueError):
        cli.inspect(tmp_path / "missing", workspace="tyit", channel_id="D0SECRET001")


def test_missing_file_is_unknown_and_does_not_expose_path(cli, tmp_path, capsys):
    assert cli.main(["--qa-file", str(tmp_path / "PRIVATE_PATH"), "--workspace", "tyit",
                     "--channel", "C0BQYAC28F3", "--latest"]) == 2
    assert "PRIVATE" not in capsys.readouterr().out


def test_cli_success_emits_only_metadata(cli, tmp_path, capsys):
    path = tmp_path / "qa.jsonl"
    path.write_text(json.dumps(row()), encoding="utf-8")
    before = path.read_bytes()
    assert cli.main(["--qa-file", str(path), "--workspace", "tyit",
                     "--channel", "C0BQYAC28F3", "--latest"]) == 0
    output = capsys.readouterr().out
    assert json.loads(output)["status"] == "recorded_refs_in_channel"
    assert "PRIVATE" not in output
    assert path.read_bytes() == before
