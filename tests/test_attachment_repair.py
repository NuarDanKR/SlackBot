"""깨진 첨부 정본의 권한 칸 복구 — **고칠 수 있는 것과, 고치면 안 되는 것.**

결정: 2026-09-26 오너 지시 2번.

옛 `render` 가 ACL 을 대괄호 없이 적어 파서가 주석으로 읽었다 — 빈 ACL 정본이
디스크에 남아 있다. reader 는 이제 거절하지만, 거절은 그 자료를 **답변에서
사라지게** 한다. 사라진 것과 없는 것은 화면에서 같아 보인다.

고치는 값은 채널 문서에서만 온다. 모르면 안 고치고, 안 고친 것이 남아 있는 동안은
재색인을 막는다 — 절반만 고친 색인은 무엇이 빠졌는지 알 수 없다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tybot.archive import attachment_doc
from tybot.archive import attachment_reader as reader
from tybot.archive import attachment_repair as repair
from tybot.archive.store import ArchiveStore

WS, CH, CHANNEL = "tyit", "C0FUND", "#팀_자금(ABB540)_주간보고"


def _doc(**over) -> attachment_doc.AttachmentDoc:
    base = {
        "workspace": WS, "channel_id": CH, "channel": CHANNEL,
        "file_id": "F1", "name": "기성내역.xlsx", "revision": "aaaa11112222",
        "visibility": "private", "acl": frozenset({CHANNEL}),
        "text": "9월 기성 청구액은 15억입니다", "conversion_state": attachment_doc.CONVERTED,
        "sha256": "a" * 64, "staged_at": "2026-09-22T10:00:00+00:00",
    }
    return attachment_doc.AttachmentDoc(**(base | over))


def _write(root: Path, doc, *, mangle=None) -> Path:
    """정본 한 장. `mangle` 은 **옛 writer 가 냈던 모양**을 흉내 낸다."""
    path = root / doc.relative_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    text = attachment_doc.render(doc)
    if mangle:
        text = mangle(text)
    path.write_text(text, encoding="utf-8")
    return path


def _empty_acl(text: str) -> str:
    return text.replace(f"acl: [{CHANNEL}]", "acl:")


class _Channel:
    def __init__(self, visibility="private", acl=(CHANNEL,), channel_id=CH, workspace=WS):
        self.workspace = workspace
        self.channel_id = channel_id
        self.visibility = visibility
        self.acl = frozenset(acl)


@pytest.fixture
def archive(tmp_path):
    return tmp_path / "archive"


# --- 무엇을 세나 ----------------------------------------------------------------

def test_a_healthy_document_is_not_a_finding(archive):
    """정상 문서가 목록에 오르면 건수가 의미를 잃는다."""
    _write(archive, _doc())

    assert repair.survey(archive, [_Channel()]) == []


def test_an_empty_acl_is_found_and_fixable(archive):
    _write(archive, _doc(), mangle=_empty_acl)

    found = repair.survey(archive, [_Channel()])

    assert len(found) == 1
    assert found[0].fixable
    assert found[0].fix == ("private", frozenset({CHANNEL}))


def test_an_unknown_visibility_is_found(archive):
    """`공개` 같은 값은 **모르는 값**이고, 모르면 막는다."""
    _write(archive, _doc(visibility="공개"))

    found = repair.survey(archive, [_Channel()])

    assert len(found) == 1
    assert "visibility" in found[0].problem


# --- 모르면 안 고친다 ------------------------------------------------------------

def test_an_unknown_channel_is_not_fixable(archive):
    """채널 문서를 못 찾으면 고칠 값이 없다. 추측하면 권한을 지어내는 것이다."""
    _write(archive, _doc(), mangle=_empty_acl)

    found = repair.survey(archive, [])

    assert found and not found[0].fixable
    assert repair.apply_fix(found[0]) is False


def test_a_channel_whose_own_acl_is_empty_is_not_fixable(archive):
    """그 값으로 고치면 같은 문제가 그대로 남는다 — 고친 척만 한다."""
    _write(archive, _doc(), mangle=_empty_acl)

    found = repair.survey(archive, [_Channel(acl=())])

    assert found and not found[0].fixable


def test_channels_that_disagree_are_not_fixable(archive):
    """어느 쪽이 참인지 고를 근거가 없다. 넓은 쪽을 고르면 권한이 넓어진다."""
    _write(archive, _doc(), mangle=_empty_acl)

    found = repair.survey(
        archive, [_Channel(acl=(CHANNEL,)), _Channel(acl=("#다른방",))]
    )

    assert found and not found[0].fixable


def test_a_workspace_with_the_same_channel_id_is_not_a_source(archive):
    """다른 워크스페이스의 같은 채널 ID 로 고치면 회사 경계를 넘는다(원칙 4)."""
    _write(archive, _doc(), mangle=_empty_acl)

    found = repair.survey(archive, [_Channel(workspace="tyfin")])

    assert found and not found[0].fixable


# --- 고친 뒤 ---------------------------------------------------------------------

def test_a_repaired_document_becomes_evidence_again(archive):
    """복구의 성패는 **reader 가 다시 읽는가** 다. 고친 모양이 다르면 또 빈다."""
    _write(archive, _doc(), mangle=_empty_acl)
    assert reader.evidence(archive, []) == []

    found = repair.survey(archive, [_Channel()])
    assert repair.apply_fix(found[0]) is True

    got = reader.evidence(archive, [])
    assert [doc.acl for doc in got] == [frozenset({CHANNEL})]
    assert repair.survey(archive, [_Channel()]) == []


def test_the_body_is_not_touched(archive):
    """정본 본문은 변환 결과다. 고치는 것은 원문 편집과 같은 무게다(원칙 1)."""
    path = _write(archive, _doc(), mangle=_empty_acl)
    before = path.read_text(encoding="utf-8").split("\n---\n")[1]

    repair.apply_fix(repair.survey(archive, [_Channel()])[0])

    assert path.read_text(encoding="utf-8").split("\n---\n")[1] == before


def test_a_survey_alone_changes_nothing(archive):
    """dry-run 이 기본이다. 세기만 해서는 한 글자도 안 바뀐다."""
    path = _write(archive, _doc(), mangle=_empty_acl)
    before = path.read_text(encoding="utf-8")

    repair.survey(archive, [_Channel()])

    assert path.read_text(encoding="utf-8") == before


def test_a_missing_acl_line_is_written_in(archive):
    """칸 자체가 없는 옛 문서도 있다. 고치려면 **없는 줄을 넣어야** 한다."""
    path = _write(archive, _doc(), mangle=lambda t: t.replace(f"acl: [{CHANNEL}]\n", ""))

    found = repair.survey(archive, [_Channel()])
    assert repair.apply_fix(found[0]) is True

    assert f"acl: [{CHANNEL}]" in path.read_text(encoding="utf-8")
    assert len(reader.evidence(archive, [])) == 1


def test_a_document_without_frontmatter_is_left_alone():
    """프론트매터가 없으면 어디를 고칠지 모른다. 본문에 칸을 끼워 넣지 않는다."""
    assert repair.rewrite_rights("# 제목\n본문", "private", frozenset({CHANNEL})) is None


# --- 재색인을 막는가 -------------------------------------------------------------

def test_nothing_broken_blocks_nothing():
    assert repair.blocking_reason([]) == ""


def test_an_unfixable_document_blocks_reindex(archive):
    _write(archive, _doc(), mangle=_empty_acl)

    reason = repair.blocking_reason(repair.survey(archive, []))

    assert "사람이 확인" in reason


def test_a_fixable_but_unfixed_document_also_blocks_reindex(archive):
    """안 고친 문서는 「없는 자료」 로 색인되고, 나중에 고쳐도 그 사실이 안 보인다."""
    _write(archive, _doc(), mangle=_empty_acl)

    reason = repair.blocking_reason(repair.survey(archive, [_Channel()]))

    assert "repair_attachment_rights.py" in reason


def test_the_block_lifts_once_repaired(archive):
    _write(archive, _doc(), mangle=_empty_acl)
    repair.apply_fix(repair.survey(archive, [_Channel()])[0])

    assert repair.blocking_reason(repair.survey(archive, [_Channel()])) == ""


# --- 실제 채널 문서로 ------------------------------------------------------------

def test_the_repair_reads_rights_from_the_real_channel_document(archive):
    """가짜 객체가 아니라 `ArchiveStore` 가 내는 문서로도 같아야 한다."""
    from datetime import UTC, datetime

    from tybot.archive import writer

    writer.ingest(
        archive, workspace=WS, channel=CHANNEL, channel_id=CH,
        messages=[writer.IncomingMessage(datetime.now(UTC), "김자금", "사람 발언")],
        acl=[CHANNEL],
    )
    _write(archive, _doc(), mangle=_empty_acl)
    store = ArchiveStore(archive)
    channels = [
        doc for doc in store.audit_docs(dm_scope="*")
        if not reader.is_attachment_doc(doc)
    ]

    found = repair.survey(archive, channels)

    assert len(found) == 1 and found[0].fixable
    assert repair.apply_fix(found[0]) is True
    assert len(reader.evidence(archive, channels)) == 1


# --- 권한 문제가 아닌 것 ----------------------------------------------------------
#
# 2026-09-26 오너 QA 4번. 복구 대상은 권한 칸뿐이지만, **막는 대상은 reader 가
# 거절하는 전부**다. 좁게 막으면 사라진 자료를 못 본 채 재색인이 지나간다.

def test_an_unknown_conversion_state_blocks_but_is_not_fixable(archive):
    """「다 읽었다」 인지 「절반만」 인지 모르는 문서를 본문으로 쓰면 안 된다."""
    _write(archive, _doc(conversion_state="이상한상태"))

    (found,) = repair.survey(archive, [_Channel()])

    assert found.code == "unknown_state"
    assert not found.fixable
    assert "사람이 확인" in repair.blocking_reason([found])


def test_a_missing_required_field_blocks_but_is_not_fixable(archive):
    """좌표가 없는 본문은 출처도 권한도 중복 방지도 할 수 없다."""
    _write(archive, _doc(), mangle=lambda t: t.replace("file_id: F1\n", ""))

    (found,) = repair.survey(archive, [_Channel()])

    assert found.code == "missing_field"
    assert not found.fixable


def test_a_path_mismatch_blocks_but_is_not_fixable(archive):
    """경로와 좌표가 어긋나면 어느 쪽이 참인지 고를 근거가 없다."""
    doc = _doc()
    wrong = (
        archive / "workspaces" / WS / "channels" / "C_OTHER"
        / "attachments" / doc.file_id / f"{doc.revision}.md"
    )
    wrong.parent.mkdir(parents=True, exist_ok=True)
    wrong.write_text(attachment_doc.render(doc), encoding="utf-8")

    (found,) = repair.survey(archive, [_Channel()])

    assert found.code == "path_mismatch"
    assert not found.fixable


def test_the_survey_uses_the_readers_judgement(archive):
    """판정이 두 곳에 있으면 한 곳만 고치는 날이 오고, 그날 둘이 갈린다."""
    from tybot.archive import attachment_reader as reader_mod

    _write(archive, _doc(conversion_state="이상한상태"))
    path = reader_mod.source_files(archive)[0]

    problem = reader_mod.validate(reader_mod.load(path), path, archive)
    (found,) = repair.survey(archive, [_Channel()])

    assert found.code == problem.code
    assert found.problem == problem.message
