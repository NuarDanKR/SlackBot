"""VIP 질문은 TYBot 과 **같은 코드**로 만들어지고, **다른 곳으로** 간다.

2026-11-09 오너 결정. VIPBot 은 별도 답변 제품이 아니다. 같은 `WorkspaceBot` ·
같은 `AnswerEngine` 을 쓰고 다른 것은 **넷**뿐이다.

1. 모델·프롬프트·비용 상한
2. VIP 사용자 허용 판정
3. 관리형 근거 채널
4. **상세 답변은 요청자 DM 으로만 간다**

4번이 나머지 셋과 성격이 다르다. 1~3 은 *무엇을 근거로 답하나*이고 4 는
*누가 그 답을 보나*다. 채널에서 불러도 본문·blocks·Canvas 링크는 채널에 남지
않는다 — 임원이 묻는 내용은 그 채널에 있는 사람 전부가 볼 것이 아니다.

그래서 이 파일이 고정하는 것은 두 축이다.

- **생성은 공통이다** — 본문·출처·Canvas 판단·근거 좌표가 TYBot 과 같은 코드에서
  같은 값으로 나온다
- **전달은 VIP 가 다르다** — 상세는 DM, 채널에는 고정 안내 한 줄, DM 이 실패하면
  **아무것도 남기지 않는다**

두 번째를 시험으로 박는 이유: 「DM 이 안 되면 채널에라도」 는 고장 났을 때 가장
자연스러운 수습처럼 보인다. 실제로는 그 순간 VIP 답변이 통째로 공개된다. 코드가
의도적으로 **안 하는** 일은 시험이 없으면 다음 사람이 「친절하게」 되살린다.

`test_vipbot_boundary.py` 가 선언(매니페스트·import)을 본다면 이 파일은 동작을
본다.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tybot import vip_profile
from tybot.access import RequestContext
from tybot.answer import Answer
from tybot.bot_access import DENY_MESSAGE
from tybot.canvas_answer import CanvasResult
from tybot.evidence_refs import EvidenceRef
from tybot.intent import Intent
from tybot.question_files import QuestionFileEvidence
from tybot.slack.pilot import (
    CHANNEL_SCOPE_NOTICE,
    PRIVATE_ANSWER_NOTICE,
    RequestAuthorizationDenied,
    WorkspaceBot,
)

VIP_WORKSPACE = "tyit"
VIP_SOURCES = frozenset({("tyit", "C-EXEC")})
DM_CHANNEL = "D-VIP"


@pytest.fixture(autouse=True)
def _use_the_default_master_path(monkeypatch):
    """Keep shared answer-path tests independent of the server rollout env."""
    monkeypatch.delenv("TYBOT_MASTER_V2_WORKSPACES", raising=False)


class FakeQALog:
    """VIP 전용 QA 디렉터리. 좌표가 TYBot 기록과 섞이면 안 된다."""

    def __init__(self, root: str = "/tmp/vip-qa"):
        self.root = root
        self.records = []
        self.thread_context: list[dict] = []
        self.dm_context: list[dict] = []

    def context_for_thread(self, workspace, channel_id, thread_ts):
        return list(self.thread_context)

    def context_for_dm(self, workspace, channel_id, user):
        return list(self.dm_context)

    def write(self, rec):
        self.records.append(rec)


class FakeEngine:
    def __init__(self, tasks, answers):
        self._tasks = tasks
        self._answers = list(answers)
        self.asked: list[str] = []
        self.plan_contexts: list[str] = []
        self.seeds: list[tuple] = []
        self.router = None

    def plan(self, text, *, conversation_context="", thread_has_refs=False,
             specialists=None):
        self.plan_contexts.append(conversation_context)
        return list(self._tasks)

    def respond(self, question, ctx, intent, *, followup=None, task=None, seed_hits=()):
        self.asked.append(question)
        self.seeds.append(tuple(seed_hits))
        return self._answers.pop(0)

    def model_info(self):
        return "vip-model"

    def spent_today(self):
        return 0.0


def _bot(tasks, answers, *, vip: bool, qa_log: FakeQALog | None = None) -> WorkspaceBot:
    """같은 클래스에서 두 구성을 만든다. **차이는 구성뿐**이어야 한다."""
    bot = WorkspaceBot.__new__(WorkspaceBot)
    bot.workspace = VIP_WORKSPACE
    bot.bot_name = "vipbot" if vip else "tybot"
    bot.engine = FakeEngine(tasks, answers)
    bot.qa_log = qa_log or FakeQALog("/tmp/vip-qa" if vip else "/tmp/qa")
    bot.reply_in_thread = False
    bot._chan_cache = {"C-EXEC": "#임원_보고(ABB000)_주간"}
    bot.path_problems = {}
    bot._user_name = lambda client, uid: "단라운"
    bot._bot_users = {"U1": False}
    bot.store = Mock(docs=lambda: [], broken=lambda: [], root=Path("/tmp/archive"))
    bot._started = datetime.now(UTC)
    bot._last_ingest_at = None
    bot._ingested = 0
    bot.archive_dir = "/tmp/archive"
    bot.exec_users = set()
    bot.cfg = SimpleNamespace(
        label="TY IT", is_root=False, readable=frozenset(), bot_token="xoxb-vip"
    )
    if vip:
        # VIP 가 다르게 가진 **넷**. 그 밖에는 같은 객체·같은 메서드다.
        bot.realtime = False
        bot.autojoin = False
        bot.operational_owner = False
        bot.private_answers = True                      # 4. 상세는 DM 으로만
        bot._request_context_resolver = lambda _client, user: RequestContext(
            workspace=VIP_WORKSPACE, user_id=user, evidence_sources=VIP_SOURCES
        )                                               # 2·3. 허용 판정과 근거 좌표
        bot._question_transform = vip_profile.question_for_engine   # 1. 모델 고정
    else:
        bot.realtime = True
        bot.autojoin = True
        bot.operational_owner = True
        bot.private_answers = False
        bot._request_context_resolver = None
        bot._question_transform = lambda question: question
        bot._context = lambda client, uid: RequestContext(
            workspace=VIP_WORKSPACE,
            user_id=uid,
            channels=frozenset({"#임원_보고(ABB000)_주간"}),
        )
    return bot


def _client(*, dm: str | None = DM_CHANNEL) -> Mock:
    client = Mock()
    client.conversations_open.return_value = (
        {"channel": {"id": dm}} if dm else {"channel": {}}
    )
    client.chat_postMessage.return_value = {"ts": "9.0"}
    return client


def _handle(bot, text, *, client=None, event=None, in_channel=True):
    """요청 하나를 끝까지 흘린다. 채널에 **실제로 나간 것**만 `sent` 에 남는다."""
    sent: list[dict] = []
    payload = {"text": text, "user": "U1", "channel": "C-EXEC", "ts": "1.0"}
    payload.update(event or {})
    bot._handle(
        payload,
        client or _client(),
        lambda **kw: sent.append(kw) or {"ts": "9.0"},
        in_channel=in_channel,
    )
    return payload, sent


def _without_scope_notice(text: str) -> str:
    """「조회 범위: 현재 채널만」 안내를 걷어낸다.

    이 한 줄은 **범위 설명**이지 답이 아니다. TYBot 의 범위는 질문이 올라온
    채널이고 VIP 의 범위는 콘솔이 등록한 좌표라, 같은 문장을 둘에 붙일 수 없다.
    나머지 본문·출처는 같아야 하므로 이것만 빼고 대조한다.
    """
    kept = [
        line for line in text.splitlines() if CHANNEL_SCOPE_NOTICE not in line
    ]
    return "\n".join(kept).strip()


def _dm_posts(client) -> list[dict]:
    """요청자 DM 으로 나간 것. `conversations.open` 이 준 채널만 센다."""
    return [
        call.kwargs
        for call in client.chat_postMessage.call_args_list
        if str(call.kwargs.get("channel") or "").startswith("D")
    ]


#: 답변이 실제로 읽은 원문 한 줄의 좌표. 다음 질문은 **이것**을 다시 연다.
VIP_REF = EvidenceRef(
    kind="archive_line",
    workspace=VIP_WORKSPACE,
    channel_id="C-EXEC",
    document_path="workspaces/tyit/channels/C-EXEC/raw/2026-11.md",
    line_no=12,
)


def _answered(
    text="임원 보고 요약입니다.",
    citations=("#임원_보고(ABB000)_주간, 📄2026-11.md(2026-11-05)",),
    *,
    refs=(VIP_REF,),
):
    return Answer(
        text, list(citations), "vip-model", 0.0, 2, "answered",
        evidence_refs=list(refs),
    )


# --- 1. 생성은 공통이다 --------------------------------------------------------
#
# 전달이 갈라져도 **만드는 쪽은 한 코드**여야 한다. 갈라지면 같은 질문에 다른
# 사실이 나오고, 그 차이는 아무 로그에도 안 남는다.


def test_the_answer_body_and_citations_are_built_by_the_common_code():
    """본문·출처는 TYBot 과 **글자까지 같다**. 다른 것은 어디로 가느냐뿐이다.

    딱 한 줄 빼고 같다 — 「조회 범위: 현재 채널만」 안내다. 그 문장은 범위를
    **채널로 고정했다**는 설명이고, VIP 의 범위는 채널이 아니라 콘솔이 등록한
    좌표다. VIP 에 그대로 붙이면 사실이 아닌 안내가 나가고, 거기 적힌
    해결책(「TYBot 개인 DM 에서 요청하세요」)도 VIP 사용자에게는 틀린 길이다.

    그래서 그 한 줄만 걷어내고 **나머지 전부**를 대조한다.
    """
    bodies = []
    for vip in (False, True):
        bot = _bot([Intent("search", question="금액")], [_answered()], vip=vip)
        client = _client()
        _, sent = _handle(bot, "금액 알려줘", client=client)
        bodies.append(_dm_posts(client)[0]["text"] if vip else sent[0]["text"])

    assert _without_scope_notice(bodies[0]) == _without_scope_notice(bodies[1])
    assert "출처:" in bodies[1]
    assert "#임원_보고(ABB000)_주간" in bodies[1]
    # 안내는 TYBot 에만, VIP 에는 없다. 양쪽을 다 단언해야 조건이 뒤집혀도 깨진다.
    assert CHANNEL_SCOPE_NOTICE in bodies[0]
    assert CHANNEL_SCOPE_NOTICE not in bodies[1]


def test_the_canvas_decision_is_made_by_the_common_code(monkeypatch):
    """Canvas 로 낼지, 그 Canvas 본문이 무엇인지도 같은 코드가 정한다."""
    made: list[str] = []
    monkeypatch.setattr(
        "tybot.slack.pilot.create_answer_canvas",
        lambda client, body, **kw: made.append(body)
        or CanvasResult("F1", "https://example.slack.com/F1"),
    )
    for vip in (False, True):
        bot = _bot([Intent("summary", question="주간 현황")], [_answered()], vip=vip)
        _handle(bot, "주간 현황을 캔버스로 답변해", client=_client())

    assert len(made) == 2, "한쪽만 Canvas 로 나갔다"
    assert _without_scope_notice(made[0]) == _without_scope_notice(made[1]), (
        "같은 답인데 Canvas 본문이 다르다"
    )


def test_canvas_brand_and_question_are_configuration_on_the_common_path(monkeypatch):
    made: list[dict] = []
    monkeypatch.setattr(
        "tybot.slack.pilot.create_answer_canvas",
        lambda client, body, **kw: made.append(kw)
        or CanvasResult("F1", "https://example.slack.com/F1"),
    )

    for vip in (False, True):
        bot = _bot([Intent("summary", question="기획팀 활동")], [_answered()], vip=vip)
        _handle(bot, "지난주 기획팀 활동을 캔버스로 답변해", client=_client())

    assert [item["bot_name"] for item in made] == ["tybot", "vipbot"]
    assert made[0]["title"].endswith(" · TYBot")
    assert made[1]["title"].endswith(" · VIPBot")
    assert [item["question"] for item in made] == [
        "지난주 기획팀 활동을 캔버스로 답변해",
        "지난주 기획팀 활동을 캔버스로 답변해",
    ]


def test_an_answer_without_evidence_is_not_wrapped_in_an_empty_canvas(monkeypatch):
    created = Mock()
    monkeypatch.setattr("tybot.slack.pilot.create_answer_canvas", created)
    bot = _bot(
        [Intent("summary", question="정리해줘")],
        [Answer("열람 가능한 문서가 없습니다.", [], None, 0.0, 0, "no_access")],
        vip=True,
    )
    client = _client()

    _handle(bot, "캔버스로 정리해줘", client=client)

    created.assert_not_called()
    assert "열람 가능한 문서가 없습니다" in _dm_posts(client)[0]["text"]


def test_an_answer_with_evidence_gets_the_common_evidence_button():
    """「근거 보기」 는 VIP DM 에도 같이 간다 — 매니페스트의 상호작용이 그래서 필요하다."""
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=True)
    client = _client()

    _handle(bot, "금액 알려줘", client=client)

    (post,) = _dm_posts(client)
    assert "근거 보기" in str(post.get("blocks") or [])


# --- 2. 전달은 DM 으로만 ------------------------------------------------------


def test_a_channel_mention_sends_the_detail_to_the_requester_dm():
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=True)
    client = _client()

    _handle(bot, "금액 알려줘", client=client)

    client.conversations_open.assert_called_once_with(users="U1")
    (post,) = _dm_posts(client)
    assert post["channel"] == DM_CHANNEL
    assert "임원 보고 요약입니다" in post["text"]


def test_the_channel_keeps_only_the_fixed_notice():
    """채널에 남는 것은 **한 줄**이다. 본문도 blocks 도 출처도 없다."""
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=True)
    client = _client()

    _, sent = _handle(bot, "금액 알려줘", client=client)

    (notice,) = sent
    assert notice["text"] == PRIVATE_ANSWER_NOTICE
    assert "blocks" not in notice
    for leaked in ("임원 보고 요약", "출처:", "📄2026-11.md"):
        assert leaked not in notice["text"]


def test_a_canvas_answer_leaves_no_link_in_the_channel(monkeypatch):
    """Canvas 링크도 채널에 두지 않는다. 링크 하나로 본문 전체가 열린다."""
    monkeypatch.setattr(
        "tybot.slack.pilot.create_answer_canvas",
        lambda client, body, **kw: CanvasResult(
            "F-VIP", "https://example.slack.com/F-VIP"
        ),
    )
    bot = _bot([Intent("summary", question="주간 현황")], [_answered()], vip=True)
    client = _client()

    _, sent = _handle(bot, "주간 현황을 캔버스로 답변해", client=client)

    assert "Canvas 열기" in _dm_posts(client)[0]["text"]
    assert sent == [{"text": PRIVATE_ANSWER_NOTICE}]
    assert "example.slack.com" not in str(sent)


def test_canvas_access_goes_to_the_requester_not_the_channel(monkeypatch):
    """권한은 `user_ids` 로 준다.

    `channel_ids` 로 주면 Canvas 본문이 채널 전체에 열린다 — 링크를 숨겨도
    그 채널 사람은 파일 목록에서 찾을 수 있다.
    """
    monkeypatch.setattr(
        "tybot.slack.pilot.create_answer_canvas",
        lambda client, body, **kw: CanvasResult(
            "F-VIP", "https://example.slack.com/F-VIP"
        ),
    )
    bot = _bot([Intent("summary", question="주간 현황")], [_answered()], vip=True)
    client = _client()

    _handle(bot, "주간 현황을 캔버스로 답변해", client=client)

    client.canvases_access_set.assert_called_once_with(
        canvas_id="F-VIP", access_level="read", user_ids=["U1"]
    )
    for call in client.canvases_access_set.call_args_list:
        assert "channel_ids" not in call.kwargs


def test_a_direct_dm_question_is_answered_in_that_dm():
    """DM 으로 물으면 그 DM 에 답한다. 새 DM 을 열지 않는다."""
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=True)
    client = _client()

    _, sent = _handle(
        bot, "금액 알려줘", client=client,
        event={"channel": "D-EXISTING", "channel_type": "im"}, in_channel=False,
    )

    client.conversations_open.assert_not_called()
    assert "임원 보고 요약입니다" in sent[0]["text"]
    assert PRIVATE_ANSWER_NOTICE not in str(sent)


# --- 3. DM 이 실패하면 아무것도 남기지 않는다 ---------------------------------
#
# 여기가 이 파일에서 가장 중요한 자리다. 「DM 이 안 되면 채널에라도」 는 고장
# 났을 때 가장 자연스러운 수습처럼 보이고, 그 순간 VIP 답변이 통째로 공개된다.


def test_a_failed_dm_open_posts_nothing_anywhere():
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=True)
    client = _client(dm=None)

    _, sent = _handle(bot, "금액 알려줘", client=client)

    assert sent == [], "DM 을 못 열자 채널로 흘렸다"
    assert client.chat_postMessage.call_args_list == []


def test_a_dm_open_that_returns_a_non_dm_channel_is_refused():
    """`conversations.open` 이 DM 이 아닌 채널을 주면 **쓰지 않는다**.

    응답 모양만 맞다고 그 채널로 보내면, 그 채널이 무엇이냐에 따라 본문이
    여러 사람 앞에 놓인다. 「열렸다」 와 「요청자와 둘뿐인 곳이 열렸다」 는
    다른 사실이다.
    """
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=True)
    client = _client(dm="G-GROUP")

    _, sent = _handle(bot, "금액 알려줘", client=client)

    assert sent == []
    assert client.chat_postMessage.call_args_list == []


def test_a_failed_dm_post_leaves_no_channel_notice():
    """안내마저 안 남긴다.

    안내만 남기면 사람은 DM 을 열어 보고 아무것도 없는 것을 확인한다. 그건
    「답이 갔다」 보다 나쁘다 — 어디를 봐야 하는지 모르게 만든다.
    """
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=True)
    client = _client()
    client.chat_postMessage.side_effect = RuntimeError("channel_not_found")

    _, sent = _handle(bot, "금액 알려줘", client=client)

    assert sent == []


def test_a_channel_denial_is_delivered_by_dm_only():
    """거절도 DM 이다. 「권한이 없습니다」 가 채널에 뜨면 그 자체가 정보다."""
    bot = _bot([], [], vip=True)
    bot._request_context_resolver = lambda _client, _user: (_ for _ in ()).throw(
        RequestAuthorizationDenied("no_grant")
    )
    client = _client()

    _, sent = _handle(bot, "금액 알려줘", client=client)

    assert sent == []
    assert _dm_posts(client)[0]["text"] == DENY_MESSAGE


def test_a_processing_failure_is_delivered_by_dm_only():
    """처리 실패 문구에도 모델·키 사정이 들어간다. 채널에 두지 않는다."""
    bot = _bot([Intent("search", question="금액")], [], vip=True)

    def boom(*args, **kwargs):
        raise RuntimeError("Error code: 401 authentication_error")

    bot.engine.respond = boom
    client = _client()

    _, sent = _handle(bot, "금액 알려줘", client=client)

    assert sent == []
    assert "ANTHROPIC_API_KEY" in _dm_posts(client)[0]["text"]


def test_no_progress_message_is_posted_into_the_channel():
    """진행 안내도 채널에 두지 않는다 — 누가 무엇을 물었는지가 드러난다."""
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=True)
    client = _client()

    _, sent = _handle(bot, "금액 알려줘", client=client)

    assert [kw["text"] for kw in sent] == [PRIVATE_ANSWER_NOTICE]


# --- 4. TYBot 의 전달은 그대로 -------------------------------------------------


def test_tybot_still_answers_in_the_channel():
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=False)
    client = _client()

    _, sent = _handle(bot, "금액 알려줘", client=client)

    assert "임원 보고 요약입니다" in sent[0]["text"]
    assert PRIVATE_ANSWER_NOTICE not in str(sent)
    client.conversations_open.assert_not_called()


def test_tybot_still_answers_inside_the_thread_it_was_called_in():
    bot = _bot([Intent("search", question="금액")], [_answered()], vip=False)
    bot.reply_in_thread = True
    client = _client()

    _, sent = _handle(
        bot, "금액 알려줘", client=client, event={"thread_ts": "0.5", "ts": "1.0"}
    )

    # **본문이** 그 스레드에 간다. 안내 한 줄에도 `thread_ts` 는 붙으므로
    # 좌표만 보면 DM 전용으로 바뀌어도 이 시험이 통과한다.
    assert sent[0]["thread_ts"] == "0.5"
    assert "임원 보고 요약입니다" in sent[0]["text"]
    client.conversations_open.assert_not_called()


def test_tybot_still_grants_canvas_access_to_the_channel(monkeypatch):
    monkeypatch.setattr(
        "tybot.slack.pilot.create_answer_canvas",
        lambda client, body, **kw: CanvasResult("F1", "https://example.slack.com/F1"),
    )
    bot = _bot([Intent("summary", question="주간 현황")], [_answered()], vip=False)
    client = _client()

    _, sent = _handle(bot, "주간 현황을 캔버스로 답변해", client=client)

    assert "Canvas 열기" in sent[0]["text"]
    client.canvases_access_set.assert_called_once_with(
        canvas_id="F1", access_level="read", channel_ids=["C-EXEC"]
    )


# --- 5. 질문 첨부 --------------------------------------------------------------


def test_a_vip_question_attachment_goes_through_the_common_reader(monkeypatch):
    """첨부는 **권한 판정 뒤에** 공통 리더가 연다. VIP 전용 다운로더는 없다."""
    bot = _bot([Intent("search", question="이 파일 요약해")], [_answered()], vip=True)
    seen: list[tuple] = []

    def fake_read(files, token, **kwargs):
        seen.append((tuple(f["id"] for f in files), token))
        return QuestionFileEvidence(
            lines=("계약 금액 15억",),
            citations=("<https://ty.slack.com/files/F1|📎견적.xlsx>",),
            files_read=1,
        )

    monkeypatch.setattr("tybot.slack.pilot.read_question_files", fake_read)
    client = _client()

    event, _ = _handle(
        bot, "이 파일 요약해", client=client, event={"files": [{"id": "F1"}]}
    )

    assert seen == [(("F1",), "xoxb-vip")], "VIP 자기 토큰으로 자기 질문 첨부만 읽는다"
    assert event["_tybot_question_file_used"] is True
    # 출처는 Slack permalink 다. 그리고 그 출처도 DM 에만 간다.
    assert "📎견적.xlsx" in _dm_posts(client)[0]["text"]


def test_a_vip_attachment_leaves_no_reopenable_coordinate(monkeypatch):
    """임시 근거는 좌표로 남지 않는다. 남으면 다음 질문이 메모리 사본을 연다."""
    bot = _bot([Intent("search", question="요약")], [_answered()], vip=True)
    monkeypatch.setattr(
        "tybot.slack.pilot.read_question_files",
        lambda files, token, **kw: QuestionFileEvidence(
            lines=("한 줄",), citations=("<https://ty.slack.com/files/F1|📎a.xlsx>",),
            files_read=1,
        ),
    )

    _handle(bot, "요약해", event={"files": [{"id": "F1"}]})

    (hits,) = [seed for seed in bot.engine.seeds if seed]
    assert all(hit.doc.channel_id is None for hit in hits), (
        "channel_id 가 있으면 `refs_from_hits` 가 좌표를 저장한다"
    )
    (rec,) = bot.qa_log.records
    assert all(
        "__question_attachment__" not in str(ref) for ref in rec.evidence_refs
    )


def test_a_rejected_vip_attachment_is_not_replaced_by_an_archive_search(monkeypatch):
    """못 읽은 첨부를 아카이브 검색으로 슬쩍 바꾸지 않는다.

    바꾸면 사람은 자기가 올린 파일을 읽고 답한 줄 안다. 실제로는 올리기 전
    자료로만 만든 답이다.
    """
    bot = _bot([Intent("search", question="요약")], [_answered()], vip=True)
    monkeypatch.setattr(
        "tybot.slack.pilot.read_question_files",
        lambda files, token, **kw: QuestionFileEvidence(codes=("file_too_large",)),
    )
    client = _client()

    _handle(bot, "요약해", client=client, event={"files": [{"id": "F1"}]})

    assert bot.engine.asked == [], "첨부를 못 읽었는데 검색으로 넘어갔다"
    assert "출처:" not in _dm_posts(client)[0]["text"]


# --- 6. 후속 질문 --------------------------------------------------------------


def test_a_vip_follow_up_carries_coordinates_not_the_previous_answer():
    """이어 가는 것은 좌표다. 답변 문장을 이어 가면 요약을 근거로 요약한다(원칙 1)."""
    bot = _bot([Intent("search", question="그 금액 다시")], [_answered()], vip=True)
    bot.qa_log.thread_context = [{
        "question": "임원 보고 금액 알려줘",
        "subject_terms": ["임원 보고"],
        "evidence_refs": [object()],
        "attachment_refs": [],
        "answer": "봇이 지난번에 한 말",
    }]

    _handle(bot, "그 금액 다시", event={"thread_ts": "0.5", "ts": "1.0"})

    (context,) = bot.engine.plan_contexts
    assert "임원 보고 금액 알려줘" in context
    assert "봇이 지난번에 한 말" not in context
    assert "이전 근거: 원문 1줄" in context


def test_vip_follow_up_history_comes_from_the_vip_qa_log():
    """VIP 좌표와 TYBot 좌표는 **다른 디렉터리**에 산다.

    두 봇의 허용 범위가 다르므로, 한쪽 기록의 좌표를 다른 쪽이 집으면 그 순간
    권한이 섞인다. 분리는 설정이 아니라 구조로 지킨다.
    """
    vip_log = FakeQALog("/var/lib/tybot-vipbot/qa")
    vip_log.thread_context = [{"question": "VIP 전용 이전 질문"}]
    bot = _bot([Intent("search", question="이어서")], [_answered()], vip=True,
               qa_log=vip_log)

    _handle(bot, "이어서", event={"thread_ts": "0.5"})

    assert "VIP 전용 이전 질문" in bot.engine.plan_contexts[0]
    assert bot.qa_log.root == "/var/lib/tybot-vipbot/qa"


def test_the_follow_up_resolver_is_the_common_one():
    bot = _bot([], [], vip=True)
    from tybot.thread_followup import ThreadFollowupResolver

    assert isinstance(bot._followup_resolver(), ThreadFollowupResolver)


# --- 7. 공통 경로에 VIP 전용 갈래가 없다 ---------------------------------------


@pytest.mark.parametrize(
    "method",
    [
        "_handle_request",
        "_question_file_evidence",
        "_question_file_hits",
        "_apply_question_file_citations",
        "_thread_turns",
        "_followup_resolver",
    ],
)
def test_no_vip_only_branch_exists_in_the_shared_answer_path(method):
    """「VIP 면 이렇게」 가 한 줄 생기면 거기서부터 두 제품이 된다.

    VIP 가 다른 것은 **주입된 구성**이어야 한다 — 권한 판정기, 질문 정규화,
    엔진, QA 경로, 전달 방식(`private_answers`). 이름으로 갈래를 치는 순간 그
    차이는 코드에 박히고, 다음 사람은 왜 달라야 하는지 모른 채 한 줄을 더 더한다.
    """
    body = inspect.getsource(getattr(WorkspaceBot, method))
    for marker in ('bot_name == "vipbot"', "bot_name=='vipbot'", "vipbot_delivery"):
        assert marker not in body, f"{method} 에 VIP 전용 갈래가 있다: {marker}"


def test_private_delivery_is_a_flag_not_a_bot_name_check():
    """전달 분기의 조건은 **구성값**이다.

    봇 이름으로 치면 다음 전용 봇마다 한 줄씩 늘고, 그 줄들은 서로 다른 곳에서
    서로 다르게 틀린다.
    """
    source = inspect.getsource(WorkspaceBot)
    assert 'getattr(self, "private_answers", False)' in source
    assert '"vipbot"' not in source


def test_the_vip_branch_only_passes_configuration_to_the_common_bot():
    """`build_bots` 의 VIP 분기가 넘기는 것은 구성뿐이다."""
    from tybot.slack import pilot

    source = inspect.getsource(pilot.build_bots)
    branch = source[source.index("vip_connections = _vip_runtime_credentials") :]
    for name in (
        "request_context_resolver",
        "question_transform",
        "bot_name",
        "realtime",
        "autojoin",
        "operational_owner",
        "private_answers",
    ):
        assert name in branch, f"VIP 분기가 {name} 을 넘기지 않는다"
    # 쓰는 쪽은 켜지지 않고, 전달은 DM 으로 고정된다.
    assert "realtime=False" in branch
    assert "autojoin=False" in branch
    assert "operational_owner=False" in branch
    assert "private_answers=True" in branch
