"""답변 불변식 회귀 하네스 — B-27a.

## 왜 골든셋과 따로 두는가

B-27 은 원래 "실제 질문 30건" 을 고정하는 항목이었다. 그런데 그것은 **실사용이
쌓여야** 만들 수 있고, 질문을 우리가 지어내면 우리 상상을 측정하는 골든셋이 된다.

그래서 갈랐다. 여기는 **자료가 0건이어도 검사할 수 있는 것**만 본다 — 답이 좋았는지가
아니라 **봇이 규칙을 지켰는지**다. 그것들은 배관이라 지어낸 질문으로도 잡힌다.

| 여기서 보는 것 | 여기서 보지 않는 것 |
|---|---|
| 출처가 붙었는가 (원칙 2) | 답이 정확한가 |
| 권한 밖 자료가 새지 않았는가 (원칙 3) | 요약이 잘 됐는가 |
| 근거가 없을 때 답을 지어내지 않는가 | 어느 표현이 더 나은가 |
| 모델이 죽어도 답이 나가는가 | |

## 이 파일이 하나로 묶인 이유

같은 불변식이 `test_answer.py`·`test_routing.py`·`test_multi_workspace.py` 에 흩어져
있었다. 흩어져 있으면 **하나가 지워져도 아무도 모른다** — 나머지가 통과하니 초록색이다.
여기서 한 번에 돌리고, 어느 원칙이 무너졌는지 실패 메시지가 말한다.

라우팅(B-36)을 넣을 때 지켜야 할 선이 바로 이것들이다. 라우팅이 깨졌을 때 잡아야 하는
것은 "답이 덜 좋아진 것" 이 아니라 **출처 누락·권한 유출·답이 아예 안 나가는 것**이다.
"""
from __future__ import annotations

import pytest

from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router

# 두 워크스페이스, 서로 안 보이는 채널 둘. 권한 유출은 이 경계를 넘는 것으로만 드러난다.
MINE = "#팀_전산(ABB155)_주간보고"
NOT_MINE = "#현장_김해외동(180182)_채팅방"

DOC_MINE = f"""---
workspace: pilot
channel: "{MINE}"
visibility: private
acl: ["{MINE}"]
doc_count: 1
last_ingested: 2026-08-19T17:00+09:00
---

## 요약 (사람이 관리, 봇은 수정 금지)
-

## 원문 (자동 취합, 편집 금지)
> [2026-08-12 09:15] 홍길동: 콘솔 배포 자동화를 끝냈습니다
"""

DOC_NOT_MINE = f"""---
workspace: pilot
channel: "{NOT_MINE}"
visibility: private
acl: ["{NOT_MINE}"]
doc_count: 1
last_ingested: 2026-08-19T17:00+09:00
---

## 요약 (사람이 관리, 봇은 수정 금지)
-

## 원문 (자동 취합, 편집 금지)
> [2026-08-12 09:15] 김철수: 기성금 3억 2천만원 청구 완료
"""


class Fake:
    """정상 프로바이더."""

    name = "anthropic"

    def __init__(self, text: str = "콘솔 배포 자동화를 끝냈습니다.") -> None:
        self.text = text
        self.calls: list[list[Message]] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        self.calls.append(list(messages))
        return LLMResponse(self.text, spec.model, self.name, 100, 20, 0.001)


class Dead:
    """모델이 죽은 프로바이더. 답변 경로가 이것으로도 끊기지 않아야 한다."""

    name = "anthropic"

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        raise RuntimeError("모델 장애")


def _make_engine(tmp_path, router):
    for name, body in (("전산.md", DOC_MINE), ("김해외동.md", DOC_NOT_MINE)):
        p = tmp_path / "channels" / "pilot" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return AnswerEngine(ArchiveStore(tmp_path), router)


def _engine(tmp_path, provider):
    router = Router(
        providers={"anthropic": provider},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    return _make_engine(tmp_path, router)


def _ctx(*channels: str) -> RequestContext:
    return RequestContext(workspace="pilot", channels=frozenset(channels))


# --- 원칙 2: 출처 강제 ------------------------------------------------------
def test_grounded_answer_always_carries_a_source(tmp_path):
    """출처 없는 답은 = 근거를 못 찾은 답이다. 그런데도 문장이 나가면 구별할 수 없다.

    **`to_slack()` 을 본다.** 출처는 거기서 붙는다(`answer.text` 는 모델 문장뿐이다).
    사용자가 받는 것을 검사해야 뜻이 있다.
    """
    engine = _engine(tmp_path, Fake())

    answer = engine.answer("콘솔 배포 어떻게 됐어", _ctx(MINE))

    assert answer.reason == "answered"
    assert "출처:" in answer.to_slack(), "근거로 답했는데 출처가 없다 (원칙 2)"
    assert answer.citations, "출처 줄만 있고 가리키는 문서가 없다"


# --- 원칙 3: 권한은 막는 쪽이 기본값 ----------------------------------------
@pytest.mark.parametrize(
    "leak",
    ["김해외동", "180182", "3억 2천", NOT_MINE],
    ids=["현장명", "현장코드", "금액", "채널명"],
)
def test_answer_never_leaks_a_channel_the_asker_cannot_see(tmp_path, leak):
    """권한 밖 자료는 **어느 조각도** 나가면 안 된다.

    채널명만 막는 것으로는 부족하다. 금액과 현장 코드가 새는 것도 같은 유출이고,
    그쪽이 오히려 눈에 안 띈다.
    """
    engine = _engine(tmp_path, Fake())

    answer = engine.answer("기성금 얼마야", _ctx(MINE))

    assert leak not in answer.to_slack(), f"권한 밖 자료가 샜다: {leak} (원칙 3)"


def test_asking_with_no_channels_answers_nothing(tmp_path):
    """채널이 하나도 없는 사람에게는 아카이브 내용이 나가지 않는다."""
    engine = _engine(tmp_path, Fake())

    answer = engine.answer("기성금 얼마야", _ctx())

    for leak in ("3억 2천", "김해외동", "콘솔 배포"):
        assert leak not in answer.to_slack()


# --- 근거 없으면 답하지 않는다 ----------------------------------------------
def test_zero_hits_does_not_invent_an_answer(tmp_path):
    """모델이 뭐라도 말하게 되어 있어도, 근거가 0건이면 그 문장을 쓰지 않는다."""
    engine = _engine(tmp_path, Fake("아마 5억쯤 될 것 같습니다."))

    answer = engine.answer("아무데도 없는 이야기 알려줘", _ctx(MINE))

    assert "5억" not in answer.to_slack(), "근거 없이 모델 문장을 그대로 내보냈다"


# --- 모델 장애 ------------------------------------------------------------
def test_a_dead_provider_falls_back_to_the_next_model(tmp_path):
    """폴백이 없던 동안 모델 장애 하나가 곧 답변 실패였다.

    2026-09-02 헬스 체크에서 오류로 끝난 질문이 22% 였고, 이 경로가 그 몫이다.
    라우팅(B-36)이 앞에 서면 실패 지점이 하나 더 늘어나므로 여기서 받친다.
    """
    dead, alive = Dead(), Fake()
    router = Router(
        providers={"anthropic": dead, "openai": alive},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            ),
            "gpt-backup": ModelSpec(
                "gpt-backup", "openai", 2.0, 10.0, Sensitivity.CONFIDENTIAL
            ),
        },
        cost_guard=CostGuard(10.0),
        fallback_models=["gpt-backup"],
    )
    engine = _make_engine(tmp_path, router)

    answer = engine.answer("콘솔 배포 어떻게 됐어", _ctx(MINE))

    assert answer.reason == "answered"
    assert "출처:" in answer.to_slack(), "폴백으로 답할 때도 출처는 붙는다"


def test_fallback_never_loosens_the_sensitivity_rule(tmp_path):
    """장애 때만 조건이 느슨해지면, 가장 급할 때 기밀이 허용 밖 모델로 나간다."""
    router = Router(
        providers={"anthropic": Dead(), "openai": Fake()},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            ),
            # 내부용까지만 허용된 모델. 기밀 요청의 폴백이 되면 안 된다.
            "cheap-internal": ModelSpec(
                "cheap-internal", "openai", 1.0, 5.0, Sensitivity.INTERNAL
            ),
        },
        cost_guard=CostGuard(10.0),
        fallback_models=["cheap-internal"],
    )

    with pytest.raises(RuntimeError):
        router.complete(
            [Message("user", "기밀 질문")],
            model="claude-sonnet-5",
            sensitivity=Sensitivity.CONFIDENTIAL,
        )


def test_without_a_configured_fallback_the_error_reaches_the_caller(tmp_path):
    """이것은 결함이 아니라 계약이다 — 삼켜 버리면 안 된다.

    호출부(`slack/pilot.py`)가 예외를 받아 사용자에게 안내를 보내고 `qa_log` 에
    예외 클래스명을 남긴다. 여기서 조용히 빈 답을 돌려주면 **오류율이 0% 로 보이고**
    무엇이 실패했는지 알 자리가 사라진다.
    """
    engine = _engine(tmp_path, Dead())

    with pytest.raises(RuntimeError):
        engine.answer("콘솔 배포 어떻게 됐어", _ctx(MINE))


# --- 하네스가 실제로 무언가를 지키고 있는지 --------------------------------
def test_the_source_check_is_caused_by_citations_not_by_boilerplate(tmp_path):
    """검사가 통과하는 것과 검사가 **작동하는** 것은 다르다.

    출처 줄이 근거와 무관하게 항상 붙는다면 위의 검사는 아무것도 지키지 않는다.
    근거를 뺐을 때 출처도 사라지는지 봐서, 통과가 실제로 근거 때문임을 확인한다.
    """
    engine = _engine(tmp_path, Fake())
    grounded = engine.answer("콘솔 배포 어떻게 됐어", _ctx(MINE))
    ungrounded = engine.answer("아무데도 없는 이야기 알려줘", _ctx(MINE))

    assert "출처:" in grounded.to_slack()
    assert "출처:" not in ungrounded.to_slack(), (
        "근거가 없는데도 출처 줄이 붙는다 — 위의 검사는 아무것도 지키지 않는다"
    )


# --- 전문가가 답할 때도 선은 그대로 --------------------------------------
class _Special:
    """전문가 훅 흉내. 엔진은 전문가를 모르고 결과만 받는다."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.seen: list[tuple[str, str]] = []

    def __call__(self, question, ctx, evidence):
        from tybot.specialist_router import SpecialistAnswer

        self.seen.append((question, evidence))
        if not self.text:
            return None
        return SpecialistAnswer(self.text, "hermes", "claude-haiku-4-5", 0.0002)


def _engine_with(tmp_path, hook):
    router = Router(
        providers={"anthropic": Fake()},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    for name, body in (("전산.md", DOC_MINE), ("김해외동.md", DOC_NOT_MINE)):
        p = tmp_path / "channels" / "pilot" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    return AnswerEngine(ArchiveStore(tmp_path), router, specialist=hook)


def test_a_specialist_answer_still_carries_our_sources(tmp_path):
    """전문가는 문장만 준다. 출처는 마스터가 붙인다(원칙 2)."""
    hook = _Special("콘솔 배포는 끝났습니다.")
    engine = _engine_with(tmp_path, hook)

    answer = engine.answer("콘솔 배포 어떻게 됐어", _ctx(MINE))

    assert answer.text == "콘솔 배포는 끝났습니다."
    assert "출처:" in answer.to_slack(), "전문가가 답해도 출처는 붙는다"
    assert answer.reason == "answered"


def test_the_specialist_only_sees_permitted_evidence(tmp_path):
    """훅에 넘어가는 근거는 이미 `visible_docs` 를 통과한 것뿐이다(원칙 3)."""
    hook = _Special("답")
    engine = _engine_with(tmp_path, hook)

    # 근거가 잡히는 질문을 쓴다. 0건이면 전문가를 아예 부르지 않으므로
    # (아래 테스트가 그것을 고정한다) 여기서는 새는지를 볼 수 없다.
    engine.answer("콘솔 배포 어떻게 됐어", _ctx(MINE))

    assert hook.seen, "훅이 불리지 않았다 — 배선이 끊겼다"
    _, evidence = hook.seen[0]
    for leak in ("김해외동", "3억 2천", "180182"):
        assert leak not in evidence, f"권한 밖 근거가 전문가에게 갔다: {leak}"


def test_a_silent_specialist_falls_back_to_the_master(tmp_path):
    """전문가가 못 답하면 마스터가 답한다. 답이 아예 안 나가면 안 된다."""
    engine = _engine_with(tmp_path, _Special(""))

    answer = engine.answer("콘솔 배포 어떻게 됐어", _ctx(MINE))

    assert answer.text.strip()
    assert "출처:" in answer.to_slack()


def test_no_evidence_means_the_specialist_is_never_asked(tmp_path):
    """근거가 0건이면 전문가에게도 묻지 않는다.

    물으면 전문가가 근거 없이 문장을 만들고, 거기에 우리 출처가 붙는다.
    """
    hook = _Special("아마 5억쯤 됩니다.")
    engine = _engine_with(tmp_path, hook)

    answer = engine.answer("아무데도 없는 이야기 알려줘", _ctx(MINE))

    assert hook.seen == [], "근거 0건인데 전문가를 불렀다"
    assert "5억" not in answer.to_slack()


# --- 요약 경로에도 전문가가 닿아야 한다 -------------------------------------
def test_the_summary_path_also_asks_the_specialist(tmp_path):
    """Hermes 의 본업이 회의록·업무 진행 요약이다.

    그런데 요약 경로에만 훅이 없어서, 등록해도 전문가가 요약 질문을 받지 못했다
    (2026-09-07 실제 발생 — 근거 152줄을 쥐고도 마스터가 "판단할 수 없다"고 답했다).
    """
    hook = _Special("전산팀 진행 상황을 정리했습니다.")
    engine = _engine_with(tmp_path, hook)

    answer = engine.summarize(_ctx(MINE), days=3650)

    assert hook.seen, "요약 경로가 전문가를 부르지 않았다"
    assert answer.text == "전산팀 진행 상황을 정리했습니다."
    assert "출처:" in answer.to_slack(), "요약도 출처는 마스터가 붙인다"


def test_the_summary_specialist_only_sees_permitted_docs(tmp_path):
    """요약은 여러 채널을 묶는다. 그만큼 권한이 새기 쉬운 자리다."""
    hook = _Special("정리")
    engine = _engine_with(tmp_path, hook)

    engine.summarize(_ctx(MINE), days=3650)

    _, evidence = hook.seen[0]
    for leak in ("김해외동", "3억 2천", "180182"):
        assert leak not in evidence, f"권한 밖 자료가 전문가에게 갔다: {leak}"


def test_the_summary_prompt_does_not_refuse_subjective_asks():
    """「중요한 내용을 알려줘」 에 "판단할 수 없다"고 답하던 것.

    그 규칙은 정당한 아카이브 질문을 거부하는 탈출구가 됐다 — 무엇이 중요한지는
    읽는 사람이 정하고, 봇은 원문에 있는 것을 정리해 보이면 된다.
    """
    from tybot.answer import SUMMARY_PROMPT

    assert "주관적인 요청도 원문으로 답한다" in SUMMARY_PROMPT
    assert "아카이브 원문으로 답할 수 있는 질문이 아닙니다" not in SUMMARY_PROMPT
