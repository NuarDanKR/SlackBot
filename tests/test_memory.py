"""기억 질문 경로 — "이전 답변 기억나?" 가 상태 응답으로 새지 않아야 한다."""
from __future__ import annotations

import json

import pytest

from tybot.audit import QALog, QARecord
from tybot.intent import MAX_TASKS, classify_by_rule, plan_by_rule


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


# 좁은 특수 처리(memory_companion_query)는 일반 분해(plan_by_rule)로 대체됐다.
# 실제 사고: "기억나? 그리고 전산팀은 무슨 일 있어?" 에서 두 번째 질문이 답변에
# 아예 반영되지 않았다 - 라벨을 하나만 고르는 구조였기 때문이다.
@pytest.mark.parametrize(
    ("text", "kinds"),
    [
        (
            "다시, 너가 예전에 했던 말 기억나? "
            "그리고 지금 전산팀 워크스페이스에서는 무슨 일이 벌어지고 있어?",
            ["memory", "summary"],
        ),
        ("전산팀 현황 알려줘. 이전 답변도 기억나?", ["summary", "memory"]),
        ("이전 답변 기억나?", ["memory"]),
        ("기억나? 왜?", ["memory"]),
    ],
)
def test_compound_question_is_split_into_tasks(text, kinds):
    assert [task.kind for task in plan_by_rule(text)] == kinds


def test_each_task_carries_its_own_question_text():
    """하위질문 원문이 있어야 답변 생성이 그 절만 보고 답할 수 있다."""
    tasks = plan_by_rule("이전 답변 기억나? 그리고 김해외동 기성금 얼마야?")
    assert len(tasks) == 2
    assert "기억" in tasks[0].question
    assert "김해외동" in tasks[1].question
    assert "기억" not in tasks[1].question


def test_single_question_keeps_full_text_as_question():
    (task,) = plan_by_rule("이전 답변 기억나?")
    assert task.question == "이전 답변 기억나?"


def test_write_intent_is_never_mixed_with_others():
    """수집 지시가 섞이면 그것만 남긴다 - 실행 대상이 모호하면 실행하지 않는다."""
    tasks = plan_by_rule("수집해. 그리고 상태 알려줘")
    assert [t.kind for t in tasks] == ["ingest"]


def test_planner_preserves_over_cap_count_for_user_notice():
    tasks = plan_by_rule(
        "상태 어때? 그리고 기억나? 그리고 김해외동 기성금 얼마야? "
        "그리고 이번주 요약해줘? 그리고 도움말 알려줘?"
    )
    assert len(tasks) > MAX_TASKS


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
