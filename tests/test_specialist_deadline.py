"""시간 예산·복구·빈 출력 진단(2026-09-16 장애).

인계: `docs/verification/2026-09-16-hermes-timeout-empty-output-handoff.md` §7

**실제로 재우지 않는다.** monotonic 시계를 주입해 결정적으로 돌린다 — 90초를
자는 테스트는 아무도 안 돌리게 되고, 안 돌리는 테스트는 없는 것과 같다.
"""
from __future__ import annotations

import pytest

from tybot.gateway.base import LLMResponse, ModelSpec, ProviderTimeout, Sensitivity
from tybot.specialist_adapters import ToolSpecialist
from tybot.specialist_contract import (
    AuthorizedEvidence,
    Deadline,
    SpecialistDeadlines,
    SpecialistRequest,
    StageTimeout,
    execute,
)


class Clock:
    """주입 시계. `tick()` 으로 시간을 옮긴다."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _deadline(clock: Clock, **kw) -> Deadline:
    return Deadline(settings=SpecialistDeadlines(**kw), clock=clock)


def _request(deadline=None) -> SpecialistRequest:
    return SpecialistRequest(
        question="현장 간 가정산 비교",
        evidence=(AuthorizedEvidence.from_acl_filter(
            workspace="tyit", text="권한을 확인한 원문", authorization_id="scope-1"
        ),),
        deadline=deadline,
    )


# --- §7.1 deadline -------------------------------------------------------------
def test_the_default_budget_is_180_seconds():
    """§7.1 · 기본 총 시간 180초. 단계 합이 정확히 맞아야 한다."""
    got = SpecialistDeadlines()

    assert got.total == 180.0
    assert got.discovery + got.finalize + got.recovery + got.delivery_reserve == got.total
    assert (got.discovery, got.finalize, got.recovery) == (100.0, 45.0, 20.0)


def test_the_settings_refuse_a_budget_whose_stages_do_not_add_up():
    """「대략 맞다」 로 두면 어느 단계가 모자란지 사고가 나야 안다."""
    with pytest.raises(ValueError):
        SpecialistDeadlines(total=180, discovery=100, finalize=45, recovery=20,
                            delivery_reserve=30)
    with pytest.raises(ValueError):
        SpecialistDeadlines(total=180, discovery=0)


def test_stage_boundaries_are_absolute_not_cumulative():
    """단계가 바뀔 때 45초를 **새로 더하지 않는다** — 그러면 시간이 늘어난다."""
    got = SpecialistDeadlines()

    assert got.discovery_ends == 100.0
    assert got.finalize_ends == 145.0
    assert got.recovery_ends == got.hard_ends == 165.0
    # 전달 예약 15초는 그 뒤다. 전문 봇이 쓰지 못한다.
    assert got.total - got.hard_ends == got.delivery_reserve


def test_remaining_never_goes_negative():
    clock = Clock()
    deadline = _deadline(clock)

    clock.tick(500)

    assert deadline.remaining() == 0.0
    assert deadline.remaining("discovery") == 0.0
    assert deadline.call_timeout() == 0.0


def test_a_provider_never_gets_more_than_what_is_left():
    """§7.1 · Provider timeout 은 단계 잔여와 `per_call` 중 작은 쪽이다."""
    clock = Clock()
    deadline = _deadline(clock)

    assert deadline.call_timeout() == 45.0        # min(100, 45)
    clock.tick(70)
    assert deadline.call_timeout() == 30.0        # min(30, 45)
    assert deadline.call_timeout() <= deadline.remaining("discovery")


def test_finalize_time_is_not_borrowed_by_discovery():
    """§7.1 · discovery 가 100초를 다 써도 finalize 45초가 남는다."""
    clock = Clock()
    deadline = _deadline(clock)

    clock.tick(100)

    assert deadline.remaining("discovery") == 0.0
    assert deadline.remaining("finalize") == 45.0
    assert deadline.may_finalize() is True


def test_recovery_gets_its_own_window_after_finalize():
    clock = Clock()
    deadline = _deadline(clock)

    clock.tick(145)

    assert deadline.remaining("finalize") == 0.0
    assert deadline.remaining("recovery") == 20.0
    assert deadline.may_recover() is True


def test_the_delivery_reserve_is_never_given_to_the_specialist():
    """§7.1 · 전달 예약 15초를 전문 봇이 쓰지 않는다."""
    clock = Clock()
    deadline = _deadline(clock)

    clock.tick(165)

    # 전문 봇에게 허용된 시간은 끝났지만, 전체 180초 중 15초가 전달용으로 남는다.
    assert deadline.remaining() == 0.0
    assert deadline.expired is True


def test_starting_a_stage_without_time_raises():
    clock = Clock()
    deadline = _deadline(clock)
    clock.tick(110)

    with pytest.raises(StageTimeout) as caught:
        deadline.require_time("discovery")

    assert caught.value.stage == "discovery"


def test_a_discovery_timeout_does_not_close_the_whole_request():
    """**`aborted` 하나로 복구를 막지 않는다**(인계 §3.3).

    탐색에서 시간이 끝난 것은 「그만 찾으라」 지 「요청 종료」 가 아니다.
    이미 읽은 것으로 마무리할 기회가 남아야 한다.
    """
    clock = Clock()
    deadline = _deadline(clock)
    clock.tick(110)
    with pytest.raises(StageTimeout):
        deadline.require_time("discovery")

    assert deadline.aborted is False
    assert deadline.may_finalize() is True
    assert deadline.may_recover() is True


def test_a_total_timeout_closes_everything():
    clock = Clock()
    deadline = _deadline(clock)
    clock.tick(200)

    with pytest.raises(StageTimeout):
        deadline.require_time("total")

    assert deadline.aborted is True
    assert deadline.may_finalize() is False
    assert deadline.may_recover() is False


def test_an_aborted_request_refuses_every_later_stage():
    deadline = Deadline(clock=Clock())
    deadline.abort("total")

    with pytest.raises(StageTimeout):
        deadline.require_time("recovery")


# --- §7.1 폴백 모델 ------------------------------------------------------------
class SlowProvider:
    name = "fake"

    def __init__(self, seconds: float, clock: Clock, *, fail: bool = False) -> None:
        self.seconds = seconds
        self.clock = clock
        self.fail = fail
        self.calls: list[float | None] = []

    def complete(self, spec, messages, **kw):
        self.calls.append(kw.get("timeout_seconds"))
        self.clock.tick(self.seconds)
        if self.fail:
            raise ProviderTimeout("느립니다")
        return LLMResponse(
            text="답", model=spec.model, provider=self.name,
            input_tokens=1, output_tokens=1, cost_usd=0.0,
        )


def _router(providers: dict, clock: Clock, models: list[str]):
    from tybot.gateway.cost import CostGuard
    from tybot.gateway.router import Router

    registry = {
        name: ModelSpec(
            model=name, provider="fake", input_price_per_mtok=0.0,
            output_price_per_mtok=0.0, max_sensitivity=Sensitivity.CONFIDENTIAL,
        )
        for name in models
    }
    return Router(
        registry=registry, providers=providers, cost_guard=CostGuard(daily_limit_usd=10),
        default_model=models[0], fallback_models=tuple(models[1:]),
    )


def test_a_fallback_model_is_not_started_after_the_time_is_gone(monkeypatch):
    """§7.1 · 첫 Provider 가 deadline 을 소진하면 폴백 호출은 0회다."""
    clock = Clock()
    monkeypatch.setattr("tybot.gateway.router.time.monotonic", clock)
    provider = SlowProvider(30.0, clock, fail=True)
    router = _router({"fake": provider}, clock, ["model-a", "model-b"])

    from tybot.gateway.base import Message

    with pytest.raises(ProviderTimeout):
        router.complete([Message("user", "질문")], timeout_seconds=25)

    # 첫 후보만 불렸다. 두 번째를 시작했으면 비용만 더 들고 답은 이미 늦었다.
    assert len(provider.calls) == 1
    assert provider.calls[0] == 25


def test_each_candidate_shares_one_clock(monkeypatch):
    """후보마다 상한을 새로 주면 한 번의 호출이 후보 수만큼 길어진다."""
    clock = Clock()
    monkeypatch.setattr("tybot.gateway.router.time.monotonic", clock)
    provider = SlowProvider(5.0, clock, fail=True)
    router = _router({"fake": provider}, clock, ["model-a", "model-b", "model-c"])

    from tybot.gateway.base import Message

    with pytest.raises(ProviderTimeout):
        router.complete([Message("user", "질문")], timeout_seconds=12)

    # 5초씩 줄어든다. 세 번째는 남은 2초로 시작하고, 네 번째는 없다.
    assert provider.calls == [12, 7, 2]


# --- §7.1 늦은 결과 ------------------------------------------------------------
def test_a_result_that_arrives_after_the_time_is_discarded():
    """§7.1 · timeout 뒤 늦게 도착한 결과를 게시하지 않는다."""
    clock = Clock()
    deadline = _deadline(clock)

    class Adapter:
        last_stop_reason = ""

        def complete(self, request):
            # 답을 만드는 사이에 바깥에서 시간이 끝났다.
            deadline.abort("total")
            return "늦게 도착한 답변"

    result = execute(Adapter(), _request(deadline), fallback=lambda: "")

    assert result.result == "fallback"
    assert result.error_code == "specialist-timeout:total"
    assert "늦게 도착한" not in result.text


def test_an_adapter_that_stops_itself_reports_its_stage():
    clock = Clock()
    deadline = _deadline(clock)

    class Adapter:
        last_stop_reason = ""

        def complete(self, request):
            raise StageTimeout("recovery")

    result = execute(Adapter(), _request(deadline), fallback=lambda: "")

    assert result.error_code == "specialist-timeout:recovery"


def test_a_provider_timeout_is_not_misreported_as_the_outer_deadline():
    """ProviderTimeout도 TimeoutError라 예외 순서를 틀리면 primary로 뭉개진다."""
    class Adapter:
        last_stop_reason = ""

        def complete(self, request):
            raise ProviderTimeout("SDK timeout")

    result = execute(Adapter(), _request(), fallback=lambda: "")

    assert result.error_code == "provider-timeout"


def test_an_outer_timeout_uses_the_adapter_phase(monkeypatch):
    """복구 대기 만료를 primary timeout으로 기록하지 않는다."""
    from concurrent.futures import TimeoutError as FutureTimeoutError

    class Future:
        def result(self, timeout):
            raise FutureTimeoutError

        def cancel(self):
            return True

        def add_done_callback(self, callback):
            # 실행되지 않은 future라 즉시 반납되는 상황을 흉내 낸다.
            callback(self)

    class Pool:
        def __init__(self, **kwargs):
            pass

        def submit(self, fn, request):
            return Future()

        def shutdown(self, **kwargs):
            pass

    class Adapter:
        phase = "recovery"
        last_stop_reason = ""

        def complete(self, request):  # pragma: no cover - 가짜 future가 실행하지 않는다
            return ""

    monkeypatch.setattr("tybot.specialist_contract.ThreadPoolExecutor", Pool)

    result = execute(Adapter(), _request(), fallback=lambda: "")

    assert result.error_code == "specialist-timeout:recovery"


def test_live_specialist_calls_are_capped():
    """취소할 수 없는 스레드를 요청마다 무제한 만들지 않는다(§2.2)."""
    import threading

    from tybot import specialist_contract as sc

    released = threading.Event()

    class Blocking:
        last_stop_reason = ""

        def complete(self, request):
            released.wait(5)
            return "늦은 답"

    # **남은 자리를 전부 잡는다.** 개수를 단정하지 않는다 — 앞 테스트의 늦은
    # 스레드가 아직 자리를 들고 있을 수 있고, 그건 이 테스트가 볼 일이 아니다.
    held = 0
    try:
        while sc._workers.acquire(blocking=False):
            held += 1
        result = execute(Blocking(), _request(), fallback=lambda: "마스터")
        assert result.error_code == "specialist-busy"
        # 자리가 없으면 **스레드를 만들지 않는다.** 만들면 상한이 의미가 없다.
        assert result.text == "마스터"
    finally:
        released.set()
        for _ in range(held):
            sc._workers.release()


# --- §7.3 빈 출력 진단 ---------------------------------------------------------
def test_an_empty_answer_at_the_token_limit_says_so():
    """§7.3 · text 없음 + `max_tokens` 는 `invalid-output:max-tokens` 다."""
    class Adapter:
        last_stop_reason = "max_tokens"

        def complete(self, request):
            return ""

    result = execute(Adapter(), _request(), fallback=lambda: "마스터")

    assert result.error_code == "invalid-output:max-tokens"


def test_an_empty_answer_for_another_reason_keeps_the_stop_reason():
    class Adapter:
        last_stop_reason = "end_turn"

        def complete(self, request):
            return ""

    result = execute(Adapter(), _request(), fallback=lambda: "마스터")

    assert result.error_code == "invalid-output:empty"


# --- §7.1 도구 루프 ------------------------------------------------------------
class FakeRouter:
    def __init__(self, responses: list, clock: Clock, seconds: float = 0.0) -> None:
        self.responses = list(responses)
        self.clock = clock
        self.seconds = seconds
        self.calls: list[dict] = []
        # 프롬프트도 남긴다 — seed-first 안내가 실제로 들어갔는지 봐야 한다.
        self.calls_messages: list = []

    def complete(self, messages, **kw):
        self.calls.append(kw)
        self.calls_messages.append(list(messages))
        self.clock.tick(self.seconds)
        return self.responses.pop(0) if self.responses else _answer("마지막 답")


def _answer(text: str, *, tools=(), stop: str = "end_turn") -> LLMResponse:
    return LLMResponse(
        text=text, model="m", provider="fake", input_tokens=10, output_tokens=5,
        cost_usd=0.01, tool_calls=tuple(tools), stop_reason=stop,
    )


class FakeToolbox:
    def __init__(self) -> None:
        from tybot.specialist_tools import ToolBudget, Touched

        self.budget = ToolBudget()
        self.touched = Touched()
        self.ran: list[str] = []

    def run(self, name, args):
        self.ran.append(name)
        return "도구 결과"


def test_no_new_tool_round_starts_after_the_primary_window_closes():
    """§7.1 · primary 경계를 넘으면 **새 검색을 시작하지 않는다.**

    라운드 상한(4회)에 닿기 전에도 시간으로 먼저 멈춘다 — 그게 이번 장애에서
    빠져 있던 것이다. 8라운드가 도는 동안 아무도 시계를 보지 않았다.
    """
    clock = Clock()
    deadline = _deadline(clock, total=60, discovery=20, finalize=20, recovery=10,
                         delivery_reserve=10, per_call=25)
    # 한 라운드가 15초. primary 20초 안에서 두 번째까지만 시작할 수 있다.
    router = FakeRouter(
        [_answer("", tools=[_call()]) for _ in range(4)], clock, seconds=15.0
    )
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")

    adapter.complete(_request(deadline))

    # 라운드 상한은 4 인데 **시간이 먼저** 멈춘다.
    assert adapter.rounds == 2
    # 마지막 호출은 최종화다 — 도구를 주지 않는다.
    assert "tools" not in router.calls[-1]


def test_the_whole_request_stops_when_the_deadline_is_gone():
    clock = Clock()
    # hard_ends 15초. 한 라운드가 18초라 **두 번째 라운드를 시작할 수 없다.**
    deadline = _deadline(clock, total=20, discovery=8, finalize=4, recovery=3,
                         delivery_reserve=5, per_call=25)
    router = FakeRouter(
        [_answer("", tools=[_call()]) for _ in range(4)], clock, seconds=18.0
    )
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")

    with pytest.raises(StageTimeout) as caught:
        adapter.complete(_request(deadline))

    # 전체 시간이 끝났다. **최종화도 시작하지 않는다** — primary 경계만 넘은 것과
    # 다르다(그건 전환이고, 이건 끝이다).
    assert caught.value.stage == "total"


def _call(name="request_search"):
    from tybot.gateway.base import ToolCall

    return ToolCall(id="t1", name=name, input={"q": "가정산"})


def test_every_model_call_carries_the_remaining_time():
    clock = Clock()
    deadline = _deadline(clock)
    router = FakeRouter([_answer("답")], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")

    adapter.complete(_request(deadline))

    assert router.calls[0]["timeout_seconds"] == 45


def test_prompt_specialist_carries_the_remaining_time():
    from tybot.specialist_adapters import PromptSpecialist

    clock = Clock()
    deadline = _deadline(clock)
    router = FakeRouter([_answer("답")], clock)
    adapter = PromptSpecialist("hermes", router, rules="규칙")

    adapter.complete(_request(deadline))

    assert router.calls[0]["timeout_seconds"] == 45


def test_editing_path_carries_the_remaining_time():
    clock = Clock()
    deadline = _deadline(clock)
    router = FakeRouter([_answer("편집 답")], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")
    base = _request(deadline)
    request = SpecialistRequest(
        question=base.question,
        evidence=base.evidence,
        editing_text="이전 답변",
        deadline=deadline,
    )

    adapter.complete(request)

    assert router.calls[0]["timeout_seconds"] == 45


def test_tools_in_one_batch_recheck_the_clock():
    """앞 도구가 오래 걸리면 뒤 도구는 시작하지 않는다."""
    clock = Clock()
    deadline = _deadline(clock, total=60, discovery=20, finalize=20, recovery=10,
                         delivery_reserve=10, per_call=25)
    toolbox = FakeToolbox()

    class Slow(FakeRouter):
        def complete(self, messages, **kw):
            self.calls.append(kw)
            if len(self.calls) == 1:
                return _answer("", tools=[_call(), _call()])
            return _answer("답")

    router = Slow([], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=toolbox, rules="규칙")
    original = toolbox.run

    def run(name, args):
        clock.tick(30)   # 첫 도구가 primary 를 다 쓴다
        return original(name, args)

    toolbox.run = run

    adapter.complete(_request(deadline))

    # 첫 도구가 primary 를 다 썼다. **두 번째 도구는 시작하지 않는다.**
    assert toolbox.ran == ["search"]
    # 그래도 끝내지는 않는다 — 읽은 것으로 최종화한다.
    assert "tools" not in router.calls[-1]


# --- §7.2 복구 -----------------------------------------------------------------
def test_recovery_asks_again_without_tools():
    """§7.2 · 복구 요청에는 tools 가 없다."""
    clock = Clock()
    deadline = _deadline(clock)
    # primary 응답(본문 없음) → 최종화 응답(여전히 없음) → 복구 응답.
    router = FakeRouter(
        [_answer("", stop="max_tokens"), _answer("", stop="max_tokens"),
         _answer("복구한 답")],
        clock,
    )
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")
    request = _request(deadline)

    adapter.complete(request)          # primary — 본문 없음
    text = adapter.recover(request)    # 복구 1회

    assert text == "복구한 답"
    assert "tools" not in router.calls[-1]
    assert adapter.phase == "recovery"


def test_recovery_reuses_the_transcript_and_searches_nothing_new():
    """§7.2 · 복구는 기존 ACL 근거만 쓰고 새 검색을 부르지 않는다."""
    clock = Clock()
    deadline = _deadline(clock)
    router = FakeRouter(
        [_answer("", stop="end_turn"), _answer("", stop="end_turn"), _answer("복구")],
        clock,
    )
    toolbox = FakeToolbox()
    adapter = ToolSpecialist("hermes", router, toolbox=toolbox, rules="규칙")
    request = _request(deadline)

    adapter.complete(request)
    adapter.recover(request)

    assert toolbox.ran == []
    # 대화 전체가 그대로 실린다 — 이미 권한을 통과한 것만 들어 있다.
    assert len(router.calls) == 3


def test_recovery_is_refused_when_no_time_is_left():
    clock = Clock()
    deadline = _deadline(clock)
    router = FakeRouter([_answer("")], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")
    request = _request(deadline)
    adapter.complete(request)
    clock.tick(200)

    with pytest.raises(StageTimeout):
        adapter.recover(request)


def test_recovery_without_a_transcript_is_refused():
    """실행된 적 없는 어댑터를 복구하면 근거 없이 문장을 만들게 된다."""
    from tybot.specialist_adapters import AdapterError

    adapter = ToolSpecialist("hermes", FakeRouter([], Clock()),
                             toolbox=FakeToolbox(), rules="규칙")

    with pytest.raises(AdapterError):
        adapter.recover(_request())


# --- §7.2 검색 축소 ------------------------------------------------------------
#
# 시간을 180초로 늘려도 같은 광범위 검색을 반복하면 p90 과 비용만 는다.
# 고치는 자리는 **시간이 아니라 범위**다.
def test_seed_evidence_comes_first_and_says_so():
    """§7.2 · seed 가 충분하면 검색 없이 답한다."""
    clock = Clock()
    router = FakeRouter([_answer("seed 로 답했습니다")], clock)
    toolbox = FakeToolbox()
    adapter = ToolSpecialist("hermes", router, toolbox=toolbox, rules="규칙")

    text = adapter.complete(_request(_deadline(clock)))

    assert text == "seed 로 답했습니다"
    assert toolbox.ran == []
    assert adapter.search_rounds == 0
    # **먼저 이것으로 답할 수 있는지 판단하라**는 말이 들어간다. 없으면 모델은
    # 도구 설명("무엇을 찾든 여기서 시작하라")을 따라 전체 검색부터 한다.
    opening = router.calls_messages[0][1].content
    assert "먼저 이것으로 답할 수 있는지" in opening
    assert "이미 찾아 둔 근거(1건)" in opening
    first_tools = {tool.name for tool in router.calls[0]["tools"]}
    assert "search" not in first_tools
    assert "request_search" in first_tools


def test_seed_search_request_is_translated_to_the_real_search_tool():
    """seed 평가 뒤 부족하다는 요청만 실제 archive 검색으로 바꾼다."""
    clock = Clock()
    router = FakeRouter([_answer("", tools=[_call()]), _answer("답")], clock)
    toolbox = FakeToolbox()
    adapter = ToolSpecialist("hermes", router, toolbox=toolbox, rules="규칙")

    adapter.complete(_request(_deadline(clock)))

    assert toolbox.ran == ["search"]
    assert adapter.search_calls == 1
    assert adapter.tool_calls_by_name == {"request_search": 1}


def test_seed_round_rejects_a_search_tool_that_was_not_offered():
    """모델이 도구 이름을 직접 만들어도 seed-first 게이트를 우회하지 못한다."""
    clock = Clock()
    router = FakeRouter(
        [_answer("", tools=[_call("search")]), _answer("답")], clock
    )
    toolbox = FakeToolbox()
    adapter = ToolSpecialist("hermes", router, toolbox=toolbox, rules="규칙")

    adapter.complete(_request(_deadline(clock)))

    assert toolbox.ran == []
    assert adapter.search_calls == 0


def test_no_seed_exposes_the_real_search_tool_immediately():
    """마스터가 후보를 못 찾은 질문은 첫 회차부터 제한된 검색을 허용한다."""
    clock = Clock()
    router = FakeRouter([_answer("답")], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")
    request = SpecialistRequest(
        question="가정산 비교",
        evidence=(),
        allow_empty_evidence=True,
        deadline=_deadline(clock),
    )

    adapter.complete(request)

    first_tools = {tool.name for tool in router.calls[0]["tools"]}
    assert "search" in first_tools
    assert "request_search" not in first_tools


def test_new_searches_stop_after_two_rounds():
    """§7.2 · seed 가 부족해도 추가 검색은 최대 2라운드다."""
    clock = Clock()
    router = FakeRouter(
        [
            _answer("", tools=[_call("request_search")]),
            *[_answer("", tools=[_call("search")]) for _ in range(3)],
        ],
        clock,
    )
    toolbox = FakeToolbox()
    adapter = ToolSpecialist("hermes", router, toolbox=toolbox, rules="규칙")

    adapter.complete(_request(_deadline(clock)))

    assert adapter.search_rounds == 2
    # 3번째 라운드부터는 검색 도구를 **주지 않는다.** 읽기는 계속 된다.
    third = router.calls[2]
    assert all(t.name != "search" for t in third.get("tools", ()))
    assert any(t.name == "read_document" for t in third.get("tools", ()))


def test_multiple_searches_in_one_model_turn_still_stop_at_two_calls():
    """한 응답에 search가 여러 개여도 실제 조회는 두 건만 실행한다."""
    from tybot.gateway.base import ToolCall
    from tybot.specialist_adapters import MAX_SEARCH_CALLS

    clock = Clock()
    calls = [
        ToolCall(str(i), "request_search", {"query": str(i)}) for i in range(5)
    ]
    router = FakeRouter(
        [_answer("", tools=calls), _answer("답")], clock
    )
    toolbox = FakeToolbox()
    adapter = ToolSpecialist("hermes", router, toolbox=toolbox, rules="규칙")

    adapter.complete(_request(_deadline(clock)))

    assert adapter.search_calls == MAX_SEARCH_CALLS == 2
    assert len(toolbox.ran) == 2


def test_the_same_file_from_two_paths_is_one_candidate():
    """§7.2 · 원본과 변환본이 한 후보로 전달된다.

    **어댑터를 지나서 본다.** 함수만 부르면 배선을 빼도 안 걸린다 — 실제로
    되돌리기 실험에서 그랬다.
    """
    clock = Clock()
    router = FakeRouter([_answer("답")], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")
    request = SpecialistRequest(
        question="외주비 얼마야",
        evidence=tuple(
            AuthorizedEvidence.from_acl_filter(
                workspace="tyit", text=text, authorization_id="scope-1"
            )
            for text in (
                "[첨부본문:손익.xlsx] 2026-07 외주비 1,200",
                "[첨부추출:손익.xlsx] 2026-07 외주비 1,200",
                "[캔버스본문:공정표] 착공 2026-03-01",
            )
        ),
        deadline=_deadline(clock),
    )

    adapter.complete(request)

    assert adapter.seed_count == 2
    opening = router.calls_messages[0][1].content
    assert "이미 찾아 둔 근거(2건)" in opening
    # 같은 파일이 두 벌 실리면 모델은 자료가 두 배라고 읽고 출처도 두 줄이 된다.
    assert opening.count("손익.xlsx") == 1


def test_same_filename_with_different_content_is_not_deduplicated():
    """파일명은 ID가 아니다. 월별·재업로드 문서를 이름만으로 합치지 않는다."""
    clock = Clock()
    router = FakeRouter([_answer("답")], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")
    request = SpecialistRequest(
        question="월별 비교",
        evidence=tuple(
            AuthorizedEvidence.from_acl_filter(
                workspace="tyit", text=text, authorization_id="scope-1"
            )
            for text in (
                "[첨부본문:손익.xlsx] 2026-07 외주비 1,200",
                "[첨부추출:손익.xlsx] 2026-08 외주비 1,500",
            )
        ),
        deadline=_deadline(clock),
    )

    adapter.complete(request)

    opening = router.calls_messages[0][1].content
    assert adapter.seed_count == 2
    assert "2026-07" in opening and "2026-08" in opening


def test_no_call_gets_the_old_8192_budget():
    """§4.3 · `max_tokens=8192` 를 모든 호출에 그대로 주지 않는다.

    도구 선택과 최종 답변을 **나누지는 않았다.** seed-first 를 넣은 뒤로 첫
    라운드가 곧 최종 답변일 수 있어, 도구 쪽만 낮추면 그 경로가 토큰 상한에서
    끊긴다 — 방금 고친 `invalid-output:max-tokens` 와 같은 모양이다.
    """
    from tybot.specialist_adapters import ANSWER_MAX_TOKENS, TOOL_MAX_TOKENS
    from tybot.specialist_contract import MAX_OUTPUT_CHARS

    clock = Clock()
    router = FakeRouter([_answer("", tools=[_call()]), _answer("답")], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")

    adapter.complete(_request(_deadline(clock)))

    assert all(call["max_tokens"] < 8192 for call in router.calls)
    # 본문 상한 3,000자를 담고 thinking 에 여유가 남아야 한다.
    assert ANSWER_MAX_TOKENS >= MAX_OUTPUT_CHARS
    assert TOOL_MAX_TOKENS == ANSWER_MAX_TOKENS


# --- §7.3 운영 회귀 fixture ----------------------------------------------------
#
# 2026-09-16 운영: 같은 비교 질문이 13:03 에 **77.3초로 성공**했고 14:23·14:31 에
# 74.5~76.2초로 `timeout` 이 났다. 75초 예산이 그 질문이 원래 쓰던 시간보다
# 짧았다 — 고친 것이 아니라 회귀였다.
#
# 개인정보·원문 없이 그 시간 패턴만 합성한다.
BUSAN_QUESTION = "부산항 신항 웅동지구 현장 외주업체 손익의 기간별 변화를 분석해 주세요."


def _busan_request(deadline):
    seeds = tuple(
        AuthorizedEvidence.from_acl_filter(
            workspace="tyit",
            text=f"[첨부본문:손익_{month}.xlsx] 2026-{month} 외주비 {month}00백만원",
            authorization_id="scope-1",
        )
        for month in ("05", "06", "07")
    )
    return SpecialistRequest(
        question=BUSAN_QUESTION, evidence=seeds, deadline=deadline
    )


def test_the_77_second_pattern_no_longer_times_out():
    """§7.3 · 이전 77초 처리 패턴에서 timeout 이 발생하지 않는다."""
    clock = Clock()
    deadline = _deadline(clock)
    # 탐색 2라운드(각 20초) + 도구 실행 + 최종 합성 25초 ≈ 77초.
    router = FakeRouter(
        [_answer("", tools=[_call()]), _answer("", tools=[_call()]),
         _answer("기간별 외주비는 5월 500, 6월 600, 7월 700백만원입니다.")],
        clock, seconds=20.0,
    )
    toolbox = FakeToolbox()
    original = toolbox.run

    def slow(name, args):
        clock.tick(8.5)   # 도구 실행도 시간을 쓴다
        return original(name, args)

    toolbox.run = slow
    adapter = ToolSpecialist("hermes", router, toolbox=toolbox, rules="규칙")

    text = adapter.complete(_busan_request(deadline))

    assert "5월 500" in text
    assert deadline.timeout_stage == ""
    assert deadline.elapsed < deadline.settings.total
    # 답변 주체는 Hermes 다 — 마스터가 대신 쓰지 않는다.
    assert adapter.key == "hermes"


def test_the_answer_stays_within_the_body_limit():
    """§7.3 · 답변은 3,000자 이하다. timeout 해결 명목으로 되돌리지 않는다."""
    from tybot.specialist_contract import MAX_OUTPUT_CHARS

    clock = Clock()
    long_answer = "가" * (MAX_OUTPUT_CHARS + 1)
    router = FakeRouter([_answer(long_answer)], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")

    result = execute(
        adapter, _busan_request(_deadline(clock)), fallback=lambda: "마스터"
    )

    assert result.error_code == "invalid-output:too-long"


def test_a_discovery_timeout_still_produces_an_answer_from_what_was_read():
    """탐색에서 시간이 끝나도 **이미 읽은 것으로** 마무리한다.

    예전에는 이 경우가 통째로 실패였다 — 근거 20건을 받아 놓고 답이 없었다.
    """
    clock = Clock()
    deadline = _deadline(clock)
    router = FakeRouter(
        [_answer("", tools=[_call()]), _answer("", tools=[_call()]),
         _answer("읽은 것으로 정리했습니다.")],
        clock, seconds=55.0,   # 두 라운드면 탐색 100초를 넘긴다
    )
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")

    text = adapter.complete(_busan_request(deadline))

    assert text == "읽은 것으로 정리했습니다."
    assert adapter.phase == "finalize"
    assert "tools" not in router.calls[-1]
