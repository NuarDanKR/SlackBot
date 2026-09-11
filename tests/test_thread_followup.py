"""스레드 후속 질문 — 답변이 아니라 **근거를 이어 간다**.

설계: `docs/design/thread-follow-up-evidence.md`

이 파일이 지키는 것 하나: 후속 질문의 답은 **이번 요청에서 다시 열어 읽은 원문**
에서만 나온다. 이전 봇 답변 문장을 바꿔치기해도 숫자가 달라지지 않아야 하고,
권한이 바뀌었으면 지난번에 보였던 자료라도 나오지 않아야 한다.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from tybot import attachment_review
from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive import writer
from tybot.archive.store import ArchiveStore
from tybot.audit import QALog, QARecord
from tybot.evidence_refs import (
    AttachmentRef,
    EvidenceRef,
    attachment_refs_to_json,
    content_hash,
    refs_to_json,
)
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.intent import Intent, apply_followup, plan_by_rule
from tybot.thread_followup import ThreadFollowupResolver

GJ = "#현장-광주도시철도(180901)-정산"
GJ_ID = "C0GJ"
FUND = "#팀-자금(ABB540)-주간보고"
FUND_ID = "C0FUND"


# --- 픽스처 ------------------------------------------------------------------


class FakeProvider:
    name = "anthropic"

    def __init__(self) -> None:
        self.calls: list[list[Message]] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0, tools=None):
        self.calls.append(list(messages))
        return LLMResponse("정리했습니다.", spec.model, self.name, 10, 5, 0.0)

    @property
    def last_prompt(self) -> str:
        got = self.calls[-1][-1].content
        return got if isinstance(got, str) else json.dumps(got, ensure_ascii=False)


def _msg(day: int, hour: int, speaker: str, text: str) -> writer.IncomingMessage:
    return writer.IncomingMessage(
        datetime(2026, 9, day, hour, 0, tzinfo=UTC), speaker, text
    )


def _stage(
    archive_dir,
    *,
    workspace: str,
    channel_id: str,
    file_id: str,
    name: str,
    status: str,
    error: str = "",
    extracted: bool = False,
    extracted_text: str = "",
) -> None:
    root = attachment_review.staging_root(archive_dir)
    d = root / workspace / "channels" / channel_id / "attachments" / file_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "metadata.json").write_text(
        json.dumps(
            {
                "name": name,
                "filetype": name.rsplit(".", 1)[-1],
                "mimetype": "application/pdf",
                "declared_size": 240 * 1024,
                "status": status,
                "error": error,
                "extracted": extracted,
                "object_path": str(d / "object.bin"),
                "staged_at": "2026-09-11T09:00:00+00:00",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    if extracted_text:
        (d / "extracted.md").write_text(extracted_text, encoding="utf-8")


@pytest.fixture
def world(tmp_path):
    """광주도시철도 정산 채널 + 자금 채널 + 첨부 메타데이터."""
    archive = tmp_path / "archive"
    writer.ingest(
        archive,
        workspace="pilot",
        channel=GJ,
        channel_id=GJ_ID,
        acl=[GJ],
        messages=[
            _msg(10, 1, "김수현", "광주도시철도 미수금 현황 공유합니다"),
            _msg(
                10, 2, "김수현",
                "원기성금액 12억 3천만원 / 회수금액 9억 1천만원 / 미수금액 3억 2천만원",
            ),
            _msg(10, 3, "김수현", "지난달 대비 회수 속도가 느립니다"),
            _msg(10, 5, "조민희", "미수금 표는 이해도 20% 미만이라고 봅니다"),
            _msg(10, 6, "김수현", "[첨부:검수대기] 광주도시철도_미수금_가정산서.pdf (pdf, 240KB)"),
        ],
    )
    writer.ingest(
        archive,
        workspace="pilot",
        channel=FUND,
        channel_id=FUND_ID,
        acl=[FUND],
        messages=[
            _msg(10, 1, "박자금", "자금팀 주간 미수금 별건 자료입니다"),
            _msg(10, 2, "박자금", "[첨부:검수대기] 자금계획.xlsx (xlsx, 90KB)"),
        ],
    )
    _stage(
        archive,
        workspace="pilot",
        channel_id=GJ_ID,
        file_id="F_GJ",
        name="광주도시철도_미수금_가정산서.pdf",
        status=attachment_review.DOWNLOAD_OR_EXTRACT_FAILED,
        error="텍스트 레이어 없음",
    )
    _stage(
        archive,
        workspace="pilot",
        channel_id=FUND_ID,
        file_id="F_FUND",
        name="자금계획.xlsx",
        status=attachment_review.FAILED,
        error="손상",
    )
    return archive


@pytest.fixture
def engine(world):
    fake = FakeProvider()
    router = Router(
        providers={"anthropic": fake},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    return AnswerEngine(ArchiveStore(world), router), fake


def _ctx(*, channels=(GJ,), channel_id=GJ_ID, channel=GJ, is_root=False):
    return RequestContext(
        workspace="pilot",
        channels=frozenset(channels),
        channel_id=channel_id,
        channel=channel,
        is_root=is_root,
    )


def _turn(ans, *, question: str, record_id: str = "r1") -> dict:
    """답변 하나를 스레드 turn 으로 (파일럿이 QA 기록을 거쳐 하는 일과 같은 모양)."""
    return {
        "record_id": record_id,
        "ts": "2026-09-11T10:00:00+09:00",
        "question": question,
        "intent_kind": "search",
        "subject_terms": list(ans.subject_terms),
        "evidence_refs": list(ans.evidence_refs),
        "attachment_refs": list(ans.attachment_refs),
        "context_parent_ids": [],
    }


def _resolver(world):
    return ThreadFollowupResolver(ArchiveStore(world), archive_dir=world)


# --- 실제 대화 회귀 (설계 §16) -----------------------------------------------


def test_three_turn_conversation_stays_on_the_prior_evidence(world, engine):
    eng, fake = engine
    ctx = _ctx()

    # 1턴 — 미수금 현황
    first = eng.answer(
        "광주도시철도 미수금 현황을 정리해줘", ctx, terms=["광주도시철도", "미수금"]
    )
    assert first.reason == "answered"
    assert first.evidence_refs, "1턴이 좌표를 안 남기면 후속 질문이 이어 갈 것이 없다"
    assert first.attachment_refs == [
        AttachmentRef(workspace="pilot", channel_id=GJ_ID, file_id="F_GJ")
    ]
    turns = [_turn(first, question="광주도시철도 미수금 현황을 정리해줘")]

    # 2턴 — "처리 안 된 문서도 다시 확인해줘"
    (second_intent,) = apply_followup(
        "처리 안 된 문서도 다시 확인해줘",
        plan_by_rule("처리 안 된 문서도 다시 확인해줘"),
        has_prior=True,
    )
    assert second_intent.reference_mode == "prior_attachments"
    assert second_intent.include_attachment_status
    resolved = _resolver(world).resolve(second_intent, ctx, turns=turns)
    assert [a.file_id for a in resolved.attachments] == ["F_GJ"]

    second = eng.respond(second_intent.question, ctx, second_intent, followup=resolved)
    assert "가정산서.pdf" in second.text
    # 채널 전체 실패 목록으로 넓히지 않는다 — 다른 채널 파일은 나오면 안 된다.
    assert "자금계획.xlsx" not in second.text

    # 3턴 — "방금 너와 나눈 대화 중 미수금 관련 내용을 요약하고 파일 변환 실패도 알려줘"
    q3 = "방금 너와 나눈 대화 중 미수금 관련 내용을 요약하고 파일 변환 실패도 알려줘"
    (third_intent,) = apply_followup(q3, plan_by_rule(q3), has_prior=True)
    assert third_intent.reference_mode == "prior_topic"
    assert third_intent.topic_terms == ["미수금"]
    assert third_intent.include_attachment_status
    # 요약과 첨부 상태를 **하나의 참조 범위**로 처리한다(설계 §8-5).
    assert third_intent.kind == "summary"

    resolved3 = _resolver(world).resolve(third_intent, ctx, turns=turns)
    third = eng.respond(q3, ctx, third_intent, followup=resolved3)

    sent = fake.last_prompt
    assert "원기성금액 12억 3천만원" in sent
    assert "회수금액 9억 1천만원" in sent
    assert "미수금액 3억 2천만원" in sent
    # 주제와 무관한 줄은 넘어가지 않는다 — 한정이 살아 있다는 뜻이다.
    assert "지난달 대비 회수 속도" not in sent
    # 사람의 평가는 발언자와 함께 전달된다(귀속 보존).
    assert "조민희" in sent
    # 다른 채널 자료도, 이전 봇 답변 문장도 근거로 들어가지 않는다.
    assert "자금계획" not in sent
    assert first.text not in sent
    assert "가정산서.pdf" in third.text


def test_a_tampered_prior_answer_cannot_change_the_facts(world, engine):
    """이전 봇 답변을 바꿔도 최종 답변의 근거는 달라지지 않는다(완료 조건)."""
    eng, fake = engine
    ctx = _ctx()
    first = eng.answer("광주도시철도 미수금", ctx, terms=["미수금"])
    turn = _turn(first, question="광주도시철도 미수금")
    turn["legacy_answer"] = "미수금액은 999억원입니다"  # 조작된 이전 답변

    q = "방금 그 미수금 내용 다시 요약해줘"
    (intent,) = apply_followup(q, plan_by_rule(q), has_prior=True)
    resolved = _resolver(world).resolve(intent, ctx, turns=[turn])
    eng.respond(q, ctx, intent, followup=resolved)

    sent = fake.last_prompt
    assert "999억" not in sent
    assert "미수금액 3억 2천만원" in sent


# --- 분류와 문맥 --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "방금 너와 나눈 대화 중 미수금 내용을 요약해줘",
        "아까 그 문서 다시 확인해줘",
        "위에서 말한 관련 파일 상태 알려줘",
    ],
)
def test_action_verbs_make_a_follow_up_not_a_memory_question(text):
    tasks = apply_followup(text, plan_by_rule(text), has_prior=True)
    assert [t.kind for t in tasks] != ["memory"]
    assert tasks[0].reference_mode != "none"


@pytest.mark.parametrize("text", ["이전 답변을 기억하니?", "우리 대화 기억해?"])
def test_asking_whether_the_bot_remembers_stays_memory(text):
    tasks = apply_followup(text, plan_by_rule(text), has_prior=True)
    assert tasks[0].kind == "memory"
    assert tasks[0].reference_mode == "none"


@pytest.mark.parametrize(
    "text", ["내가 전에 물어본 적 있어?", "아까 뭐라고 했지?", "그 문서 기억나?"]
)
def test_questions_about_remembering_are_not_scoped_follow_ups(text):
    """기억 여부를 묻는 말에는 실행 범위를 붙이지 않는다(설계 §8-2)."""
    tasks = apply_followup(text, plan_by_rule(text), has_prior=True)
    assert tasks[0].reference_mode == "none"


def test_without_a_prior_turn_nothing_is_treated_as_a_follow_up():
    text = "아까 그 문서 다시 확인해줘"
    tasks = apply_followup(text, plan_by_rule(text), has_prior=False)
    assert tasks[0].reference_mode == "none"


def test_a_long_prior_answer_does_not_break_the_link(world, engine, tmp_path):
    """답변이 4,000자를 넘어도 좌표로 이어진다 — 글자 예산과 무관하다."""
    eng, _ = engine
    ctx = _ctx()
    first = eng.answer("광주도시철도 미수금", ctx, terms=["미수금"])

    log = QALog(tmp_path / "qa", write_md=False)
    log.write(
        QARecord.build(
            workspace="pilot",
            channel=GJ,
            channel_id=GJ_ID,
            user="U1",
            user_name="홍길동",
            question="광주도시철도 미수금 현황",
            intent_kind="search",
            intent_source="llm",
            reason="answered",
            hits=first.hit_count,
            scope="현재 채널",
            citations=list(first.citations),
            model="m",
            cost_usd=0.0,
            elapsed_ms=1,
            answer="가" * 9000,  # 상한을 넘는 긴 답변
            thread_ts="T1",
            request_ts="1",
            response_ts="2",
            channel_type="channel",
            error="",
            evidence_refs=refs_to_json(first.evidence_refs),
            attachment_refs=attachment_refs_to_json(first.attachment_refs),
            subject_terms=list(first.subject_terms),
        )
    )
    (turn,) = log.context_for_thread("pilot", GJ_ID, "T1")
    assert turn["evidence_refs"], "좌표가 남지 않으면 긴 답변이 연결을 끊는다"
    # 좌표가 있는 레코드는 답변 전문을 planner 로 보내지 않는다.
    assert "legacy_answer" not in turn

    q = "방금 그 미수금 내용 다시 요약해줘"
    (intent,) = apply_followup(q, plan_by_rule(q), has_prior=True)
    resolved = _resolver(world).resolve(intent, ctx, turns=[turn])
    assert resolved.evidence_hits


def test_a_legacy_record_without_refs_does_not_widen_to_the_whole_channel(world):
    """구형 레코드에는 좌표가 없다. 그때도 채널 전체로 넓히지 않는다."""
    ctx = _ctx()
    legacy = {
        "record_id": "old",
        "question": "광주도시철도 미수금",
        "subject_terms": [],
        "evidence_refs": [],
        "attachment_refs": [],
        "legacy_answer": "미수금액 3억 2천만원입니다",
    }
    q = "방금 그 문서 다시 확인해줘"
    (intent,) = apply_followup(q, plan_by_rule(q), has_prior=True)
    resolved = _resolver(world).resolve(intent, ctx, turns=[legacy])
    assert resolved.evidence_hits == []
    assert resolved.attachments == []
    assert "no_prior_refs" in resolved.dropped_codes


# --- 원문 복원 ----------------------------------------------------------------


def _first_ref(store, ctx) -> EvidenceRef:
    from tybot.evidence_refs import ref_from_hit

    hits = store.search("미수금액", ctx)
    assert hits
    ref = ref_from_hit(hits[0], store.root)
    assert ref is not None
    return ref


def test_a_moved_line_is_still_resolved_by_its_hash(world):
    store = ArchiveStore(world)
    ctx = _ctx()
    ref = _first_ref(store, ctx)
    expected = store.search("미수금액", ctx)[0].line.text

    # 원문 앞쪽에 줄이 끼어들어 번호가 밀린다.
    path = world / ref.document_path
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "> [2026-09-10 10:00] 김수현: 광주도시철도 미수금 현황 공유합니다",
            "> [2026-09-10 09:00] 김수현: 사전 공지\n"
            "> [2026-09-10 10:00] 김수현: 광주도시철도 미수금 현황 공유합니다",
        ),
        encoding="utf-8",
    )
    moved = ArchiveStore(world)
    assert moved.search("사전 공지", ctx), "줄이 실제로 끼어들었는지 먼저 확인"
    hits, dropped = moved.resolve_refs([ref], ctx)
    assert [h.line.text for h in hits] == [expected]
    assert hits[0].line.lineno != ref.line_no, "줄 번호가 밀린 상황이어야 의미가 있다"
    assert "hash_mismatch" not in dropped


def test_a_changed_line_is_dropped_rather_than_reused(world):
    store = ArchiveStore(world)
    ctx = _ctx()
    ref = _first_ref(store, ctx)

    path = world / ref.document_path
    path.write_text(
        path.read_text(encoding="utf-8").replace(
            "미수금액 3억 2천만원", "미수금액 99억원"
        ),
        encoding="utf-8",
    )
    hits, dropped = ArchiveStore(world).resolve_refs([ref], ctx)
    assert hits == []
    assert "hash_mismatch" in dropped


def test_a_path_outside_the_archive_is_refused(world):
    ctx = _ctx()
    escape = EvidenceRef(
        kind="archive_line",
        workspace="pilot",
        channel_id=GJ_ID,
        document_path="../../etc/passwd",
        line_no=1,
        content_hash=content_hash("x", "y", "z"),
    )
    # 모양 검사에서 이미 걸린다. 경로로 쓰이는 값은 파일에서 읽어 온 값이다.
    assert EvidenceRef.from_json(escape.to_json()) is None
    hits, dropped = ArchiveStore(world).resolve_refs([escape], ctx)
    assert hits == []
    assert dropped == ["path_rejected"]


def test_same_file_name_with_different_ids_is_never_mixed(world, engine):
    """같은 이름의 파일이 둘이면 참조를 만들지 않는다 — 어느 것인지 모른다."""
    _stage(
        world,
        workspace="pilot",
        channel_id=GJ_ID,
        file_id="F_GJ_DUP",
        name="광주도시철도_미수금_가정산서.pdf",
        status=attachment_review.CONVERTED,
        extracted=True,
    )
    eng, _ = engine
    ans = eng.answer("광주도시철도 미수금", _ctx(), terms=["미수금"])
    assert ans.attachment_refs == []


def test_current_metadata_beats_an_old_failure_sentence(world, engine):
    """과거 대화에 「처리실패」라고 적혀 있어도 지금 변환됐으면 변환된 것이다."""
    eng, _ = engine
    ctx = _ctx()
    first = eng.answer("광주도시철도 미수금", ctx, terms=["미수금"])
    turns = [_turn(first, question="광주도시철도 미수금")]

    # 그 사이에 변환에 성공했다.
    _stage(
        world,
        workspace="pilot",
        channel_id=GJ_ID,
        file_id="F_GJ",
        name="광주도시철도_미수금_가정산서.pdf",
        status=attachment_review.CONVERTED,
        extracted=True,
    )
    q = "처리 안 된 문서도 다시 확인해줘"
    (intent,) = apply_followup(q, plan_by_rule(q), has_prior=True)
    resolved = _resolver(world).resolve(intent, ctx, turns=turns)
    ans = eng.respond(q, ctx, intent, followup=resolved)
    assert "변환 완료" in ans.text
    assert "변환 실패" not in ans.text


def test_a_pii_blocked_attachment_never_leaks_its_extracted_text(world, engine):
    _stage(
        world,
        workspace="pilot",
        channel_id=GJ_ID,
        file_id="F_GJ",
        name="광주도시철도_미수금_가정산서.pdf",
        status=attachment_review.PII_REFUSED,
        error="등기부등본 표현 감지",
        extracted=True,
        extracted_text="주민등록번호 900101-1234567 홍길동",
    )
    eng, fake = engine
    ctx = _ctx()
    first = eng.answer("광주도시철도 미수금", ctx, terms=["미수금"])
    turns = [_turn(first, question="광주도시철도 미수금")]

    q = "처리 안 된 문서도 다시 확인해줘"
    (intent,) = apply_followup(q, plan_by_rule(q), has_prior=True)
    resolved = _resolver(world).resolve(intent, ctx, turns=turns)
    ans = eng.respond(q, ctx, intent, followup=resolved)

    assert "민감정보" in ans.text
    assert "900101" not in ans.text
    assert "홍길동" not in ans.text
    assert all("900101" not in json.dumps(r, ensure_ascii=False) for r in
               refs_to_json(ans.evidence_refs))
    assert "900101" not in json.dumps(
        attachment_refs_to_json(ans.attachment_refs), ensure_ascii=False
    )
    if fake.calls:
        assert "900101" not in fake.last_prompt


# --- 권한 --------------------------------------------------------------------


def test_a_reference_to_another_channel_is_dropped(world):
    """채널 A 의 후속 질문은 이전 레코드에 B 참조가 섞여 있어도 A 만 쓴다."""
    store = ArchiveStore(world)
    both = RequestContext(
        workspace="pilot", channels=frozenset({GJ, FUND}), channel_id=GJ_ID, channel=GJ
    )
    fund_hits = store.search("자금팀", RequestContext(workspace="pilot", channels=frozenset({FUND})))
    assert fund_hits
    from tybot.evidence_refs import ref_from_hit

    fund_ref = ref_from_hit(fund_hits[0], store.root)
    gj_ref = _first_ref(store, _ctx())

    hits, dropped = ArchiveStore(world).resolve_refs([gj_ref, fund_ref], both)
    assert [h.doc.channel_id for h in hits] == [GJ_ID]
    assert "permission_changed" in dropped or "channel_scope" in dropped


def test_evidence_is_dropped_when_membership_was_removed(world):
    store = ArchiveStore(world)
    ref = _first_ref(store, _ctx())
    # 이 사용자는 더 이상 그 채널의 멤버가 아니다.
    gone = RequestContext(workspace="pilot", channels=frozenset(), channel_id="", channel="")
    hits, dropped = ArchiveStore(world).resolve_refs([ref], gone)
    assert hits == []
    assert "permission_changed" in dropped


def test_root_in_a_channel_still_sees_only_that_channel(world):
    store = ArchiveStore(world)
    fund_ctx = RequestContext(workspace="pilot", channels=frozenset({FUND}))
    from tybot.evidence_refs import ref_from_hit

    fund_ref = ref_from_hit(store.search("자금팀", fund_ctx)[0], store.root)
    root_in_gj = RequestContext(
        workspace="pilot",
        channels=frozenset({GJ, FUND}),
        role="admin",
        is_root=True,
        channel_id=GJ_ID,
        channel=GJ,
    )
    hits, _ = ArchiveStore(world).resolve_refs([fund_ref], root_in_gj)
    assert hits == []


def test_a_dm_may_combine_channels_the_user_can_see(world):
    store = ArchiveStore(world)
    gj_ref = _first_ref(store, _ctx())
    fund_ctx = RequestContext(workspace="pilot", channels=frozenset({FUND}))
    from tybot.evidence_refs import ref_from_hit

    fund_ref = ref_from_hit(store.search("자금팀", fund_ctx)[0], store.root)
    dm = RequestContext(
        workspace="pilot", channels=frozenset({GJ, FUND}), channel_id="", channel=""
    )
    hits, _ = ArchiveStore(world).resolve_refs([gj_ref, fund_ref], dm)
    assert {h.doc.channel_id for h in hits} == {GJ_ID, FUND_ID}


def test_the_specialist_only_receives_permission_filtered_evidence(world):
    """전문 봇 입력에 권한 밖 원문도, 이전 봇 답변도 없다(설계 §12)."""
    seen: list[str] = []

    class Special:
        text = "요약했습니다."
        model = "m"
        cost_usd = 0.0
        specialist = "hermes"
        documents: ClassVar[list] = []
        live_links: ClassVar[list] = []

    def hook(question, ctx, evidence):
        seen.append(evidence)
        return Special()

    fake = FakeProvider()
    router = Router(
        providers={"anthropic": fake},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    eng = AnswerEngine(ArchiveStore(world), router, specialist=hook)
    ctx = _ctx()
    first = eng.answer("광주도시철도 미수금", ctx, terms=["미수금"])
    turns = [_turn(first, question="광주도시철도 미수금")]

    q = "방금 그 미수금 내용 다시 요약해줘"
    (intent,) = apply_followup(q, plan_by_rule(q), has_prior=True)
    resolved = _resolver(world).resolve(intent, ctx, turns=turns)
    eng.respond(q, ctx, intent, followup=resolved)

    joined = "\n".join(seen)
    assert "미수금액 3억 2천만원" in joined
    assert "자금팀" not in joined
    assert first.text not in joined


def test_a_specialist_citing_a_document_outside_the_acl_falls_back_to_master(world):
    """계약 위반 — 권한 밖 출처를 든 전문가 답은 쓰지 않는다."""

    class Rogue:
        text = "권한 밖 문서로 답합니다."
        model = "m"
        cost_usd = 0.0
        specialist = "hermes"
        live_links: ClassVar[list] = []

        def __init__(self, doc):
            self.documents = [doc]

    fund_doc = next(
        d for d in ArchiveStore(world).docs() if d.channel_id == FUND_ID
    )
    fake = FakeProvider()
    router = Router(
        providers={"anthropic": fake},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    eng = AnswerEngine(
        ArchiveStore(world), router, specialist=lambda q, ctx, ev: Rogue(fund_doc)
    )
    ans = eng.answer("광주도시철도 미수금", _ctx(), terms=["미수금"])
    assert ans.specialist == ""  # 마스터가 답했다
    assert "권한 밖 문서로 답합니다." not in ans.text


# --- 모호한 지칭 --------------------------------------------------------------


def test_an_ambiguous_singular_reference_asks_instead_of_widening(world, engine):
    _stage(
        world,
        workspace="pilot",
        channel_id=GJ_ID,
        file_id="F_GJ2",
        name="정산내역.pdf",
        status=attachment_review.FAILED,
        error="손상",
    )
    eng, _ = engine
    ctx = _ctx()
    turn = {
        "record_id": "r1",
        "question": "첨부 확인해줘",
        "subject_terms": [],
        "evidence_refs": [],
        "attachment_refs": [
            AttachmentRef(workspace="pilot", channel_id=GJ_ID, file_id="F_GJ"),
            AttachmentRef(workspace="pilot", channel_id=GJ_ID, file_id="F_GJ2"),
        ],
    }
    q = "처리 안 된 하나의 문서도 다시 확인해줘"
    (intent,) = apply_followup(q, plan_by_rule(q), has_prior=True)
    resolved = _resolver(world).resolve(intent, ctx, turns=[turn])
    assert resolved.needs_clarification
    ans = eng.respond(q, ctx, intent, followup=resolved)
    assert ans.reason == "clarify"
    assert "가정산서.pdf" in ans.text and "정산내역.pdf" in ans.text


def test_an_unresolvable_reference_says_so_instead_of_answering_something_else(
    world, engine
):
    eng, _ = engine
    ctx = _ctx()
    first = eng.answer("광주도시철도 미수금", ctx, terms=["미수금"])
    turns = [_turn(first, question="광주도시철도 미수금")]

    # 멤버십이 사라진 뒤의 후속 질문.
    gone = RequestContext(workspace="pilot", channels=frozenset(), channel_id="", channel="")
    q = "방금 그 미수금 내용 다시 요약해줘"
    (intent,) = apply_followup(q, plan_by_rule(q), has_prior=True)
    resolved = _resolver(world).resolve(intent, gone, turns=turns)
    ans = eng.respond(q, gone, intent, followup=resolved)
    assert ans.reason == "no_hits"
    assert "다시 확인하지 못했습니다" in ans.text
    assert "3억 2천만원" not in ans.text


def test_a_topic_with_no_matching_evidence_does_not_pull_in_other_subjects(
    world, engine
):
    eng, _ = engine
    ctx = _ctx()
    first = eng.answer("광주도시철도 미수금", ctx, terms=["미수금"])
    turns = [_turn(first, question="광주도시철도 미수금")]

    intent = Intent(
        "summary",
        question="방금 대화 중 감리비 관련 내용만 요약해줘",
        reference_mode="prior_topic",
        topic_terms=["감리비"],
    )
    resolved = _resolver(world).resolve(intent, ctx, turns=turns)
    assert resolved.evidence_hits == []
    assert "topic_no_match" in resolved.dropped_codes
    ans = eng.respond(intent.question, ctx, intent, followup=resolved)
    assert ans.reason == "no_hits"
    assert "미수금액" not in ans.text
