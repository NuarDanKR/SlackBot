"""Canvas 산출물 계약 — 제목·Disclaimer·재수집 금지·판정 분리.

설계: `docs/design/pii-guardrail-and-canvas-artifacts.md` §3, §4, §7.2, §7.4
"""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from tybot import master_planner
from tybot.canvas_answer import (
    DISCLAIMER,
    DISCLAIMER_MARK,
    TITLE,
    TITLE_SUFFIX,
    create,
    is_generated,
    markdown,
    remember_generated,
)
from tybot.intent import Intent
from tybot.specialist_router import display_hint_for, is_execution_refusal


def _client(canvas_id: str = "F-CANVAS") -> Mock:
    client = Mock()
    client.canvases_create.return_value = {"canvas_id": canvas_id}
    client.files_info.return_value = {"file": {"permalink": "https://slack/canvas"}}
    return client


def _task(**kw) -> Intent:
    kw.setdefault("kind", "summary")
    kw.setdefault("question", "우리 팀 공지 채널 내용을 정리해줘")
    return Intent(**kw)


# --- §7.2-1,2,3 제목과 Disclaimer ---------------------------------------------
def test_title_comes_from_the_question_not_a_fixed_string():
    """§7.2-1 · 제목이 전부 같으면 Slack 목록에서 문서를 구별할 수 없다."""
    artifact = master_planner.artifact_for(
        _task(artifact_title="전산팀 공지 일정 정리", artifact_delivery="canvas")
    )
    assert artifact.title == "전산팀 공지 일정 정리"
    assert artifact.title_source == "planner"
    assert artifact.title != TITLE


def test_canvas_metadata_title_keeps_the_authorship_mark():
    """§7.2-2 · `{AI 제목} · TYBot`. 본문에는 제목을 다시 넣지 않는다."""
    artifact = master_planner.artifact_for(
        _task(artifact_title="전산팀 공지 일정 정리", artifact_delivery="canvas")
    )
    assert artifact.canvas_title == f"전산팀 공지 일정 정리{TITLE_SUFFIX}"

    client = _client()
    create(client, "본문", title=artifact.canvas_title)
    sent = client.canvases_create.call_args.kwargs
    assert sent["title"] == "전산팀 공지 일정 정리 · TYBot"
    body = sent["document_content"]["markdown"]
    assert "전산팀 공지 일정 정리" not in body.splitlines()[0]


def test_disclaimer_is_the_first_block():
    """§7.2-3 · 사람이 먼저 보는 것은 「이건 AI 문서다」 라는 사실이다."""
    rendered = markdown("결론\n\n출처: #채널")
    assert rendered.startswith(DISCLAIMER)
    assert f"# {TITLE}" not in rendered


# --- 제목 검증 ----------------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "TYBot 정식 답변",                 # 예전 고정 제목
        "제목\n두 번째 줄",                # 여러 줄
        "[링크](https://example.invalid)",  # Markdown 문법
        "x" * 80,                          # 너무 김
        "Canvas 답변",                     # 산출물 이름은 제목이 아니다
    ],
)
def test_bad_titles_fall_back_deterministically(bad):
    artifact = master_planner.artifact_for(_task(artifact_title=bad))
    assert artifact.title_source == "fallback"
    assert artifact.title == "우리 팀 공지 채널 내용을 정리해줘"


def test_title_may_not_invent_numbers_or_names_absent_from_the_question():
    """제목이 **새 사실**을 만들면 그건 근거 없는 문장이다."""
    invented = master_planner.artifact_for(
        _task(question="공지 일정 정리해줘", artifact_title="9월 12일 일정 정리")
    )
    assert invented.title_source == "fallback"

    named = master_planner.artifact_for(
        _task(question="공지 일정 정리해줘", artifact_title="김 부장 지시 정리")
    )
    assert named.title_source == "fallback"

    kept = master_planner.artifact_for(
        _task(question="9월 공지 일정 정리해줘", artifact_title="9월 공지 일정")
    )
    assert kept.title_source == "planner"

    substring = master_planner.artifact_for(
        _task(question="19개 공지 정리해줘", artifact_title="9개 공지 정리")
    )
    assert substring.title_source == "fallback"


def test_fallback_title_also_obeys_the_title_contract():
    artifact = master_planner.artifact_for(
        _task(
            question="[공지](https://example.invalid)를 Canvas에 정리해줘",
            research_question="공지 일정을 정리해줘",
            artifact_delivery="canvas",
            artifact_title="Canvas 답변",
        )
    )
    assert artifact.title == "공지 일정을 정리해줘"
    assert "Canvas" not in artifact.canvas_title
    assert "https://" not in artifact.canvas_title


# --- §7.2-5,6 재수집 금지 ------------------------------------------------------
def test_dynamic_title_canvas_is_still_excluded_from_collection(tmp_path, monkeypatch):
    """§7.2-5 · 제목이 달라져도 우리 문서를 다시 수집하지 않는다(원칙 1)."""
    from tybot.archive.canvas import is_generated_canvas

    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert is_generated_canvas("전산팀 공지 일정 정리 · TYBot") is True

    # 제목을 사람이 고쳐도 기록한 ID 로 걸린다.
    remember_generated("F-1", workspace="pilot", channel_id="C1", qa_record_id="Q1")
    assert is_generated(canvas_id="F-1") is True
    assert is_generated_canvas("사람이 고친 제목", "F-1") is True

    # 기록도 제목도 없으면 본문 표식이 마지막 방어선이다.
    assert is_generated_canvas("사람이 고친 제목", "F-2", DISCLAIMER_MARK) is True
    # 사람이 만든 Canvas 는 그대로 수집한다.
    assert is_generated_canvas("9월 공정 계획", "F-3", "착공 일정 정리") is False


def test_legacy_fixed_title_canvas_stays_excluded(tmp_path, monkeypatch):
    """§7.2-6 · 예전 형식으로 만들어진 Canvas 가 아직 남아 있다."""
    from tybot.archive.canvas import is_generated_canvas

    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert is_generated_canvas("TYBot 정식 답변") is True


def test_generated_log_holds_only_coordinates(tmp_path, monkeypatch):
    """기록에 제목·본문이 들어가면 감사 기록이 근거의 사본이 된다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    remember_generated("F-9", workspace="pilot", channel_id="C1", qa_record_id="Q9")
    row = (tmp_path / "generated-canvases.jsonl").read_text(encoding="utf-8")
    assert "F-9" in row and "Q9" in row
    assert "정식 답변" not in row and "본문" not in row


def test_missing_log_does_not_exclude_human_canvases(tmp_path, monkeypatch):
    """기록을 못 읽으면 **False** 다 — 사람 자료가 조용히 사라지는 쪽이 더 나쁘다."""
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "nowhere"))
    assert is_generated("F-1") is False


# --- §7.2-7,10,11 `캘린더` 표면형으로 정하지 않는다 -----------------------------
def test_calendar_word_alone_does_not_force_a_grid():
    """§7.2-7 · 같은 낱말이라도 문맥에 따라 판정이 갈린다."""
    grid = master_planner.artifact_for(
        _task(artifact_delivery="canvas", artifact_layout="calendar_grid")
    )
    assert grid.layout == "calendar_grid" and grid.wants_canvas

    timeline = master_planner.artifact_for(
        _task(artifact_delivery="canvas", artifact_layout="timeline")
    )
    assert timeline.layout == "timeline"

    # planner 가 아무 말도 안 하면 `auto` 다. 규칙이 `캘린더` 를 보고 정하지 않는다.
    plain = master_planner.artifact_for(_task(question="캘린더에 있는 일정이 뭐야"))
    assert plain.layout == "auto"
    assert plain.delivery == "message"
    assert plain.wants_canvas is False


def test_registering_real_calendar_events_is_not_silently_turned_into_a_canvas():
    """§7.2-10 · 「캘린더에 등록」 을 답변 Canvas 생성으로 바꿔 치지 않는다."""
    artifact = master_planner.artifact_for(
        _task(
            question="이 일정을 실제 캘린더에 등록해줘",
            artifact_delivery="canvas",
            artifact_operation="create_calendar_events",
        )
    )
    assert artifact.unsupported_operation == "create_calendar_events"
    assert artifact.wants_canvas is False


def test_editing_an_existing_canvas_is_reported_as_unsupported():
    artifact = master_planner.artifact_for(
        _task(
            question="채널 Canvas의 캘린더를 고쳐줘",
            artifact_delivery="canvas",
            artifact_operation="edit_existing_canvas",
        )
    )
    assert artifact.unsupported_operation == "edit_existing_canvas"
    assert artifact.wants_canvas is False


def test_unknown_enum_values_fall_back_to_the_safe_default():
    """열거형 밖이면 **메시지 답변**이다. 모르면서 문서를 만들지 않는다."""
    artifact = master_planner.artifact_for(
        _task(artifact_delivery="hologram", artifact_operation="delete_everything",
              artifact_layout="3d", target_unit="달러")
    )
    assert artifact.delivery == "message"
    assert artifact.operation == "answer_document"
    assert artifact.layout == "auto"
    assert artifact.target_unit == ""


def test_explicit_canvas_request_survives_a_dead_classifier():
    """LLM 이 죽어도 「캔버스로 답변해줘」 는 살아 있어야 한다(§6 B)."""
    artifact = master_planner.artifact_for(_task(), canvas_requested=True)
    assert artifact.delivery == "canvas"
    assert artifact.wants_canvas is True


# --- §7.4 오케스트레이션 -------------------------------------------------------
COMPOUND = "우리 팀 공지 채널에 올라온 내용을 Canvas에 캘린더로 작성해줘"


def test_research_question_drops_the_canvas_command():
    """§7.4 · Hermes 에는 **Canvas 편집 명령이 제거된** 질문이 간다."""
    task = Intent(
        kind="summary",
        question=COMPOUND,
        research_question="현재 채널 공지에서 일정 항목의 날짜·내용·관련 공지를 근거와 함께 정리해줘",
        artifact_delivery="canvas",
        artifact_operation="answer_document",
        artifact_layout="calendar_grid",
        artifact_title="전산팀 공지 일정 정리",
    )
    decision = master_planner.from_intents([task], text=COMPOUND)
    master_task = decision.tasks[0]

    assert "Canvas" not in master_task.question
    assert master_task.question.startswith("현재 채널 공지에서")
    assert master_task.artifact.wants_canvas is True
    assert master_task.artifact.layout == "calendar_grid"


def test_display_hint_is_a_format_note_not_an_action():
    """전문 봇에 가는 것은 형식 안내뿐이다 — 생성·공유는 호출자 몫이다."""
    task = Intent(kind="summary", question=COMPOUND, artifact_layout="calendar_grid")
    master_task = master_planner.from_intents([task], text=COMPOUND).tasks[0]
    hint = display_hint_for(master_task)
    assert "표" in hint
    assert "생성" not in hint and "Canvas" not in hint


def test_unresolved_research_question_keeps_the_original_sentence():
    """planner 가 못 풀었으면 **정규식으로 잘라 내지 않는다.**

    반쯤 맞는 문장으로 검색하면 묻지 않은 자료가 섞인다.
    """
    task = Intent(kind="summary", question=COMPOUND)
    master_task = master_planner.from_intents([task], text=COMPOUND).tasks[0]
    assert master_task.research_question == ""
    assert master_task.question == COMPOUND


def test_execution_refusal_is_told_apart_from_no_evidence():
    """「Canvas는 못 만든다」 와 「자료가 없다」 는 다른 실패다."""
    assert is_execution_refusal("죄송하지만 Canvas를 생성할 수 없습니다.") is True
    assert is_execution_refusal("Canvas 편집은 제 역할이 아닙니다.") is True
    assert is_execution_refusal("확인 가능한 근거에 해당 일정이 없습니다.") is False
    # 본문 중간의 언급까지 거절로 보면 정상 답변이 버려진다.
    long_answer = "\n".join(["9/22 착공계 제출", "9/25 감리 계약", "", "Canvas 작성은 불가합니다"])
    assert is_execution_refusal(long_answer) is False
