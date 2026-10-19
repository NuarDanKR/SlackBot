"""의도 분류 — LLM 판단 + 규칙 폴백."""
from __future__ import annotations

import json

import pytest

from tybot import intent
from tybot.gateway.base import LLMResponse, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.intent import CLASSIFIER_MODEL, classify, classify_by_rule, parse_period


class ScriptedProvider:
    """분류기가 뱉을 문자열을 그대로 돌려준다."""

    name = "anthropic"

    def __init__(self, payload: str, *, raises: Exception | None = None):
        self.payload = payload
        self.raises = raises
        self.models: list[str] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0,
                 timeout_seconds=None):
        if self.raises:
            raise self.raises
        self.models.append(spec.model)
        return LLMResponse(self.payload, spec.model, self.name, 200, 30, 0.0002)


def _router(provider, *, with_haiku=True):
    registry = {
        "claude-sonnet-5": ModelSpec(
            "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
        )
    }
    if with_haiku:
        registry[CLASSIFIER_MODEL] = ModelSpec(
            CLASSIFIER_MODEL, "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
        )
    return Router(
        providers={"anthropic": provider}, registry=registry, cost_guard=CostGuard(10.0)
    )


def test_llm_decides_kind_and_terms():
    p = ScriptedProvider(json.dumps({"kind": "search", "terms": ["김해외동", "기성금"]}))
    intent = classify("김해외동 기성금 얼마인지 알려줘", _router(p))
    assert intent.kind == "search"
    assert intent.terms == ["김해외동", "기성금"]  # 요청 표현('알려줘')은 검색어에서 빠진다
    assert intent.source == "llm"


def test_uses_cheap_classifier_model():
    p = ScriptedProvider(json.dumps({"kind": "status"}))
    classify("너 지금 잘 돌아가?", _router(p))
    assert p.models == [CLASSIFIER_MODEL]  # 분류는 최저가 모델로


def test_falls_back_to_default_model_when_classifier_missing():
    p = ScriptedProvider(json.dumps({"kind": "status"}))
    intent = classify("상태 어때", _router(p, with_haiku=False))
    assert p.models == ["claude-sonnet-5"]
    assert intent.kind == "status"


def test_code_fenced_json_is_parsed():
    p = ScriptedProvider('```json\n{"kind": "summary", "days": 30}\n```')
    intent = classify("한달치 정리해줘", _router(p))
    assert intent.kind == "summary" and intent.days == 30


@pytest.mark.parametrize(
    "payload", ["설명만 하고 JSON 없음", '{"kind": "존재하지않는것"}', '{"kind": '],
)
def test_bad_output_falls_back_to_rules(payload):
    intent = classify("현재 너의 상태 알려줘", _router(ScriptedProvider(payload)))
    assert intent.source == "regex"
    assert intent.kind == "status"


def test_llm_failure_does_not_break_bot():
    p = ScriptedProvider("", raises=RuntimeError("API 다운"))
    intent = classify("이번주 요약해줘", _router(p))
    assert intent.source == "regex" and intent.kind == "summary"


def test_no_router_uses_rules():
    assert classify("도움말", None).kind == "help"


@pytest.mark.parametrize(
    "text,kind",
    [
        ("현재 너의 상태 알려줘", "status"),
        ("연결 상태 어때?", "status"),
        ("지금 살아있어?", "status"),
        ("사용법 알려줘", "help"),
        ("이번주 진행 상황", "summary"),
        ("김해외동 기성금 얼마야?", "search"),
        ("과거 자료를 가져오는데 최신 자료를 가져오는 게 좋겠어", "feedback"),
    ],
)
def test_rule_fallback_covers_common_phrasing(text, kind):
    assert classify_by_rule(text).kind == kind


def test_rule_fallback_strips_request_words():
    assert "알려줘" not in classify_by_rule("김해외동 기성금 알려줘").terms


@pytest.mark.parametrize(
    "text,days", [("30일 요약", 30), ("2주 진행상황", 14), ("이번달 요약", 30), ("요약해줘", 7)]
)
def test_parse_period(text, days):
    assert parse_period(text) == days


@pytest.mark.parametrize(
    "text,kind",
    [
        ("내용 수집해", "ingest"),
        ("수집해줘", "ingest"),
        ("이 채널 취합해줘", "ingest"),
        ("대화 모아줘", "ingest"),
        ("수집", "ingest"),
        ("전체 수집해", "ingest_all"),
        ("모든 채널 수집", "ingest_all"),
        # 현황 '질문'은 수집 실행이 아니다
        ("몇 건 수집했어?", "status"),
        ("수집 상태 알려줘", "status"),
    ],
)
def test_ingest_command_vs_status_question(text, kind):
    assert classify_by_rule(text).kind == kind


# --- 상태 질문: LLM 분류기가 죽었을 때가 가장 중요하다 -------------------------
# 실제 사고: 키가 401 이 되어 분류기가 규칙으로 폴백했는데, 규칙이 "현재 상태" 를
# search 로 봤다. 그래서 상태 질문이 아카이브 검색 -> LLM 호출 -> 또 401 로 죽었다.
STATUS_QUESTIONS = [
    "현재 상태",
    "지금 상태",
    "상태 알려줘",
    "상태는 어때?",
    "상태 확인해줘",
    "봇 상태",
    "상태",
    "시스템 상태",
    "서버 상황",
    "현재 상황 보여줘",
]


@pytest.mark.parametrize("q", STATUS_QUESTIONS)
def test_status_questions_need_no_llm(q):
    assert classify_by_rule(q).kind == "status"


# 상태 표현이 들어갔지만 실제로는 아카이브 질문인 것들 - status 로 새면 안 된다.
NOT_STATUS = [
    ("현재 자금 상태 문서 찾아줘", "search"),
    ("이번주 현장 진행 상황 요약해줘", "summary"),
    ("이전 답변 기억나?", "memory"),
]


@pytest.mark.parametrize(("q", "kind"), NOT_STATUS)
def test_status_pattern_does_not_swallow_archive_questions(q, kind):
    assert classify_by_rule(q).kind == kind


# --- 문서 집합 요약: 기간의 범위 (설계 §9) --------------------------------------
#
# 「여태까지 수집된 주간 보고를 종합해줘」 를 최근 7일로 처리하면, 파일이 멀쩡히
# 아카이브에 있어도 후보에 안 들어온다. 사용자가 요청한 범위와 실제 조회 범위가
# 다른데 답변은 「자료가 없다」 로 나가서, 그 차이가 어디에도 안 보인다.
REAL_QUESTION = "여태까지 수집된 주간 보고 회의 관련 내용을 종합해줘"


def test_the_real_question_is_a_summary_not_a_status():
    """`수집된` 이 status 로 새면 요약 요청이 통째로 사라진다(2026-09-11 실측)."""
    got = intent.classify_by_rule(REAL_QUESTION)
    assert got.kind == "summary"


def test_the_real_question_asks_for_all_time():
    got = intent.classify_by_rule(REAL_QUESTION)
    assert got.time_scope == intent.SCOPE_ALL
    assert got.wants_all_time


def test_the_real_question_carries_the_document_kinds():
    """본문에 「주간 보고 회의」 가 없어도 파일명으로 찾을 수 있어야 한다(§2.6)."""
    got = intent.classify_by_rule(REAL_QUESTION)
    assert "주간보고" in got.document_query
    assert "주간업무보고" in got.document_query
    assert "업무보고" in got.document_query
    assert got.is_document_set


def test_summary_keeps_its_topic_terms():
    """검색어가 비면 채널 전체 최근 줄을 쓴다 — 질문과 상관없는 답이 된다(§2.5)."""
    assert intent.classify_by_rule(REAL_QUESTION).terms


@pytest.mark.parametrize("text", [
    "여태까지 올라온 자료 정리해줘",
    "지금까지 수집된 업무보고 종합해줘",
    "그동안 쌓인 내용 요약해줘",
    "전체 기간 현황 정리해줘",
])
def test_all_time_words(text):
    assert intent.parse_time_scope(text) == intent.SCOPE_ALL


@pytest.mark.parametrize("text", [
    "이번주 진행상황 정리해줘",
    "최근 30일 미수금 정리해줘",
    "오늘 뭐 있었어 요약해줘",
])
def test_explicit_periods_are_bounded(text):
    assert intent.parse_time_scope(text) == intent.SCOPE_BOUNDED


def test_no_period_stays_default():
    assert intent.parse_time_scope("진행상황 정리해줘") == intent.SCOPE_DEFAULT


def test_days_is_not_overwritten_by_the_scope():
    """`days` 를 0 이나 큰 수로 덮어쓰면 「기본이라 7일」 과 「전체 요청」 을 못 가른다."""
    got = intent.classify_by_rule(REAL_QUESTION)
    assert got.days == intent.DEFAULT_DAYS
    assert got.time_scope == intent.SCOPE_ALL


# --- 동의어는 검토된 사전에서만 -----------------------------------------------
def test_document_synonyms_do_not_spread_to_other_kinds():
    """「보고」 에서 「회계보고」·「사고보고」 로 번지면 묻지 않은 문서가 섞인다."""
    got = intent.expand_document_query("주간보고 종합해줘")
    assert "주간보고" in got
    assert not any("회계" in x or "사고" in x for x in got)


def test_unknown_document_kind_yields_nothing():
    assert intent.expand_document_query("점심 메뉴 정리해줘") == []


def test_spaces_in_the_document_name_still_match():
    """사람은 「주간 보고」 라고 띄어 쓴다. 파일명은 붙여 쓴다."""
    assert intent.expand_document_query("주간 보고 종합해줘")


def test_every_synonym_entry_includes_its_own_key():
    """키가 빠지면 그 이름으로 올라온 파일을 자기 이름으로 못 찾는다."""
    for key, values in intent.DOCUMENT_SYNONYMS.items():
        assert key in values, key


# --- status 와 요약의 경계 -----------------------------------------------------
@pytest.mark.parametrize("text", ["수집 현황 알려줘", "몇 건 수집됐어?", "수집했어?"])
def test_real_collection_status_questions_still_work(text):
    assert intent.classify_by_rule(text).kind == "status"


def test_document_query_is_empty_for_non_summary():
    """검색 질문에 문서 종류를 붙이면 단일 사실 질문이 문서 집합으로 바뀐다."""
    got = intent.classify_by_rule("김해외동 기성금 얼마야")
    assert got.kind == "search"
    assert got.document_query == []
    assert not got.is_document_set


# =============================================================================
# 2026-09-14 실제 사고 — 기계적 분류가 스레드 후속 질문을 놓쳤다
# =============================================================================


def test_a_conjugated_action_verb_still_counts_as_a_request():
    """표면형 목록은 한국어에서 반드시 샌다.

    `찾아` 는 있는데 `찾고` 가 없어서, 스레드 안에서 이전 문답을 다 가진 채로
    "저는 이전 대화를 기억하지 못해요" 라고 답했다.
    """
    from tybot.intent import apply_followup, plan_by_rule

    text = (
        "다시, 우리가 방금 나눴던 얘기중에 너가 제대로 못한 답변이 있어. "
        "뭔지 찾고 제대로된 답을 얘기해봐"
    )

    (task,) = apply_followup(text, plan_by_rule(text), has_prior=True)

    assert task.kind != "memory", "실행 요청이 기억 확인으로 샜다"
    assert task.reference_mode != "none"


@pytest.mark.parametrize(
    "verb",
    ["찾고", "찾을", "찾는", "정리하고", "확인해봐", "알려줄", "말해봐", "답해봐", "얘기해줘"],
)
def test_action_detection_survives_korean_conjugation(verb):
    from tybot.intent import ACTION_RE

    assert ACTION_RE.search(f"방금 그거 {verb}")


def test_asking_whether_we_remember_is_still_a_memory_question():
    """고친 쪽으로 너무 넓히면 기억 질문이 실행으로 샌다."""
    from tybot.intent import apply_followup, plan_by_rule

    for text in ("이전 답변 기억나?", "우리 대화 기억해?"):
        (task,) = apply_followup(text, plan_by_rule(text), has_prior=True)
        assert task.kind == "memory", text


# --- 「이런 자료도 읽느냐」 ---------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "그럼 채널에 있는 폴더는?",
        "캔버스의 내용도 읽고 답해주냐는거였어",
        "첨부파일도 근거로 보나요?",
        "이미지 읽을 수 있어?",
        "엑셀 표도 인식해?",
    ],
)
def test_asking_what_we_read_is_answered_not_searched(text):
    """업무 내용이 아니라 **우리 범위**를 묻는 질문이다.

    아카이브 검색으로 보내면 0건이 나오고 도움말이 나간다 — 사용자는 답을 못
    받는다. 자료 종류마다 정규식을 하나씩 두면 반드시 새므로 한 부류로 묶는다.
    """
    from tybot.intent import is_source_capability, plan

    assert is_source_capability(text), text
    assert plan(text, None)[0].kind == "help"


@pytest.mark.parametrize(
    "text",
    [
        "김해외동 기성금 얼마야?",
        "이번주 요약해줘",
        "광주도시철도 미수금 정리해줘",
        # 자료 낱말이 들어 있고 물음표로 끝나지만 **업무 질문**이다. 길이 상한이
        # 없으면 이런 문장이 통째로 기능 안내로 새고, 사용자는 답을 못 받는다.
        "김해외동 현장에서 지난달에 올라온 문서 중에 기성금 관련된 게 얼마인지 알려줄 수 있어?",
        "주간 보고 파일에 적힌 이번 분기 목표 금액이 정확히 얼마인지 확인해 줄 수 있을까요?",
    ],
)
def test_a_business_question_is_not_mistaken_for_a_capability_question(text):
    from tybot.intent import is_source_capability

    assert not is_source_capability(text), text


def test_the_scope_answer_names_what_we_do_not_read():
    """「못 찾았습니다」 만 나가면 사용자는 **자료가 없다**고 읽는다.

    실제로는 우리가 그 자리를 안 보는 것이고, 둘은 사람이 할 일이 완전히 다르다 —
    하나는 자료를 올리는 것이고 하나는 우리에게 말하는 것이다.
    """
    from tybot.slack.pilot import SOURCE_SCOPE_ANSWER

    assert "읽지 않는 것" in SOURCE_SCOPE_ANSWER
    assert "폴더" in SOURCE_SCOPE_ANSWER, "물어본 것을 답하지 않는다"
    assert "초대" in SOURCE_SCOPE_ANSWER, "왜 없는지와 어떻게 넣는지를 말해야 한다"
    assert "수집" in SOURCE_SCOPE_ANSWER


def test_the_scope_answer_does_not_overclaim_canvas():
    """Canvas 를 「읽는다」 고만 적으면 **안에 걸린 파일까지** 읽는 줄 안다.

    실측(2026-09-14): 본문 글자만 받고, 안에 걸린 파일은 내려받지도 변환하지도
    않는다. HTML Canvas 는 링크 대상(URL)까지 버린다 — 라벨만 남는다.
    그 차이를 안 밝히면 「이름은 보이는데 내용은 없다」 를 사용자가 버그로 읽는다.
    """
    from tybot.slack.pilot import SOURCE_SCOPE_ANSWER

    assert "Canvas 안에" in SOURCE_SCOPE_ANSWER
    assert "따라가지 않습니다" in SOURCE_SCOPE_ANSWER


# --- 규칙 폴백이 업무 질문을 도움말로 삼키지 않는다 (2026-09-18 패킷) ----------
def test_a_file_request_is_not_a_capability_question():
    """LLM 분해가 흔들린 순간, 평범한 업무 요청이 도움말 덤프로 나갔다.

    자료 낱말 뒤 40자 안에 `읽|보|답…` 이 오기만 하면 능력 질문으로 보던 그물에
    `보고서` 의 「보」, `보여줘` 의 「보」 까지 걸렸다(QA 98fa5853).
    """
    for text in (
        "주간 업무보고 파일 보여줘",
        "첨부된 파일 내에 적혀있는 목록을 전부 찾아 알려주세요",
        "그 문서에서 금액만 뽑아줘",
        "계약 문서 어디 있는지 조회해줘",
    ):
        assert not intent.is_source_capability(text), text
        assert intent.classify_by_rule(text).kind != "help", text


def test_a_real_capability_question_is_still_caught():
    """능력을 묻는 말은 따로 있다. 검색으로 보내면 0건이 나오고 답을 못 받는다."""
    for text in (
        "캔버스 내용도 읽을 수 있어?",
        "첨부 파일도 읽나요?",
        "엑셀 표도 인식 가능한가요?",
        "그럼 채널에 있는 폴더는?",
    ):
        assert intent.is_source_capability(text), text


# --- 분해 출력 잘림 (2026-10-19 tyit C0C9A86JUSJ) -----------------------------
#
# 응답이 JSON 으로 시작해 `kind=search` 까지 적다가 상한에서 끊겼는데,
# `_extract_json()` 은 닫는 괄호가 없자 **「JSON 없음」** 으로 보고했다. 모델이
# 엉뚱한 소리를 한 것과 분량이 모자란 것이 같은 오류로 보여, 상한이 원인이라는
# 신호가 어디에도 없었다.


class _CountingProvider:
    """호출마다 다른 응답을 돌려준다. **상한을 그대로 기록한다.**"""

    name = "anthropic"

    def __init__(self, *responses, raises: Exception | None = None):
        self.responses = list(responses)
        self.raises = raises
        self.caps: list[int] = []
        self.timeouts: list[float | None] = []
        self.calls = 0

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0,
                 timeout_seconds=None):
        self.calls += 1
        self.caps.append(max_tokens)
        self.timeouts.append(timeout_seconds)
        if self.raises:
            raise self.raises
        text, stop = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        return LLMResponse(text, spec.model, self.name, 900, 40, 0.0002, stop_reason=stop)


LONG_COMPARE = (
    "코오롱베네시트 AX BOOST SUMMIT 2026 에서 CEO 연설과 코오롱인더스트리 AX "
    "도입사례 두 발표를 비교해서 각각의 핵심 논점과 성과 지표, 그리고 우리 "
    "회사에 적용할 때의 시사점을 정리해줘"
)

# 운영에서 난 모양 그대로 — JSON 으로 시작하고 kind 까지 나왔는데 닫히지 않았다.
TRUNCATED = (
    '{"tasks": [{"kind": "search", "question": "코오롱베네시트 AX BOOST SUMMIT '
    '2026 CEO 연설과 코오롱인더스트리 AX 도입사례의 핵심 논점", '
    '"standalone_question": "코오롱베네시트 AX BOOST SUMMIT 2026 에서 CEO'
)

GOOD = json.dumps(
    {"tasks": [{"kind": "search", "question": "핵심 논점", "terms": ["AX", "도입사례"]}]},
    ensure_ascii=False,
)


def test_a_cut_off_json_is_not_reported_as_no_json():
    """**잘린 것과 틀린 것은 다르다.** 사람이 할 일이 다르다."""
    with pytest.raises(intent.PlannerOutputTruncated) as cut:
        intent._extract_json(TRUNCATED)
    assert cut.value.code == "planner_truncated"

    with pytest.raises(intent.PlannerOutputError) as bad:
        intent._extract_json("죄송합니다. 무슨 뜻인지 모르겠습니다.")
    assert bad.value.code == "planner_invalid_json"
    assert not isinstance(bad.value, intent.PlannerOutputTruncated)


def test_the_provider_stop_reason_decides_even_without_a_brace():
    """프로바이더가 「상한에서 끊었다」 고 말했으면 본문 모양을 추측하지 않는다."""
    with pytest.raises(intent.PlannerOutputTruncated):
        intent._extract_json("코오롱베네시트 AX BOOST", stop_reason="max_tokens")


def test_a_truncated_plan_is_retried_once_with_a_bigger_cap():
    """이번 긴 비교 질문. 한 번 더, **더 넉넉한 상한으로** 묻는다."""
    p = _CountingProvider((TRUNCATED, "max_tokens"), (GOOD, "end_turn"))

    tasks = intent.plan(LONG_COMPARE, _router(p), allow_rule_fallback=False)

    assert p.calls == 2, "잘렸으면 한 번 더 묻는다"
    assert p.caps == [intent.PLANNER_MAX_TOKENS, intent.PLANNER_RETRY_MAX_TOKENS]
    assert [t.kind for t in tasks] == ["search"]
    assert tasks[0].source == "llm"


def test_a_good_plan_is_not_retried():
    """정상 JSON 에 재시도를 붙이면 분류 비용이 두 배가 된다."""
    p = _CountingProvider((GOOD, "end_turn"))

    tasks = intent.plan("핵심 논점 알려줘", _router(p), allow_rule_fallback=False)

    assert p.calls == 1
    assert p.caps == [intent.PLANNER_MAX_TOKENS]
    assert [t.kind for t in tasks] == ["search"]


def test_a_schema_error_is_not_retried():
    """스키마가 틀린 답은 다시 물어도 같은 답이 온다. 비용만 쓴다."""
    p = _CountingProvider((json.dumps({"tasks": []}), "end_turn"))

    with pytest.raises(intent.PlanningUnavailable):
        intent.plan("핵심 논점 알려줘", _router(p), allow_rule_fallback=False)

    assert p.calls == 1


def test_a_twice_truncated_plan_gives_up_instead_of_closing_the_json():
    """**끊긴 JSON 을 닫아서 실행하지 않는다.** 두 번째도 끊기면 실패로 올린다."""
    p = _CountingProvider((TRUNCATED, "max_tokens"))

    with pytest.raises(intent.PlanningUnavailable):
        intent.plan(LONG_COMPARE, _router(p), allow_rule_fallback=False)

    assert p.calls == 2, "재시도는 한 번뿐이다"


def test_an_api_failure_still_falls_back_by_rule_when_allowed():
    """API 가 죽어도 봇은 멈추지 않는다 — 규칙 폴백이 허용된 경로에서만."""
    p = _CountingProvider(raises=RuntimeError("provider down"))

    tasks = intent.plan("김해외동 기성금 얼마야?", _router(p))

    assert tasks and tasks[0].source != "llm"


def test_master_v2_never_falls_back_to_the_regex_planner():
    """Master v2 는 정규식으로 내려가지 않는다. 못 판단했으면 못 판단한 것이다."""
    for provider in (
        _CountingProvider(raises=RuntimeError("provider down")),
        _CountingProvider((TRUNCATED, "max_tokens")),
        _CountingProvider(("설명만 적었습니다", "end_turn")),
    ):
        with pytest.raises(intent.PlanningUnavailable):
            intent.plan(LONG_COMPARE, _router(provider), allow_rule_fallback=False)


def test_the_retry_budget_failure_reports_the_truncation():
    """재시도가 막히면(비용 상한 등) 사람이 봐야 할 것은 **잘렸다** 이다."""
    class _OnceThenDead(_CountingProvider):
        def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0,
                     timeout_seconds=None):
            self.calls += 1
            self.caps.append(max_tokens)
            self.timeouts.append(timeout_seconds)
            if self.calls > 1:
                raise RuntimeError("cost limit")
            return LLMResponse(TRUNCATED, spec.model, self.name, 900, 40, 0.0002,
                               stop_reason="max_tokens")

    p = _OnceThenDead()
    with pytest.raises(intent.PlanningUnavailable) as err:
        intent.plan(LONG_COMPARE, _router(p), allow_rule_fallback=False)

    assert isinstance(err.value.__cause__, intent.PlannerOutputTruncated)
    assert p.calls == 2


def test_the_diagnostic_log_carries_no_question_or_answer_body(caplog):
    """진단 로그에 **질문도 모델 응답 본문도** 남기지 않는다(원칙 5)."""
    p = _CountingProvider((TRUNCATED, "max_tokens"))

    with caplog.at_level("INFO", logger="tybot.intent"), pytest.raises(
        intent.PlanningUnavailable
    ):
        intent.plan(LONG_COMPARE, _router(p), allow_rule_fallback=False)

    said = "\n".join(r.getMessage() for r in caplog.records)
    assert "코오롱" not in said, "업무 내용이 로그로 복사됐다"
    assert "standalone_question" not in said
    # 대신 계기판은 남는다.
    assert "code=planner_truncated" in said
    assert "stop=max_tokens" in said
    assert f"model={CLASSIFIER_MODEL}" in said
    assert "in=900" in said and "out=40" in said


def test_the_output_cap_fits_the_current_schema():
    """500 은 지금 스키마에 맞지 않았다. 한 task 의 글자 칸만 그만큼 쓴다."""
    assert intent.PLANNER_MAX_TOKENS > 500
    assert intent.PLANNER_RETRY_MAX_TOKENS > intent.PLANNER_MAX_TOKENS


def test_the_planner_prompt_asks_for_fewer_repeated_fields():
    """같은 문장을 세 칸에 쓰면 긴 질문에서 상한을 넘는다."""
    prompt = intent.PLANNER_PROMPT

    assert "생략한다" in prompt
    # `confidence` 는 생략하면 전문 봇 선택이 통째로 버려진다(min_confidence).
    assert "`confidence` 도 **반드시** 넣는다" in prompt


def test_an_omitted_standalone_question_falls_back_to_the_question():
    """생략은 「같다」 는 뜻이다. 비워 두면 이전 턴 질문이 검색어에 섞인다."""
    p = _CountingProvider((json.dumps(
        {"tasks": [{"kind": "search", "question": "기성금 얼마야", "terms": ["기성금"]}]},
        ensure_ascii=False,
    ), "end_turn"))

    tasks = intent.plan("기성금 얼마야", _router(p), allow_rule_fallback=False)

    assert tasks[0].standalone_question == "기성금 얼마야"


def test_a_response_without_a_stop_reason_still_plans():
    """`stop_reason` 을 안 들고 오는 응답 객체도 있다.

    거기서 AttributeError 가 나면 **분해 전체가 조용히 규칙 폴백으로 떨어진다** —
    잘림을 고치려다 더 넓게 망가뜨리는 길이다.
    """
    class _Bare:
        def __init__(self, text):
            self.text, self.model, self.cost_usd = text, "fake", 0.0

    class _BareRouter:
        def complete(self, messages, **kw):
            return _Bare(GOOD)

    tasks = intent.plan("핵심 논점", _BareRouter(), allow_rule_fallback=False)

    assert [t.kind for t in tasks] == ["search"]
    assert tasks[0].source == "llm"


# --- Codex 검수 반영 (2026-10-19) ---------------------------------------------


def test_a_max_tokens_stop_is_truncated_even_when_the_json_closes():
    """**종료 사유가 형식 판정보다 먼저다.**

    괄호가 맞아도 모델이 스스로 멈춘 것이 아니다. 세 개를 쓰려다 첫 개를 닫은
    자리에서 끊겼는지, 다 쓰고 뒤에 설명을 붙이다 끊겼는지는 본문 모양으로
    구별할 수 없다. 구별할 수 없는 것을 추측해 정상으로 수락하면 **반쪽 계획이
    완성된 판단처럼 흘러간다.**
    """
    with pytest.raises(intent.PlannerOutputTruncated):
        intent._extract_json(GOOD, stop_reason="max_tokens")

    # 같은 본문이라도 모델이 스스로 멈췄으면 정상이다.
    assert intent._extract_json(GOOD, stop_reason="end_turn")["tasks"]


def test_a_closed_json_cut_at_the_cap_is_retried_not_accepted():
    """plan() 경로에서도 같다. 수락하지 않고 한 번 더 묻는다."""
    p = _CountingProvider((GOOD, "max_tokens"), (GOOD, "end_turn"))

    tasks = intent.plan(LONG_COMPARE, _router(p), allow_rule_fallback=False)

    assert p.calls == 2
    assert [t.kind for t in tasks] == ["search"]


def test_the_failure_log_reports_the_response_it_actually_read():
    """재시도의 파싱이 실패하면 **두 번째** 응답을 찍는다.

    첫 응답을 찍으면 상한을 올린 뒤에도 옛 숫자(max_tokens·1,200)가 남아, 사람이
    「상한이 아직 모자라다」 로 거꾸로 짚는다.
    """
    class _SecondIsGarbage(_CountingProvider):
        def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0,
                     timeout_seconds=None):
            self.calls += 1
            self.caps.append(max_tokens)
            self.timeouts.append(timeout_seconds)
            if self.calls == 1:
                return LLMResponse(TRUNCATED, spec.model, self.name, 900, 1200,
                                   0.0002, stop_reason="max_tokens")
            return LLMResponse("죄송합니다. 못 알아듣겠습니다.", spec.model, self.name,
                               900, 2400, 0.0004, stop_reason="end_turn")

    p = _SecondIsGarbage()
    seen: list[str] = []
    import logging

    class _Catch(logging.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    log = logging.getLogger("tybot.intent")
    handler = _Catch()
    log.addHandler(handler)
    try:
        with pytest.raises(intent.PlanningUnavailable):
            intent.plan(LONG_COMPARE, _router(p), allow_rule_fallback=False)
    finally:
        log.removeHandler(handler)

    final = [line for line in seen if line.startswith("분해 실패")][-1]
    assert "code=planner_invalid_json" in final, final
    assert "stop=end_turn" in final, "첫 응답의 max_tokens 가 찍혔다"
    assert "out=2400" in final, "첫 응답의 토큰 수가 찍혔다"


def test_both_calls_share_one_time_budget():
    """첫 호출과 재시도가 **같은 시계**를 쓴다. 비용 검사와 시간은 별개다."""
    p = _CountingProvider((TRUNCATED, "max_tokens"), (GOOD, "end_turn"))

    intent.plan(LONG_COMPARE, _router(p), allow_rule_fallback=False)

    assert len(p.timeouts) == 2
    assert all(t is not None for t in p.timeouts), "시간 예산을 안 넘겼다"
    assert p.timeouts[0] <= intent.PLANNER_DEADLINE_SECONDS
    assert p.timeouts[1] < p.timeouts[0], "재시도는 남은 시간만 쓴다"


def test_the_retry_is_skipped_when_the_budget_is_spent(monkeypatch):
    """2초 남은 상태로 2,400토큰을 부르면 어차피 시간 초과로 끝난다."""
    ticks = iter([0.0, 0.0, intent.PLANNER_DEADLINE_SECONDS - 1.0])
    monkeypatch.setattr(intent, "_monotonic", lambda: next(ticks))
    p = _CountingProvider((TRUNCATED, "max_tokens"), (GOOD, "end_turn"))

    with pytest.raises(intent.PlanningUnavailable):
        intent.plan(LONG_COMPARE, _router(p), allow_rule_fallback=False)

    assert p.calls == 1, "남은 시간이 없는데 재시도했다"


# 2026-10-19 08:19 tyit `C0C9A86JUSJ` 에서 난 **토목현장 비교 질문**의 모양.
# 사내 질문 원문을 저장소에 넣지 않으므로 같은 성격(현장 둘 + 여러 지표 비교)의
# 문장으로 세운다 — 길이와 비교 축의 수가 분해 출력 분량을 정한다.
SITE_COMPARE = (
    "김해외동 현장이랑 평택고덕 현장 두 군데 공사기간이랑 누적 기성, 공정률, "
    "그리고 설계변경 건수까지 비교해서 어느 쪽이 더 지연되고 있는지랑 그 원인이 "
    "뭔지 정리해줘"
)


def test_the_site_comparison_question_survives_a_truncated_first_try():
    """이번에 터진 모양 그대로 — 긴 현장 비교 질문이 한 번 잘리고 되살아난다."""
    p = _CountingProvider((TRUNCATED, "max_tokens"), (GOOD, "end_turn"))

    tasks = intent.plan(SITE_COMPARE, _router(p), allow_rule_fallback=False)

    assert p.calls == 2
    assert p.caps == [intent.PLANNER_MAX_TOKENS, intent.PLANNER_RETRY_MAX_TOKENS]
    assert [t.kind for t in tasks] == ["search"]
