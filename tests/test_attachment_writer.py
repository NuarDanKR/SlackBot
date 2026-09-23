"""첨부를 raw 에서 떼어 따로 보관한다.

설계: `docs/design/archiving-bot-separation-2026-09-23.md` §3
회의(2026-09-23): 「Archiving Bot은 첨부파일을 별도로 저장하겠음」

## 왜 스위치가 있나
바꾸는 것은 **수집의 저장 모양**이다. 켜는 순간부터 새 첨부 본문이 raw 에 안
들어가고, 읽는 쪽이 준비되지 않았으면 그 본문은 답변에서 사라진다 — 오류 없이.
분리 결정 §6 이 그걸 중지조건으로 못박았다.

그래서 기본은 꺼짐이고, 이 파일은 **꺼졌을 때와 켜졌을 때가 각각 무엇을 쓰는지**를
고정한다.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from tybot.archive import attachment_doc as ad
from tybot.archive import attachment_writer as aw


@dataclass
class _Staged:
    """`StagedAttachmentResult` 에서 이 모듈이 보는 부분만."""

    metadata_path: Path
    lines: list[str] = field(default_factory=list)
    reference_lines: list[str] = field(default_factory=list)
    body_lines: list[str] = field(default_factory=list)


def _staged(tmp_path, file_id="F1", name="주간보고.hwpx", body="금액은 1,200만원", **meta):
    staged = tmp_path / "staging" / file_id
    staged.mkdir(parents=True, exist_ok=True)
    payload = {
        "slack_file_id": file_id,
        "name": name,
        "filetype": "hwpx",
        "sha256": "abcdef1234567890",
        "conversion_state": "succeeded",
        "staged_at": "2026-09-23T10:00:00+00:00",
    }
    payload.update(meta)
    (staged / "metadata.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    if body is not None:
        (staged / "extracted.md").write_text(f"# {name}\n\n{body}\n", encoding="utf-8")

    reference = [f"[첨부:자동변환] {name} (hwpx, 1KB) · id:{file_id}"]
    body_lines = [f"[첨부추출:{name}] {body}"] if body is not None else []
    return _Staged(
        metadata_path=staged / "metadata.json",
        lines=[*reference, *body_lines],
        reference_lines=reference,
        body_lines=body_lines,
    )


def _write(tmp_path, results):
    return aw.write_docs(
        tmp_path / "archive",
        results,
        workspace="tyit",
        channel_id="C1",
        channel="#팀-전산_abb155-공지",
        visibility="공개",
        acl=frozenset({"#팀-전산_abb155-공지"}),
    )


# --- 스위치 --------------------------------------------------------------------
def test_separation_is_off_by_default(monkeypatch):
    """읽는 쪽이 붙기 전에 켜면 본문이 조용히 답변에서 빠진다(§6 중지조건)."""
    monkeypatch.delenv("ARCHIVE_SEPARATE_ATTACHMENTS", raising=False)
    assert not aw.separate_attachments()


@pytest.mark.parametrize("value", ["1", "true", "yes", "on"])
def test_separation_can_be_turned_on(monkeypatch, value):
    monkeypatch.setenv("ARCHIVE_SEPARATE_ATTACHMENTS", value)
    assert aw.separate_attachments()


def test_raw_keeps_the_body_while_separation_is_off(monkeypatch, tmp_path):
    """켜기 전에는 지금까지 하던 그대로다. 바뀌는 날만 바뀐다."""
    monkeypatch.delenv("ARCHIVE_SEPARATE_ATTACHMENTS", raising=False)
    item = _staged(tmp_path)

    assert aw.raw_lines_for(item) == item.lines
    assert any("[첨부추출:" in line for line in aw.raw_lines_for(item))


def test_raw_keeps_only_the_reference_once_separation_is_on(monkeypatch, tmp_path):
    """본문은 정본으로 가고 원문에는 **참조만** 남는다."""
    monkeypatch.setenv("ARCHIVE_SEPARATE_ATTACHMENTS", "1")
    item = _staged(tmp_path)

    lines = aw.raw_lines_for(item)

    assert lines == item.reference_lines
    assert not any("[첨부추출:" in line for line in lines)
    # 참조는 남아야 한다 — 없으면 파일이 있었다는 사실 자체가 사라진다.
    assert "주간보고.hwpx" in lines[0]
    # 그리고 정본과 이을 좌표를 들고 있다.
    assert "id:F1" in lines[0]


def test_an_old_result_without_the_new_fields_still_keeps_its_reference(monkeypatch, tmp_path):
    """옛 결과 객체에는 `reference_lines` 가 없다. 빈 목록을 주면 흔적이 사라진다."""
    monkeypatch.setenv("ARCHIVE_SEPARATE_ATTACHMENTS", "1")
    old = _Staged(
        metadata_path=tmp_path / "x" / "metadata.json",
        lines=["[첨부:자동변환] 옛파일.pdf (pdf, 1KB)", "[첨부추출:옛파일.pdf] 본문"],
    )

    assert aw.raw_lines_for(old) == ["[첨부:자동변환] 옛파일.pdf (pdf, 1KB)"]


# --- 정본 쓰기 -----------------------------------------------------------------
def test_an_attachment_becomes_a_document_under_its_channel(tmp_path):
    (doc,) = _write(tmp_path, [_staged(tmp_path)])

    path = tmp_path / "archive" / doc.relative_path()
    assert path.exists()
    body = path.read_text(encoding="utf-8")
    assert "금액은 1,200만원" in body
    assert "kind: attachment" in body


def test_documents_are_written_even_while_separation_is_off(monkeypatch, tmp_path):
    """**켜기 전에 정본이 쌓여 있어야** 켠 날 과거 첨부가 통째로 안 보이는 일이 없다.

    그동안 본문이 두 곳에 있게 되지만, 중복 계수는 `usable_evidence()` 가 막는다.
    """
    monkeypatch.delenv("ARCHIVE_SEPARATE_ATTACHMENTS", raising=False)
    assert len(_write(tmp_path, [_staged(tmp_path)])) == 1


def test_one_broken_attachment_does_not_lose_the_others(tmp_path):
    """첨부 하나 때문에 그 메시지의 다른 첨부까지 잃으면, 사람은 「일부만
    들어왔다」 를 오류 없이 겪는다."""
    broken = _Staged(metadata_path=tmp_path / "없음" / "metadata.json")
    good = _staged(tmp_path, file_id="F2", name="보고.pdf")

    written = _write(tmp_path, [broken, good])

    assert [doc.file_id for doc in written] == ["F2"]


def test_an_attachment_without_a_file_id_is_skipped(tmp_path):
    """어느 파일인지 모르면 중복 방지도 출처 표시도 할 수 없다(§3)."""
    assert _write(tmp_path, [_staged(tmp_path, slack_file_id="")]) == []


def test_a_blocked_attachment_still_gets_a_document(tmp_path):
    """「PII 로 막혔다」 와 「그런 파일이 없다」 는 사람이 할 일이 다르다."""
    item = _staged(tmp_path, file_id="F3", body=None,
                   conversion_state="blocked", error_code="pii_refused")

    (doc,) = _write(tmp_path, [item])

    assert not doc.usable
    body = (tmp_path / "archive" / doc.relative_path()).read_text(encoding="utf-8")
    assert "개인정보" in body


def test_rewriting_the_same_revision_replaces_it(tmp_path):
    """같은 원본을 다시 변환하면 같은 판이다. 판이 쌓이면 근거가 늘어난 것처럼 보인다."""
    _write(tmp_path, [_staged(tmp_path)])
    (doc,) = _write(tmp_path, [_staged(tmp_path, body="고친 본문")])

    holder = (tmp_path / "archive" / doc.relative_path()).parent
    assert [p.name for p in sorted(holder.glob("*.md"))] == [f"{doc.revision}.md"]
    assert "고친 본문" in (tmp_path / "archive" / doc.relative_path()).read_text(encoding="utf-8")


def test_no_temporary_file_is_left_behind(tmp_path):
    """반쯤 쓰인 문서를 검색이 읽으면 프론트매터가 잘려 그 채널이 통째로 빠진다."""
    (doc,) = _write(tmp_path, [_staged(tmp_path)])

    holder = (tmp_path / "archive" / doc.relative_path()).parent
    assert not list(holder.glob("*.tmp"))


def test_the_document_is_readable_by_the_archive_schema(tmp_path):
    from tybot.archive.store import REQUIRED_FIELDS, parse_frontmatter

    (doc,) = _write(tmp_path, [_staged(tmp_path)])
    text = (tmp_path / "archive" / doc.relative_path()).read_text(encoding="utf-8")

    fm = parse_frontmatter(text)
    assert not [key for key in REQUIRED_FIELDS if key not in fm]


# --- 두 모양이 공존하는 동안 -------------------------------------------------------
def test_the_same_attachment_is_not_counted_twice_during_the_overlap(tmp_path):
    """분리를 켜기 전에는 본문이 raw 와 정본 둘 다에 있다. 그때 두 번 세면
    답이 「두 군데서 확인됨」 처럼 보이는데 사실은 한 군데다."""
    (doc,) = _write(tmp_path, [_staged(tmp_path)])

    @dataclass
    class _RawDoc:
        channel_id: str
        raw_lines: list

    @dataclass
    class _Line:
        text: str

    legacy = ad.legacy_index([
        _RawDoc("C1", [_Line("[첨부추출:주간보고.hwpx] 금액은 1,200만원")])
    ])

    assert ad.usable_evidence([doc], legacy) == []
