"""권한 분포 진단기. **세는 것과 안 담는 것**을 함께 건다."""
from __future__ import annotations

import pytest

from tybot.archive.attachment_doc import AttachmentDoc
from tybot.archive.image_description_acl_census import (
    BUCKETS,
    NOT_AN_IMAGE,
    PRIVATE_EMPTY_ACL,
    PRIVATE_WITH_ACL,
    PUBLIC_EMPTY_ACL,
    PUBLIC_WITH_ACL,
    UNKNOWN_VISIBILITY,
    UNREADABLE_ACL,
    AclCensus,
    take_census,
)
from tybot.archive.image_description_source import FILETYPE_MIME

#: 결과에 **절대 나오면 안 되는** 값들.
FILENAME = "주민등록증-사본.png"
CHANNEL = "#팀_전산(ABB540)_공지"
MEMBER = "U_HONG_GILDONG"
BODY = "계약 금액 1,200,000,000원"


def doc(**over) -> AttachmentDoc:
    base = dict(
        workspace="tyit", channel_id="C0BQYAC28F3", channel=CHANNEL,
        file_id="F0C9LHFTHLM", name=FILENAME, revision="r1",
        visibility="private", acl=frozenset({MEMBER}), text=BODY,
        filetype="png", sha256="a" * 64, conversion_state="converted",
    )
    return AttachmentDoc(**(base | over))


def count(*docs) -> dict:
    return take_census(docs).counts


# --- 칸 나누기 ---------------------------------------------------------------


@pytest.mark.parametrize("filetype", sorted(FILETYPE_MIME))
def test_every_filetype_the_selector_takes_is_counted_as_an_image(filetype):
    """선별기와 **같은 규칙**이어야 한다. 갈라지면 없는 문제를 가리킨다."""
    assert count(doc(filetype=filetype))[NOT_AN_IMAGE] == 0


@pytest.mark.parametrize("filetype", ["pdf", "hwp", "xlsx", "", "PNG.bak", "gif"])
def test_other_filetypes_land_in_not_an_image(filetype):
    assert count(doc(filetype=filetype))[NOT_AN_IMAGE] == 1


@pytest.mark.parametrize("filetype", ["PNG", " jpeg ", "JPG"])
def test_the_filetype_spelling_is_tidied_before_counting(filetype):
    assert count(doc(filetype=filetype))[NOT_AN_IMAGE] == 0


@pytest.mark.parametrize("visibility,acl,bucket", [
    ("public", frozenset(), PUBLIC_EMPTY_ACL),
    ("public", frozenset({MEMBER}), PUBLIC_WITH_ACL),
    ("private", frozenset(), PRIVATE_EMPTY_ACL),
    ("private", frozenset({MEMBER}), PRIVATE_WITH_ACL),
    ("private", set(), PRIVATE_EMPTY_ACL),
    ("private", {MEMBER}, PRIVATE_WITH_ACL),
])
def test_the_four_minimum_buckets(visibility, acl, bucket):
    said = count(doc(visibility=visibility, acl=acl))
    assert said[bucket] == 1
    assert sum(said.values()) == 1


@pytest.mark.parametrize("visibility", ["", "   ", "internal", "공개", "Public?"])
def test_a_visibility_we_do_not_know_is_unknown_not_public(visibility):
    """빈 값은 「공개」 가 아니라 **모름**이다(원칙 3)."""
    assert count(doc(visibility=visibility))[UNKNOWN_VISIBILITY] == 1


@pytest.mark.parametrize("visibility", ["PUBLIC", " public ", "Private"])
def test_the_visibility_spelling_is_tidied_before_counting(visibility):
    said = count(doc(visibility=visibility, acl=frozenset()))
    assert said[UNKNOWN_VISIBILITY] == 0


@pytest.mark.parametrize("acl", [None, MEMBER, ["U_ONE"], ("U_ONE",), 7])
def test_an_acl_that_is_not_a_set_is_unreadable_not_empty(acl):
    """빈 것과 **섞으면 안 된다** — 섞으면 「고치면 열리는 수」 를 부풀린다."""
    assert count(doc(acl=acl))[UNREADABLE_ACL] == 1


def test_a_non_set_acl_never_counts_as_having_members():
    """문자열을 순회하면 글자가 멤버가 된다. 없는 권한을 있다고 세면 안 된다."""
    said = count(doc(visibility="private", acl=MEMBER))
    assert said[PRIVATE_WITH_ACL] == 0
    assert said[PRIVATE_EMPTY_ACL] == 0


def test_not_an_image_wins_over_a_broken_acl():
    """칸은 **정확히 하나**다. 이미지가 아니면 권한을 세지 않는다."""
    said = count(doc(filetype="pdf", visibility="", acl=None))
    assert said[NOT_AN_IMAGE] == 1
    assert sum(said.values()) == 1


# --- 합계 --------------------------------------------------------------------


def test_each_document_lands_in_exactly_one_bucket():
    docs = [
        doc(visibility="public", acl=frozenset()),
        doc(visibility="public", acl=frozenset({MEMBER})),
        doc(visibility="private", acl=frozenset()),
        doc(visibility="private", acl=frozenset({MEMBER})),
        doc(visibility=""),
        doc(filetype="pdf"),
        doc(acl=None),
    ]
    said = take_census(docs)
    assert said.total == len(docs)
    assert sum(said.counts.values()) == len(docs)


def test_the_image_total_excludes_non_images():
    said = take_census([doc(), doc(filetype="pdf"), doc(filetype="hwp")])
    assert (said.total, said.image_total) == (3, 1)


def test_the_blocked_count_is_what_filling_in_acl_would_open():
    said = take_census([
        doc(visibility="public", acl=frozenset()),
        doc(visibility="private", acl=frozenset()),
        doc(visibility="private", acl=frozenset({MEMBER})),
        doc(visibility=""),          # 모름은 ACL 만 채워서 안 열린다
        doc(acl=None),               # 읽을 수 없는 것도 아니다
    ])
    assert said.blocked_by_empty_acl == 2


def test_an_empty_list_counts_every_bucket_as_zero():
    """「0 건」 과 「안 세어 봤다」 가 같아 보이면 안 된다."""
    said = take_census([])
    assert said.total == 0
    assert set(said.counts) == set(BUCKETS)
    assert all(value == 0 for value in said.counts.values())


def test_none_is_treated_as_nothing_to_count():
    assert take_census(None).total == 0


def test_the_rows_keep_a_fixed_order():
    """개수 순으로 정렬하면 실행마다 줄이 움직여 변화를 못 읽는다."""
    said = take_census([doc(), doc(filetype="pdf")])
    assert tuple(name for name, _ in said.rows) == BUCKETS


def test_a_generator_is_accepted():
    said = take_census(doc() for _ in range(3))
    assert said.total == 3


# --- 담지 않는 것 ------------------------------------------------------------


def test_nothing_identifying_reaches_the_result():
    """본문·파일명·경로·사용자·채널 이름이 결과에 없어야 한다."""
    said = take_census([doc(), doc(filetype="pdf"), doc(visibility="")])
    printed = repr(said) + str(said.counts) + str(said.rows)
    for secret in (FILENAME, CHANNEL, MEMBER, BODY, "tyit", "C0BQYAC28F3", "F0C9LHFTHLM"):
        assert secret not in printed, secret


def test_the_result_holds_only_bucket_names_and_numbers():
    said = take_census([doc()])
    assert set(said.counts) == set(BUCKETS)
    assert all(isinstance(value, int) for value in said.counts.values())
    assert all(name.isascii() for name in said.counts)


def _code_lines() -> list[str]:
    import inspect

    from tybot.archive import image_description_acl_census as mod

    source = inspect.getsource(mod).replace(mod.__doc__ or "", "")
    return [line for line in source.splitlines() if not line.strip().startswith("#")]


def test_the_module_never_reads_a_body_name_or_path():
    """센 뒤에 담지 않는 것으로는 부족하다 — **읽지도 않는다.**"""
    forbidden = ("doc.text", '"text"', "doc.name", '"name"', "source_path",
                 "permalink", "channel", "workspace", "file_id", "sha256")
    hits = [line for line in _code_lines() for word in forbidden if word in line]
    assert hits == [], hits


def test_the_module_opens_no_file_or_network():
    forbidden = ("open(", "Path(", "requests", "httpx", "socket", "urllib",
                 "psycopg", "subprocess", "load_original", "router")
    hits = [line for line in _code_lines() for word in forbidden if word in line]
    assert hits == [], hits


def test_the_inputs_are_never_modified():
    said = doc()
    before = (said, said.acl, said.visibility)
    take_census([said])
    assert (said, said.acl, said.visibility) == before


def test_the_census_is_a_plain_value():
    """돌려준 것을 고쳐도 다음 집계에 영향이 없어야 한다."""
    first = take_census([doc()])
    first.counts[PRIVATE_WITH_ACL] = 999
    assert take_census([doc()]).counts[PRIVATE_WITH_ACL] == 1
    assert isinstance(first, AclCensus)
