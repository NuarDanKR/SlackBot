"""도구를 갖춘 전문가 (A+) — 루프 (2026-09-11).

`PromptSpecialist` 는 마스터가 고른 근거로 한 번 답한다. Hermes 는 그렇게
동작하지 않는다 — 검색하고, 읽고, 모자라면 다시 검색한다. 그 루프가 그 봇의
값이고, 여기서 고정하는 것은 **그 루프가 끝나는가·비용이 묶이는가·짝이 맞는가** 다.
"""
from __future__ import annotations

import pytest

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


def test_http_mode_is_refused_instead_of_silently_becoming_prompt():
    with pytest.raises(adapters.AdapterError, match="unsupported-execution-mode"):
        adapters.build("hermes", FakeRouter(_answer("답")), execution_mode="http")


def test_search_instructions_allow_bounded_synonym_retry():
    from tybot.specialist_tools import SEARCH_DESCRIPTION

    assert "동의어" in SEARCH_DESCRIPTION
    assert "낱말을 바꿔 다시 부르지" not in SEARCH_DESCRIPTION


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


def test_tools_mode_without_a_toolbox_is_refused():
    """**선언과 실제가 갈릴 바에는 부르지 않는다**(2026-09-14).

    예전에는 조용히 프롬프트로 내려갔다. 그래서 DB·계약·콘솔이 모두 `tools` 라고
    말하는데 실제로 도는 것은 `prompt` 인 상태가 몇 주 동안 이어졌고, "왜 답이
    부실하지" 를 되짚을 단서가 경고 한 줄뿐이었다.
    """
    with pytest.raises(adapters.AdapterError, match="toolbox-unavailable"):
        adapters.build(
            "hermes", FakeRouter(_answer("답")), rules="규칙",
            execution_mode="tools", toolbox=None,
        )


def test_the_router_row_carries_the_execution_mode():
    """DB 열을 안 읽으면 도구형으로 등록해도 프롬프트로 돈다."""
    from tybot.specialist_router import Specialist

    row = Specialist(
        key="hermes", name="H", domain="d", routing_hint="", adapter="hermes",
        model="", min_confidence=0.6, rules="", execution_mode="tools",
    )

    assert row.execution_mode == "tools"


class _FakeCursor:
    """DB 행 하나를 돌려주는 최소 커서."""

    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        return None

    def fetchall(self):
        return self._rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeConn:
    def __init__(self, rows):
        self._rows = rows

    def cursor(self):
        return _FakeCursor(self._rows)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("mode", ["tools", "http", "prompt"])
def test_a_db_row_keeps_its_execution_mode_all_the_way_to_the_object(monkeypatch, mode):
    """**DB 행 → 런타임 객체**를 실제로 통과시킨다(설계 §9.4).

    이 검사가 없어서 고장이 몇 주 동안 조용했다. SQL 에 열 이름이 있는지만 보는
    검사와 `Specialist(...)` 를 손으로 만드는 검사는 둘 다 통과했는데, 정작 그
    사이의 **행 매핑**이 빠져 있었다. 검사가 실제 경로를 지나지 않으면 그 검사는
    통과해도 아무것도 보장하지 않는다.
    """
    from tybot import specialist_router

    row = {
        "key": "hermes", "name": "Hermes", "domain": "내부 기록",
        "routing_hint": "", "adapter": "hermes", "model": "",
        "min_confidence": 0.5, "rules": "", "execution_mode": mode,
    }
    monkeypatch.setenv("DATABASE_URL", "postgresql://fake/none")
    monkeypatch.setattr("psycopg.connect", lambda *a, **k: _FakeConn([row]))
    specialist_router.clear_cache()
    try:
        (got,) = specialist_router.available("pilot")
    finally:
        specialist_router.clear_cache()

    assert got.execution_mode == mode, "DB 가 말한 실행 방식이 런타임에서 사라졌다"


def _registry_row(**kw):
    from tybot.specialist_router import Specialist

    base = dict(
        key="hermes", name="H", domain="내부 기록", routing_hint="",
        adapter="hermes", model="", min_confidence=0.5, rules="",
        execution_mode="tools",
    )
    base.update(kw)
    return Specialist(**base)


def _task(**kw):
    from types import SimpleNamespace

    base = dict(
        required_capability="internal_document_qa",
        suggested_specialist="",
        routing_confidence=0.9,
        question="회의록 정리해줘",
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_a_tools_row_actually_receives_a_toolbox(monkeypatch):
    """DB 행이 `tools` 면 런타임에 **도구 묶음이 실제로 전달된다.**

    소스에 낱말이 있는지 보는 검사로는 이 고장을 못 잡는다 — 실제로 그랬다.
    SQL 은 열을 읽는데 생성자에 안 넘겨서, 모든 전문 봇이 `prompt` 로 돌았다.
    """
    from tybot import specialist_adapters, specialist_router

    seen = {}

    class FakeAdapter:
        touched = None
        last_model = "m"
        last_cost_usd = 0.0

        def complete(self, request):
            return "정리했습니다."

    def fake_build(key, router, *, model="", rules="", execution_mode="prompt",
                   toolbox=None, live=False):
        seen["mode"] = execution_mode
        seen["toolbox"] = toolbox
        return FakeAdapter()

    monkeypatch.setattr(specialist_adapters, "build", fake_build)
    monkeypatch.setattr(specialist_router, "available", lambda ws: [_registry_row()])
    monkeypatch.setattr(
        specialist_router, "capabilities_of", lambda s: ("internal_document_qa",)
    )
    made = []
    outcome = specialist_router.serve(
        _task(),
        workspace="pilot",
        evidence=["원문"],
        router=None,
        authorization_id="pilot:member",
        toolbox_factory=lambda: made.append(1) or "TOOLBOX",
        record_call_row=False,
    )

    assert outcome.ok
    assert seen["mode"] == "tools"
    assert seen["toolbox"] == "TOOLBOX"
    assert made == [1], "요청마다 새 묶음을 만들어야 한다"


def test_a_prompt_row_gets_no_toolbox(monkeypatch):
    """프롬프트형에게 도구를 주면 계약 밖의 능력을 쥐여 주는 것이다."""
    from tybot import specialist_adapters, specialist_router

    seen = {}

    class FakeAdapter:
        touched = None
        last_model = "m"
        last_cost_usd = 0.0

        def complete(self, request):
            return "정리했습니다."

    monkeypatch.setattr(
        specialist_adapters, "build",
        lambda key, router, **kw: (seen.update(kw), FakeAdapter())[1],
    )
    monkeypatch.setattr(
        specialist_router, "available", lambda ws: [_registry_row(execution_mode="prompt")]
    )
    monkeypatch.setattr(
        specialist_router, "capabilities_of", lambda s: ("internal_document_qa",)
    )

    specialist_router.serve(
        _task(),
        workspace="pilot",
        evidence=["원문"],
        router=None,
        authorization_id="pilot:member",
        toolbox_factory=lambda: "TOOLBOX",
        record_call_row=False,
    )

    assert seen["toolbox"] is None


def test_the_hook_binds_the_request_context_into_the_toolbox():
    """`ctx` 를 묶은 새 묶음을 **요청마다** 만든다. 재사용하면 앞 요청의 권한으로
    읽게 된다."""
    from types import SimpleNamespace

    from tybot.slack import pilot

    captured = {}

    def fake_serve(task, **kw):
        captured["factory"] = kw["toolbox_factory"]
        from tybot.specialist_router import UNAVAILABLE, SpecialistOutcome

        return SpecialistOutcome(UNAVAILABLE, error_code="테스트")

    store = object()
    ctx = SimpleNamespace(workspace="pilot", role="member", channel="#팀-전산_ABB110-회의")
    hook = pilot.specialist_hook(router=None, store=store)
    import tybot.specialist_router as sr

    original, sr.serve = sr.serve, fake_serve
    try:
        hook("회의록 정리해줘", ctx, "원문")
    finally:
        sr.serve = original

    box = captured["factory"]()
    assert box.ctx is ctx, "권한 컨텍스트를 안 묶었다"
    assert box.store is store


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
