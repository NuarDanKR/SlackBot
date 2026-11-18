"""질문·답변 원본 — VIPBot 감사기록도 읽고, 어느 봇이 답했는지 폴더로 가른다."""
from __future__ import annotations

import json
from datetime import date

from tybot.console import answer_records
from tybot.console.auth import ADMIN, ConsoleUser


def _write(directory, rows):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "qa-2026-11.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _row(rid, ts, **over):
    row = {"record_id": rid, "ts": ts, "workspace": "mgmt", "user": "U1",
           "question": "q", "answer": "a"}
    row.update(over)
    return row


def _admin():
    return ConsoleUser(email="a@taeyoung.com", name="관리자", role=ADMIN, workspaces=frozenset(), all_workspaces=True)


def _dirs(tmp_path, monkeypatch):
    ty, vip = tmp_path / "qa", tmp_path / "vip"
    monkeypatch.setenv("QA_LOG_DIR", str(ty))
    monkeypatch.setenv("VIPBOT_QA_LOG_DIR", str(vip))
    _write(ty, [_row("a" * 32, "2026-11-18T09:31:00+09:00")])
    _write(vip, [_row("b" * 32, "2026-11-12T16:03:00+09:00")])
    return ty, vip


def test_vip_records_open_with_their_bot(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch)
    row, _ = answer_records.find_visible(_admin(), "b" * 32)
    assert row is not None
    detail = answer_records.detail(row, [])
    assert detail["bot"] == "vipbot"
    assert detail["question"] == "q"

    ty_row, _ = answer_records.find_visible(_admin(), "a" * 32)
    assert answer_records.detail(ty_row, [])["bot"] == "tybot"


def test_list_can_filter_by_bot(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch)
    span = dict(start=date(2026, 11, 1), end=date(2026, 11, 30))
    rows, _ = answer_records.visible_records(_admin(), **span)
    assert [r["bot"] for r in answer_records.summaries(rows, [])] == ["tybot", "vipbot"]
    vip, _ = answer_records.visible_records(_admin(), bot="vipbot", **span)
    assert [answer_records.record_key(r) for r in vip] == ["b" * 32]


def test_same_folder_is_not_read_twice(tmp_path, monkeypatch):
    ty, _ = _dirs(tmp_path, monkeypatch)
    monkeypatch.setenv("VIPBOT_QA_LOG_DIR", str(ty))
    rows, _ = answer_records.visible_records(_admin(), start=date(2026, 11, 1), end=date(2026, 11, 30))
    assert len(rows) == 1 and answer_records.answer_bot(rows[0]) == "tybot"
