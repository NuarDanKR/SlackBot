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
def test_the_settings_refuse_a_budget_that_cannot_hold_its_stages():
    """합이 넘으면 recovery 가 시작도 못 하고 끝난다 — 있으나 마나 한 단계다."""
    with pytest.raises(ValueError):
        SpecialistDeadlines(total=30, primary=25, recovery=15, reserve=5)
    with pytest.raises(ValueError):
        SpecialistDeadlines(total=75, primary=0)


def test_remaining_never_goes_negative():
    clock = Clock()
    deadline = _deadline(clock)

    clock.tick(500)

    assert deadline.remaining() == 0.0
    assert deadline.remaining_primary() == 0.0
    assert deadline.call_timeout() == 0.0


def test_a_provider_never_gets_more_than_what_is_left():
    """§7.1 · Provider 가 받은 timeout 이 `remaining()` 보다 크지 않다."""
    clock = Clock()
    deadline = _deadline(clock, total=75, primary=55, per_call=25)

    assert deadline.call_timeout() == 25.0        # min(55, 25)
    clock.tick(45)
    assert deadline.call_timeout() == 10.0        # min(10, 25)
    assert deadline.call_timeout() <= deadline.remaining(reserve=False)


def test_the_reserve_is_kept_for_delivery():
    """전달 예약을 쓰면 답이 늦게 도착한다 — 남은 시간에서 뺀다."""
    clock = Clock()
    deadline = _deadline(clock, total=75, reserve=5)

    clock.tick(70)

    assert deadline.remaining() == 0.0
    assert deadline.remaining(reserve=False) == 5.0


def test_starting_a_stage_without_time_raises_and_aborts():
    clock = Clock()
    deadline = _deadline(clock)
    clock.tick(60)

    with pytest.raises(StageTimeout) as caught:
        deadline.require_time("primary")

    assert caught.value.stage == "primary"
    # **표식이 선다.** 스레드를 못 죽이니 어댑터가 보고 스스로 멈춘다.
    assert deadline.aborted is True


def test_an_aborted_request_refuses_every_later_stage():
    deadline = Deadline(clock=Clock())
    deadline.abort("primary")

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
            deadline.abort("primary")
            return "늦게 도착한 답변"

    result = execute(Adapter(), _request(deadline), fallback=lambda: "")

    assert result.result == "fallback"
    assert result.error_code == "specialist-timeout:primary"
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

    def complete(self, messages, **kw):
        self.calls.append(kw)
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
    deadline = _deadline(clock, total=75, primary=20, per_call=25)
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
    deadline = _deadline(clock, total=40, primary=20, per_call=25, recovery=15, reserve=5)
    router = FakeRouter(
        [_answer("", tools=[_call()]) for _ in range(4)], clock, seconds=18.0
    )
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")

    with pytest.raises(StageTimeout) as caught:
        adapter.complete(_request(deadline))

    # 전체 시간이 끝났다. **최종화도 시작하지 않는다** — primary 경계만 넘은 것과
    # 다르다(그건 전환이고, 이건 끝이다).
    assert caught.value.stage == "total"


def _call():
    from tybot.gateway.base import ToolCall

    return ToolCall(id="t1", name="search", input={"q": "가정산"})


def test_every_model_call_carries_the_remaining_time():
    clock = Clock()
    deadline = _deadline(clock, total=75, primary=55, per_call=25)
    router = FakeRouter([_answer("답")], clock)
    adapter = ToolSpecialist("hermes", router, toolbox=FakeToolbox(), rules="규칙")

    adapter.complete(_request(deadline))

    assert router.calls[0]["timeout_seconds"] == 25


def test_prompt_specialist_carries_the_remaining_time():
    from tybot.specialist_adapters import PromptSpecialist

    clock = Clock()
    deadline = _deadline(clock, total=75, primary=55, per_call=25)
    router = FakeRouter([_answer("답")], clock)
    adapter = PromptSpecialist("hermes", router, rules="규칙")

    adapter.complete(_request(deadline))

    assert router.calls[0]["timeout_seconds"] == 25


def test_editing_path_carries_the_remaining_time():
    clock = Clock()
    deadline = _deadline(clock, total=75, primary=55, per_call=25)
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

    assert router.calls[0]["timeout_seconds"] == 25


def test_tools_in_one_batch_recheck_the_clock():
    """앞 도구가 오래 걸리면 뒤 도구는 시작하지 않는다."""
    clock = Clock()
    deadline = _deadline(clock, total=75, primary=20)
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
