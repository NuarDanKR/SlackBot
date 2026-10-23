"""여러 현장·여러 기간 질문의 검색 예산과 **확인 못 한 범위** (2026-10-22).

## 막으려는 일

「김해외동·평택고덕·송산서측 세 현장의 7·8·9월 기성을 비교해줘」 는 범위가
**아홉 개**다. 그런데 검색 상한이 질문 하나당 고정이라, 첫 범위가 예산을 다
쓰고 나머지 여덟은 **보지도 않았다.** 그리고 그 사실이 어디에도 안 남았다.

| 벌어진 일 | 사람이 보는 것 |
|---|---|
| 답이 나왔다 | **아홉 범위를 다 본 답**으로 읽는다 |
| 답이 안 나왔다 | 「검색 한도에 닿았습니다」 — 어디까지 봤는지 모른다 |

이 파일이 지키는 것 셋.

1. **범위 수만큼 예산이 늘어난다**(상한 안에서). 그리고 한 범위가 다 먹지 못한다.
2. **최종 답변 몫을 남긴다.** 도구를 끝까지 긁어 쓰면 답이 아예 안 나간다.
3. **안 본 범위를 이름으로 적는다.** 모델이 빠뜨리면 **코드가 붙인다** —
   프롬프트로만 시키면 지켜질 때도 있고 아닐 때도 있다.

모델도 네트워크도 부르지 않는다. 라우터·도구상자는 대역이다.
"""
from __future__ import annotations

import pytest

from tybot import specialist_scope as sc
from tybot.gateway.base import LLMResponse, ModelSpec, Sensitivity, ToolCall
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.specialist_adapters import ToolSpecialist
from tybot.specialist_contract import SpecialistRequest
from tybot.specialist_tools import ToolRun

THREE_SITES_THREE_MONTHS = (
    "김해외동 현장이랑 평택고덕 현장, 송산서측 현장의 "
    "2026.07, 2026.08, 2026.09 기성 실적을 비교해 주세요."
)


# --- 범위 파싱 -------------------------------------------------------------------


def test_three_sites_times_three_months_is_nine_scopes():
    """**이 질문이 그 질문이다.** 범위를 하나로 세면 예산도 하나다."""
    plan = sc.plan(THREE_SITES_THREE_MONTHS)

    assert len(plan.scopes) == 9
    assert plan.multi is True
    labels = {s.label for s in plan.scopes}
    assert "김해외동 2026.07" in labels
    assert "송산서측 2026.09" in labels


def test_the_search_budget_grows_with_the_scopes():
    """범위가 아홉인데 검색이 둘이면 일곱은 **보지도 못한다.**"""
    one = sc.plan("김해외동 현장 기성 얼마야")
    many = sc.plan(THREE_SITES_THREE_MONTHS)

    assert one.total_searches == sc.BASE_SEARCHES
    assert many.total_searches > sc.BASE_SEARCHES
    assert many.total_searches <= sc.MAX_TOTAL_SEARCHES


def test_an_unparsed_question_keeps_the_old_budget():
    """못 읽으면 **늘리지 않는다.** 애매한 질문에서 늘면 비용만 는다."""
    for text in ("상태 어때", "", "요약해줘", "이거 다시 정리해줘"):
        plan = sc.plan(text)
        assert plan.multi is False
        assert plan.total_searches == sc.BASE_SEARCHES
        assert plan.coverage_note() == ""


def test_a_bare_noun_is_not_a_site():
    """접미사 없이 명사를 현장으로 추측하면 사람 이름·부서가 현장이 된다."""
    assert sc.plan("김해외동 기성금 알려줘").scopes == ()
    # 「세 현장」 의 「세」 같은 수식어도 현장이 아니다.
    labels = {s.label for s in sc.plan("세 현장 2026.07, 2026.08 비교").scopes}
    assert "세" not in labels


def test_periods_alone_are_scopes_too():
    plan = sc.plan("2026.07, 2026.08, 2026.09 기성 추이 알려줘")

    assert [s.label for s in plan.scopes] == ["2026.07", "2026.08", "2026.09"]
    assert plan.multi is True


def test_too_many_scopes_are_cut_and_the_cut_is_recorded():
    """잘린 것을 **「없다」 로 적지 않는다.**"""
    sites = ", ".join(f"현장{n}" for n in range(1, 8)) + " 현장"
    plan = sc.plan(f"{sites} 의 1월, 2월, 3월 비교")

    assert len(plan.scopes) <= sc.MAX_SCOPES
    assert plan.truncated > 0
    assert "제외" in plan.briefing()


def test_scopes_cut_by_the_cap_are_named_in_the_answer():
    """수만 세어 두면 답변에는 **아무것도 안 남는다.**

    사람은 그 범위를 물어본 적 없는 것처럼 읽고, 「찾아봤는데 없었다」 와
    「상한에 걸려 아예 안 봤다」 가 같아 보인다. 뒤쪽은 질문을 나눠 다시
    물어야 하는 일이라 할 일이 다르다.
    """
    sites = ", ".join(f"가평{n}" for n in range(1, 8)) + " 현장"
    plan = sc.plan(f"{sites} 의 1월, 2월, 3월 비교")

    assert plan.dropped, "상한을 넘겨야 시험이 성립한다"
    note = plan.coverage_note()
    assert f"범위 상한({sc.MAX_SCOPES}개)으로 이번 답변에서 제외" in note
    first = plan.dropped[0]
    assert first.site in note and first.period in note
    assert "나눠서 다시 물어" in note


def test_every_cut_scope_stays_identifiable_past_nine():
    """「외 N개」 로 줄이면 **어느 범위를 다시 물어야 하는지** 모른다.

    현장 일곱 × 달 셋이면 빠지는 범위가 벌써 아홉이다 — 흔한 질문이다.
    """
    sites = ", ".join(f"가평{n}" for n in range(1, 8)) + " 현장"
    plan = sc.plan(f"{sites} 의 1월, 2월, 3월 비교")
    note = plan.coverage_note()

    assert len(plan.dropped) >= 9
    assert "외 " not in note.split("제외:")[-1], "제외된 범위를 줄여 적었다"
    for scope in plan.dropped:
        assert scope.site in note and scope.period in note, scope.label
    # 쌍을 전부 나열하지 않는다 — 현장 하나에 기간을 묶는다.
    assert "가평5(1월, 2월, 3월)" in note


def test_the_uncovered_list_is_not_shortened_either():
    """미확인은 상한 안쪽이라 길어야 12개다. 그 안에서는 **다 적는다.**"""
    sites = ", ".join(f"가평{n}" for n in range(1, 8)) + " 현장"
    plan = sc.plan(f"{sites} 의 1월, 2월, 3월 비교")

    head = plan.coverage_note().split("/")[0]
    assert "외 " not in head
    for scope in plan.uncovered:
        assert scope.label in head


def test_cut_scopes_are_not_offered_to_the_model_as_next_targets():
    """이번 회차에서 일부러 뺀 범위를 「다음은 여기」 로 적으면 상한이 무의미하다."""
    sites = ", ".join(f"가평{n}" for n in range(1, 8)) + " 현장"
    plan = sc.plan(f"{sites} 의 1월, 2월, 3월 비교")

    assert all(s not in plan.uncovered for s in plan.dropped)
    assert all(s.label not in plan.briefing() for s in plan.dropped)


# --- 한 범위가 예산을 다 먹지 못한다 -------------------------------------------------


def test_one_scope_cannot_eat_the_whole_budget():
    plan = sc.plan(THREE_SITES_THREE_MONTHS)

    first = "김해외동 2026.07 기성"
    assert plan.refuse(first) == ""
    plan.spend(first)

    said = plan.refuse("김해외동 2026.07 기성 다시")
    assert "김해외동 2026.07" in said
    # **안 본 범위를 이름으로 알려 준다.** 「다른 데를 보라」 만으로는 어디인지 모른다.
    assert "평택고덕" in said or "송산서측" in said


def test_a_query_that_matches_no_scope_is_not_blocked():
    """범위를 못 고르면 막지 않는다. 막으면 **일반 검색이 통째로 죽는다.**"""
    plan = sc.plan(THREE_SITES_THREE_MONTHS)

    assert plan.refuse("외주비 추이") == ""


def test_the_total_cap_still_holds():
    plan = sc.plan(THREE_SITES_THREE_MONTHS)
    for n in range(plan.total_searches):
        plan.spend(f"범위{n}")

    said = plan.refuse("김해외동 2026.07")
    assert "다 썼습니다" in said


# --- 확인하지 못한 범위 ---------------------------------------------------------------


def test_unsearched_scopes_are_named():
    plan = sc.plan(THREE_SITES_THREE_MONTHS)
    plan.spend("김해외동 2026.07 기성")

    note = plan.coverage_note()
    assert note.startswith("※ 확인하지 못한 범위")
    assert "평택고덕 2026.08" in note
    # **「자료가 없다」 와 다르다.**
    assert "자료가 없다는 뜻이 아닙니다" in note
    assert "김해외동 2026.07" not in note


def test_covering_everything_leaves_no_note():
    """고친 쪽으로 너무 넓히면 **모든 답에 상투어**가 붙는다."""
    plan = sc.plan(THREE_SITES_THREE_MONTHS)
    for scope in plan.scopes:
        plan.spend(scope.label)

    assert plan.coverage_note() == ""


def test_a_single_scope_question_never_gets_the_note():
    """범위가 하나면 나눌 것이 없다. 상투어가 되면 진짜 미확인도 안 읽힌다."""
    plan = sc.plan("부산항 신항 웅동지구 현장 외주 손익 분석해줘")

    assert plan.uncovered == ()
    assert plan.coverage_note() == ""


# --- 어댑터 배선 --------------------------------------------------------------------


class _Provider:
    """회차마다 미리 정한 응답을 돌려주는 대역."""

    name = "anthropic"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0,
                 tools=(), timeout_seconds=None):
        self.calls.append({"messages": messages, "tools": [t.name for t in tools]})
        text, calls = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        return LLMResponse(text, spec.model, self.name, 100, 20, 0.001,
                           tool_calls=tuple(calls))


def _router(provider):
    return Router(
        providers={"anthropic": provider},
        registry={"claude-sonnet-5": ModelSpec(
            "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL)},
        cost_guard=CostGuard(10.0), default_model="claude-sonnet-5",
    )


class _Budget:
    """`ToolBudget` 대역. **실제와 같은 축을 들고 있다** — 호출·글자·시간."""

    def __init__(self, max_calls=12, max_per_tool=6, max_chars=90_000,
                 max_seconds=90.0):
        self.max_calls = max_calls
        self.max_per_tool = max_per_tool
        self.max_chars = max_chars
        self.max_seconds = max_seconds
        self.calls = 0
        self.chars = 0
        self.tool_seconds = 0.0
        self.reason = ""

    @property
    def exhausted(self):
        return bool(self.reason)

    def summary(self):
        return "budget=fake"


class _Toolbox:
    """도구상자 대역. **시도한 것과 `ok` 로 끝난 것을 따로** 센다.

    둘을 한 자리에 섞으면 거절·오류·잘린 검색이 「찾아본 범위」 로 기록돼도
    시험이 통과한다 — 이 파일이 막으려는 고장이 바로 그것이다.
    """

    def __init__(self, budget=None, result="검색 결과 3건", statuses=()):
        self.budget = budget or _Budget()
        #: 도구상자까지 내려온 검색어 — 상태와 무관하게 전부.
        self.attempts: list[str] = []
        #: 그중 `ok` 로 끝난 것. 기존 시험들이 「실행된 검색」 으로 읽는 값이다.
        self.queries: list[str] = []
        self.reserve_chars: list[int] = []
        self.result = result
        #: 호출 순서대로 돌려줄 상태. 모자라면 마지막 값을 이어 쓴다.
        self.statuses = list(statuses)

    class _Touched:
        documents = ()
        evidence_hits = ()
        live_permalinks = ()
        live_messages = ()

    touched = _Touched()

    def run_with_status(self, name, args, *, reserve_chars=0):
        query = str((args or {}).get("query") or "")
        self.attempts.append(query)
        self.reserve_chars.append(reserve_chars)
        status = (
            self.statuses[min(len(self.attempts) - 1, len(self.statuses) - 1)]
            if self.statuses else "ok"
        )
        if status != "ok":
            if status in ("refused", "reserved"):
                # 거절은 예산을 쓰지 않는다. 실제 `ToolBox.run_with_status` 와 같다.
                self.budget.reason = self.budget.reason or "calls"
            return ToolRun(f"({status})", status)
        self.queries.append(query)
        self.budget.calls += 1
        return ToolRun(self.result, "ok")

    def run(self, name, args):
        return self.run_with_status(name, args).text


def _search(call_id: str, query: str) -> ToolCall:
    return ToolCall(id=call_id, name="search", input={"query": query})


def _request(question: str) -> SpecialistRequest:
    # 근거 없이 시작하는 경로다 — 도구로 스스로 찾는 것이 이 어댑터의 전제다.
    return SpecialistRequest(question=question, evidence=(), allow_empty_evidence=True)


def test_the_adapter_tells_the_model_the_scopes_and_the_budget():
    """모델이 범위를 모르면 첫 범위부터 파고든다."""
    provider = _Provider(("정리했습니다.", ()))
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=_Toolbox(),
                             rules="규칙")

    adapter.complete(_request(THREE_SITES_THREE_MONTHS))

    opening = provider.calls[0]["messages"][1].content
    assert "범위가 9개입니다" in opening
    assert "김해외동 2026.07" in opening
    assert "한 범위에 몰아 쓰지 말고" in opening


def test_a_single_scope_question_gets_no_briefing():
    provider = _Provider(("정리했습니다.", ()))
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=_Toolbox(),
                             rules="규칙")

    adapter.complete(_request("부산항 신항 웅동지구 현장 손익 분석해줘"))

    assert "범위가" not in provider.calls[0]["messages"][1].content


def test_one_scope_repeated_is_refused_and_the_others_are_named():
    """**이것이 그 고장이다.** 한 범위를 계속 파면 나머지가 굶는다."""
    box = _Toolbox()
    provider = _Provider(
        ("", (_search("c1", "김해외동 2026.07 기성"),
              _search("c2", "김해외동 2026.07 기성 다시"))),
        ("정리했습니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    adapter.complete(_request(THREE_SITES_THREE_MONTHS))

    assert box.queries == ["김해외동 2026.07 기성"], "같은 범위를 두 번 돌렸다"
    results = provider.calls[1]["messages"][-1].content
    refused = [r["content"] for r in results if "범위는 검색" in r["content"]]
    assert refused and ("평택고덕" in refused[0] or "송산서측" in refused[0])


def test_the_answer_names_the_scopes_that_were_never_searched():
    """부분 결과를 **전체 완료로 말하지 않는다.**"""
    provider = _Provider(
        ("", (_search("c1", "김해외동 2026.07 기성"),)),
        ("김해외동 7월 기성은 3.2억입니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=_Toolbox(),
                             rules="규칙")

    text = adapter.complete(_request(THREE_SITES_THREE_MONTHS))

    assert "3.2억" in text, "찾은 것은 그대로 남는다"
    assert "※ 확인하지 못한 범위" in text
    assert "평택고덕" in text
    assert "자료가 없다는 뜻이 아닙니다" in text


def test_the_coverage_line_is_added_even_if_the_model_omits_it():
    """프롬프트로만 시키면 지켜질 때도 있고 아닐 때도 있다. **코드가 붙인다.**"""
    provider = _Provider(
        ("", (_search("c1", "김해외동 2026.07 기성"),)),
        ("김해외동만 정리했습니다.", ()),   # 모델이 범위 줄을 빠뜨렸다
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=_Toolbox(),
                             rules="규칙")

    text = adapter.complete(_request(THREE_SITES_THREE_MONTHS))

    assert text.startswith("김해외동만 정리했습니다.")
    assert text.count("※ 확인하지 못한 범위") == 1


def test_the_coverage_line_is_not_duplicated():
    note = sc.plan(THREE_SITES_THREE_MONTHS)
    note.spend("김해외동 2026.07")
    line = note.coverage_note()
    provider = _Provider(
        ("", (_search("c1", "김해외동 2026.07 기성"),)),
        (f"김해외동 7월 3.2억입니다.\n\n{line}", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=_Toolbox(),
                             rules="규칙")

    text = adapter.complete(_request(THREE_SITES_THREE_MONTHS))

    assert text.count("※ 확인하지 못한 범위") == 1


def test_an_empty_answer_does_not_become_a_coverage_only_answer():
    """안내만 남으면 **근거를 못 읽은 회차가 답을 낸 것처럼** 된다.

    그 판정은 라우터 몫이고, 여기서 빈 답을 문장으로 바꾸면 그 판정이 무력해진다.
    """
    provider = _Provider(
        ("", (_search("c1", "김해외동 2026.07 기성"),)),
        ("", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=_Toolbox(),
                             rules="규칙")

    assert adapter.complete(_request(THREE_SITES_THREE_MONTHS)).strip() == ""


def test_the_final_answer_keeps_its_share_of_the_tool_budget():
    """도구를 끝까지 긁어 쓰면 **답이 아예 안 나간다.**

    `ToolBudget` 이 소진 표시를 세우고, 그 상태의 빈 답은 라우터가
    `search-budget-exhausted` 로 닫는다.
    """
    budget = _Budget(max_calls=2)
    budget.calls = 1          # 남은 몫이 예약분과 같다
    box = _Toolbox(budget=budget)
    provider = _Provider(
        ("", (_search("c1", "김해외동 2026.07 기성"),)),
        ("읽은 것으로 정리했습니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    text = adapter.complete(_request(THREE_SITES_THREE_MONTHS))

    assert box.queries == [], "답변 몫까지 써 버렸다"
    assert budget.exhausted is False
    assert "읽은 것으로 정리했습니다." in text


def test_the_reserve_does_not_block_a_normal_question():
    """예약분을 넣었다고 멀쩡한 질문의 첫 검색까지 막으면 안 된다."""
    box = _Toolbox(budget=_Budget(max_calls=12))
    provider = _Provider(
        ("", (_search("c1", "김해외동 2026.07 기성"),)),
        ("정리했습니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    adapter.complete(_request(THREE_SITES_THREE_MONTHS))

    assert box.queries == ["김해외동 2026.07 기성"]


def test_a_multi_scope_question_gets_more_search_rounds():
    """횟수만 늘리고 라운드를 두면 **다음 기회가 없다.**"""
    provider = _Provider(("정리했습니다.", ()))
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=_Toolbox(),
                             rules="규칙")
    adapter.complete(_request(THREE_SITES_THREE_MONTHS))
    many, many_rounds = adapter._search_round_cap(), adapter._round_budget()

    adapter.complete(_request("부산항 신항 웅동지구 현장 손익"))
    one = adapter._search_round_cap()

    assert many > one
    assert many <= many_rounds - 1, "최종화 라운드를 남긴다"


@pytest.mark.parametrize("question", [
    THREE_SITES_THREE_MONTHS,
    "김해외동 현장, 평택고덕 현장 2026.09 비교",
    "2026.07, 2026.08 추이",
])
def test_the_briefing_never_invents_a_scope(question):
    """범위 이름은 **질문에 적힌 말**뿐이다."""
    plan = sc.plan(question)

    for scope in plan.scopes:
        if scope.site:
            assert scope.site in question
        if scope.period:
            assert scope.period.replace(" ", "") in question.replace(" ", "")


# --- 검수에서 나온 네 건 (2026-10-22) ------------------------------------------------
#
# 아래 시험들이 없을 때, 위의 시험은 **전부 초록이면서** 운영에서는 아무 일도
# 하지 않았다. 문서에 적어 둔 문장이 범위 하나로 읽혔기 때문이다.


# 설계 문서·모듈 docstring 에 적힌 **그 문장 그대로.** 풀어 쓴 문장으로만
# 시험하면 파서가 실제 말투를 못 읽는 것을 못 잡는다.
DOCUMENTED_PHRASING = "김해외동·평택고덕·송산서측 세 현장의 7·8·9월 기성을 비교해줘"


def test_the_documented_phrasing_is_nine_scopes():
    """직접 실행하면 9범위가 아니라 **1범위**였다."""
    plan = sc.plan(DOCUMENTED_PHRASING)

    assert len(plan.scopes) == 9
    labels = {s.label for s in plan.scopes}
    assert labels == {
        f"{site} {month}월"
        for site in ("김해외동", "평택고덕", "송산서측")
        for month in (7, 8, 9)
    }


def test_a_count_word_does_not_swallow_the_site_list():
    """「…송산서측 **세** 현장」 의 「세」 를 목록의 끝으로 읽으면 앞이 다 떨어진다."""
    for counted in ("세 현장", "3개 현장", "현장"):
        said = f"김해외동, 평택고덕, 송산서측 {counted} 기성"
        assert [s.site for s in sc.plan(said).scopes] == [
            "김해외동", "평택고덕", "송산서측",
        ], said
    # 수량사만 있는 말은 여전히 현장이 아니다.
    assert sc.plan("세 현장 상황 알려줘").scopes == ()


def test_a_two_digit_month_is_not_truncated():
    """`2026.10` 이 `2026.1` 로 잘리면 **질문에 없는 기간**이 범위 이름이 된다."""
    for text, label in (("2026.10", "2026.10"), ("2026.11", "2026.11"),
                        ("2026-12 기성", "2026-12"), ("2026년 10월", "2026년 10월")):
        assert [s.label for s in sc.plan(f"{text} 자료 줘").scopes] == [label]


def test_a_year_month_is_not_counted_twice():
    """연-월 패턴과 달-만 패턴에 **두 번** 걸리면 범위가 부풀고 예산이 쪼개진다."""
    plan = sc.plan("2026년 9월 기성 알려줘")

    assert [s.label for s in plan.scopes] == ["2026년 9월"]
    assert plan.multi is False
    assert plan.coverage_note() == ""


# --- 같은 기간의 다른 표기 -----------------------------------------------------------


@pytest.mark.parametrize("query", [
    "김해외동 2026-09 기성",
    "김해외동 2026.09 기성",
    "김해외동 2026/09 기성",
    "김해외동 9월 기성",
])
def test_the_same_period_written_differently_is_the_same_scope(query):
    """질문은 「2026년 9월」, 모델은 `2026-09` 로 찾는다.

    글자만 맞춰 보면 그 검색은 **어느 범위도 아닌 것**이 되고, 범위별 상한이
    안 걸리면서 그 범위는 찾았는데도 「확인하지 못한 범위」 에 남는다.
    """
    plan = sc.plan("김해외동 현장, 평택고덕 현장의 2026년 9월 기성 비교")
    hit = plan.scope_of(query)

    assert hit is not None and hit.label == "김해외동 2026년 9월"

    plan.spend(query)
    assert "김해외동 2026년 9월" not in plan.coverage_note()
    assert "평택고덕 2026년 9월" in plan.coverage_note()


def test_a_different_month_is_still_a_different_scope():
    """표기를 맞춰 본다고 **아무 기간이나 같아지면** 상한이 무의미하다."""
    plan = sc.plan("김해외동 현장의 2026년 9월, 2026년 10월 기성 비교")

    assert plan.scope_of("김해외동 2026-10").label == "김해외동 2026년 10월"
    assert plan.scope_of("김해외동 2025-09") is None, "다른 해를 같은 범위로 봤다"


def test_an_ambiguous_period_picks_no_scope():
    """여러 해의 같은 달이 동시에 걸리면 **막지도 세지도 않는다.**"""
    plan = sc.plan("김해외동 현장의 2025년 9월, 2026년 9월 기성 비교")

    assert plan.scope_of("김해외동 9월 기성") is None
    assert plan.refuse("김해외동 9월 기성") == ""


def test_a_month_list_sharing_one_suffix_is_expanded():
    plan = sc.plan("7·8·9월 추이")

    assert [s.label for s in plan.scopes] == ["7월", "8월", "9월"]


# --- 허용 횟수와 실행 횟수 -----------------------------------------------------------


def test_the_allowance_never_exceeds_what_the_toolbox_runs():
    """8회를 안내하고 6회에서 거절당하면, 남은 둘은 **찾을 기회가 없다.**"""
    plan = sc.plan(DOCUMENTED_PHRASING, limit=6)

    assert plan.total_searches == 6
    assert "검색은 모두 6회" in plan.briefing()

    box = _Toolbox(budget=_Budget(max_per_tool=6))
    adapter = ToolSpecialist("hermes", _router(_Provider(("끝.", ()))),
                             toolbox=box, rules="규칙")
    adapter.complete(_request(DOCUMENTED_PHRASING))

    assert adapter._search_call_cap() <= box.budget.max_per_tool


@pytest.mark.parametrize("status", ["refused", "reserved", "error", "truncated"])
def test_only_an_ok_run_marks_a_scope_as_covered(status):
    """`ok` 가 아닌 결과를 「본 범위」 로 적으면 **못 본 범위가 사라진다.**

    `truncated` 가 특히 그렇다. 글자는 돌아왔지만 도구상자가 그 호출의 좌표를
    **지운** 상태라, 봤다고 적으면 인용할 수 없는 것을 봤다고 적는 것이 된다.
    """
    box = _Toolbox(budget=_Budget(max_per_tool=6), statuses=["ok", status])
    provider = _Provider(
        ("", (_search("c1", "김해외동 7월 기성"), _search("c2", "평택고덕 8월 기성"))),
        ("김해외동 7월만 정리했습니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    text = adapter.complete(_request(DOCUMENTED_PHRASING))

    assert box.attempts == ["김해외동 7월 기성", "평택고덕 8월 기성"]
    assert box.queries == ["김해외동 7월 기성"]
    assert "평택고덕 8월" in text, f"{status} 인 범위가 확인한 범위로 둔갑했다"
    assert "김해외동 7월" not in text.split("※")[-1], "본 범위가 미확인에 남았다"


def test_attempted_searches_and_covered_scopes_are_counted_apart():
    """한 숫자에 담으면 **몇 범위를 실제로 봤나**를 로그에서도 알 수 없다."""
    box = _Toolbox(budget=_Budget(max_per_tool=6), statuses=["ok", "error", "ok"])
    provider = _Provider(
        ("", (_search("c1", "김해외동 7월 기성"),
              _search("c2", "평택고덕 8월 기성"),
              _search("c3", "송산서측 9월 기성"))),
        ("정리했습니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    adapter.complete(_request(DOCUMENTED_PHRASING))

    assert adapter.search_calls == 3, "시도는 시도대로 센다 — 상한에 들어간다"
    assert adapter.search_ok == 2, "근거가 된 것만 따로 센다"
    assert len(adapter.scope_plan.searched) == 2


def test_the_adapter_hands_the_toolbox_the_answers_char_reserve():
    """글자 몫은 **도구상자가** 지킨다. 안 넘기면 아무도 안 지킨다."""
    from tybot.specialist_adapters import FINAL_ANSWER_RESERVE_CHARS

    box = _Toolbox(budget=_Budget(max_per_tool=6))
    provider = _Provider(
        ("", (_search("c1", "김해외동 7월 기성"),)),
        ("정리했습니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    adapter.complete(_request(DOCUMENTED_PHRASING))

    assert box.reserve_chars == [FINAL_ANSWER_RESERVE_CHARS]


def test_a_toolbox_without_the_status_api_still_works():
    """구형·대역 도구상자는 `run` 으로 내려간다. 거기서도 거절은 미확인이다."""

    class _Old:
        touched = _Toolbox._Touched()

        def __init__(self):
            self.budget = _Budget(max_per_tool=6)
            self.queries: list[str] = []

        def run(self, name, args):
            self.queries.append(str((args or {}).get("query") or ""))
            if len(self.queries) > 1:
                self.budget.reason = "calls"      # 예산 소진으로 거절했다
                return "(검색 예산을 다 썼습니다.)"
            self.budget.calls += 1
            return "검색 결과 3건"

    box = _Old()
    provider = _Provider(
        ("", (_search("c1", "김해외동 7월 기성"), _search("c2", "평택고덕 8월 기성"))),
        ("김해외동 7월만 정리했습니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    text = adapter.complete(_request(DOCUMENTED_PHRASING))

    assert adapter.search_calls == 2
    assert adapter.search_ok == 1
    assert "평택고덕 8월" in text


# --- 최종 답변 예약은 세 축 전부 ------------------------------------------------------


def _read(call_id: str, name: str):
    return ToolCall(id=call_id, name="read_document", input={"name": name})


@pytest.mark.parametrize("field, value", [
    ("chars", 90_000 - 1),          # 글자 예산이 거의 끝났다
    ("tool_seconds", 90.0 - 1.0),   # 도구 시간이 거의 안 남았다
])
def test_no_new_tool_call_starts_when_a_budget_axis_is_nearly_gone(field, value):
    """호출 수만 보면 `read_document` 가 글자·시간 예산을 먼저 끝낸다.

    셋 중 **하나만** 닿아도 `ToolBudget` 은 소진 표시를 세우고, 그 상태의 빈
    답은 라우터가 `search-budget-exhausted` 로 닫는다 — 답이 아예 안 간다.

    **계약은 축마다 다르다.** 호출 수와 글자 수는 실제로 몫이 남는다(글자는
    도구상자가 `reserve_chars` 로 잘라 준다). 도구 시간은 **새 호출을 시작하지
    않는 것**뿐이고, 이미 시작한 호출의 길이는 여기서 못 막는다 — 호출별 시간
    제한이 없기 때문이다. 그래서 이 시험은 「10초가 남는다」 가 아니라
    「10초가 안 남으면 더 시작하지 않는다」 를 잰다.
    """
    budget = _Budget(max_calls=12)
    setattr(budget, field, value)
    box = _Toolbox(budget=budget)
    provider = _Provider(
        ("", (_read("c1", "기성 보고서"),)),
        ("읽은 것으로 정리했습니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    text = adapter.complete(_request(DOCUMENTED_PHRASING))

    assert box.queries == [], "답변 몫까지 써 버렸다"
    assert "읽은 것으로 정리했습니다." in text


def test_the_reserve_does_not_block_reading_when_there_is_room():
    box = _Toolbox(budget=_Budget(max_calls=12))
    provider = _Provider(
        ("", (_read("c1", "기성 보고서"),)),
        ("정리했습니다.", ()),
    )
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    adapter.complete(_request(DOCUMENTED_PHRASING))

    assert box.budget.calls == 1


# --- 라운드도 같이 늘어난다 ----------------------------------------------------------


def test_more_than_three_scopes_are_tried_one_search_per_round():
    """모델이 회차마다 검색 하나씩 내는 것이 **흔한 말투**다.

    라운드가 3이면 범위 아홉 중 셋만 시도되고 여섯이 통째로 미확인이 된다.
    """
    box = _Toolbox(budget=_Budget(max_per_tool=6))
    sites = ("김해외동", "평택고덕", "송산서측")
    provider = _Provider(*[
        ("", (_search(f"c{n}", f"{site} {month}월 기성"),))
        for n, (site, month) in enumerate(
            [(s, m) for s in sites for m in (7, 8, 9)][:6]
        )
    ], ("정리했습니다.", ()))
    adapter = ToolSpecialist("hermes", _router(provider), toolbox=box, rules="규칙")

    text = adapter.complete(_request(DOCUMENTED_PHRASING))

    assert len(box.queries) > 3, "라운드가 모자라 세 범위에서 멈췄다"
    assert len(box.queries) <= box.budget.max_per_tool
    # 끝까지 못 본 범위는 **그대로 남는다** — 라운드를 늘렸다고 덮지 않는다.
    assert "※ 확인하지 못한 범위" in text


def test_the_plan_carries_no_document_text():
    """범위 이름 말고는 아무것도 담지 않는다(원칙 5)."""
    plan = sc.plan(THREE_SITES_THREE_MONTHS)
    plan.spend("김해외동 2026.07 기성금 3억 2천만원")

    assert "3억" not in plan.coverage_note()
    assert "3억" not in plan.briefing()
    assert all("3억" not in label for label in plan.scope_used)
