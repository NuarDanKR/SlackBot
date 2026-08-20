"""공개 전환 도구 — 프론트매터만 바꾸고 원문은 절대 건드리지 않는지."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from share import set_visibility  # noqa: E402
from tybot.archive.store import SchemaError, load_doc  # noqa: E402

DOC = """---
workspace: pilot
channel: "#프로젝트-업데이트"
visibility: private
acl: [#프로젝트-업데이트]
doc_count: 2
last_ingested: 2026-08-19T17:00+09:00
---

## 요약 (사람이 관리, 봇은 수정 금지)
- 사람이 쓴 요약

## 원문 (자동 취합, 편집 금지)
> [2026-08-19 09:15] 홍길동: 기성금 3억 2천만원 청구 완료
> [2026-08-19 10:00] 이순신: 승인 났습니다
"""


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "프로젝트-업데이트.md"
    p.write_text(DOC, encoding="utf-8")
    return p


def test_flip_to_public_changes_only_frontmatter(doc):
    before = doc.read_text(encoding="utf-8")
    set_visibility(doc, "public")
    after = doc.read_text(encoding="utf-8")

    assert load_doc(doc).visibility == "public"
    # 원문 블록은 바이트 단위로 동일해야 한다
    assert before.split("## 원문", 1)[1] == after.split("## 원문", 1)[1]
    # 요약 섹션도 그대로
    assert "사람이 쓴 요약" in after
    assert after.count("visibility:") == 1


def test_round_trip_restores_original(doc):
    original = doc.read_text(encoding="utf-8")
    set_visibility(doc, "public")
    set_visibility(doc, "private")
    assert doc.read_text(encoding="utf-8") == original


def test_dry_run_writes_nothing(doc):
    original = doc.read_text(encoding="utf-8")
    msg = set_visibility(doc, "public", dry_run=True)
    assert "예정" in msg
    assert doc.read_text(encoding="utf-8") == original


def test_no_op_when_already_set(doc):
    assert "변경없음" in set_visibility(doc, "private")


def test_raw_lines_preserved_count(doc):
    n = len(load_doc(doc).raw_lines)
    set_visibility(doc, "public")
    assert len(load_doc(doc).raw_lines) == n == 2


def test_broken_schema_is_refused(tmp_path):
    p = tmp_path / "깨진.md"
    p.write_text("프론트매터 없음\n\n## 원문\n> [2026-08-19 09:00] a: b\n", encoding="utf-8")
    with pytest.raises(SchemaError):
        set_visibility(p, "public")


def test_missing_visibility_line_is_refused(tmp_path):
    p = tmp_path / "필드누락.md"
    p.write_text(
        DOC.replace("visibility: private\n", ""), encoding="utf-8"
    )
    with pytest.raises(SchemaError):
        set_visibility(p, "public")
