"""도구를 갖춘 전문가 (A+) — 루프 (2026-09-11).

`PromptSpecialist` 는 마스터가 고른 근거로 한 번 답한다. Hermes 는 그렇게
동작하지 않는다 — 검색하고, 읽고, 모자라면 다시 검색한다. 그 루프가 그 봇의
값이고, 여기서 고정하는 것은 **그 루프가 끝나는가·비용이 묶이는가·짝이 맞는가** 다.
"""
from __future__ import annotations

from tybot import specialist_adapters as adapters
from tybot.gateway.base import LLMResponse, ToolCall


class FakeRouter:
    """정해진 순서로 응답을 돌려준다. 마지막 응답은 반복한다."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def complete(self, messages, *, model=None, sensitivity=None,
                 max_tokens=1024, tools=()):
        self.calls.append({"messages": list(messages), "tools": tuple(tools)})
        response = (
            self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        )
        # **도구를 안 준 호출에는 tool_use 가 올 수 없다.** 가짜가 그것을
        # 돌려주면 상한 뒤 마지막 호출을 검증할 수 없다.
        if not tools and response.tool_calls:
            return _answer("지금까지 읽은 것으로는 3.2억입니다.")
        return response


def _answer(text, cost=0.001):
    return LLMResponse(text=text, model="claude-opus-5", provider="anthropic",
                       input_tokens=10, output_tokens=5, cost_usd=cost)


def _wants(name, args, *, call_id="t1", cost=0.001):
    return LLMResponse(text="", model="claude-opus-5", provider="anthropic",
                       input_tokens=10, output_tokens=5, cost_usd=cost,
                       tool_calls=(ToolCall(id=call_id, name=name, input=args),),
                       stop_reason="tool_use")


class FakeBox:
    def __init__(self, reply="결과"):
        self.reply = reply
        self.ran: list[tuple[str, dict]] = []
        self.touched = type("T", (), {"documents": [], "live_permalinks": []})()

    def run(self, name, args):
        self.ran.append((name, args))
        return self.reply


class Request:
    question = "3공구 기성금은?"
    evidence = ()


def _specialist(router, box, **kw):
    return adapters.ToolSpecialist(
        "hermes", router, toolbox=box, rules="너는 기록 담당자다.", **kw
    )


# --- 루프 --------------------------------------------------------------------
def test_a_direct_answer_needs_no_tools():
    router = FakeRouter(_answer("3.2억입니다."))
    box = FakeBox()

    got = _specialist(router, box).complete(Request())

    assert got == "3.2억입니다."
    assert box.ran == []
    system = router.calls[0]["messages"][0].content
    assert "발언자와 날짜" in system
    assert "전문 봇 자신의 평가처럼" in system


def test_a_tool_call_is_executed_and_fed_back():
    router = FakeRouter(_wants("search", {"query": "기성금"}), _answer("3.2억입니다."))
    box = FakeBox(reply="[2026-09-01] 홍길동: 기성금 3.2억")

    got = _specialist(router, box).complete(Request())

    assert got == "3.2억입니다."
    assert box.ran == [("search", {"query": "기성금"})]


def test_the_tool_result_is_paired_with_its_call_id():
    """짝이 깨지면 다음 호출이 400 이다. 그 400 은 「전문가가 답을 못 한다」 로만 보인다."""
    router = FakeRouter(_wants("search", {"query": "x"}, call_id="toolu_9"),
                        _answer("답"))
    box = FakeBox()

    _specialist(router, box).complete(Request())

    second = router.calls[1]["messages"][-1]
    assert second.content[0]["tool_use_id"] == "toolu_9"


def test_the_assistant_turn_is_replayed_whole():
    """텍스트만 넣으면 tool_use 와 tool_result 의 짝이 깨진다."""
    router = FakeRouter(_wants("search", {"query": "x"}), _answer("답"))
    box = FakeBox()

    _specialist(router, box).complete(Request())

    replayed = router.calls[1]["messages"][-2]
    assert replayed.role == "assistant"
    assert any(b["type"] == "tool_use" for b in replayed.content)


def test_the_loop_is_bounded():
    """상한이 없으면 모델이 검색을 무한히 돈다 — 비용도 지연도 상한이 없어진다."""
    router = FakeRouter(_wants("search", {"query": "x"}))
    box = FakeBox()
    specialist = _specialist(router, box, max_rounds=3)

    specialist.complete(Request())

    assert specialist.rounds == 3
    assert len(box.ran) == 3


def test_hitting_the_limit_asks_without_tools():
    """상한 뒤에도 도구를 주면 또 부르고, 상한이 상한이 아니게 된다.

    도구 없이 한 번 더 물어 지금까지 읽은 것으로 답하게 한다. 그래도 비면
    빈 문자열이고, 계약 검사가 그것을 위반으로 보고 마스터가 답한다 —
    모자란 채로 억지 문장을 만드는 것보다 낫다.
    """
    router = FakeRouter(_wants("search", {"query": "x"}))
    box = FakeBox()
    specialist = _specialist(router, box, max_rounds=2)

    got = specialist.complete(Request())

    assert got  # 마지막 호출은 도구 없이 답을 받는다
    last = router.calls[-1]
    assert last["tools"] == (), "상한 뒤에도 도구를 주면 또 부른다"
    assert "더 찾지 말고" in last["messages"][-1].content


def test_the_cost_accumulates_across_rounds():
    """한 회차만 세면 도구를 많이 부른 질문이 싸 보인다."""
    router = FakeRouter(_wants("search", {"query": "x"}, cost=0.01),
                        _answer("답", cost=0.02))
    specialist = _specialist(router, FakeBox())

    specialist.complete(Request())

    assert abs(specialist.last_cost_usd - 0.03) < 1e-9


# --- 도구 노출 ----------------------------------------------------------------
def test_live_fetch_is_only_given_when_enabled():
    """도구를 안 주면 모델이 못 부른다. 프롬프트로 막는 것보다 확실하다."""
    router = FakeRouter(_answer("답"))
    _specialist(router, FakeBox(), live=False).complete(Request())
    without = {t.name for t in router.calls[0]["tools"]}

    router2 = FakeRouter(_answer("답"))
    _specialist(router2, FakeBox(), live=True).complete(Request())
    with_live = {t.name for t in router2.calls[0]["tools"]}

    assert "fetch_recent_slack" not in without
    assert "fetch_recent_slack" in with_live


def test_seed_evidence_is_offered_but_not_required():
    """마스터가 이미 고른 근거가 있으면 함께 준다. 없어도 돈다 — 스스로 찾는
    것이 이 어댑터의 전제다."""
    router = FakeRouter(_answer("답"))

    _specialist(router, FakeBox()).complete(Request())

    opening = router.calls[0]["messages"][-1].content
    assert "질문: 3공구 기성금은?" in opening
    assert "이미 찾아 둔 근거" not in opening


def test_the_specialist_exposes_what_it_read():
    """출처는 마스터가 붙인다. 그러려면 **무엇이 실제로 근거가 됐는지** 알아야
    하고, 그 유일한 통로가 `touched` 다. 모델이 본문에 적은 것을 믿고 출처를
    만들면 그게 곧 환각이다."""
    box = FakeBox()
    specialist = _specialist(FakeRouter(_answer("답")), box)

    assert specialist.touched is box.touched


# --- 실제로 연결됐는가 (2026-09-11) ------------------------------------------
#
# 도구도 어댑터도 만들었는데 **라우터가 안 쓰면** 아무 일도 안 일어난다.
# 그건 오류 없이 「전문가가 그냥 프롬프트로 돈다」 로만 보인다.
def test_the_factory_picks_the_tool_adapter():
    box = FakeBox()

    made = adapters.build(
        "hermes", FakeRouter(_answer("답")), rules="규칙",
        execution_mode="tools", toolbox=box,
    )

    assert isinstance(made, adapters.ToolSpecialist)


def test_the_factory_defaults_to_prompt():
    made = adapters.build("hermes", FakeRouter(_answer("답")), rules="규칙")

    assert isinstance(made, adapters.PromptSpecialist)


def test_tools_mode_without_a_toolbox_falls_back_to_prompt(caplog):
    """도구를 못 만든 사정 하나가 전문가를 통째로 끄면 안 된다 — 마스터가
    고른 근거로라도 답하는 편이 낫다."""
    with caplog.at_level("WARNING"):
        made = adapters.build(
            "hermes", FakeRouter(_answer("답")), rules="규칙",
            execution_mode="tools", toolbox=None,
        )

    assert isinstance(made, adapters.PromptSpecialist)
    assert "프롬프트로 내려간다" in caplog.text


def test_the_router_row_carries_the_execution_mode():
    """DB 열을 안 읽으면 도구형으로 등록해도 프롬프트로 돈다."""
    from tybot.specialist_router import Specialist

    row = Specialist(
        key="hermes", name="H", domain="d", routing_hint="", adapter="hermes",
        model="", min_confidence=0.6, rules="", execution_mode="tools",
    )

    assert row.execution_mode == "tools"


def test_the_query_reads_the_execution_mode():
    import inspect

    from tybot import specialist_router

    source = inspect.getsource(specialist_router.available)

    assert "execution_mode" in source, "쿼리가 실행 방식을 안 읽는다"


def test_the_hook_builds_a_toolbox_for_tool_mode():
    """`ctx` 를 묶은 새 묶음을 **요청마다** 만든다. 재사용하면 앞 요청의 권한으로
    읽게 된다."""
    import inspect

    from tybot.slack import pilot

    source = inspect.getsource(pilot.specialist_hook)

    assert "ToolBox(" in source
    assert "ctx=ctx" in source, "권한 컨텍스트를 안 묶는다"
    assert "execution_mode" in source, "도구형인지 보지 않는다"


def test_the_answer_cites_what_the_specialist_read():
    """도구형은 마스터가 고른 것과 다른 문서를 연다. 마스터 검색 결과로 출처를
    붙이면 답과 출처가 어긋난다."""
    from pathlib import Path
    from types import SimpleNamespace

    from tybot.answer import _specialist_citations

    doc = SimpleNamespace(
        workspace="tyit", channel="#팀-전산_ABB110-주간회의",
        path=Path("2026-09-01.md"),
    )
    special = SimpleNamespace(documents=(doc,), live_links=())
    ctx = SimpleNamespace(workspace="tyit")

    got = _specialist_citations(special, [], ctx)

    assert got == ["#팀-전산_ABB110-주간회의, 📄2026-09-01.md"]


def test_live_evidence_cites_slack_not_the_archive():
    """아카이브 문서로 붙이면 그 문서에는 아직 없는 내용이다."""
    from types import SimpleNamespace

    from tybot.answer import _specialist_citations

    special = SimpleNamespace(
        documents=(), live_links=("https://slack.com/archives/C1/p1",)
    )

    got = _specialist_citations(special, [], SimpleNamespace(workspace="tyit"))

    assert any("실시간" in c and "slack.com" in c for c in got)
