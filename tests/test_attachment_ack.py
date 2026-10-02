"""첨부 결말 분류와 **변환 완료 뒤 갱신.**

결정: 2026-10-02 오너 지시 — pending·부분 변환·미지원·차단·원본 저장 실패를
구분하고, 변환이 끝나면 ACK 를 다시 센다.

전에는 다섯이 `partial` 하나로 들어가 같은 문장으로 나갔고, 변환이 끝나도 아무도
ACK 를 갱신하지 않아 **첨부가 있는 메시지는 영원히 `partial`** 이었다.
"""

from __future__ import annotations

import pytest

from tybot.archive import ack_reconcile, attachment_ack, ingest_ack
from tybot.archive.archiving_state import (
    ATTACHMENT_ORIGINAL_MISSING_CODE,
    ATTACHMENT_PARTIAL_CODE,
    ATTACHMENT_PENDING_CODE,
    ATTACHMENT_SCREENED_CODE,
    ATTACHMENT_UNSUPPORTED_CODE,
    IngestProgress,
    IngestState,
    searchable_claim,
)
from tybot.archive.attachment_ack import ATTACHMENT_CONVERSION_FAILED_CODE
from tybot.archive.attachment_doc import (
    BLOCKED,
    CONVERTED,
    FAILED,
    PARTIAL,
    PENDING,
    UNSUPPORTED,
)


def _one(state: str, *, stored: bool = True, text: bool = True):
    return attachment_ack.classify(
        ["F1"], stored={"F1": stored}, conversion={"F1": state}, has_text={"F1": text}
    )


# ---------------------------------------------------------------------------
# 결말 하나하나
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (PENDING, ATTACHMENT_PENDING_CODE),
        (PARTIAL, ATTACHMENT_PARTIAL_CODE),
        (UNSUPPORTED, ATTACHMENT_UNSUPPORTED_CODE),
        (BLOCKED, ATTACHMENT_SCREENED_CODE),
        (FAILED, ATTACHMENT_CONVERSION_FAILED_CODE),
    ],
)
def test_each_conversion_state_gets_its_own_code(state, code):
    outcome = _one(state)
    assert outcome.error_code == code
    assert outcome.ready == 0
    assert not outcome.all_ready


def test_a_converted_attachment_with_text_is_ready():
    outcome = _one(CONVERTED)
    assert outcome.all_ready
    assert (outcome.ready, outcome.total, outcome.error_code) == (1, 1, "")


def test_a_converted_attachment_without_text_is_not_ready():
    """성공이라는데 본문이 비어 있다. **검색되지 않으므로 성공이 아니다.**

    여기를 세면 ACK 가 검색 결과보다 앞서 간다 — 사람이 찾으러 갔다가 못 찾고,
    그 다음부터는 맞는 답도 확인하러 간다.
    """
    outcome = _one(CONVERTED, text=False)
    assert not outcome.all_ready
    assert outcome.error_code == ATTACHMENT_CONVERSION_FAILED_CODE


def test_a_missing_original_beats_a_successful_conversion():
    """원본이 없으면 변환본이 있어도 **다시 만들 수 없다.**

    변환본만 보고 `ready` 를 내면, 그 변환본이 깨진 날 되살릴 근거가 없다는
    사실을 아무도 모른 채로 지나간다.
    """
    outcome = _one(CONVERTED, stored=False)
    assert outcome.error_code == ATTACHMENT_ORIGINAL_MISSING_CODE
    assert outcome.ready == 0


def test_an_unknown_file_is_counted_as_pending():
    """모르는 것을 **끝났다고 하지 않는다.**"""
    outcome = attachment_ack.classify(["F1"], stored={"F1": True}, conversion={})
    assert outcome.error_code == ATTACHMENT_PENDING_CODE


def test_a_file_without_a_stored_flag_is_not_assumed_stored():
    outcome = attachment_ack.classify(["F1"], stored={}, conversion={"F1": CONVERTED})
    assert outcome.error_code == ATTACHMENT_ORIGINAL_MISSING_CODE


def test_no_attachments_is_ready():
    outcome = attachment_ack.classify([], stored={}, conversion={})
    assert outcome.all_ready
    assert outcome.total == 0


# ---------------------------------------------------------------------------
# 섞였을 때 무엇을 말하나
# ---------------------------------------------------------------------------


def test_a_lost_original_wins_over_everything_else():
    """지금 **다시 올려야** 하는 것이 먼저다. 늦으면 되살릴 수 없다."""
    outcome = attachment_ack.classify(
        ["F1", "F2", "F3"],
        stored={"F1": True, "F2": False, "F3": True},
        conversion={"F1": CONVERTED, "F2": CONVERTED, "F3": UNSUPPORTED},
    )
    assert outcome.error_code == ATTACHMENT_ORIGINAL_MISSING_CODE
    assert (outcome.ready, outcome.total) == (1, 3)


def test_pending_wins_over_finished_problems():
    """아직 끝나지 않았으면 **아직**이다.

    끝난 것부터 말하면 사람은 다 끝난 줄 알고 다시 안 쳐다본다.
    """
    outcome = attachment_ack.classify(
        ["F1", "F2"],
        stored={"F1": True, "F2": True},
        conversion={"F1": UNSUPPORTED, "F2": PENDING},
    )
    assert outcome.error_code == ATTACHMENT_PENDING_CODE


def test_partial_is_the_quietest_problem():
    outcome = attachment_ack.classify(
        ["F1", "F2"],
        stored={"F1": True, "F2": True},
        conversion={"F1": CONVERTED, "F2": PARTIAL},
    )
    assert outcome.error_code == ATTACHMENT_PARTIAL_CODE
    assert (outcome.ready, outcome.total) == (1, 2)


# ---------------------------------------------------------------------------
# 미지원은 **ready 가 아니다**
# ---------------------------------------------------------------------------


def test_an_unsupported_attachment_never_becomes_ready():
    """끝났다고 `ready` 로 올리면 그 상태의 뜻이 조용히 바뀐다.

    `ready` 는 「전부 검색된다」 이고 `is_searchable()` 이 그걸 믿는다. 변환할 수
    없는 첨부는 검색되지 않으므로 거기 들어가면 안 된다 — 끝났다는 것은 상태가
    아니라 **문장**으로 말한다.
    """
    outcome = _one(UNSUPPORTED)
    assert not outcome.all_ready

    claim = searchable_claim(
        IngestProgress(IngestState.PARTIAL, 1, 0, outcome.error_code), require_ack=True
    )
    assert "아직" not in claim
    assert "변환할 수 없는 형식" in claim
    assert "원본은 보관했습니다" in claim


def test_a_settled_outcome_is_not_called_ready_even_with_full_counts():
    """세기만 맞으면 `ready` 가 되던 자리를 막는다."""
    outcome = attachment_ack.Outcome(1, 1, ATTACHMENT_UNSUPPORTED_CODE)
    assert not outcome.all_ready


# ---------------------------------------------------------------------------
# ACK 를 요구하지 않는 경로에서도 실패를 성공으로 말하지 않는다
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", [IngestState.REFUSED, IngestState.FAILED])
def test_a_failure_is_never_told_as_success_without_ack(state):
    """전에는 `received` 가 아닌 모든 상태가 「원문은 기록했습니다」 로 나갔다.

    스위치의 뜻은 「첨부까지 확인해서 말할지」 이지 「실패를 성공으로 말해도
    되는지」 가 아니다.
    """
    claim = searchable_claim(IngestProgress(state), require_ack=False)
    assert "기록했습니다" not in claim


def test_a_lost_original_is_told_even_without_ack():
    claim = searchable_claim(
        IngestProgress(IngestState.PARTIAL, 1, 0, ATTACHMENT_ORIGINAL_MISSING_CODE),
        require_ack=False,
    )
    assert "다시 올려 주세요" in claim


def test_a_lost_original_is_told_even_in_shadow():
    """그림자든 운영이든 **못 쓴 것은 못 썼다**고 해야 한다.

    「그림자에는 기록됐다」 고 하면 사람은 자료가 어딘가 남았다고 읽는다.
    """
    status = ingest_ack.AckStatus(
        IngestProgress(IngestState.PARTIAL, 1, 0, ATTACHMENT_ORIGINAL_MISSING_CODE),
        written_to=ingest_ack.SHADOW,
    )
    assert ingest_ack.claim_for(status, require_ack=True) != ingest_ack.SHADOW_CLAIM
    assert "다시 올려 주세요" in ingest_ack.claim_for(status, require_ack=True)


def test_an_ordinary_shadow_state_still_says_shadow():
    status = ingest_ack.AckStatus(
        IngestProgress(IngestState.RAW_WRITTEN), written_to=ingest_ack.SHADOW
    )
    assert ingest_ack.claim_for(status, require_ack=True) == ingest_ack.SHADOW_CLAIM


# ---------------------------------------------------------------------------
# 스위치가 실제로 문장을 바꾼다
# ---------------------------------------------------------------------------


def test_the_switch_decides_when_the_caller_does_not(monkeypatch):
    """`require_attachment_ack` 이 **인자 기본값이 아니라** 결정한다."""
    status = ingest_ack.AckStatus(
        IngestProgress(IngestState.PARTIAL, 1, 0, ATTACHMENT_PENDING_CODE),
        written_to=ingest_ack.LIVE,
    )
    monkeypatch.setattr(ingest_ack, "require_ack_flag", lambda: True)
    assert "첨부 변환 0/1" in ingest_ack.claim_for(status)

    monkeypatch.setattr(ingest_ack, "require_ack_flag", lambda: False)
    assert ingest_ack.claim_for(status) == "원문은 기록했습니다"


def test_the_switch_falls_back_to_telling_more(monkeypatch):
    """못 읽으면 **더 많이 확인해서 말하는 쪽**이다.

    반대로 두면 DB 가 흔들린 동안 「원문은 기록했습니다」 만 나가고, 첨부가
    검색 안 된다는 사실이 사라진다.
    """
    monkeypatch.setattr(ingest_ack, "enabled", lambda: True)
    monkeypatch.setattr(
        ingest_ack, "_connect", lambda: (_ for _ in ()).throw(RuntimeError("down"))
    )
    ingest_ack.reset_flag_cache()
    try:
        assert ingest_ack.require_ack_flag() is True
    finally:
        ingest_ack.reset_flag_cache()


# ---------------------------------------------------------------------------
# 변환이 끝나면 다시 센다
# ---------------------------------------------------------------------------


class _Doc:
    def __init__(self, file_id, state, text="본문", message_ts="1790070000.000001"):
        self.workspace, self.channel_id = "tyit", "C1"
        self.file_id, self.conversion_state = file_id, state
        self.text, self.message_ts = text, message_ts


def _row(state: str, total: int, ready: int, code: str = "", ts="1790070000.000001"):
    return {
        "workspace": "tyit", "channel_id": "C1", "message_ts": ts,
        "state": state, "attachment_total": total, "attachment_ready": ready,
        "written_to": "live", "error_code": code,
    }


def _docs(*docs):
    return {("tyit", "C1", doc.message_ts): [doc] for doc in docs} if docs else {}


def test_a_finished_conversion_moves_partial_to_ready():
    """`partial -> ready` 간선은 처음부터 열려 있었다. **가는 코드가 없었다.**"""
    changes = ack_reconcile.plan(
        [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)],
        _docs(_Doc("F1", CONVERTED)),
    )
    assert len(changes) == 1
    assert (changes[0].before, changes[0].after) == ("partial", "ready")
    assert changes[0].outcome.error_code == ""


def test_an_unsupported_attachment_updates_the_reason_but_stays_partial():
    changes = ack_reconcile.plan(
        [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)],
        _docs(_Doc("F1", UNSUPPORTED)),
    )
    assert len(changes) == 1
    assert changes[0].after == "partial"
    assert changes[0].outcome.error_code == ATTACHMENT_UNSUPPORTED_CODE


def test_nothing_changes_when_the_outcome_is_the_same():
    assert ack_reconcile.plan(
        [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)],
        _docs(_Doc("F1", PENDING)),
    ) == []


def test_a_lost_original_is_left_alone():
    """변환이 끝났다고 **없던 원본이 생기지 않는다.**

    여기서 덮으면 사람이 다시 올려야 한다는 사실이 조용히 지워진다.
    """
    assert ack_reconcile.plan(
        [_row("partial", 1, 0, ATTACHMENT_ORIGINAL_MISSING_CODE)],
        _docs(_Doc("F1", CONVERTED)),
    ) == []


def test_a_message_without_attachments_is_left_alone():
    assert ack_reconcile.plan([_row("raw_written", 0, 0)], _docs()) == []


def test_a_missing_canonical_document_keeps_it_pending():
    """표는 첨부 2개라는데 정본이 1개다. **모자란 만큼 아직으로 센다.**

    메우지 않고 `ready` 를 내면 문서가 없는 첨부를 검색 가능하다고 말하게 된다.
    """
    changes = ack_reconcile.plan(
        [_row("partial", 2, 0, ATTACHMENT_PENDING_CODE)],
        _docs(_Doc("F1", CONVERTED)),
    )
    assert len(changes) == 1
    assert changes[0].after == "partial"
    assert changes[0].outcome.error_code == ATTACHMENT_PENDING_CODE
    assert changes[0].outcome.ready == 1


def test_a_document_without_a_coordinate_is_not_attached_to_a_message():
    """좌표 없는 정본을 가까운 메시지에 붙이지 않는다.

    붙이면 **남의 메시지가 `ready`** 가 된다. 그래서 그 메시지는 아직 아무것도
    끝나지 않은 채로 남고(변화 없음), 그게 맞는 결과다.
    """
    changes = ack_reconcile.plan(
        [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)],
        {("tyit", "C1", ""): [_Doc("F1", CONVERTED, message_ts="")]},
    )
    assert changes == []


def test_an_unreadable_state_table_is_not_reported_as_nothing_to_do(monkeypatch, tmp_path):
    """못 본 것과 없는 것은 다르다.

    빈 결과를 성공으로 읽으면 장애 중에 「전부 최신입니다」 가 쌓인다.
    """
    def boom(*_args, **_kwargs):
        raise ingest_ack.AckUnavailable("down")

    monkeypatch.setattr(ingest_ack, "unfinished", boom)
    with pytest.raises(ingest_ack.AckUnavailable):
        ack_reconcile.run(tmp_path, "tyit")


def test_run_writes_nothing_unless_asked(monkeypatch, tmp_path):
    """기본이 **안 쓰는 쪽**이다. 사람에게 하는 말을 바꾸는 동작이다."""
    monkeypatch.setattr(
        ingest_ack, "unfinished",
        lambda *_a, **_k: [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)],
    )
    monkeypatch.setattr(
        ack_reconcile, "docs_by_message", lambda _root: _docs(_Doc("F1", CONVERTED))
    )
    pushed = []
    monkeypatch.setattr(ingest_ack, "advance", lambda **kw: pushed.append(kw))

    assert len(ack_reconcile.run(tmp_path, "tyit")) == 1
    assert pushed == []

    assert len(ack_reconcile.run(tmp_path, "tyit", apply=True)) == 1
    assert len(pushed) == 1
    assert pushed[0]["target"] == IngestState.READY
    assert pushed[0]["attachment_ready"] == 1


def test_a_full_count_and_a_clean_reason_always_agree():
    """`ready == total` 과 「사유 없음」 은 **같은 말이어야 한다.**

    `archiving_bot` 은 `outcome.all_ready` 로 `ready` 를 정한다. 누가 그걸
    `outcome.ready == len(staged)` 로 되돌려도 지금은 결과가 같은데, 그건 이
    성질이 참이기 때문이다. 성질이 깨지는 순간 그 표현은 **미지원 첨부를
    「모두 검색 가능」 으로 올린다.** 그러니 성질 자체를 여기서 지킨다.
    """
    states = (PENDING, PARTIAL, UNSUPPORTED, BLOCKED, FAILED, CONVERTED, "모르는값")
    for first in states:
        for second in states:
            for stored in ((True, True), (True, False), (False, False)):
                outcome = attachment_ack.classify(
                    ["F1", "F2"],
                    stored={"F1": stored[0], "F2": stored[1]},
                    conversion={"F1": first, "F2": second},
                )
                full = outcome.ready == outcome.total
                clean = outcome.error_code == ""
                assert full == clean, (
                    f"{first}/{second} stored={stored}: "
                    f"ready={outcome.ready}/{outcome.total} code={outcome.error_code!r}"
                )
                assert outcome.all_ready == full
