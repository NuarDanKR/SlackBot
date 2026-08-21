"""아카이브 파싱 캐시 — 재파싱을 줄이면서도 낡은 내용을 답하지 않는지 검증."""
from __future__ import annotations

import time

from tybot.access import RequestContext
from tybot.archive.store import ArchiveStore

DOC = """---
workspace: pilot
channel: "#팀_자금(ABB540)_주간보고"
visibility: public
acl: []
doc_count: 1
last_ingested: 2026-08-19T17:00+09:00
---

## 원문 (자동 취합, 편집 금지)
> [2026-08-12 09:15] 홍길동: 김해외동 기성금 3억 2천만원 청구했습니다
"""

CTX = RequestContext(workspace="pilot")


def _write(tmp_path, text=DOC, name="주간보고.md"):
    d = tmp_path / "channels" / "pilot"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return p


def test_repeated_reads_parse_file_once(tmp_path, monkeypatch):
    """한 질문이 visible_docs 를 여러 번 불러도 파일은 한 번만 파싱한다."""
    _write(tmp_path)
    store = ArchiveStore(tmp_path)

    from tybot.archive import store as store_mod

    calls = {"n": 0}
    real = store_mod.load_doc

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(store_mod, "load_doc", counting)

    store.search("기성금", CTX)
    store.titles(CTX)
    store.docs()
    assert calls["n"] == 1


def test_appended_line_is_visible_immediately(tmp_path):
    """수집기가 append 하면 캐시를 무시하고 새 원문을 답한다(낡은 답 금지)."""
    path = _write(tmp_path)
    store = ArchiveStore(tmp_path)
    assert not store.search("착공일", CTX)

    time.sleep(0.01)  # mtime 해상도 여유
    path.write_text(
        DOC + "> [2026-08-13 10:00] 이순신: 착공일은 9월 1일입니다\n", encoding="utf-8"
    )

    hits = store.search("착공일", CTX)
    assert len(hits) == 1
    assert "9월 1일" in hits[0].line.text


def test_new_file_is_picked_up(tmp_path):
    """캐시가 파일 목록을 고정하지 않는다 — 새 채널 문서도 보인다."""
    _write(tmp_path)
    store = ArchiveStore(tmp_path)
    assert len(store.docs()) == 1

    _write(tmp_path, text=DOC.replace('"#팀_자금(ABB540)_주간보고"', '"#현장_김해외동(180182)"'),
           name="김해외동.md")
    assert len(store.docs()) == 2


def test_deleted_file_disappears(tmp_path):
    """삭제된 문서를 캐시에서 계속 답하지 않는다."""
    path = _write(tmp_path)
    store = ArchiveStore(tmp_path)
    assert store.docs()

    path.unlink()
    assert store.docs() == []
    assert store.titles(CTX) == []


def test_broken_doc_is_reported_not_answered(tmp_path):
    """형식 위반은 근거로 쓰지 않고 broken() 으로 드러낸다(조용한 0건 금지)."""
    _write(tmp_path, text="프론트매터 없는 파일\n> [2026-08-12 09:15] 홍길동: 기성금 3억\n")
    store = ArchiveStore(tmp_path)

    assert store.docs() == []
    assert not store.search("기성금", CTX)
    broken = store.broken()
    assert len(broken) == 1
    assert "프론트매터" in broken[0][1]


def test_fixed_doc_stops_being_broken(tmp_path):
    """형식 위반 결과도 캐시하지만, 고치면 곧바로 반영된다."""
    path = _write(tmp_path, text="깨진 파일\n")
    store = ArchiveStore(tmp_path)
    assert store.broken()

    time.sleep(0.01)
    path.write_text(DOC, encoding="utf-8")
    assert store.broken() == []
    assert len(store.docs()) == 1
