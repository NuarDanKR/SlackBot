"""VIPBot 표시 형식·불만 처리·맥락 안내 (2026-11-19 운영 문제).

운영에서 본 세 가지를 고정한다.

1. 「canvas와 표를 써서 정리해줘」 가 Canvas 로 안 나왔다 — 명시 요청 정규식이
   「캔버스로 답변/작성」 한 모양만 잡았고, 나머지는 LLM 의 「확신 없으면 message」
   에 맡겨졌다. 표는 메시지에서 글머리표로 풀려 사라졌다
2. 「표와 canvas 사용해 답변하라는데 왜 자꾸 안 해?」 가 `HELP_RE` 에 걸려 맥락도
   안 보고 전체 사용법이 나갔다. 불만은 LLM 이 판단하고, 답은 LLM 이 상황에 맞춰 쓴다
3. 「이전 답변 기억나?」 에 「기억하지 않는다」 만 말해, 실제로 이어지는 스레드·DM
   맥락이 없는 것처럼 들렸다
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot import intent
from tybot.audit import DM_CONTEXT_MINUTES, THREAD_TURNS, QALog
from tybot.canvas_answer import (
    AUTO_CANVAS_LINES,
    PREFERRED_CANVAS_LINES,
    automatic,
    has_markdown_table,
    parse_request,
)
from tybot.feedback import FeedbackLog
from tybot.master_planner import DELIVERY_CANVAS, ArtifactRequest
from tybot.slack import pilot
from tybot.slack.pilot import WorkspaceBot, display_request
from tybot.user_memory import PREFERENCE, UserMemory, _preference, build_context

TABLE = "| 순위 | 항목 |\n|---|---|\n| 1 | 3분기 보고서 |\n| 2 | 보증채무 |"
COMPLAINT = "표와 canvas 사용해 답변하라는데, 그렇게 자꾸 안하는 이유는 무엇인가요?"


# --- 1. 명시 요청 ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "cleaned"),
    [
        (
            "위 답변 내용을 canvas와 표를 써서 다시 정리해 보여줘.",
            "위 답변 내용을 다시 정리해 보여줘",
        ),
        (
            "위 답변을 참고해서, 기술리더 관점 Top 5를 canvas와 표를 써서 정리해주세요.",
            "위 답변을 참고해서, 기술리더 관점 Top 5를 정리해주세요",
        ),
        ("금주 보고를 Canvas로 정리해줘", "금주 보고를"),
        ("김해외동 기성금 캔버스에 만들어줘", "김해외동 기성금"),
        ("캔버스로 답변해줘", ""),
        ("메시지 말고 정식 답변해줘", ""),
    ],
)
def test_explicit_canvas_requests_in_any_common_form_are_recognised(text, cleaned):
    assert parse_request(text) == (True, cleaned)


@pytest.mark.parametrize(
    "text",
    [
        COMPLAINT,  # 불만 — 지시가 아니다
        "캔버스 쓰는 법 알려줘",  # 사용법 질문
        "캔버스 사용 방법",
        "canvas로 정리 가능해?",
        "캔버스로 작성 안 해?",
        "캔버스 내용도 읽어?",  # 근거 범위 질문
    ],
)
def test_complaints_and_how_to_questions_are_not_canvas_orders(text):
    matched, cleaned = parse_request(text)

    assert matched is False
    # 지시가 아니면 문장을 고치지도 않는다 — 고치면 그 질문의 뜻이 망가진다.
    assert cleaned == text.strip(" ,.!?")


# --- 1. 자동 전환 ---------------------------------------------------------------


def test_a_requested_table_goes_to_canvas_for_every_bot():
    """메시지는 표를 글머리표로 풀어 버린다. 표를 원했으면 Canvas 다."""
    assert has_markdown_table(TABLE)
    assert automatic(TABLE, "Top 10을 표로 상세히 정리해줘") is True
    assert automatic(TABLE, "Top 10 알려줘") is False


def test_a_preferring_bot_uses_canvas_for_detailed_or_ranked_requests():
    short = "1. 보고서\n2. 보증채무"

    assert automatic(short, "CEO 관점 Top 10 상세히 정리", prefer_canvas=True) is True
    assert automatic(short, "누가 승인했어", prefer_canvas=True) is False
    long = "\n".join(f"줄 {i}" for i in range(PREFERRED_CANVAS_LINES + 1))
    assert automatic(long, "누가 승인했어", prefer_canvas=True) is True
    assert automatic(long, "누가 승인했어") is (PREFERRED_CANVAS_LINES + 1 > AUTO_CANVAS_LINES)


@pytest.mark.parametrize(
    "request_text", ["표로 메시지로 줘", "캔버스 말고 표로", "canvas 없이 표로"]
)
def test_saying_message_always_wins(request_text):
    assert automatic(TABLE, request_text, prefer_canvas=True) is False


def test_personal_preferences_change_only_the_default():
    assert automatic(TABLE, "정리해줘", prefer_canvas=True, avoid_canvas=True) is False
    assert automatic(TABLE, "알려줘", table_preferred=True) is True


def test_a_word_that_merely_contains_pyo_is_not_a_table_request():
    assert automatic(TABLE, "대표로 누가 했어") is False
    assert automatic(TABLE, "발표를 정리") is False


def test_any_task_that_wants_canvas_decides_the_display():
    message = SimpleNamespace(artifact=ArtifactRequest())
    canvas = SimpleNamespace(artifact=ArtifactRequest(delivery=DELIVERY_CANVAS, title="Top 5"))

    assert display_request([message, canvas]).wants_canvas
    assert display_request([message]) == message.artifact
    assert display_request([]) == ArtifactRequest()


def test_vipbot_is_built_to_prefer_canvas():
    source = Path(pilot.__file__).read_text(encoding="utf-8")
    vip = source[source.index("vip_bot = WorkspaceBot(") :]
    vip = vip[: vip.index(")\n")]

    assert "prefer_canvas=True" in vip
    assert "prefer_canvas: bool = False" in source  # TYBot 기본값은 그대로


# --- 2. 불만은 LLM 이 판단한다 ---------------------------------------------------------


def test_a_complaint_is_not_settled_by_regex_before_the_llm():
    assert intent.classify_by_rule(COMPLAINT).kind == "help"  # 폴백 규칙은 여전히 help
    assert intent._deterministic_plan(COMPLAINT) is None  # 하지만 확정하지 않는다


@pytest.mark.parametrize("text", ["도움말", "사용법", "명령어 뭐야?", "상태", "연결 상태 확인해줘"])
def test_bare_commands_still_skip_the_llm(text):
    planned = intent._deterministic_plan(text)

    assert planned and planned[0].kind in ("help", "status")


def test_the_planner_is_told_complaints_are_feedback_not_help():
    assert "요청대로 하지 않았다는 불만·항의**는 help 가 아니라 feedback" in intent.PLANNER_PROMPT
    assert "표와 canvas로 답하라는데 자꾸 안 하는 이유가 뭐야?" in intent.PLANNER_PROMPT


def _bot(tmp_path, *, prefer_canvas=False) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "mgmt"
    bot.bot_name = "vipbot"
    bot.prefer_canvas = prefer_canvas
    bot.qa_log = QALog(tmp_path, write_md=False)
    bot.feedback_log = FeedbackLog(tmp_path)
    bot.engine = SimpleNamespace(router=object())
    return bot


def test_a_complaint_gets_an_llm_explanation_built_from_code_facts(tmp_path, monkeypatch):
    bot = _bot(tmp_path, prefer_canvas=True)
    memory = SimpleNamespace(
        remember=lambda *a, **k: SimpleNamespace(
            saved=True,
            memory_id=3,
            decision=SimpleNamespace(kind="preference"),
        )
    )
    bot._user_memory_service_obj = memory
    seen = {}

    def fake_writer(router, *, question, facts, fallback, **_):
        seen.update(question=question, facts=facts, fallback=fallback)
        return "LLM 안내문"

    monkeypatch.setattr(pilot, "write_from_facts", fake_writer)

    reply = bot._record_natural_feedback(user_id="U1", channel_id="D1", text=COMPLAINT)

    assert reply == "LLM 안내문"
    assert seen["question"] == COMPLAINT
    rules = seen["facts"]["답변_형식_규칙"]
    assert str(PREFERRED_CANVAS_LINES) in rules["기본_형식"]
    assert "canvas와 표로" in rules["Canvas_명시_요청"]
    assert "저장했다" in seen["facts"]["개인_선호_저장"]
    # LLM 이 실패하면 기록 사실은 그대로 알린다.
    assert "VIPBot 답변 품질 피드백으로 기록" in seen["fallback"]
    # 기록은 코드가 한다 — 답 문장과 무관하게 원장에 남는다.
    assert list(tmp_path.glob("feedback-*.jsonl"))


def test_a_sentence_help_question_is_answered_not_dumped(tmp_path, monkeypatch):
    bot = _bot(tmp_path)
    calls = []
    monkeypatch.setattr(
        pilot,
        "write_from_facts",
        lambda router, *, question, facts, fallback, **_: calls.append(facts) or "맞춤 안내",
    )

    tailored = bot._self_reply(
        "help", "캔버스로 받으려면 어떻게 해야 해?", client=None, user_id="U1"
    )
    dumped = bot._self_reply("help", "도움말", client=None, user_id="U1")

    assert tailored == "맞춤 안내"
    assert "명령_목록" in calls[0] and calls[0]["명령_목록"]
    assert "사용법" in dumped and len(calls) == 1


# --- 3. 맥락 안내 -------------------------------------------------------------------


def test_the_memory_answer_states_the_context_that_really_carries_over(tmp_path):
    bot = _bot(tmp_path)

    facts = bot._memory_facts("U1")

    assert f"최근 {THREAD_TURNS}개 문답" in facts["같은_스레드_맥락"]
    assert f"최근 {DM_CONTEXT_MINUTES // 60}시간" in facts["DM_맥락"]
    assert "이어진다" in facts["맥락_요약"]


# --- 개인 선호 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (COMPLAINT, "format.canvas=prefer"),
        ("앞으로 캔버스로 정리해줘", "format.canvas=prefer"),
        ("캔버스 말고 메시지로 줘", "format.canvas=avoid"),
        ("표로 정리해줘", "format.table=prefer"),
    ],
)
def test_canvas_preferences_are_recognised(text, expected):
    assert _preference(text) == expected


def test_selected_preferences_reach_the_request_context():
    memories = [
        UserMemory(
            id=1,
            workspace="mgmt",
            user_id="U1",
            kind=PREFERENCE,
            body="format.canvas=prefer",
            normalized="format.canvas=prefer",
        ),
    ]

    context = build_context(memories, question="Top 5 알려줘")

    assert context.preferences == ("format.canvas=prefer",)
    assert "Canvas 문서로 받기를 선호함" in context.prompt
