"""VIPBot 후속 질문: **좌표만 되살리고 넓히지 않는다** (B-83).

여기 쓰는 글은 전부 합성이다. 실제 질문·답변·채널을 넣지 않는다.

붙들고 있는 것은 넷이다.

1. 지칭이 없으면 **아무것도 안 한다** — 기존 `engine.answer()` 경로 그대로
2. 지칭이 있으면 좌표를 **지금 권한으로** 다시 연다
3. 권한 변경·삭제·좌표 불일치면 **못 열었다고** 끝낸다. 넓히지 않는다
4. 지난 답변 문장은 **계획 어디에도 없다**
"""

from __future__ import annotations

import logging
from dataclasses import fields

import pytest

from tybot.access import RequestContext
from tybot.evidence_refs import ARCHIVE_LINE, EvidenceRef
from tybot.vipbot_followup import FollowupPlan, plan_followup

WS = "pilot"
USER = "U0VIP0001"
CHANNEL = "C0SRC0001"
ANSWER = "지난 답변 본문 ZZZPRIORANSWERZZZ"


def ctx() -> RequestContext:
    return RequestContext(
        workspace=WS,
        user_id=USER,
        evidence_sources=frozenset({(WS, CHANNEL)}),
    )


def ref(channel=CHANNEL, line=3) -> EvidenceRef:
    return EvidenceRef(
        kind=ARCHIVE_LINE,
        workspace=WS,
        channel_id=channel,
        document_path=f"workspaces/{WS}/channels/{channel}/raw/2026-11-01.md",
        line_no=line,
    )


def turn(**changes) -> dict:
    row = {
        "record_id": "rec-0001",
        "ts": "2026-11-01T09:00:00+09:00",
        "question": "미수금 현황 알려줘",
        "intent_kind": "search",
        "subject_terms": ["미수금"],
        "evidence_refs": [ref()],
        "attachment_refs": [],
        "context_parent_ids": [],
    }
    row.update(changes)
    return row


class Hit:
    """`ArchiveStore.resolve_refs` 가 돌려주는 모양의 최소 대역."""

    def __init__(self, text="합성 원문 줄"):
        self.line = type("L", (), {"text": text, "speaker": "담당자"})()
        self.score = 1


class Resolver:
    """`ThreadFollowupResolver` 대역. 되살린 결과만 돌려준다."""

    def __init__(self, hits=(), dropped=(), parents=("rec-0001",)):
        self.calls: list[dict] = []
        self._hits = list(hits)
        self._dropped = list(dropped)
        self._parents = list(parents)

    def resolve(self, intent, context, *, turns, channel_id=""):
        self.calls.append(
            {
                "mode": intent.reference_mode,
                "topics": list(intent.topic_terms or []),
                "turns": len(turns),
                "channel_id": channel_id,
                "sources": getattr(context, "evidence_sources", None),
            }
        )
        return type(
            "R",
            (),
            {
                "evidence_hits": self._hits,
                "dropped_codes": self._dropped,
                "parent_record_ids": self._parents,
                "refs_requested": len(self._hits) + len(self._dropped),
            },
        )()


FOLLOWUP = "방금 그 문서 다시 정리해줘"
NEW_QUESTION = "이번 달 미수금 현황 알려줘"


# --- 1. 지칭이 없으면 아무것도 안 한다 ------------------------------------------


@pytest.mark.parametrize(
    "question",
    [NEW_QUESTION, "자금팀 공지 찾아줘", "오늘 일정 알려줘", ""],
)
def test_a_plain_question_leaves_the_existing_path_alone(question):
    resolver = Resolver(hits=[Hit()])
    plan = plan_followup(question, ctx(), history=[turn()], resolver=resolver)

    assert plan.reason == "not_followup"
    assert plan.applied is False
    assert plan.hits == ()
    # 해석기를 **부르지도 않는다** — 평범한 질문에 비용을 들이지 않는다.
    assert resolver.calls == []


def test_a_followup_without_history_is_treated_as_a_new_question():
    """이어 갈 기록이 없다. 지칭이 있었다는 이유로 답을 막지 않는다."""
    resolver = Resolver(hits=[Hit()])
    plan = plan_followup(FOLLOWUP, ctx(), history=[], resolver=resolver)

    assert plan.reason == "no_history"
    assert plan.applied is False
    assert resolver.calls == []


# --- 2. 좌표를 지금 권한으로 되살린다 -------------------------------------------


def test_a_followup_reopens_the_prior_coordinates():
    resolver = Resolver(hits=[Hit(), Hit()])
    plan = plan_followup(FOLLOWUP, ctx(), history=[turn()], resolver=resolver)

    assert plan.reason == "resolved"
    assert plan.applied is True
    assert len(plan.hits) == 2
    assert plan.parent_record_ids == ("rec-0001",)


def test_the_managed_source_set_is_what_reopens_them():
    """채널로 좁히지 않는다 — VIP 범위는 **관리형 근거 채널 집합**이다."""
    resolver = Resolver(hits=[Hit()])
    plan_followup(FOLLOWUP, ctx(), history=[turn()], resolver=resolver)

    call = resolver.calls[0]
    assert call["channel_id"] == ""
    assert call["sources"] == frozenset({(WS, CHANNEL)})


def test_the_asking_channel_never_narrows_the_vip_scope():
    """채널에서 물어도 범위는 **관리형 근거 채널 집합**이다.

    묻는 채널로 좁히면 다른 등록 채널의 좌표가 전부 떨어진다 — VIP 는 여러
    채널에 걸친 근거로 답하라고 만든 경로다. 지금 VIP 컨텍스트에는 채널이
    실리지 않지만, 실리는 날 이 시험이 먼저 깨진다.
    """
    asking = RequestContext(
        workspace=WS,
        user_id=USER,
        channel_id="C0ASKING1",
        channel="#묻는채널",
        evidence_sources=frozenset({(WS, CHANNEL), (WS, "C0OTHER1")}),
    )
    resolver = Resolver(hits=[Hit()])
    plan_followup(FOLLOWUP, asking, history=[turn()], resolver=resolver)

    assert resolver.calls[0]["channel_id"] == ""
    assert resolver.calls[0]["sources"] == frozenset({(WS, CHANNEL), (WS, "C0OTHER1")})


def test_every_history_turn_is_offered_to_the_resolver():
    resolver = Resolver(hits=[Hit()])
    rows = [turn(record_id="rec-0001"), turn(record_id="rec-0002")]
    plan_followup(FOLLOWUP, ctx(), history=rows, resolver=resolver)

    assert resolver.calls[0]["turns"] == 2


def test_a_topic_followup_carries_its_topic_terms():
    resolver = Resolver(hits=[Hit()])
    plan_followup("아까 미수금 자료 다시 정리해줘", ctx(), history=[turn()], resolver=resolver)

    call = resolver.calls[0]
    assert call["mode"] in {"prior_topic", "prior_turn"}
    assert call["topics"]


# --- 3. 못 열면 못 열었다고 끝낸다. 넓히지 않는다 --------------------------------


@pytest.mark.parametrize(
    "dropped",
    [
        ("permission_changed",),
        ("source_missing",),
        ("channel_scope",),
        ("bot_source_scope",),
        ("workspace_coordinate",),
        ("topic_no_match",),
        (),
    ],
)
def test_coordinates_that_no_longer_open_never_widen_the_search(dropped):
    """권한 변경·삭제·좌표 불일치. 셋 다 **못 열었다**로 끝난다."""
    resolver = Resolver(hits=[], dropped=dropped)
    plan = plan_followup(FOLLOWUP, ctx(), history=[turn()], resolver=resolver)

    assert plan.reason == "coordinates_unavailable"
    assert plan.applied is True  # 범위를 정하는 계획이다 — 다만 비어 있다
    assert plan.hits == ()
    assert plan.widened is False


def test_the_plan_cannot_claim_resolution_without_coordinates():
    with pytest.raises(ValueError, match="resolved requires"):
        FollowupPlan("resolved")
    with pytest.raises(ValueError, match="resolved requires"):
        FollowupPlan("not_followup", hits=(Hit(),))


def test_only_fixed_reasons_can_be_recorded():
    with pytest.raises(ValueError, match="unknown followup reason"):
        FollowupPlan("그럭저럭")


def test_a_plan_that_did_not_apply_never_claims_a_scope():
    for reason in ("not_followup", "no_history"):
        assert FollowupPlan(reason).applied is False


# --- 4. 지난 답변 문장은 어디에도 없다 ------------------------------------------


def test_the_prior_answer_text_never_enters_the_plan(caplog):
    """기록에 답변이 섞여 들어와도 계획은 **좌표만** 든다."""
    resolver = Resolver(hits=[Hit()])
    with caplog.at_level(logging.DEBUG):
        plan = plan_followup(
            FOLLOWUP,
            ctx(),
            history=[turn(answer=ANSWER, editing_text=ANSWER)],
            resolver=resolver,
        )

    blob = repr(plan) + "".join(str(getattr(plan, item.name)) for item in fields(plan))
    assert ANSWER not in blob
    assert ANSWER not in caplog.text


def test_the_plan_has_nowhere_to_put_an_answer_or_a_question():
    names = {item.name for item in fields(FollowupPlan)}
    assert names == {"reason", "hits", "parent_record_ids", "dropped"}
    for forbidden in ("answer", "question", "text", "prose", "editing"):
        assert not any(forbidden in name for name in names)


def test_the_log_line_carries_counts_and_codes_only(caplog):
    resolver = Resolver(hits=[Hit()], dropped=("permission_changed",))
    with caplog.at_level(logging.DEBUG):
        plan_followup(FOLLOWUP, ctx(), history=[turn()], resolver=resolver)

    assert "permission_changed" in caplog.text
    assert "미수금 현황 알려줘" not in caplog.text
    assert FOLLOWUP not in caplog.text


def test_the_module_never_reads_an_answer_field():
    import inspect

    import tybot.vipbot_followup as module

    source = inspect.getsource(module)
    for forbidden in ('"answer"', "'answer'", "editing_text", "to_slack"):
        assert forbidden not in source
