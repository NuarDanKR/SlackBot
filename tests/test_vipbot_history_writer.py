from __future__ import annotations

import json
import os
from datetime import datetime
from types import SimpleNamespace

import pytest

from tybot.audit import KST
from tybot.evidence_refs import ARCHIVE_LINE, AttachmentRef, EvidenceRef
from tybot.vipbot_history import load_vip_history
from tybot.vipbot_history_writer import record_vip_turn

NOW = datetime(2026, 11, 9, 13, 14, 15, tzinfo=KST)
WS = "tyit"
USER = "U0VIP0001"


def _answer(**changes):
    values = {
        "text": "THIS MODEL OUTPUT MUST NEVER BE WRITTEN",
        "citations": ["THIS RENDERED CITATION MUST NEVER BE WRITTEN"],
        "reason": "answered",
        "subject_terms": ["미수금"],
        "evidence_refs": [
            EvidenceRef(
                kind=ARCHIVE_LINE,
                workspace=WS,
                channel_id="C0SOURCE01",
                document_path="workspaces/tyit/channels/C0SOURCE01/raw/2026-11-09.md",
                line_no=7,
            )
        ],
        "attachment_refs": [],
        "context_parent_ids": ["parent-1"],
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _rows(tmp_path):
    path = tmp_path / "qa-2026-11.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_a_delivered_turn_writes_only_the_question_and_reopenable_coordinates(tmp_path):
    assert record_vip_turn(
        tmp_path,
        workspace=WS,
        slack_user=USER,
        question="미수금 현황을 다시 확인해줘",
        answer=_answer(),
        now=NOW,
    )

    (row,) = _rows(tmp_path)
    assert set(row) == {
        "record_id",
        "ts",
        "workspace",
        "user",
        "question",
        "intent_kind",
        "subject_terms",
        "evidence_refs",
        "attachment_refs",
        "context_parent_ids",
    }
    assert row["question"] == "미수금 현황을 다시 확인해줘"
    assert row["evidence_refs"][0]["line_no"] == 7
    blob = json.dumps(row, ensure_ascii=False)
    assert "MODEL OUTPUT" not in blob
    assert "RENDERED CITATION" not in blob
    assert "answer" not in row and "citations" not in row


def test_the_reader_can_load_the_writer_output_without_a_second_format(tmp_path):
    record_vip_turn(
        tmp_path,
        workspace=WS,
        slack_user=USER,
        question="이전 질문",
        answer=_answer(),
        now=NOW,
    )

    turns = load_vip_history(tmp_path, workspace=WS, slack_user=USER, now=NOW)

    assert len(turns) == 1
    assert turns[0]["question"] == "이전 질문"
    assert turns[0]["evidence_refs"][0].channel_id == "C0SOURCE01"


def test_attachment_coordinates_are_kept_but_model_text_is_not(tmp_path):
    answer = _answer(
        evidence_refs=[],
        attachment_refs=[AttachmentRef(WS, "C0SOURCE01", "F0FILE001")],
    )

    assert record_vip_turn(
        tmp_path,
        workspace=WS,
        slack_user=USER,
        question="첨부를 다시 봐줘",
        answer=answer,
        now=NOW,
    )

    assert _rows(tmp_path)[0]["attachment_refs"] == [
        {"workspace": WS, "channel_id": "C0SOURCE01", "file_id": "F0FILE001"}
    ]


@pytest.mark.parametrize(
    ("workspace", "user", "question"),
    [
        ("", USER, "질문"),
        ("Bad Workspace", USER, "질문"),
        (WS, "bad", "질문"),
        (WS, USER, ""),
    ],
)
def test_invalid_coordinates_or_an_empty_question_write_nothing(
    tmp_path, workspace, user, question
):
    assert not record_vip_turn(
        tmp_path,
        workspace=workspace,
        slack_user=user,
        question=question,
        answer=_answer(),
        now=NOW,
    )
    assert list(tmp_path.iterdir()) == []


def test_an_answer_without_reopenable_coordinates_is_not_history(tmp_path):
    assert not record_vip_turn(
        tmp_path,
        workspace=WS,
        slack_user=USER,
        question="근거 없는 질문",
        answer=_answer(evidence_refs=[], attachment_refs=[]),
        now=NOW,
    )
    assert list(tmp_path.iterdir()) == []


def test_malformed_coordinates_fail_closed_without_writing(tmp_path):
    answer = _answer(evidence_refs=[{"not": "an EvidenceRef"}])

    assert not record_vip_turn(
        tmp_path,
        workspace=WS,
        slack_user=USER,
        question="질문",
        answer=answer,
        now=NOW,
    )
    assert list(tmp_path.iterdir()) == []


def test_each_append_is_one_complete_json_line(tmp_path):
    for question in ("첫 질문", "둘째 질문"):
        assert record_vip_turn(
            tmp_path,
            workspace=WS,
            slack_user=USER,
            question=question,
            answer=_answer(),
            now=NOW,
        )

    assert [row["question"] for row in _rows(tmp_path)] == ["첫 질문", "둘째 질문"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX file mode contract")
def test_the_history_file_is_private(tmp_path):
    record_vip_turn(
        tmp_path,
        workspace=WS,
        slack_user=USER,
        question="질문",
        answer=_answer(),
        now=NOW,
    )
    assert (tmp_path / "qa-2026-11.jsonl").stat().st_mode & 0o777 == 0o600


def test_an_unwritable_destination_does_not_break_the_answer_path(tmp_path):
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")

    assert not record_vip_turn(
        blocker,
        workspace=WS,
        slack_user=USER,
        question="질문",
        answer=_answer(),
        now=NOW,
    )
