"""요청 처리 전 구간 — 복합 질문이 두 답을 모두 받는지.

이미지로 보고된 실제 사고를 그대로 재현한다:
  "@tybot 다시, 너가 예전에 했던 말 기억나?
   그리고 지금 전산팀 워크스페이스에서는 무슨일이 벌어지고 있어?"
봇은 기억 설명만 내보내고 두 번째 질문은 답변에 **아예 등장하지 않았다.**
"""
from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import Mock

import pytest

from tybot.answer import Answer
from tybot.canvas_answer import CanvasResult
from tybot.intent import Intent
from tybot.slack.pilot import CHANNEL_SCOPE_NOTICE, WorkspaceBot


class FakeQALog:
    def __init__(self):
        self.root = "/tmp/qa"
        self.records = []
        self.dm_context = []

    def recent_for_user(self, workspace, user_id):
        return [("2026-08-27T15:32", "현재상태")]

    def context_for_thread(self, workspace, channel_id, thread_ts):
        return []

    def context_for_dm(self, workspace, channel_id, user):
        return list(self.dm_context)

    def write(self, rec):
        self.records.append(rec)


class FakeEngine:
    """분해 결과와 아카이브 답변을 시험이 지정한다."""

    def __init__(self, tasks, answers):
        self._tasks = tasks
        self._answers = list(answers)
        self.asked: list[str] = []
        self.plan_contexts: list[str] = []
        self.router = None  # compose 는 fallback 문구를 쓴다

    def plan(self, text, *, conversation_context="", thread_has_refs=False, specialists=None):
        self.plan_contexts.append(conversation_context)
        return list(self._tasks)

    def respond(self, question, ctx, intent, *, followup=None, task=None):
        self.asked.append(question)
        return self._answers.pop(0)

    def model_info(self):
        return "fake-model"

    def spent_today(self):
        return 0.0


def _bot(tasks, answers) -> WorkspaceBot:
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = "mgmt"
    bot.bot_name = "tybot"
    bot.engine = FakeEngine(tasks, answers)
    bot.qa_log = FakeQALog()
    bot.reply_in_thread = False
    bot._chan_cache = {"C1": "#팀-전산_ABB110-회의"}
    bot.path_problems = {}
    bot._user_name = lambda client, uid: "단라운"
    bot._context = lambda client, uid: Mock(
        role="member",
        workspace="mgmt",
        is_root=False,
        channels={"#팀-전산_ABB110-회의"},
        readable_workspaces=frozenset(),
    )
    # 상태 답변이 쓰는 값들. LLM 이 없어도 결정적 블록이 나와야 한다.
    bot.store = Mock(docs=lambda: [], broken=lambda: [])
    bot._started = datetime.now(UTC)
    bot._last_ingest_at = None
    bot._ingested = 0
    bot.realtime = True
    bot.autojoin = True
    bot.archive_dir = "/tmp/a"
    bot.cfg = Mock(label="경영본부", is_root=True, readable=frozenset())
    return bot


def _handle(bot, text):
    sent: list[str] = []
    bot._handle(
        {"text": text, "user": "U1", "channel": "C1", "ts": "1.0"},
        Mock(),
        lambda **kw: sent.append(kw["text"]),
        in_channel=True,
    )
    return sent


def test_both_questions_are_answered():
    """기억 설명 + 전산팀 아카이브 답변이 한 메시지에 모두 들어간다."""
    tasks = [
        Intent("memory", question="너가 예전에 했던 말 기억나?"),
        Intent("summary", question="지금 전산팀 워크스페이스에서는 무슨일이 벌어지고 있어?"),
    ]
    archive = Answer(
        "전산팀은 이번주 서버 이관을 진행했습니다.",
        ["#팀-전산_ABB110-회의, 📄2026-08.md(2026-08-27)"],
        "fake",
        0.01,
        3,
        "answered",
    )
    bot = _bot(tasks, [archive])
    (reply,) = _handle(bot, "다시, 너가 예전에 했던 말 기억나? 그리고 지금 전산팀은?")

    assert "기억하지 않습니다" in reply           # 첫 질문
    assert "서버 이관" in reply                   # 두 번째 질문 - 예전에는 없었다
    assert "출처:" in reply                       # 출처가 살아 있다(원칙 2)
    assert "───" in reply                         # 두 답이 구분된다
    assert reply.count(CHANNEL_SCOPE_NOTICE) == 1  # 복합 질문이어도 범위 안내는 한 번만


def test_archive_task_gets_its_own_clause_not_the_whole_message():
    """엔진에 넘기는 질문이 그 하위질문이어야 검색어가 오염되지 않는다."""
    tasks = [
        Intent("memory", question="기억나?"),
        Intent("search", question="김해외동 기성금 얼마야", terms=["김해외동", "기성금"]),
    ]
    ans = Answer("15억입니다.", ["#현장, 📄doc(2026-08-01)"], "m", 0.0, 1, "answered")
    bot = _bot(tasks, [ans])
    _handle(bot, "기억나? 그리고 김해외동 기성금 얼마야?")

    assert bot.engine.asked == ["김해외동 기성금 얼마야"]


def test_follow_up_in_a_thread_passes_prior_bot_exchange_only_to_the_planner():
    """구형 레코드(좌표 없음)에서만 이전 답변 조각이 planner 로 간다.

    이 값은 **지칭어 해석 전용**이다. 검색 근거나 전문 봇 입력으로 넘어가면
    요약을 근거로 요약하게 된다(원칙 1).
    """
    ans = Answer("문서 내용을 다시 확인했습니다.", ["#현장, 📄doc.md(2026-09-11)"],
                 "m", 0.0, 1, "answered")
    bot = _bot([Intent("search", question="가정산서.pdf 다시 확인해줘",
                              terms=["가정산서.pdf"])], [ans])
    bot.qa_log.context_for_thread = lambda workspace, channel_id, thread_ts: [{
        "question": "첨부 문서 내용 알려줘",
        "evidence_refs": [],
        "attachment_refs": [],
        "legacy_answer": "가정산서.pdf 하나는 자동 변환에 실패했습니다.",
    }]
    sent: list[str] = []

    bot._handle(
        {
            "text": "처리 안 된 하나의 문서도 다시 확인해줘",
            "user": "U1",
            "channel": "C1",
            "ts": "2.0",
            "thread_ts": "1.0",
        },
        Mock(),
        lambda **kw: sent.append(kw["text"]),
        in_channel=True,
    )

    assert "가정산서.pdf" in bot.engine.plan_contexts[0]
    assert bot.engine.asked == ["가정산서.pdf 다시 확인해줘"]
    assert "이전 봇 답변" not in bot.engine.asked[0]


def test_audit_record_keeps_every_intent():
    tasks = [Intent("memory", question="기억나?"), Intent("status", question="상태 어때?")]
    bot = _bot(tasks, [])
    _handle(bot, "기억나? 그리고 상태 어때?")

    (rec,) = bot.qa_log.records
    assert rec.intent_kind == "memory+status"


def test_audit_preserves_each_business_task_result():
    tasks = [
        Intent("summary", question="summarize", planner_model="planner-test"),
        Intent("search", question="find"),
    ]
    answers = [
        Answer("summary", [], "specialist-test", 0.0, 1, "answered",
               specialist="hermes", attempted_specialists=["hermes"]),
        Answer("unavailable", [], "", 0.0, 0, "unavailable",
               attempted_specialists=["hermes"], specialist_error_code="timeout"),
    ]
    bot = _bot(tasks, answers)
    _handle(bot, "summarize and find")

    (rec,) = bot.qa_log.records
    assert rec.decision_id
    assert rec.planner_model == "planner-test"
    assert [trace["task_index"] for trace in rec.task_traces] == [0, 1]
    assert [trace["final_responder"] for trace in rec.task_traces] == ["hermes", "none"]
    assert [trace["result"] for trace in rec.task_traces] == ["answered", "unavailable"]
    assert rec.task_traces[1]["error_code"] == "timeout"
    assert all(trace["required_capability"] for trace in rec.task_traces)


def test_write_intent_runs_alone_even_if_plan_adds_more():
    """수집이 섞여 오면 수집만 실행한다 - 모호한 쓰기는 실행하지 않는다."""
    tasks = [Intent("ingest", question="수집해"), Intent("summary", question="요약")]
    bot = _bot(tasks, [])
    bot._ingest_channel = lambda client, cid: "수집 결과"
    (reply,) = _handle(bot, "수집해 그리고 요약")
    assert reply == "수집 결과"
    assert bot.engine.asked == []


def test_ingest_outside_channel_is_refused():
    bot = _bot([Intent("ingest", question="수집해")], [])
    sent: list[str] = []
    bot._handle(
        {"text": "수집해", "user": "U1", "channel": "D1", "ts": "1.0"},
        Mock(),
        lambda **kw: sent.append(kw["text"]),
        in_channel=False,
    )
    assert "채널에서만" in sent[0]


def test_over_cap_questions_are_announced_not_dropped_silently():
    tasks = [Intent("memory", question=f"q{i}") for i in range(5)]
    bot = _bot(tasks, [])
    (reply,) = _handle(bot, "질문 다섯 개")
    assert "따로 물어봐" in reply


def test_exception_still_reaches_the_user():
    """예외가 나면 👀 만 남기지 않고 무슨 일인지 알린다."""
    bot = _bot([Intent("summary", question="요약")], [])

    def boom(*a, **kw):
        raise RuntimeError("Error code: 401 authentication_error")

    bot.engine.respond = boom
    (reply,) = _handle(bot, "요약해줘")
    assert "ANTHROPIC_API_KEY" in reply


def test_explicit_canvas_request_posts_canvas_link(monkeypatch):
    ans = Answer(
        "전산팀 주간 현황", ["#업무, 📄doc(2026-09-02)"], "m", 0.0, 1, "answered"
    )
    bot = _bot([Intent("summary", question="주간 현황")], [ans])
    created = Mock(return_value=CanvasResult("F-CANVAS", "https://example.slack.com/F-CANVAS"))
    monkeypatch.setattr("tybot.slack.pilot.create_answer_canvas", created)
    client = Mock()
    sent: list[str] = []

    bot._handle(
        {"text": "주간 현황을 캔버스로 답변해", "user": "U1", "channel": "C1", "ts": "1.0"},
        client,
        lambda **kw: sent.append(kw["text"]),
        in_channel=True,
    )

    assert "Canvas 열기" in sent[0]
    assert "출처:" in created.call_args.args[1]
    assert bot.engine.asked == ["주간 현황"]
    client.canvases_access_set.assert_called_once_with(
        canvas_id="F-CANVAS", access_level="read", channel_ids=["C1"]
    )


@pytest.mark.parametrize(
    "question",
    [
        "혹시 너 캔버스의 내용을 읽고 답해줘?",
        "그럼 채널에 있는 폴더는?",
        "첨부파일도 근거로 보나요?",
    ],
)
def test_a_source_scope_question_is_answered_not_given_the_full_help(question):
    """「이런 자료도 읽느냐」 는 한 부류다. 자료마다 문장을 따로 두면 샌다.

    캔버스만 적어 두었더니 "그럼 채널에 있는 폴더는?" 이 전체 사용법으로
    빠졌다(2026-09-14 실제 발생).
    """
    bot = _bot([Intent("help", question=question)], [])
    (reply,) = _handle(bot, question)

    assert "Canvas" in reply
    assert "읽지 않는 것" in reply, "안 보는 것을 말해야 사용자가 다음 행동을 안다"
    assert "폴더" in reply
    assert "/피드백" not in reply, "전체 사용법이 나갔다"


def test_canvas_capability_question_bypasses_the_llm_planner():
    from tybot.intent import plan

    router = Mock()
    got = plan("혹시 너 캔버스의 내용을 읽고 답해줘?", router)

    assert [task.kind for task in got] == ["help"]
    router.complete.assert_not_called()


def test_explicit_dm_followup_uses_recent_own_dm_context():
    ans = Answer("다시 정리했습니다", [], "m", 0.0, 1, "answered")
    bot = _bot([
        Intent(
            "summary",
            question="방금 답변 다시 정리해줘",
            reference_mode="prior_turn",
        )
    ], [ans])
    bot.qa_log.dm_context = [{
        "question": "현황을 정리해줘", "subject_terms": ["현황"],
        "evidence_refs": [], "attachment_refs": [], "legacy_answer": "이전 답변",
    }]
    sent = []
    bot._handle(
        {"text": "방금 답변 다시 정리해줘", "user": "U1", "channel": "D1", "ts": "1.0"},
        Mock(), lambda **kw: sent.append(kw["text"]), in_channel=False,
    )
    assert "이전 질문: 현황을 정리해줘" in bot.engine.plan_contexts[0]


def test_new_dm_topic_does_not_mix_previous_dm_context():
    ans = Answer("새 답변", [], "m", 0.0, 1, "answered")
    bot = _bot([Intent("search", question="새 계약 금액")], [ans])
    bot.qa_log.dm_context = [{"question": "무관한 이전 질문"}]
    bot._handle(
        {"text": "새 계약 금액 알려줘", "user": "U1", "channel": "D1", "ts": "1.0"},
        Mock(), lambda **kw: None, in_channel=False,
    )
    assert bot.engine.plan_contexts == [""]


@pytest.mark.parametrize("kind", ["status", "help", "smalltalk", "out_of_scope"])
def test_self_kinds_answer_without_touching_the_archive(kind):
    """LLM·아카이브가 없어도 봇 자신에 대한 질문은 답한다(401 상황)."""
    bot = _bot([Intent(kind, question="질문")], [])
    (reply,) = _handle(bot, "질문")
    assert reply.strip()
    assert bot.engine.asked == []


@pytest.mark.parametrize("fails", [False, True])
def test_long_answer_automatically_creates_canvas_or_readable_fallback(monkeypatch, fails):
    text = "## 주간 현황\n" + "**진행 중**\n" * 22
    ans = Answer(text, [], "m", 0.0, 1, "answered")
    bot = _bot([Intent("summary", question="정리해줘")], [ans])
    created = Mock(return_value=CanvasResult("FC", "https://example.slack.com/FC"))
    if fails:
        created.side_effect = RuntimeError("canvas unavailable")
    monkeypatch.setattr("tybot.slack.pilot.create_answer_canvas", created)
    (reply,) = _handle(bot, "정리해줘")
    created.assert_called_once()
    assert "## 주간 현황" in created.call_args.args[1]
    if fails:
        assert "## " not in reply
        assert "**진행 중**" not in reply
        assert "*진행 중*" in reply
    else:
        assert "Canvas 열기" in reply
