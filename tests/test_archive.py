"""아카이브 스키마/검색/수집 — 절대 원칙이 코드로 지켜지는지 검증."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tybot.access import RequestContext
from tybot.archive import writer
from tybot.archive.store import ArchiveStore, SchemaError, validate

DOC = """---
workspace: pilot
channel: "#팀_자금(ABB540)_주간보고"
visibility: private
acl: [#팀_자금(ABB540)_주간보고]
doc_count: 2
last_ingested: 2026-08-19T17:00+09:00
---

## 요약 (사람이 관리, 봇은 수정 금지)
- 요약에만 있는 유령숫자 999억

## 원문 (자동 취합, 편집 금지)
> [2026-08-12 09:15] 홍길동: 김해외동 기성금 3억 2천만원 청구했습니다
> [2026-08-12 09:20] 이순신: 승인 났습니다
"""


def _write(tmp_path, name="주간보고.md", text=DOC):
    p = tmp_path / "channels" / "pilot" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _ctx(channels=("#팀_자금(ABB540)_주간보고",), role="member"):
    return RequestContext(workspace="pilot", channels=frozenset(channels), role=role)


def test_validate_requires_frontmatter_and_raw_section():
    with pytest.raises(SchemaError):
        validate("본문만 있음")
    with pytest.raises(SchemaError):
        validate("---\nworkspace: p\nchannel: c\nvisibility: private\nacl: [c]\n---\n\n## 요약\n")


def test_search_returns_raw_lines_only(tmp_path):
    _write(tmp_path)
    hits = ArchiveStore(tmp_path).search("기성금", _ctx())
    assert len(hits) == 1
    assert "3억 2천만원" in hits[0].line.text
    # 2겹: 요약 섹션은 근거가 아니다
    assert not ArchiveStore(tmp_path).search("유령숫자", _ctx())


def test_citation_format(tmp_path):
    _write(tmp_path)
    hit = ArchiveStore(tmp_path).search("기성금", _ctx())[0]
    assert hit.citation() == "#팀_자금(ABB540)_주간보고, 📄주간보고.md(2026-08-12)"


def test_acl_blocks_non_member(tmp_path):
    _write(tmp_path)
    store = ArchiveStore(tmp_path)
    assert store.search("기성금", _ctx(channels=())) == []
    assert store.titles(_ctx(channels=())) == []
    # 3겹: 권한 있으면 0건 폴백용 제목 목록이 나온다
    assert store.titles(_ctx()) == ["#팀_자금(ABB540)_주간보고"]


def test_cross_workspace_blocked(tmp_path):
    _write(tmp_path)
    other = RequestContext(workspace="other", channels=frozenset({"#팀_자금(ABB540)_주간보고"}))
    assert ArchiveStore(tmp_path).search("기성금", other) == []


def _msg(text, speaker="홍길동", is_bot=False, minute=15):
    return writer.IncomingMessage(
        ts=datetime(2026, 8, 12, 0, minute, tzinfo=UTC),
        speaker=speaker,
        text=text,
        is_bot=is_bot,
    )


def test_ingest_skips_bot_output_and_pii(tmp_path):
    r = writer.ingest(
        tmp_path,
        workspace="pilot",
        channel="#팀_자금(ABB540)_주간보고",
        messages=[
            _msg("기성금 3억 청구"),
            _msg("요약드리면...", speaker="태봇", is_bot=True, minute=16),
            _msg("주민등록번호 900101-1234567 입니다", minute=17),
            _msg("등기부등본 첨부합니다", minute=18),
        ],
        acl=["#팀_자금(ABB540)_주간보고"],
    )
    assert r.written == 1
    assert r.skipped_bot == 1
    assert len(r.refused) == 2
    text = r.path.read_text(encoding="utf-8")
    assert "태봇" not in text and "900101" not in text
    validate(text, path=str(r.path))


def test_ingest_is_idempotent_and_append_only(tmp_path):
    args = dict(workspace="pilot", channel="#a_b", acl=["#a_b"])
    writer.ingest(tmp_path, messages=[_msg("첫 메시지")], **args)
    first = writer.doc_path(tmp_path, "pilot", "#a_b").read_text(encoding="utf-8")
    r2 = writer.ingest(tmp_path, messages=[_msg("첫 메시지"), _msg("둘째", minute=30)], **args)
    assert r2.written == 1
    after = r2.path.read_text(encoding="utf-8")
    assert first.split("## 원문")[1].strip() in after  # 기존 원문 라인 보존
    assert after.count("첫 메시지") == 1
