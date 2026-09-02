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
from tybot.slack.pilot import WorkspaceBot


class FakeQALog:
    def __init__(self):
        self.root = "/tmp/qa"
        self.records = []

    def recent_for_user(self, workspace, user_id):
        return [("2026-08-27T15:32", "현재상태")]

    def write(self, rec):
        self.records.append(rec)


class FakeEngine:
    """분해 결과와 아카이브 답변을 시험이 지정한다."""

    def __init__(self, tasks, answers):
        self._tasks = tasks
        self._answers = list(answers)
        self.asked: list[str] = []
        self.router = None  # compose 는 fallback 문구를 쓴다

    def plan(self, text):
        return list(self._tasks)

    def respond(self, question, ctx, intent):
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
        role="member", workspace="mgmt", is_root=False, channels={"#팀-전산_ABB110-회의"}
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


def test_audit_record_keeps_every_intent():
    tasks = [Intent("memory", question="기억나?"), Intent("status", question="상태 어때?")]
    bot = _bot(tasks, [])
    _handle(bot, "기억나? 그리고 상태 어때?")

    (rec,) = bot.qa_log.records
    assert rec.intent_kind == "memory+status"


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


@pytest.mark.parametrize("kind", ["status", "help", "smalltalk", "out_of_scope"])
def test_self_kinds_answer_without_touching_the_archive(kind):
    """LLM·아카이브가 없어도 봇 자신에 대한 질문은 답한다(401 상황)."""
    bot = _bot([Intent(kind, question="질문")], [])
    (reply,) = _handle(bot, "질문")
    assert reply.strip()
    assert bot.engine.asked == []
