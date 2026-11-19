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


def test_vip_body_is_hidden_from_everyone_but_the_asker(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch)
    row, _ = answer_records.find_visible(_admin(), "b" * 32)
    row["citations"] = ["인사안.xlsx"]
    row["task_traces"] = [{"task_kind": "summary", "query": "본문 조각", "result": "ok"}]

    monkeypatch.setattr(answer_records, "review_scope", lambda email: answer_records.ReviewScope())
    assert not answer_records.body_visible(row, _admin())
    hidden = answer_records.detail(row, [], show_body=False)
    assert hidden["bodyHidden"] is True
    assert hidden["question"] == hidden["answer"] == "" and hidden["citations"] == []
    assert hidden["taskTraces"] == [{"task_kind": "summary", "result": "ok"}]
    assert hidden["bot"] == "vipbot" and hidden["model"] == "-"

    asker = answer_records.ReviewScope(own_users=frozenset({("mgmt", "U1")}))
    monkeypatch.setattr(answer_records, "review_scope", lambda email: asker)
    assert answer_records.body_visible(row, _admin())


def test_tybot_body_stays_visible(tmp_path, monkeypatch):
    _dirs(tmp_path, monkeypatch)
    monkeypatch.setattr(answer_records, "review_scope", lambda email: answer_records.ReviewScope())
    row, _ = answer_records.find_visible(_admin(), "a" * 32)
    assert answer_records.body_visible(row, _admin())
    assert answer_records.detail(row, [])["bodyHidden"] is False


def test_direct_answers_join_the_routing_list(tmp_path, monkeypatch):
    """봇 분류 상태 — 전문 봇 없이 답한 질문도 답변 봇과 함께 나온다."""
    _dirs(tmp_path, monkeypatch)
    rows = answer_records.direct_answer_rows(exclude_keys=set(), allowed=None)
    assert [(r["bot"], r["result"], r["specialist"]) for r in rows] == [
        ("tybot", "direct", ""), ("vipbot", "direct", ""),
    ]
    assert rows[0]["at"] > rows[1]["at"]              # 최신이 먼저
    assert "question" not in rows[0]                  # 본문은 싣지 않는다

    only_vip = answer_records.direct_answer_rows(exclude_keys=set(), allowed=None, bot="vipbot")
    assert [r["qaRecordKey"] for r in only_vip] == ["b" * 32]
    # 이미 전문 봇 호출로 잡힌 질문은 두 번 나오지 않는다
    assert answer_records.direct_answer_rows(exclude_keys={"a" * 32, "b" * 32}, allowed=None) == []
    # 볼 수 없는 워크스페이스는 뺀다
    assert answer_records.direct_answer_rows(exclude_keys=set(), allowed={"civil"}) == []
