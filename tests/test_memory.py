"""기억 질문 경로 — "이전 답변 기억나?" 가 상태 응답으로 새지 않아야 한다."""
from __future__ import annotations

import json

import pytest

from tybot.audit import QALog, QARecord
from tybot.intent import classify_by_rule, memory_companion_query


@pytest.mark.parametrize(
    "text",
    [
        "이전에 너가 했던 답변들이 기억나?",
        "아까 뭐라고 했지?",
        "우리 대화 기억해?",
        "맥락 유지돼?",
        "방금 말한 거 다시",
        "전에 한 답변 기억나",
    ],
)
def test_memory_questions_are_not_status(text):
    assert classify_by_rule(text).kind == "memory"


@pytest.mark.parametrize(
    "text,kind",
    [
        ("현재 연결상황 확인해줘", "status"),
        ("몇 건 수집했어?", "status"),
        ("내용 수집해", "ingest"),
        ("김해외동 기성금 얼마야?", "search"),
        ("이번주 요약", "summary"),
    ],
)
def test_other_intents_unaffected(text, kind):
    assert classify_by_rule(text).kind == kind


@pytest.mark.parametrize(
    "text,expected",
    [
        (
            "다시, 너가 예전에 했던 말 기억나? 그리고 지금 전산팀 워크스페이스에서는 무슨 일이 벌어지고 있어?",
            "지금 전산팀 워크스페이스에서는 무슨 일이 벌어지고 있어",
        ),
        ("전산팀 현황 알려줘. 이전 답변도 기억나?", "전산팀 현황 알려줘"),
        ("이전 답변 기억나?", None),
        ("기억나? 왜?", None),
    ],
)
def test_memory_companion_query_preserves_separate_work_question(text, expected):
    assert memory_companion_query(text) == expected


def _rec(**kw):
    base = dict(
        workspace="pilot", channel="#ch", channel_id="C1", user="U1", user_name="단라운",
        question="기성금 얼마야", intent_kind="search", intent_source="llm",
        reason="answered", hits=1, scope="채널 1개",
    )
    base.update(kw)
    return QARecord.build(**base)


def test_recent_for_user_returns_only_own_questions(tmp_path):
    log = QALog(tmp_path, write_md=False)
    log.write(_rec(user="U1", question="내 질문"))
    log.write(_rec(user="U2", user_name="남", question="남의 질문"))

    rows = log.recent_for_user("pilot", "U1")
    assert [q for _, q in rows] == ["내 질문"]


def test_recent_for_user_filters_by_workspace(tmp_path):
    log = QALog(tmp_path, write_md=False)
    log.write(_rec(workspace="pilot", question="파일럿 질문"))
    log.write(_rec(workspace="mgmt", question="경영 질문"))

    rows = log.recent_for_user("pilot", "U1")
    assert [q for _, q in rows] == ["파일럿 질문"]


def test_recent_for_user_newest_first_and_limited(tmp_path):
    log = QALog(tmp_path, write_md=False)
    for i in range(8):
        log.write(_rec(question=f"질문 {i}"))
    rows = log.recent_for_user("pilot", "U1", limit=3)
    assert len(rows) == 3
    assert rows == sorted(rows, reverse=True)


def test_recent_for_user_survives_broken_lines(tmp_path):
    log = QALog(tmp_path, write_md=False)
    log.write(_rec(question="정상 질문"))
    path = next(tmp_path.glob("qa-*.jsonl"))
    with path.open("a", encoding="utf-8") as f:
        f.write("깨진 줄\n")
        f.write(json.dumps({"workspace": "pilot"}, ensure_ascii=False) + "\n")
    assert [q for _, q in log.recent_for_user("pilot", "U1")] == ["정상 질문"]


def test_recent_for_user_without_user_id(tmp_path):
    log = QALog(tmp_path, write_md=False)
    log.write(_rec())
    assert log.recent_for_user("pilot", "") == []


def test_missing_log_dir_is_not_an_error(tmp_path):
    assert QALog(tmp_path / "없음").recent_for_user("pilot", "U1") == []
