"""첨부 정본 문서 — 변환 본문을 raw 에서 떼어 낸다.

설계: `docs/design/archiving-bot-separation-2026-09-23.md` §2·§3

## 무엇을 고정하나
지금 첨부 변환본은 두 곳에 있다 — `staging/…/extracted.md` 와 raw 의
`[첨부추출:이름]` 줄. 검색이 읽는 것은 raw 쪽이라 첨부가 **채팅 원문의 일부처럼**
취급되고, 그래서 첨부별 상태(PII 차단·부분 변환·변환 실패)를 잃는다.

정본으로 떼어내면 그 상태가 살아난다. 대신 **같은 첨부를 두 번 세는** 새 위험이
생긴다 — raw 줄과 정본 문서가 둘 다 근거로 잡히면 답이 「두 군데서 확인됨」 처럼
보이는데 사실은 한 군데다. 이 파일의 절반이 그 하나를 막는 시험이다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from tybot.archive import attachment_doc as ad


def _doc(**over) -> ad.AttachmentDoc:
    values = {
        "workspace": "tyit",
        "channel_id": "C1",
        "channel": "#팀-전산_abb155-공지",
        "file_id": "F1",
        "name": "주간보고.hwpx",
        "revision": "abc123",
        "visibility": "internal",
        "acl": frozenset({"C1"}),
        "text": "금액은 1,200만원",
        "conversion_state": ad.CONVERTED,
    }
    values.update(over)
    return ad.AttachmentDoc(**values)


# --- 프론트매터 ----------------------------------------------------------------
def test_the_frontmatter_has_what_the_store_requires():
    """네 칸이 없으면 스키마 오류로 문서 전체가 검색에서 빠진다.

    그리고 그 사실은 오류 없이 「자료가 없음」 으로 보인다.
    """
    from tybot.archive.store import REQUIRED_FIELDS, parse_frontmatter

    fm = parse_frontmatter(ad.render(_doc()))
    assert not [key for key in REQUIRED_FIELDS if key not in fm]


def test_the_channel_name_is_quoted():
    """`#` 로 시작하는 채널명을 따옴표 없이 적으면 파서가 주석으로 읽는다.

    표시명이 빈 문자열이 되고, 그러면 관계없는 문서가 한 채널로 합쳐진다(B-61).
    """
    from tybot.archive.store import parse_frontmatter

    fm = parse_frontmatter(ad.render(_doc()))
    assert fm["channel"] == "#팀-전산_abb155-공지"


def test_the_document_says_which_file_it_is():
    text = ad.render(_doc())
    assert "kind: attachment" in text
    assert "file_id: F1" in text


def test_the_acl_is_inherited_from_the_channel():
    """첨부는 그 채널에 올라온 것이므로 채널보다 넓게 열릴 수 없다."""
    from tybot.archive.store import parse_frontmatter

    fm = parse_frontmatter(ad.render(_doc(acl=frozenset({"C1", "C2"}))))
    assert set(str(fm["acl"]).split(",")) == {"C1", "C2"}


# --- 상태를 잃지 않는다 ----------------------------------------------------------
def test_a_partial_conversion_says_so_in_the_body():
    """「본문이 나왔다」 와 「다 읽었다」 는 다르다.

    부분 변환본을 전체인 것처럼 두면 없는 내용을 「없다」 고 답하게 된다.
    """
    text = ad.render(_doc(conversion_state=ad.PARTIAL, coverage_read=3, coverage_total=10))
    assert "부분 변환본" in text
    assert "(3/10)" in text


def test_coverage_is_omitted_when_unknown():
    """모르는 것을 0 으로 적으면 모르는 것을 안다고 적는 셈이다."""
    text = ad.render(_doc(coverage_read=None, coverage_total=None))
    assert "coverage_total" not in text


@pytest.mark.parametrize(
    ("state", "note"),
    [
        (ad.BLOCKED, "개인정보"),
        (ad.FAILED, "변환에 실패"),
        (ad.UNSUPPORTED, "변환할 수 없는"),
        (ad.PENDING, "아직 변환되지"),
    ],
)
def test_a_document_without_body_still_exists_and_says_why(state, note):
    """「PII 로 막혔다」 와 「그런 파일이 없다」 는 사람이 할 일이 다르다."""
    doc = _doc(text="", conversion_state=state)
    text = ad.render(doc)

    assert not doc.usable
    assert note in text
    assert "file_id: F1" in text


def test_a_blocked_attachment_carries_the_code_not_the_reason_text():
    """판정 근거는 코드만 남긴다 — OCR 본문·번호 일부를 복제하지 않는다."""
    text = ad.render(_doc(text="", conversion_state=ad.BLOCKED, error_code="pii_refused"))
    assert "error_code: pii_refused" in text


# --- 판(revision) --------------------------------------------------------------
def test_the_revision_comes_from_the_original_digest():
    """같은 원본을 다시 변환하면 같은 판이라 덮어쓰기가 된다."""
    assert ad.revision_of("abcdef1234567890") == "abcdef123456"


def test_a_missing_digest_still_produces_a_path():
    """경로를 못 만들면 그 첨부는 정본이 아예 안 생긴다 — 그게 더 나쁘다."""
    got = ad.revision_of("", staged_at="2026-09-23T10:00:00+00:00")
    assert got.startswith("t")
    assert got != "unknown"


@pytest.mark.parametrize("hostile", ["F/../../etc", "..", ".", "../..", "/etc/passwd"])
def test_a_hostile_file_id_cannot_escape_the_channel(hostile):
    """`file_id` 는 우리가 만든 값이 아니다. 그럴 리 없다고 믿지 않는다.

    `.` 이 허용 문자라 `..` 는 걸러도 그대로 남는다 — 그 둘은 파일 이름이 아니라
    **경로 지시자**다. 막지 않으면 `attachments/../…` 로 채널 밖에 쓴다.
    """
    from pathlib import Path, PurePosixPath

    path = _doc(file_id=hostile, revision="r1").relative_path()
    parts = PurePosixPath(*Path(path).parts)

    assert ".." not in parts.parts
    assert "." not in parts.parts
    # 여전히 그 채널 아래여야 한다.
    assert parts.parts[:5] == ("workspaces", "tyit", "channels", "C1", "attachments")


def test_a_hostile_revision_cannot_escape_either():
    """판 번호도 같은 자리에 들어간다."""
    from pathlib import Path

    parts = Path(_doc(file_id="F1", revision="..").relative_path()).parts
    assert ".." not in parts


# --- staging 에서 읽기 ----------------------------------------------------------
def _stage(tmp_path, **over):
    meta = {
        "slack_file_id": "F1",
        "name": "주간보고.hwpx",
        "filetype": "hwpx",
        "sha256": "abcdef1234567890",
        "conversion_state": "succeeded",
        "origin_message_ts": "1758600000.000100",
        "staged_at": "2026-09-23T10:00:00+00:00",
    }
    meta.update(over)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def _from(staged):
    return ad.from_staged(
        staged,
        workspace="tyit",
        channel_id="C1",
        channel="#팀-전산_abb155-공지",
        visibility="internal",
        acl=frozenset({"C1"}),
    )


def test_a_staged_attachment_becomes_a_canonical_document(tmp_path):
    """새로 수집할 것이 없다. metadata.json 과 extracted.md 가 이미 있다."""
    staged = _stage(tmp_path / "F1")
    (staged / "extracted.md").write_text(
        "<!-- 로컬 변환본 -->\n# 주간보고.hwpx\n\n금액은 1,200만원\n", encoding="utf-8"
    )

    got = _from(staged)

    assert got is not None
    assert got.file_id == "F1"
    assert got.text == "금액은 1,200만원"
    assert got.message_ts == "1758600000.000100"


def test_the_local_header_and_title_are_stripped():
    """남겨 두면 검색에 제목이 본문으로 잡힌다."""
    assert ad._strip_local_header("<!-- x -->\n# 이름.hwpx\n\n본문\n") == "본문"


def test_an_attachment_without_a_file_id_makes_no_document(tmp_path):
    """좌표가 없으면 임의 채널에 붙이지 않는다(설계 §3).

    어느 파일인지 모르면 중복 방지도 출처 표시도 할 수 없다.
    """
    assert _from(_stage(tmp_path / "x", slack_file_id="")) is None


def test_a_broken_metadata_file_does_not_stop_the_rest(tmp_path):
    """첨부 하나가 깨졌다고 나머지 수백 개의 정본화가 멈추면 안 된다."""
    staged = tmp_path / "F9"
    staged.mkdir(parents=True)
    (staged / "metadata.json").write_text("{깨진", encoding="utf-8")

    assert _from(staged) is None


def test_a_missing_staging_directory_is_not_an_error(tmp_path):
    assert _from(tmp_path / "없음") is None


def test_a_staged_attachment_with_no_body_keeps_its_state(tmp_path):
    staged = _stage(tmp_path / "F2", conversion_state="blocked", error_code="pii_refused")
    got = _from(staged)

    assert got is not None
    assert not got.usable
    assert got.conversion_state == ad.BLOCKED


# --- 같은 첨부를 두 번 세지 않는다 -------------------------------------------------
@dataclass
class _Line:
    text: str


@dataclass
class _RawDoc:
    channel_id: str
    raw_lines: list = field(default_factory=list)


def test_legacy_extract_lines_are_found_per_channel():
    index = ad.legacy_index([
        _RawDoc("C1", [_Line("[첨부추출:주간보고.hwpx] 금액은 1,200만원")]),
        _RawDoc("C2", [_Line("[첨부본문:메모.txt] 한 줄")]),
    ])

    assert index.has("C1", "주간보고.hwpx")
    assert index.has("C2", "메모.txt")
    # 채널이 다르면 다른 첨부다.
    assert not index.has("C2", "주간보고.hwpx")


def test_a_plain_line_is_not_mistaken_for_an_attachment():
    index = ad.legacy_index([_RawDoc("C1", [_Line("[첨부추출 이야기] 그냥 대화")])])
    assert index.by_channel == {}


def test_an_attachment_already_in_raw_is_not_counted_again():
    """이것이 이 모듈의 핵심이다.

    raw 줄과 정본 문서가 둘 다 근거로 잡히면 답이 「두 군데서 확인됨」 처럼 보이는데
    사실은 한 군데다.
    """
    legacy = ad.legacy_index([_RawDoc("C1", [_Line("[첨부추출:주간보고.hwpx] 금액")])])

    assert ad.usable_evidence([_doc()], legacy) == []


def test_a_new_attachment_not_in_raw_is_counted():
    """다리는 스스로 걷힌다 — 새 writer 가 raw 에 본문을 안 쓰면 아무것도 안 걸린다."""
    legacy = ad.legacy_index([_RawDoc("C1", [_Line("사람이 쓴 줄")])])

    (got,) = ad.usable_evidence([_doc()], legacy)
    assert got.file_id == "F1"


def test_the_same_name_in_another_channel_is_still_counted():
    legacy = ad.legacy_index([_RawDoc("C9", [_Line("[첨부추출:주간보고.hwpx] 금액")])])
    assert len(ad.usable_evidence([_doc()], legacy)) == 1


def test_only_the_newest_revision_is_evidence():
    """옛 판을 지우지 않는 것은 되짚기 위해서지 근거로 쓰기 위해서가 아니다."""
    old = _doc(revision="r1", staged_at="2026-09-01T00:00:00+00:00", text="옛 본문")
    new = _doc(revision="r2", staged_at="2026-09-23T00:00:00+00:00", text="새 본문")

    (got,) = ad.usable_evidence([old, new], ad.LegacyIndex())
    assert got.revision == "r2"


def test_an_unknown_timestamp_does_not_displace_a_known_one():
    """모르는 것으로 아는 것을 덮으면 최신이 옛것으로 밀린다."""
    known = _doc(revision="r2", staged_at="2026-09-23T00:00:00+00:00")
    unknown = _doc(revision="r3", staged_at="")

    (got,) = ad.usable_evidence([known, unknown], ad.LegacyIndex())
    assert got.revision == "r2"


def test_a_document_without_body_is_not_evidence():
    """상태는 문서에 남아 있고, 그건 화면이 본다. 근거는 아니다."""
    assert ad.usable_evidence([_doc(text="", conversion_state=ad.BLOCKED)], ad.LegacyIndex()) == []


def test_different_files_are_all_counted():
    docs = [_doc(file_id="F1", name="a.hwpx"), _doc(file_id="F2", name="b.hwpx")]
    assert len(ad.usable_evidence(docs, ad.LegacyIndex())) == 2
