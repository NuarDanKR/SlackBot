"""원본 메타데이터 → 이미지 설명 후보 선별 (B-81).

이 파일이 지키는 것 다섯.

1. **아는 위험을 통과시키지 않는다.** `blocked` 로 판정된 첨부는 후보가 아니다.
2. **모르는 것을 안전으로 바꾸지 않는다.** 업로더를 모르면 후보가 아니고,
   글자 검사가 돌았다는 것을 「개인정보 없음」 으로 적지 않는다.
3. **버린 것을 이름으로 남긴다.** 「후보 0건」 과 「전부 걸러짐」 은 다르다 —
   앞은 기다리는 일이고 뒤는 고치는 일이다.
4. **채널 경계를 넘어 묶지 않는다.** 같은 사진이 두 채널에 있으면 둘 다 후보다.
5. **같은 입력이면 같은 배치가 나온다.** 입력 순서에 기대지 않는다.

파일·DB·네트워크를 쓰지 않는다. 메타데이터만 보는 순수 함수다.
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from tybot.archive.attachment_doc import (
    BLOCKED,
    CONVERTED,
    FAILED,
    PARTIAL,
    PENDING,
    UNSUPPORTED,
    AttachmentDoc,
)
from tybot.archive.image_description import ALLOWED_MIME
from tybot.archive.image_description_source import (
    DEFAULT_LIMIT,
    FILETYPE_MIME,
    SKIP_ALREADY_INDEXED,
    SKIP_BAD_SHA256,
    SKIP_CODES,
    SKIP_DUPLICATE,
    SKIP_NO_ACL,
    SKIP_NO_COORDINATES,
    SKIP_NO_SHA256,
    SKIP_NOT_AN_IMAGE,
    SKIP_NOT_HUMAN,
    SKIP_OVER_LIMIT,
    SKIP_PENDING,
    SKIP_PII_BLOCKED,
    confirmed_human_file_ids,
    select_image_candidates,
)

SHA = "a" * 64


def _doc(**over) -> AttachmentDoc:
    base = {
        "workspace": "tyit",
        "channel_id": "C0TEST0001",
        "channel": "#팀_전산(ABB100)_공지",
        "file_id": "F0TEST0001",
        "name": "현장사진",
        "revision": "r1",
        "visibility": "private",
        "acl": frozenset({"U1", "U2"}),
        "filetype": "png",
        "sha256": SHA,
        "conversion_state": CONVERTED,
        "staged_at": "2026-10-27T10:00:00+09:00",
        "message_ts": "1761500000.000100",
        "permalink": "https://slack.example/archives/C0TEST0001/p1",
    }
    base.update(over)
    return AttachmentDoc(**base)


def _pick(docs, *, human=None, indexed=(), limit=DEFAULT_LIMIT):
    ids = frozenset(d.file_id for d in docs) if human is None else frozenset(human)
    return select_image_candidates(
        docs, human_file_ids=ids, already_indexed=indexed, limit=limit
    )


def _reasons(out) -> set[str]:
    return {s.reason for s in out.skipped}


# --- 고르는 것 ---------------------------------------------------------------


def test_a_human_uploaded_png_is_a_candidate():
    out = _pick([_doc()])

    assert len(out.candidates) == 1
    got = out.candidates[0]
    assert (got.workspace, got.channel_id, got.file_id) == ("tyit", "C0TEST0001", "F0TEST0001")
    assert got.original_sha256 == SHA
    assert got.mime == "image/png"
    assert out.skipped == ()


@pytest.mark.parametrize("filetype,mime", sorted(FILETYPE_MIME.items()))
def test_every_image_filetype_maps_to_a_mime_the_core_accepts(filetype, mime):
    out = _pick([_doc(filetype=filetype)])

    assert out.candidates[0].mime == mime
    assert mime in ALLOWED_MIME


def test_the_filetype_is_matched_case_insensitively():
    out = _pick([_doc(filetype="PNG"), _doc(file_id="F2", filetype="JPEG", sha256="b" * 64)])

    assert len(out.candidates) == 2
    assert {c.mime for c in out.candidates} == {"image/png", "image/jpeg"}


def test_the_coordinates_are_copied_not_invented():
    out = _pick([_doc(revision="r9", message_ts="1.2", permalink="https://x/y")])
    got = out.candidates[0]

    assert (got.revision, got.message_ts, got.permalink) == ("r9", "1.2", "https://x/y")
    assert got.visibility == "private"
    assert got.acl == frozenset({"U1", "U2"})


def test_a_candidate_carries_no_bytes():
    """선별이 곧 적재가 되면 **승인 전에 원본이 메모리로 올라온다.**"""
    got = _pick([_doc()]).candidates[0]

    assert not hasattr(got, "image")
    assert not hasattr(got, "data")
    assert not any(isinstance(v, bytes) for v in vars(got).values())


# --- 아는 위험 --------------------------------------------------------------


def test_a_pii_blocked_attachment_is_never_a_candidate():
    """이미 개인정보로 판정된 것을 통과시킬 이유가 없다(원칙 5)."""
    out = _pick([_doc(conversion_state=BLOCKED)])

    assert out.candidates == ()
    assert _reasons(out) == {SKIP_PII_BLOCKED}


def test_pii_is_reported_before_the_shape_of_the_file():
    """「이미지가 아님」 이 먼저 나오면 차단된 첨부가 평범해 보인다."""
    out = _pick([_doc(conversion_state=BLOCKED, filetype="pdf", sha256="")])

    assert _reasons(out) == {SKIP_PII_BLOCKED}


# --- 모르는 것 --------------------------------------------------------------


def test_an_unknown_uploader_is_not_a_candidate():
    """`AttachmentDoc` 에는 업로더가 없다. **모르면 후보가 아니다.**"""
    out = _pick([_doc()], human=())

    assert out.candidates == ()
    assert _reasons(out) == {SKIP_NOT_HUMAN}


def test_only_canonical_provenance_supplies_human_file_ids():
    docs = [
        _doc(file_id="F1", human_upload_confirmed=True),
        _doc(file_id="F2", sha256="b" * 64),
    ]

    assert confirmed_human_file_ids(docs) == frozenset({"F1"})


def test_only_the_named_files_become_candidates():
    docs = [_doc(), _doc(file_id="F2", sha256="b" * 64)]
    out = _pick(docs, human={"F2"})

    assert [c.file_id for c in out.candidates] == ["F2"]
    assert [s.file_id for s in out.skipped] == ["F0TEST0001"]


@pytest.mark.parametrize("state,screened", [
    (CONVERTED, True),
    (PARTIAL, True),
    (FAILED, False),
    (UNSUPPORTED, False),
])
def test_whether_the_text_screen_actually_ran_is_reported(state, screened):
    """OCR 이 실패하면 글자 검사는 **아무것도 못 본 채** 지나간다."""
    out = _pick([_doc(conversion_state=state)])

    assert len(out.candidates) == 1
    assert out.candidates[0].text_screened is screened
    assert out.candidates[0].conversion_state == state


def test_a_screened_candidate_is_not_declared_safe():
    """`text_screened` 는 사실이지 판정이 아니다. 안전 필드를 만들지 않는다."""
    got = _pick([_doc(conversion_state=CONVERTED)]).candidates[0]

    assert got.text_screened is True
    for forbidden in ("pii_blocked", "safe", "approved", "access_verified", "cleared"):
        assert not hasattr(got, forbidden), forbidden


def test_a_pending_attachment_waits():
    """아직 아무것도 안 돌았다. 글자 검사도, 원본 자리 잡힘도 모른다."""
    out = _pick([_doc(conversion_state=PENDING)])

    assert out.candidates == ()
    assert _reasons(out) == {SKIP_PENDING}


# --- 좌표와 해시 ------------------------------------------------------------


@pytest.mark.parametrize("over,reason", [
    ({"filetype": "pdf"}, SKIP_NOT_AN_IMAGE),
    ({"filetype": "hwp"}, SKIP_NOT_AN_IMAGE),
    ({"filetype": ""}, SKIP_NOT_AN_IMAGE),
    ({"filetype": "gif"}, SKIP_NOT_AN_IMAGE),
    ({"workspace": ""}, SKIP_NO_COORDINATES),
    ({"channel_id": ""}, SKIP_NO_COORDINATES),
    ({"file_id": ""}, SKIP_NO_COORDINATES),
    ({"sha256": ""}, SKIP_NO_SHA256),
    ({"sha256": "   "}, SKIP_NO_SHA256),
    ({"sha256": "a" * 63}, SKIP_BAD_SHA256),
    ({"sha256": "z" * 64}, SKIP_BAD_SHA256),
    ({"sha256": "A" * 64 + "a"}, SKIP_BAD_SHA256),
    ({"visibility": ""}, SKIP_NO_ACL),
])
def test_a_source_that_cannot_be_pinned_is_skipped(over, reason):
    out = _pick([_doc(**over)])

    assert out.candidates == ()
    assert _reasons(out) == {reason}


def test_an_uppercase_hash_is_normalised_not_rejected():
    out = _pick([_doc(sha256="A" * 64)])

    assert out.candidates[0].original_sha256 == "a" * 64


def test_an_empty_acl_set_is_still_pinnable():
    """`visibility` 가 있으면 묶을 자리는 있다. 빈 ACL 은 **좁은 쪽**이다."""
    out = _pick([_doc(acl=frozenset())])

    assert len(out.candidates) == 1
    assert out.candidates[0].acl == frozenset()


# --- 중복과 채널 경계 ---------------------------------------------------------


def test_the_same_image_twice_in_one_channel_is_one_candidate():
    docs = [_doc(), _doc(file_id="F2", revision="r2")]
    out = _pick(docs)

    assert len(out.candidates) == 1
    assert _reasons(out) == {SKIP_DUPLICATE}


def test_the_same_image_in_two_channels_is_two_candidates():
    """채널마다 멤버가 다르다. 한 줄만 색인하면 **다른 채널이 그것을 집는다.**"""
    docs = [_doc(), _doc(channel_id="C0OTHER", file_id="F2")]
    out = _pick(docs)

    assert len(out.candidates) == 2
    assert {c.channel_id for c in out.candidates} == {"C0TEST0001", "C0OTHER"}
    assert out.skipped == ()


def test_the_same_image_in_two_workspaces_is_two_candidates():
    """workspace 경계 밖에서 재사용하지 않는다(원칙 4)."""
    docs = [_doc(), _doc(workspace="tyhq", file_id="F2")]
    out = _pick(docs)

    assert len(out.candidates) == 2
    assert {c.workspace for c in out.candidates} == {"tyit", "tyhq"}


def test_an_already_indexed_source_is_skipped():
    out = _pick([_doc()], indexed={("tyit", "C0TEST0001", SHA)})

    assert out.candidates == ()
    assert _reasons(out) == {SKIP_ALREADY_INDEXED}


def test_an_index_entry_from_another_channel_does_not_skip_this_one():
    out = _pick([_doc()], indexed={("tyit", "C0OTHER", SHA)})

    assert len(out.candidates) == 1


# --- 배치 상한 --------------------------------------------------------------


def test_over_the_limit_is_named_not_dropped():
    """조용히 사라지면 그 사진은 **아무도 다시 안 본다.**"""
    docs = [
        _doc(file_id=f"F{n}", sha256=f"{n:064d}".replace("0", "a", 1))
        for n in range(1, 6)
    ]
    out = _pick(docs, limit=2)

    assert len(out.candidates) == 2
    assert len(out.skipped) == 3
    assert _reasons(out) == {SKIP_OVER_LIMIT}
    assert out.counts == {SKIP_OVER_LIMIT: 3}


def test_the_limit_does_not_hide_a_real_problem():
    """상한에 걸린 것과 걸러진 것을 **같은 코드로 적지 않는다.**"""
    docs = [_doc(file_id="F1"), _doc(file_id="F2", conversion_state=BLOCKED,
                                     sha256="b" * 64)]
    out = _pick(docs, limit=1)

    assert _reasons(out) == {SKIP_PII_BLOCKED}


@pytest.mark.parametrize("limit", [0, -1, 1.5, "3", None])
def test_a_bad_limit_is_refused(limit):
    with pytest.raises(ValueError):
        select_image_candidates([_doc()], human_file_ids={"F0TEST0001"}, limit=limit)


# --- 결정성 -----------------------------------------------------------------


def test_the_same_input_gives_the_same_batch_in_any_order():
    docs = [
        _doc(file_id="F1", sha256="1" * 63 + "a", staged_at="2026-10-27T09:00:00+09:00"),
        _doc(file_id="F2", sha256="2" * 63 + "a", staged_at="2026-10-27T11:00:00+09:00"),
        _doc(file_id="F3", sha256="3" * 63 + "a", channel_id="C0OTHER"),
    ]
    first = _pick(docs, limit=2)
    second = _pick(list(reversed(docs)), limit=2)

    assert [c.file_id for c in first.candidates] == [c.file_id for c in second.candidates]
    assert first.skipped == second.skipped


def test_the_batch_is_ordered_by_coordinates_then_time():
    docs = [
        _doc(file_id="F2", sha256="2" * 63 + "a", converted_at="2026-10-27T12:00:00+09:00"),
        _doc(file_id="F1", sha256="1" * 63 + "a", converted_at="2026-10-27T10:00:00+09:00"),
    ]
    out = _pick(docs)

    assert [c.file_id for c in out.candidates] == ["F1", "F2"]


# --- 보고 -------------------------------------------------------------------


def test_the_counts_name_every_reason():
    docs = [
        _doc(file_id="F1", conversion_state=BLOCKED),
        _doc(file_id="F2", filetype="pdf", sha256="2" * 63 + "a"),
        _doc(file_id="F3", sha256=""),
        _doc(file_id="F4", sha256="4" * 63 + "a"),
    ]
    out = _pick(docs, human={"F1", "F2", "F3", "F4"})

    assert out.counts == {SKIP_PII_BLOCKED: 1, SKIP_NOT_AN_IMAGE: 1, SKIP_NO_SHA256: 1}
    assert [c.file_id for c in out.candidates] == ["F4"]


def test_no_candidates_and_everything_filtered_look_different():
    empty = _pick([])
    filtered = _pick([_doc(conversion_state=BLOCKED)])

    assert empty.candidates == () and empty.skipped == ()
    assert filtered.candidates == () and filtered.skipped != ()


def test_a_skip_record_carries_no_file_name_or_body():
    """사유는 운영 화면으로 그대로 나간다. **코드와 좌표만.**"""
    out = _pick([_doc(name="주민등록증 사본", text="본문", conversion_state=BLOCKED)])
    said = repr(out.skipped)

    assert "주민등록증" not in said
    assert "본문" not in said
    assert SKIP_PII_BLOCKED in said


def test_every_skip_code_is_non_sensitive():
    for code in SKIP_CODES:
        assert code.isascii() and code.replace("_", "").isalnum(), code


def test_the_mime_table_cannot_drift_from_the_core():
    """코어가 안 받는 형식을 올리면 **승인·예약을 다 지나고** 호출 직전에 떨어진다."""
    assert set(FILETYPE_MIME.values()) <= set(ALLOWED_MIME)


def test_a_doc_is_never_modified():
    doc = _doc()
    before = replace(doc)

    _pick([doc])

    assert doc == before
