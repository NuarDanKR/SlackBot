from __future__ import annotations

import json
from datetime import datetime, timedelta

from tybot.audit import KST
from tybot.vipbot_history import VIP_HISTORY_DAYS, load_vip_history

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=KST)


def _ref(*, workspace: str = "tyit", channel: str = "C01234567") -> dict:
    return {
        "kind": "archive_line",
        "workspace": workspace,
        "channel_id": channel,
        "document_path": f"workspaces/{workspace}/channels/{channel}/raw/2026-11-01.md",
        "line_no": 3,
        "source_ts": "1790000000.000001",
        "content_hash": "a" * 64,
        "message_ts": "",
    }


def _row(**changes) -> dict:
    row = {
        "record_id": "record-1",
        "ts": (NOW - timedelta(days=1)).isoformat(),
        "workspace": "tyit",
        "user": "U01234567",
        "question": "지난 현황을 다시 확인해줘",
        "answer": "THIS MODEL OUTPUT MUST NEVER RETURN",
        "legacy_answer": "NOR THIS LEGACY OUTPUT",
        "intent_kind": "summary",
        "subject_terms": ["현황"],
        "evidence_refs": [_ref()],
        "attachment_refs": [],
        "context_parent_ids": ["parent-1"],
    }
    row.update(changes)
    return row


def _write(tmp_path, *rows: dict, month: str = "2026-11") -> None:
    path = tmp_path / f"qa-{month}.jsonl"
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8"
    )


def test_vip_history_returns_questions_and_coordinates_but_never_answers(tmp_path):
    _write(tmp_path, _row())

    turns = load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW)

    assert len(turns) == 1
    assert turns[0]["question"] == "지난 현황을 다시 확인해줘"
    assert turns[0]["evidence_refs"][0].channel_id == "C01234567"
    assert "answer" not in turns[0]
    assert "legacy_answer" not in turns[0]
    assert "THIS MODEL OUTPUT" not in repr(turns)


def test_vip_history_is_bound_to_the_same_workspace_and_user(tmp_path):
    _write(
        tmp_path,
        _row(record_id="allowed"),
        _row(record_id="other-user", user="U99999999"),
        _row(record_id="other-workspace", workspace="mgmt"),
    )

    turns = load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW)

    assert [turn["record_id"] for turn in turns] == ["allowed"]


def test_vip_history_excludes_expired_future_and_unreferenced_turns(tmp_path):
    _write(
        tmp_path,
        _row(
            record_id="expired", ts=(NOW - timedelta(days=VIP_HISTORY_DAYS, seconds=1)).isoformat()
        ),
        _row(record_id="future", ts=(NOW + timedelta(seconds=1)).isoformat()),
        _row(record_id="unsupported", evidence_refs=[], attachment_refs=[]),
        _row(record_id="allowed"),
    )

    turns = load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW)

    assert [turn["record_id"] for turn in turns] == ["allowed"]


def test_vip_history_accepts_attachment_only_turns(tmp_path):
    _write(
        tmp_path,
        _row(
            evidence_refs=[],
            attachment_refs=[
                {"workspace": "tyit", "channel_id": "C01234567", "file_id": "F01234567"}
            ],
        ),
    )

    turns = load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW)

    assert turns[0]["attachment_refs"][0].file_id == "F01234567"


def test_vip_history_orders_oldest_to_newest_and_applies_limit(tmp_path):
    _write(
        tmp_path,
        _row(record_id="new", ts=(NOW - timedelta(hours=1)).isoformat()),
        _row(record_id="old", ts=(NOW - timedelta(days=2)).isoformat()),
        _row(record_id="middle", ts=(NOW - timedelta(days=1)).isoformat()),
    )

    turns = load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW, limit=2)

    assert [turn["record_id"] for turn in turns] == ["middle", "new"]


def test_vip_history_reads_every_calendar_month_touched_by_the_window(tmp_path):
    _write(
        tmp_path,
        _row(record_id="october", ts=(NOW - timedelta(days=29)).isoformat()),
        month="2026-10",
    )
    _write(tmp_path, _row(record_id="november"))

    turns = load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW)

    assert [turn["record_id"] for turn in turns] == ["october", "november"]


def test_vip_history_ignores_files_outside_the_exact_time_window(tmp_path):
    _write(tmp_path, _row(record_id="future-name"), month="9999-12")
    _write(tmp_path, _row(record_id="allowed"))

    turns = load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW)

    assert [turn["record_id"] for turn in turns] == ["allowed"]


def test_vip_history_rejects_scalar_metadata_lists(tmp_path):
    _write(tmp_path, _row(subject_terms="not-a-list", context_parent_ids="not-a-list"))

    turns = load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW)

    assert turns[0]["subject_terms"] == []
    assert turns[0]["context_parent_ids"] == []


def test_vip_history_discards_malformed_coordinates_and_rows(tmp_path):
    path = tmp_path / "qa-2026-11.jsonl"
    path.write_text(
        "not-json\n"
        + json.dumps(_row(evidence_refs=[{"kind": "archive_line"}]), ensure_ascii=False),
        encoding="utf-8",
    )

    assert load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW) == []


def test_vip_history_fails_closed_for_invalid_limits_or_unreadable_root(tmp_path):
    assert (
        load_vip_history(tmp_path, workspace="tyit", slack_user="U01234567", now=NOW, days=0) == []
    )
    assert load_vip_history(tmp_path, workspace="", slack_user="U01234567", now=NOW) == []
    assert (
        load_vip_history(tmp_path / "missing", workspace="tyit", slack_user="U01234567", now=NOW)
        == []
    )
