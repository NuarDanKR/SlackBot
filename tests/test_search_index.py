"""검색 색인 (B-40).

핵심 성질 네 가지를 지킨다.
  1. DB 를 못 읽는 것과 색인에 없는 것을 **구별한다**
  2. 점수는 한 곳에서만 만든다 — 색인 경로와 파일 경로가 갈리면 안 된다
  3. 권한은 코드가 소유한다. 색인은 통과한 채널만 받는다
  4. 색인이 비어 있어도 답이 나온다(파일 스캔 폴백)
"""
from __future__ import annotations

import pytest

from tybot import search_index
from tybot.access import RequestContext
from tybot.archive.store import ArchiveStore

MINE = "#팀_전산(ABB155)_주간보고"
OTHER = "#현장_김해외동(180182)_채팅방"


def _doc(channel: str, lines: list[tuple[str, str, str]]) -> str:
    body = "\n".join(f"> [{ts}] {who}: {text}" for ts, who, text in lines)
    return (
        "---\n"
        f"workspace: pilot\n"
        f'channel: "{channel}"\n'
        "visibility: private\n"
        f'acl: ["{channel}"]\n'
        "doc_count: 1\n"
        "last_ingested: 2026-08-19T17:00+09:00\n"
        "---\n\n"
        "## 요약 (사람이 관리, 봇은 수정 금지)\n-\n\n"
        "## 원문 (자동 취합, 편집 금지)\n"
        f"{body}\n"
    )


@pytest.fixture
def store(tmp_path, monkeypatch):
    """DB 없는 환경. 색인 경로가 아니라 파일 스캔이 도는 상태다."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "channels" / "pilot").mkdir(parents=True)
    (tmp_path / "channels" / "pilot" / "전산.md").write_text(
        _doc(MINE, [
            ("2026-08-01 09:00", "홍길동", "기성금 1억 청구했습니다"),
            ("2026-08-20 09:00", "홍길동", "기성금 3억으로 정정합니다"),
        ]),
        encoding="utf-8",
    )
    (tmp_path / "channels" / "pilot" / "김해외동.md").write_text(
        _doc(OTHER, [("2026-08-10 09:00", "김철수", "기성금 5억 청구")]),
        encoding="utf-8",
    )
    return ArchiveStore(tmp_path)


def _ctx(*channels: str) -> RequestContext:
    return RequestContext(workspace="pilot", channels=frozenset(channels))


# --- 1. 못 읽음 vs 없음 -----------------------------------------------------
def test_no_database_is_not_the_same_as_no_match(monkeypatch):
    """빈 목록은 '색인에 없다', None 은 '색인을 못 봤다'.

    섞으면 DB 장애가 「자료를 찾지 못했습니다」 로 나가고, 그건 정상 답처럼 보인다.
    """
    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert search_index.candidates("기성금", [MINE]) is None


def test_a_failing_query_falls_back_instead_of_raising(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/none")

    assert search_index.candidates("기성금", [MINE]) is None


# --- 2. 점수는 한 곳 --------------------------------------------------------
def test_token_rule_lives_in_one_place():
    """규칙이 두 곳에 있으면 색인 후보와 파일 스캔이 다른 토큰으로 찾는다.

    에러는 안 나고 **같은 질문에 다른 답**으로 나타난다.
    """
    import tybot.archive.store as store_mod

    assert not hasattr(store_mod, "TOKEN_RE"), "store 에 토큰 규칙을 다시 두지 않는다"
    assert search_index.tokens_of("기성 a 금액") == ["기성", "금액"], "1자는 토큰이 아니다"


def test_phrase_match_outranks_scattered_tokens():
    scattered = search_index.score_line(
        ["기성", "정정"], "기성 정정", "홍길동", "정정할 것이 있고 기성 관련입니다"
    )
    phrase = search_index.score_line(
        ["기성", "정정"], "기성 정정", "홍길동", "기성 정정 요청드립니다"
    )

    assert phrase > scattered


def test_a_line_with_nothing_matching_scores_zero():
    assert search_index.score_line(["기성"], "기성", "홍길동", "회의 잡겠습니다") == 0


# --- 3. 권한은 코드가 --------------------------------------------------------
def test_search_still_filters_by_permission(store):
    """색인을 쓰든 파일을 훑든 권한 판정은 `visible_docs` 가 한다."""
    hits = store.search("기성금", _ctx(MINE))

    assert hits
    assert all(h.doc.channel == MINE for h in hits)
    assert not any("5억" in h.line.text for h in hits), "권한 밖 채널이 섞였다"


def test_candidates_never_decides_permission(monkeypatch):
    """이 함수에 채널을 안 주면 아무것도 찾지 못해야 한다 — 스스로 넓히지 않는다."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://nowhere/none")

    assert search_index.candidates("기성금", []) is None


# --- 4. 색인이 없어도 답이 나온다 -------------------------------------------
def test_search_works_with_no_index_at_all(store):
    hits = store.search("기성금", _ctx(MINE))

    assert hits, "색인이 없으면 파일 스캔으로 답해야 한다"


def test_same_score_puts_the_newer_line_first(store):
    """예전에는 점수 다음이 파일명이었다.

    같은 점수면 오래된 줄이 먼저 올라와, 바뀐 숫자를 묻는 질문에 **옛 값이 근거로**
    붙었다. 1억 → 3억 으로 정정된 채널이 그 경우다.
    """
    hits = store.search("기성금", _ctx(MINE))

    assert hits[0].line.ts.startswith("2026-08-20"), (
        f"최근 줄이 먼저 와야 한다: {[h.line.ts for h in hits]}"
    )


# --- 색인 최신성 (설계 §2.2·§7.2) ----------------------------------------------
#
# 색인에 **일부** 결과가 있으면 예전에는 거기서 끝났다. 그래서 방금 들어온 줄(예:
# 재변환한 첨부)이 조용히 검색에서 빠졌다 — 오류도 0건도 아니라 「예전 것만 나오는」
# 답이 된다. 그게 가장 찾기 어려운 실패다.
def _candidate(doc_path, line_no, when="2026-08-01 09:00"):
    return search_index.Candidate(doc_path=doc_path, line_no=line_no, spoken_at=when)


@pytest.fixture
def indexed(store, monkeypatch):
    """색인이 첫 줄 하나만 알고 있는 상태."""
    path = search_index.rel_path(
        store.root / "channels" / "pilot" / "전산.md", store.root
    )
    doc = next(d for d in store.source_docs() if d.channel == MINE)
    first = doc.raw_lines[0]
    monkeypatch.setattr(
        search_index, "candidates", lambda q, ch: [_candidate(path, first.lineno)]
    )
    return path, doc


def test_stale_document_lines_are_merged_from_the_file(indexed, store, monkeypatch):
    path, _doc = indexed
    # 색인은 1줄만 알고 파일에는 2줄이 있다 → 낡았다.
    monkeypatch.setattr(search_index, "indexed_counts", lambda paths: {path: 1})
    got = store.search("기성금", _ctx(MINE))
    assert len(got) == 2
    assert any("3억" in h.line.text for h in got)


def test_a_fresh_index_is_not_rescanned(indexed, store, monkeypatch):
    """최신이면 파일을 다시 훑지 않는다. 매번 훑으면 색인이 무의미해진다."""
    path, doc = indexed
    scanned = []
    original = ArchiveStore._scan

    def _spy(self, query, tokens, docs, limit):
        scanned.append(len(docs))
        return original(self, query, tokens, docs, limit)

    monkeypatch.setattr(ArchiveStore, "_scan", _spy)
    monkeypatch.setattr(
        search_index, "indexed_counts", lambda paths: {path: len(doc.raw_lines)}
    )
    got = store.search("기성금", _ctx(MINE))
    assert len(got) == 1
    assert scanned == []


def test_unreadable_db_does_not_trigger_a_full_scan(indexed, store, monkeypatch):
    """이미 색인 결과를 받은 뒤다. 두 번 부담을 지우는 대신 있는 것으로 답한다."""
    monkeypatch.setattr(search_index, "indexed_counts", lambda paths: None)
    got = store.search("기성금", _ctx(MINE))
    assert len(got) == 1


def test_merged_results_are_not_duplicated(indexed, store, monkeypatch):
    """같은 줄이 색인과 파일 양쪽에서 오면 같은 사실이 두 번 인용된다."""
    path, _doc = indexed
    monkeypatch.setattr(search_index, "indexed_counts", lambda paths: {path: 0})
    got = store.search("기성금", _ctx(MINE))
    keys = [(str(h.doc.path), h.line.lineno) for h in got]
    assert len(keys) == len(set(keys))


def test_merged_results_keep_permission(indexed, store, monkeypatch):
    """보완 경로가 권한을 우회하면 그게 가장 나쁜 회귀다."""
    path, _ = indexed
    monkeypatch.setattr(search_index, "indexed_counts", lambda paths: {path: 0})
    got = store.search("기성금", _ctx(MINE))
    assert all(h.doc.channel == MINE for h in got)


def test_stale_and_fresh_use_the_same_score(indexed, store, monkeypatch):
    """다른 점수를 쓰면 같은 질문에 경로에 따라 순서가 달라진다."""
    path, _doc = indexed
    monkeypatch.setattr(search_index, "indexed_counts", lambda paths: {path: 1})
    got = store.search("기성금", _ctx(MINE))
    for hit in got:
        expected = search_index.score_line(
            search_index.tokens_of("기성금"), "기성금",
            hit.line.speaker, hit.line.text,
        )
        assert hit.score == expected
