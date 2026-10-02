"""첨부 결말 분류와 **변환 완료 뒤 갱신.**

결정: 2026-10-02 오너 지시 — pending·부분 변환·미지원·차단·원본 저장 실패를
구분하고, 변환이 끝나면 ACK 를 다시 센다.

전에는 다섯이 `partial` 하나로 들어가 같은 문장으로 나갔고, 변환이 끝나도 아무도
ACK 를 갱신하지 않아 **첨부가 있는 메시지는 영원히 `partial`** 이었다.
"""

from __future__ import annotations

import json
from pathlib import Path

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
    """정본 한 장의 **읽는 쪽 모양.** 원본 보관 여부는 들고 있지 않다."""

    def __init__(self, file_id, state, text="본문", message_ts="1790070000.000001",
                 source_path=None, channel_id="C1"):
        self.workspace, self.channel_id = "tyit", channel_id
        self.file_id, self.conversion_state = file_id, state
        self.text, self.message_ts = text, message_ts
        self.source_path = source_path


def _on_disk(root: Path, file_id, state, *, retained, channel_id="C1", **kw) -> _Doc:
    """정본과 staging metadata 를 실제로 깐다.

    `retained` 는 `True`(보관) · `False`(실패) · `None`(metadata 자체가 없음).
    """
    channel = root / "tyit" / f"{channel_id}__팀-전산-공지"
    canonical = channel / "archive" / "attachments" / file_id / "r1.md"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text("정본", encoding="utf-8")
    if retained is not None:
        if retained:
            original = channel / "objects" / file_id / "report.bin"
            original.parent.mkdir(parents=True, exist_ok=True)
            original.write_bytes(b"original bytes")
        meta = channel / "staging" / file_id / "metadata.json"
        meta.parent.mkdir(parents=True, exist_ok=True)
        meta.write_text(
            json.dumps({
                "original_state": "retained" if retained else "missing",
                "name": "report.bin",
            }),
            encoding="utf-8",
        )
    return _Doc(file_id, state, source_path=canonical, channel_id=channel_id, **kw)


def _row(state: str, total: int, ready: int, code: str = "",
         ts="1790070000.000001", written_to="live", updated_at=None):
    return {
        "workspace": "tyit", "channel_id": "C1", "message_ts": ts,
        "state": state, "attachment_total": total, "attachment_ready": ready,
        "written_to": written_to, "error_code": code,
        "updated_at": updated_at if updated_at is not None else 0,
    }


def _docs(*docs):
    found: dict = {}
    for doc in docs:
        found.setdefault((doc.workspace, doc.channel_id, doc.message_ts), []).append(doc)
    return found


def _plan(rows, docs, root):
    return ack_reconcile.plan(rows, docs, root=root)


def test_a_finished_conversion_moves_partial_to_ready(tmp_path):
    """`partial -> ready` 간선은 처음부터 열려 있었다. **가는 코드가 없었다.**"""
    doc = _on_disk(tmp_path, "F1", CONVERTED, retained=True)
    changes = _plan([_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path)
    assert len(changes) == 1
    assert (changes[0].before, changes[0].after) == ("partial", "ready")
    assert changes[0].outcome.error_code == ""


def test_an_unsupported_attachment_updates_the_reason_but_stays_partial(tmp_path):
    doc = _on_disk(tmp_path, "F1", UNSUPPORTED, retained=True)
    changes = _plan([_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path)
    assert len(changes) == 1
    assert changes[0].after == "partial"
    assert changes[0].outcome.error_code == ATTACHMENT_UNSUPPORTED_CODE


def test_nothing_changes_when_the_outcome_is_the_same(tmp_path):
    doc = _on_disk(tmp_path, "F1", PENDING, retained=True)
    assert _plan([_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path) == []


def test_a_lost_original_is_left_alone(tmp_path):
    """변환이 끝났다고 **없던 원본이 생기지 않는다.**"""
    doc = _on_disk(tmp_path, "F1", CONVERTED, retained=True)
    assert _plan(
        [_row("partial", 1, 0, ATTACHMENT_ORIGINAL_MISSING_CODE)], _docs(doc), tmp_path
    ) == []


def test_a_message_without_attachments_is_left_alone(tmp_path):
    assert _plan([_row("raw_written", 0, 0)], {}, tmp_path) == []


def test_a_missing_canonical_document_keeps_it_pending(tmp_path):
    """표는 첨부 2개라는데 정본이 1개다. **모자란 만큼 아직으로 센다.**"""
    doc = _on_disk(tmp_path, "F1", CONVERTED, retained=True)
    changes = _plan([_row("partial", 2, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path)
    assert len(changes) == 1
    assert changes[0].after == "partial"
    assert changes[0].outcome.error_code == ATTACHMENT_PENDING_CODE
    assert changes[0].outcome.ready == 1


def test_a_document_without_a_coordinate_is_not_attached_to_a_message(tmp_path):
    """좌표 없는 정본을 가까운 메시지에 붙이지 않는다."""
    doc = _on_disk(tmp_path, "F1", CONVERTED, retained=True, message_ts="")
    assert _plan(
        [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path
    ) == []


# ---------------------------------------------------------------------------
# 정본이 있다고 **원본이 있는 것이 아니다**
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("retained", [False, None], ids=["missing", "unknown"])
def test_a_canonical_document_alone_never_proves_the_original_was_stored(
    tmp_path, retained
):
    """원본 쓰기가 실패해도 digest 는 이미 계산돼 있어 정본은 나온다.

    그러니 정본만 보고 `ready` 를 내면 **없는 원본을 있다고 세는** 것이고,
    사람은 그 말을 「내 파일이 안전하다」 로 읽는다. 그 상태에서 Slack 원본을
    지우면 되살릴 자료가 어디에도 없다.

    모르는 경우(metadata 없음)도 올리지 않는다 — 「없다」 와 「모른다」 는 사람이
    할 일이 다르지만, **둘 다 `ready` 는 아니다.**
    """
    doc = _on_disk(tmp_path, "F1", CONVERTED, retained=retained)
    changes = _plan([_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path)
    assert len(changes) == 1
    assert changes[0].after == "partial"
    assert changes[0].outcome.error_code == ATTACHMENT_ORIGINAL_MISSING_CODE
    assert changes[0].outcome.ready == 0


def test_an_object_file_counts_as_a_stored_original(tmp_path):
    """metadata 가 없어도 objects 아래 실물이 있으면 보관으로 본다."""
    doc = _on_disk(tmp_path, "F1", CONVERTED, retained=None)
    objects = tmp_path / "tyit" / "C1__팀-전산-공지" / "objects" / "F1"
    objects.mkdir(parents=True)
    (objects / "보고서.pdf").write_bytes(b"%PDF-1.4 ...")

    changes = _plan([_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path)
    assert changes[0].after == "ready"


def test_an_empty_object_file_is_not_a_stored_original(tmp_path):
    doc = _on_disk(tmp_path, "F1", CONVERTED, retained=None)
    objects = tmp_path / "tyit" / "C1__팀-전산-공지" / "objects" / "F1"
    objects.mkdir(parents=True)
    (objects / "보고서.pdf").write_bytes(b"")

    changes = _plan([_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path)
    assert changes[0].outcome.error_code == ATTACHMENT_ORIGINAL_MISSING_CODE


def test_a_retained_marker_without_the_original_does_not_make_ack_ready(tmp_path):
    doc = _on_disk(tmp_path, "F1", CONVERTED, retained=True)
    original = tmp_path / "tyit" / "C1__팀-전산-공지" / "objects" / "F1" / "report.bin"
    original.unlink()

    changes = _plan([_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path)
    assert len(changes) == 1
    assert changes[0].after == "partial"
    assert changes[0].outcome.error_code == ATTACHMENT_ORIGINAL_MISSING_CODE


def test_a_different_object_does_not_satisfy_retained_metadata(tmp_path):
    doc = _on_disk(tmp_path, "F1", CONVERTED, retained=True)
    objects = tmp_path / "tyit" / "C1__팀-전산-공지" / "objects" / "F1"
    (objects / "report.bin").unlink()
    (objects / "unrelated.bin").write_bytes(b"unrelated bytes")

    changes = _plan([_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), tmp_path)
    assert changes[0].after == "partial"
    assert changes[0].outcome.error_code == ATTACHMENT_ORIGINAL_MISSING_CODE


def test_legacy_attachment_storage_remains_readable(tmp_path):
    root = tmp_path / "archive"
    suffix = Path("workspaces/tyit/channels/C1/attachments/F1")
    canonical = root / suffix / "r1.md"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("정본", encoding="utf-8")
    metadata = tmp_path / "staging" / suffix / "metadata.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(
        json.dumps({"original_state": "retained", "name": "report.bin"}),
        encoding="utf-8",
    )
    original = tmp_path / "objects" / suffix / "report.bin"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"original bytes")
    doc = _Doc("F1", CONVERTED, source_path=canonical)

    changes = _plan([_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], _docs(doc), root)
    assert changes[0].after == "ready"


def test_a_document_without_a_source_path_is_not_assumed_stored(tmp_path):
    """경로를 못 되짚으면 보관 여부를 모른다. 모르면 올리지 않는다."""
    changes = _plan(
        [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)],
        _docs(_Doc("F1", CONVERTED)),
        tmp_path,
    )
    assert changes[0].outcome.error_code == ATTACHMENT_ORIGINAL_MISSING_CODE


# ---------------------------------------------------------------------------
# 루트는 `written_to` 가 고른다
# ---------------------------------------------------------------------------


def _wire(monkeypatch, rows, docs_by_root):
    monkeypatch.setattr(
        ingest_ack, "unfinished",
        lambda _ws, limit=500, after=None: rows if after is None else [],
    )
    monkeypatch.setattr(
        ack_reconcile, "docs_by_message", lambda root: docs_by_root.get(Path(root), {})
    )
    pushed: list = []
    monkeypatch.setattr(ingest_ack, "advance", lambda **kw: pushed.append(kw))
    return pushed


def test_each_row_is_recounted_against_its_own_root(tmp_path, monkeypatch):
    """그림자와 운영은 **다른 곳**이다.

    한 루트로만 다시 세면 다른 쪽 행은 정본을 못 찾아 끝난 일이 영원히 「아직」 이
    되고, 반대로 같은 file ID 를 엉뚱한 루트에서 찾으면 남의 자료로 남의 메시지를
    `ready` 로 올린다.
    """
    live_root, shadow_root = tmp_path / "live", tmp_path / "shadow"
    live_doc = _on_disk(live_root, "F1", CONVERTED, retained=True)
    shadow_doc = _on_disk(shadow_root, "F2", UNSUPPORTED, retained=True,
                          message_ts="1790070000.000002")
    rows = [
        _row("partial", 1, 0, ATTACHMENT_PENDING_CODE, written_to="live"),
        _row("partial", 1, 0, ATTACHMENT_PENDING_CODE,
             ts="1790070000.000002", written_to="shadow"),
    ]
    pushed = _wire(monkeypatch, rows, {
        live_root: _docs(live_doc), shadow_root: _docs(shadow_doc),
    })

    changes = ack_reconcile.run(
        "tyit", roots={"live": live_root, "shadow": shadow_root}, apply=True
    )

    by_ts = {c.message_ts: c for c in changes}
    assert by_ts["1790070000.000001"].after == "ready"
    assert by_ts["1790070000.000002"].after == "partial"
    assert by_ts["1790070000.000002"].outcome.error_code == ATTACHMENT_UNSUPPORTED_CODE
    # 목적지를 **그대로** 되민다. 비우면 그림자 기록이 운영으로 보인다.
    assert {kw["written_to"] for kw in pushed} == {"live", "shadow"}


def test_a_row_with_an_unknown_destination_is_skipped(tmp_path, monkeypatch):
    """어느 루트인지 모르면 **짐작해서 다시 세지 않는다.**"""
    live_root = tmp_path / "live"
    doc = _on_disk(live_root, "F1", CONVERTED, retained=True)
    rows = [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE, written_to="")]
    pushed = _wire(monkeypatch, rows, {live_root: _docs(doc)})

    assert ack_reconcile.run("tyit", roots={"live": live_root}, apply=True) == []
    assert pushed == []


def test_the_label_of_a_root_is_not_guessed(tmp_path):
    roots = {"live": tmp_path / "live", "shadow": tmp_path / "shadow"}
    assert ack_reconcile.label_for(roots, tmp_path / "live") == "live"
    assert ack_reconcile.label_for(roots, tmp_path / "shadow") == "shadow"
    assert ack_reconcile.label_for(roots, tmp_path / "somewhere-else") == ""


# ---------------------------------------------------------------------------
# 앞에 쌓인 행이 뒤를 막지 않는다
# ---------------------------------------------------------------------------


def test_unchanged_rows_at_the_front_do_not_block_the_rest(tmp_path, monkeypatch):
    """바뀌지 않는 행은 `updated_at` 도 안 바뀐다 — **앞자리를 영원히 차지한다.**

    한 쪽(500건)만 읽으면 그 뒤의 행은 변환이 끝나도 갱신되지 않는다. 그리고 그
    고장은 조용하다 — 잡은 매번 정상 종료한다.
    """
    root = tmp_path / "live"
    stuck = _on_disk(root, "F0", PENDING, retained=True, message_ts="stuck")
    done = _on_disk(root, "F1", CONVERTED, retained=True, message_ts="done")
    page_one = [
        _row("partial", 1, 0, ATTACHMENT_PENDING_CODE, ts="stuck", updated_at=index)
        for index in range(500)
    ]
    page_two = [
        _row("partial", 1, 0, ATTACHMENT_PENDING_CODE, ts="done", updated_at=500)
    ]
    pages = {None: page_one, (499, "C1", "stuck"): page_two}
    monkeypatch.setattr(
        ingest_ack, "unfinished",
        lambda _ws, limit=500, after=None: pages.get(after, []),
    )
    monkeypatch.setattr(
        ack_reconcile, "docs_by_message", lambda _root: _docs(stuck, done)
    )
    monkeypatch.setattr(ingest_ack, "advance", lambda **_kw: None)

    changes = ack_reconcile.run("tyit", roots={"live": root}, limit=500)

    assert [c.message_ts for c in changes] == ["done"]
    assert changes[0].after == "ready"


def test_a_short_page_ends_the_sweep(tmp_path, monkeypatch):
    """끝까지 읽되 **끝나면 멈춘다.** 안 멈추면 매 주기마다 전체를 다시 읽는다."""
    root = tmp_path / "live"
    calls: list = []

    def _unfinished(_ws, limit=500, after=None):
        calls.append(after)
        return [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)] if after is None else []

    monkeypatch.setattr(ingest_ack, "unfinished", _unfinished)
    monkeypatch.setattr(ack_reconcile, "docs_by_message", lambda _root: {})

    ack_reconcile.run("tyit", roots={"live": root}, limit=500)

    assert calls == [None]


# ---------------------------------------------------------------------------
# 변환 완료 직후 자동 실행
# ---------------------------------------------------------------------------


def test_refresh_one_uses_the_destination_of_the_row(tmp_path, monkeypatch):
    """변환 잡은 자기가 돌린 아카이브만 안다. **목적지는 행이 안다.**"""
    shadow_root = tmp_path / "shadow"
    doc = _on_disk(shadow_root, "F1", CONVERTED, retained=True)
    monkeypatch.setattr(
        ingest_ack, "read",
        lambda *_a: ingest_ack.AckStatus(
            IngestProgress(IngestState.PARTIAL, 1, 0, ATTACHMENT_PENDING_CODE),
            written_to="shadow",
        ),
    )
    monkeypatch.setattr(ack_reconcile, "docs_by_message", lambda _root: _docs(doc))
    pushed: list = []
    monkeypatch.setattr(ingest_ack, "advance", lambda **kw: pushed.append(kw))

    change = ack_reconcile.refresh_one(
        "tyit", "C1", "1790070000.000001",
        roots={"live": tmp_path / "live", "shadow": shadow_root},
    )

    assert change is not None and change.after == "ready"
    assert pushed[0]["written_to"] == "shadow"
    assert pushed[0]["target"] == IngestState.READY


def test_refresh_one_does_nothing_for_a_terminal_row(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ingest_ack, "read",
        lambda *_a: ingest_ack.AckStatus(
            IngestProgress(IngestState.READY, 1, 1), written_to="live"
        ),
    )
    assert ack_reconcile.refresh_one(
        "tyit", "C1", "1790070000.000001", roots={"live": tmp_path}
    ) is None


def test_refresh_one_skips_a_destination_it_has_no_root_for(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ingest_ack, "read",
        lambda *_a: ingest_ack.AckStatus(
            IngestProgress(IngestState.PARTIAL, 1, 0), written_to="shadow"
        ),
    )
    assert ack_reconcile.refresh_one(
        "tyit", "C1", "1790070000.000001", roots={"live": tmp_path}
    ) is None


def test_the_conversion_job_calls_the_reconciler():
    """변환이 끝나는 자리가 **ACK 재계산을 부른다.**

    전에는 이 자리가 자기 나름의 셈을 했고, 그 셈은 정본 개수만 봐서 미지원·실패
    정본을 「준비됨」 으로 세었다.
    """
    source = (
        Path(__file__).resolve().parents[1] / "scripts" / "drain_conversion_queue.py"
    ).read_text(encoding="utf-8")
    assert "ack_reconcile.refresh_one" in source
    assert "ack_reconcile.label_for" in source
    # 옛 셈이 남아 있으면 두 규칙이 공존한다. 설명문에 이름이 남는 것은
    # 괜찮으므로 **문자열 리터럴**만 본다.
    assert chr(34) + 'attachment-not-searchable' + chr(34) not in source
    assert "ingest_ack.advance(" not in source


def test_run_writes_nothing_unless_asked(monkeypatch, tmp_path):
    """기본이 **안 쓰는 쪽**이다. 사람에게 하는 말을 바꾸는 동작이다."""
    root = tmp_path / "live"
    doc = _on_disk(root, "F1", CONVERTED, retained=True)
    pushed = _wire(
        monkeypatch, [_row("partial", 1, 0, ATTACHMENT_PENDING_CODE)], {root: _docs(doc)}
    )

    assert len(ack_reconcile.run("tyit", roots={"live": root})) == 1
    assert pushed == []

    assert len(ack_reconcile.run("tyit", roots={"live": root}, apply=True)) == 1
    assert len(pushed) == 1
    assert pushed[0]["target"] == IngestState.READY
    assert pushed[0]["attachment_ready"] == 1


def test_an_unreadable_state_table_is_not_reported_as_nothing_to_do(monkeypatch, tmp_path):
    """못 본 것과 없는 것은 다르다."""
    def boom(*_args, **_kwargs):
        raise ingest_ack.AckUnavailable("down")

    monkeypatch.setattr(ingest_ack, "unfinished", boom)
    with pytest.raises(ingest_ack.AckUnavailable):
        ack_reconcile.run("tyit", roots={"live": tmp_path})


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


# ---------------------------------------------------------------------------
# staging metadata 가 보관 여부의 1차 근거
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [("retained", True), ("missing", False), ("", None), ("무슨값", None)],
)
def test_metadata_decides_whether_the_original_was_kept(tmp_path, state, expected):
    path = tmp_path / "metadata.json"
    path.write_text(json.dumps({"original_state": state}), encoding="utf-8")
    assert attachment_ack.retained_from_metadata(path) is expected


def test_an_unreadable_metadata_file_is_not_read_as_kept(tmp_path):
    """못 읽은 것을 **보관됐다**고 하지 않는다."""
    path = tmp_path / "metadata.json"
    path.write_text("{깨진", encoding="utf-8")
    assert attachment_ack.retained_from_metadata(path) is None
    assert attachment_ack.retained_from_metadata(tmp_path / "없음.json") is None
    assert attachment_ack.retained_from_metadata(None) is None


# ---------------------------------------------------------------------------
# 수집 경로도 추정하지 않는다
# ---------------------------------------------------------------------------


def _collect_one(tmp_path, monkeypatch, *, original_state, conversion):
    """첨부 하나짜리 메시지를 실제 수집 경로로 흘린다. 마지막 ACK 를 돌려준다."""
    import dataclasses
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from tybot import archiving_bot
    from tybot.archive import writer

    acked: list = []
    monkeypatch.setattr(
        archiving_bot.ingest_ack, "advance", lambda **kw: acked.append(kw) or None
    )
    monkeypatch.delenv("DATABASE_URL", raising=False)

    meta_path = tmp_path / "staging" / "F1" / "metadata.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps({"original_state": original_state}), encoding="utf-8"
    )
    reference = "[첨부:보고서] 보고서.pdf (pdf, 1KB) · id:F1"
    staged = SimpleNamespace(
        file_id="F1", reference_lines=[reference], lines=[reference],
        metadata_path=meta_path,
    )

    def fake_messages(*_args, staged_out, attachment_line_selector, **_kw):
        staged_out.append(staged)
        return [
            writer.IncomingMessage(
                ts=datetime.fromtimestamp(1790070000.000001, tz=UTC),
                speaker="U12345678", text=reference, source_ts="1790070000.000001",
            )
        ]

    monkeypatch.setattr(archiving_bot, "_messages_from", fake_messages)
    monkeypatch.setattr(
        archiving_bot, "write_attachment_docs",
        lambda *_a, **_k: [
            SimpleNamespace(file_id="F1", conversion_state=conversion, text="본문")
        ],
    )
    monkeypatch.setattr(
        archiving_bot, "confirm_archived",
        lambda *_a, **_k: {"F1": archiving_bot.ARCHIVE_DONE},
    )
    env = {
        "ARCHIVER_CONFIG_SOURCE": "env", "ARCHIVER_WORKSPACES": "tyit",
        "ARCHIVER_BOT_TOKEN_TYIT": "archiver-bot",
        "ARCHIVER_APP_TOKEN_TYIT": "archiver-app",
        "ARCHIVER_TEAM_ID_TYIT": "T12345678",
        "SLACK_BOT_TOKEN_TYIT": "master-bot", "SLACK_APP_TOKEN_TYIT": "master-app",
        "ARCHIVER_MASTER_BOT_USER_TYIT": "U_MASTER",
        "ARCHIVER_CHANNEL_IDS_TYIT": "C12345678",
        "ARCHIVE_DIR": str(tmp_path / "live"),
        "ARCHIVER_SHADOW_DIR": str(tmp_path / "shadow" / "archive"),
    }
    cfg = dataclasses.replace(
        archiving_bot.load_archiver_workspaces(env)[0], separate_attachments=True
    )
    collector = archiving_bot.ShadowCollector(cfg, tmp_path / "shadow")

    class _Client:
        def users_info(self, user):
            return {"user": {"name": user}}

        def conversations_info(self, channel):
            return {"channel": {"id": channel, "name": "팀_전산(ABB155)_공지",
                                "is_private": False, "is_member": True}}

        def conversations_members(self, channel, limit=200, cursor=None):
            return {"members": [], "response_metadata": {}}

    result = collector.ingest_event(_Client(), {
        "channel_type": "channel", "channel": "C12345678", "subtype": "file_share",
        "user": "U12345678", "ts": "1790070000.000001", "text": "",
        "files": [{"id": "F1"}],
    })
    return result, acked[-1]


def test_a_kept_original_with_a_converted_document_reaches_ready(tmp_path, monkeypatch):
    result, last = _collect_one(
        tmp_path, monkeypatch, original_state="retained", conversion=CONVERTED
    )
    assert result == "written"
    assert last["target"] == IngestState.READY
    assert last["error_code"] == ""


@pytest.mark.parametrize("original_state", ["missing", ""], ids=["missing", "unknown"])
def test_a_raw_reference_alone_never_proves_the_original_was_kept(
    tmp_path, monkeypatch, original_state
):
    """`ARCHIVE_DONE` 은 **첨부 참조 줄이 원문에 들어갔다**는 뜻이다.

    원본 바이트가 남았다는 뜻이 아니다 — digest 는 쓰기 전에 계산되므로 원본
    쓰기가 실패해도 참조 줄과 정본은 나온다. 둘을 같은 것으로 보면 없는 원본을
    있다고 세고, 그 메시지가 `ready` 가 된다.
    """
    result, last = _collect_one(
        tmp_path, monkeypatch, original_state=original_state, conversion=CONVERTED
    )
    assert result == "partial"
    assert last["target"] == IngestState.PARTIAL
    assert last["error_code"] == ATTACHMENT_ORIGINAL_MISSING_CODE
    assert last["attachment_ready"] == 0
