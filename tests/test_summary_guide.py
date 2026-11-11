"""승인 요약을 검색 길잡이로만 쓴다 (B-56).

승인 문장은 사람이 확인한 파생 정보지 원문이 아니다. 여기서 보는 것은 셋이다 —
**찾기는 넓어지는가**, **근거는 원문인가**, **권한과 어긋난 좌표는 막히는가**.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from tybot import summary_guide
from tybot.access import RequestContext
from tybot.archive import writer
from tybot.archive.store import ArchiveStore
from tybot.evidence_refs import content_hash

CHANNEL = "#팀-전산_ABB110-회의"
RAW = "미회수 채권 12억원 잔액이 남았습니다"
APPROVED = "미수금 12억원 잔액이 남았습니다"


def _unlike(pattern: str) -> str:
    """`%낱말%` LIKE 패턴을 글자 그대로의 낱말로 되돌린다(이스케이프 해제)."""
    body = pattern[1:-1]
    out, escaped = [], False
    for ch in body:
        if escaped:
            out.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        else:
            out.append(ch)
    return "".join(out)


class _Cursor:
    def __init__(self, conn) -> None:
        self.conn = conn
        self.rows: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.sql.append((" ".join(sql.split()), params))
        if "approved_summary_item a" not in sql:
            self.rows = []
            return
        scopes = set((params or {}).get("workspaces") or [])
        rows = [
            row for row in self.conn.rows
            if row.get("workspace") in scopes and row.get("evidence_hash")
        ]
        # B-89: 낱말 조건(`kwN`)과 LIMIT 을 PostgreSQL 과 같은 뜻으로 흉내 낸다.
        # 본문 식은 lower(replace(body, ' ', '')), 패턴은 `%낱말%`(\ 이스케이프).
        words = [
            _unlike(value) for key, value in sorted((params or {}).items())
            if key.startswith("kw")
        ]
        if words:
            rows = [
                row for row in rows
                if any(word in row["body"].replace(" ", "").lower() for word in words)
            ]
        self.rows = rows[: int((params or {}).get("lim") or len(rows))]

    def fetchall(self):
        return self.rows


class _Conn:
    """길잡이가 쓰는 만큼만 흉내 내는 손잡이."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.sql: list[tuple[str, object]] = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1


def _archive(tmp_path, *, text: str = RAW, source_ts: str = "1758012345.123456"):
    writer.ingest(
        tmp_path,
        workspace="pilot",
        channel=CHANNEL,
        channel_id="C1",
        acl=[CHANNEL],
        messages=[writer.IncomingMessage(
            ts=datetime(2026, 9, 16, 1, 0, tzinfo=UTC),
            speaker="홍길동",
            text=text,
            source_ts=source_ts,
        )],
    )
    return ArchiveStore(tmp_path)


def _locator(store) -> tuple[str, str]:
    doc = store.source_docs()[0]
    line = doc.raw_lines[0]
    return f"{doc.path.name}:{line.lineno}", line.ts


def _row(store, **kw) -> dict:
    doc = store.source_docs()[0]
    line = doc.raw_lines[0]
    locator, at = f"{doc.path.name}:{line.lineno}", line.ts
    base = {
        "candidate_id": "11111111-1111-1111-1111-111111111111",
        "workspace": "pilot",
        "channel_id": "C1",
        "body": APPROVED,
        "evidence_quote": RAW,
        "evidence_at": at,
        "evidence_author": "홍길동",
        "evidence_locator": locator,
        "evidence_hash": content_hash(line.ts, line.speaker, line.text),
    }
    base.update(kw)
    return base


def _ctx(channels=(CHANNEL,)):
    return RequestContext(workspace="pilot", channels=frozenset(channels))


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    # 색인·길잡이가 실제 DB 를 찾아가면 테스트가 환경에 따라 달라진다.
    monkeypatch.delenv("DATABASE_URL", raising=False)


# --- 찾기는 넓어지고, 근거는 원문이다 ------------------------------------------
def test_approved_summary_finds_wording_the_search_alone_missed(tmp_path):
    """사람 말("미수금")과 문서 말("미회수 채권")이 다를 때가 이 기능의 자리다."""
    store = _archive(tmp_path)
    ctx = _ctx()
    assert store.search("미수금", ctx) == []

    hits = summary_guide.expand(store, ctx, "미수금", conn=_Conn([_row(store)]))

    assert [hit.line.text for hit in hits] == [RAW]


def test_the_approved_sentence_itself_never_becomes_evidence(tmp_path):
    """길잡이는 좌표만 준다. 승인 문장이 근거 자리에 들어가면 요약 재귀다(원칙 1)."""
    store = _archive(tmp_path)

    hits = summary_guide.expand(store, _ctx(), "미수금", conn=_Conn([_row(store)]))

    assert hits
    assert all(APPROVED not in hit.line.text for hit in hits)


def test_an_unrelated_question_pulls_nothing(tmp_path):
    store = _archive(tmp_path)

    assert summary_guide.expand(store, _ctx(), "회의실 예약", conn=_Conn([_row(store)])) == []


# --- 권한 (원칙 3) -------------------------------------------------------------
def test_losing_channel_access_hides_the_original_even_with_an_approved_summary(tmp_path):
    """승인은 그때 그 검토자의 권한으로 한 것이다. 지금 묻는 사람의 것이 아니다."""
    store = _archive(tmp_path)

    hits = summary_guide.expand(
        store, _ctx(channels=("#다른팀_공지",)), "미수금", conn=_Conn([_row(store)])
    )

    assert hits == []


# --- 반려·보류·폐기는 들어올 길이 없다 -----------------------------------------
def test_only_approved_live_items_are_read(tmp_path):
    store = _archive(tmp_path)
    conn = _Conn([_row(store)])

    summary_guide.active_items("pilot", conn=conn)

    sql = conn.sql[0][0]
    assert "c.state IN ('approved', 'auto_bot_summary')" in sql
    assert "a.superseded_at IS NULL" in sql
    assert "a.stale_at IS NULL" in sql
    assert "a.workspace = ANY(%(workspaces)s)" in sql
    assert "c.evidence_hash <> ''" in sql
    # 정정 모달 입력은 사람이 쓴 문장이지 원문이 아니다 — 읽지도 않는다(원칙 7).
    assert "correction" not in sql


def test_an_item_without_a_locator_is_not_a_guide(tmp_path):
    """좌표가 없으면 다시 열 원문이 없다. 남는 것은 승인 문장뿐이라 쓰지 않는다."""
    store = _archive(tmp_path)

    assert summary_guide.active_items(
        "pilot", conn=_Conn([_row(store, evidence_locator="")])
    ) == []


def test_an_old_item_without_a_source_hash_is_not_a_guide(tmp_path):
    store = _archive(tmp_path)

    assert summary_guide.active_items(
        "pilot", conn=_Conn([_row(store, evidence_hash="")])
    ) == []


# --- 어긋난 좌표는 재검토로 돌린다 ---------------------------------------------
def test_changed_evidence_is_sent_back_to_review_not_silently_swapped(tmp_path):
    """그 자리에 다른 문장이 있으면 비슷한 줄로 갈아 끼우지 않는다."""
    store = _archive(tmp_path)
    conn = _Conn([_row(store, evidence_quote="공정률은 55.0%입니다", body="미수금 공정률 요약")])

    hits = summary_guide.expand(store, _ctx(), "미수금", conn=conn)

    assert hits == []
    updates = [sql for sql, _ in conn.sql if "UPDATE approved_summary_item" in sql]
    assert updates and "stale_at = now()" in updates[0]
    assert summary_guide.STALE_EVIDENCE_MOVED in [
        params[0] for sql, params in conn.sql if "UPDATE approved_summary_item" in sql
    ]


def test_full_source_hash_detects_text_added_after_the_approved_quote(tmp_path):
    changed = f"{RAW} 그러나 이 수치는 오류입니다"
    store = _archive(tmp_path, text=changed)
    row = _row(
        store,
        evidence_quote=RAW,
        evidence_hash=content_hash("2026-09-16 10:00", "홍길동", RAW),
    )
    conn = _Conn([row])

    assert summary_guide.expand(store, _ctx(), "미수금", conn=conn) == []
    assert summary_guide.STALE_EVIDENCE_MOVED in [
        params[0] for sql, params in conn.sql if "UPDATE approved_summary_item" in sql
    ]


def test_root_dm_can_use_a_guide_from_another_readable_workspace(tmp_path):
    store = _archive(tmp_path)
    ctx = RequestContext(workspace="mgmt", is_root=True)

    hits = summary_guide.expand(store, ctx, "미수금", conn=_Conn([_row(store)]))

    assert [hit.line.text for hit in hits] == [RAW]


def test_a_managed_source_list_guides_across_workspaces_but_only_where_registered(
    tmp_path,
):
    """VIPBot 처럼 **관리형 근거 범위**로 묻는 요청.

    길잡이는 등록된 다른 워크스페이스까지 넓어져야 하고, 같은 워크스페이스라도
    **등록되지 않은 채널**은 승인 요약이 있어도 원문을 열지 못해야 한다. 둘 중
    하나만 맞으면 경영진 질문이 반쪽 근거로 답해지거나, 등록하지 않은 채널이
    요약을 타고 새어 나온다.
    """
    store = _archive(tmp_path)  # pilot / C1 — 등록하지 않은 채널
    writer.ingest(
        tmp_path,
        workspace="mgmt",
        channel="#팀-경영_ZZZ000-근거",
        channel_id="C0EXEC001",
        acl=["#팀-경영_ZZZ000-근거"],
        messages=[writer.IncomingMessage(
            ts=datetime(2026, 9, 16, 2, 0, tzinfo=UTC),
            speaker="담당자",
            text="미회수 채권 34억원 잔액이 남았습니다",
            source_ts="1758012345.654321",
        )],
    )
    writer.ingest(
        tmp_path,
        workspace="mgmt",
        channel="#팀-경영_ZZZ000-미등록",
        channel_id="C0OTHER1",
        acl=["#팀-경영_ZZZ000-미등록"],
        messages=[writer.IncomingMessage(
            ts=datetime(2026, 9, 16, 3, 0, tzinfo=UTC),
            speaker="담당자",
            text="미회수 채권 99억원 잔액이 남았습니다",
            source_ts="1758012345.999999",
        )],
    )
    store = ArchiveStore(tmp_path)
    registered = next(
        doc for doc in store.source_docs() if doc.channel_id == "C0EXEC001"
    )
    unregistered = next(
        doc for doc in store.source_docs() if doc.channel_id == "C0OTHER1"
    )
    other = unregistered.raw_lines[0]
    sibling_row = _row(
        store,
        candidate_id="33333333-3333-3333-3333-333333333333",
        workspace="mgmt",
        channel_id="C0OTHER1",
        evidence_quote=other.text,
        evidence_at=other.ts,
        evidence_author=other.speaker,
        evidence_locator=f"{unregistered.path.name}:{other.lineno}",
        evidence_hash=content_hash(other.ts, other.speaker, other.text),
    )
    line = registered.raw_lines[0]
    managed_row = _row(
        store,
        candidate_id="22222222-2222-2222-2222-222222222222",
        workspace="mgmt",
        channel_id="C0EXEC001",
        evidence_quote=line.text,
        evidence_at=line.ts,
        evidence_author=line.speaker,
        evidence_locator=f"{registered.path.name}:{line.lineno}",
        evidence_hash=content_hash(line.ts, line.speaker, line.text),
    )
    ctx = RequestContext(
        workspace="tyit",
        evidence_sources=frozenset({("mgmt", "C0EXEC001")}),
    )

    hits = summary_guide.expand(
        store, ctx, "미수금", conn=_Conn([_row(store), managed_row, sibling_row])
    )

    assert [hit.line.text for hit in hits] == [line.text]
    assert all(other.text not in hit.line.text for hit in hits)


def test_a_missing_source_file_is_marked_for_review(tmp_path):
    store = _archive(tmp_path)
    conn = _Conn([_row(store, evidence_locator="2001-01-01.md:12")])

    assert summary_guide.expand(store, _ctx(), "미수금", conn=conn) == []
    assert summary_guide.STALE_SOURCE_MISSING in [
        params[0] for sql, params in conn.sql if "UPDATE approved_summary_item" in sql
    ]


def test_a_shifted_line_number_does_not_quietly_use_another_line(tmp_path):
    store = _archive(tmp_path)
    locator, _ = _locator(store)
    name, _, line_no = locator.rpartition(":")
    conn = _Conn([_row(store, evidence_locator=f"{name}:{int(line_no) + 1}")])

    assert summary_guide.expand(store, _ctx(), "미수금", conn=conn) == []


# --- DB 가 없을 때 ------------------------------------------------------------
def test_without_a_database_the_guide_is_simply_absent(tmp_path):
    """길잡이는 검색을 넓히는 장치다. 없으면 예전과 같은 결과가 나와야 한다."""
    store = _archive(tmp_path)

    assert summary_guide.expand(store, _ctx(), "미수금") == []


# --- 답변 경로에 붙는 자리 ------------------------------------------------------
def _engine(store):
    from tybot.answer import AnswerEngine

    return AnswerEngine(store, None)


def test_the_engine_appends_guide_lines_behind_the_search_results(tmp_path, monkeypatch):
    """원문 검색이 먼저다. 길잡이가 찾은 줄이 검색 결과를 밀어내면 안 된다."""
    store = _archive(tmp_path)
    ctx = _ctx()
    extra = summary_guide.expand(store, ctx, "미수금", conn=_Conn([_row(store)]))
    assert extra
    monkeypatch.setattr(summary_guide, "expand", lambda *a, **kw: list(extra))
    found = store.search("미회수", ctx)
    assert found

    merged = _engine(store)._with_approved_guide("미회수", found, ctx)

    # 같은 줄이 두 경로로 들어와도 근거가 두 번 인용되면 안 된다.
    assert [(str(h.doc.path), h.line.lineno) for h in merged] == [
        (str(h.doc.path), h.line.lineno) for h in found
    ]


def test_a_broken_guide_never_blocks_an_answer(tmp_path, monkeypatch):
    store = _archive(tmp_path)

    def boom(*_a, **_kw):
        raise RuntimeError("DB 없음")

    monkeypatch.setattr(summary_guide, "expand", boom)
    found = store.search("미회수", _ctx())

    assert _engine(store)._with_approved_guide("미회수", found, _ctx()) == found


def test_a_guide_line_the_search_missed_is_added_to_the_evidence(tmp_path, monkeypatch):
    """승인 전에는 못 찾던 표현이 답변 근거로 들어오는지 — 이 기능의 완료 조건."""
    store = _archive(tmp_path)
    ctx = _ctx()
    extra = summary_guide.expand(store, ctx, "미수금", conn=_Conn([_row(store)]))
    monkeypatch.setattr(summary_guide, "expand", lambda *a, **kw: list(extra))
    assert store.search("미수금", ctx) == []

    merged = _engine(store)._with_approved_guide("미수금", [], ctx)

    assert [hit.line.text for hit in merged] == [RAW]


# --- B-89: DB 에서 먼저 거른다 --------------------------------------------------


def test_an_old_matching_summary_beyond_the_recent_window_is_still_found(tmp_path):
    """예전에는 최근 500건만 읽어, 그보다 오래된 승인 요약은 영영 길잡이가 못 됐다."""
    store = _archive(tmp_path)
    filler = [
        _row(store, candidate_id=f"00000000-0000-0000-0000-{index:012d}",
             body=f"무관한 회의 안내 {index}")
        for index in range(summary_guide.CANDIDATE_LIMIT + 50)
    ]
    conn = _Conn([*filler, _row(store)])  # 맞는 항목은 가장 오래된 자리

    hits = summary_guide.expand(store, _ctx(), "미수금 잔액", conn=conn)

    assert [hit.line.text for hit in hits] == [RAW]
    sql, params = conn.sql[0]
    assert "lower(replace(a.body, ' ', '')) LIKE %(kw0)s" in sql
    assert params["kw0"] == "%미수금%"


def test_the_db_filter_never_drops_an_item_the_score_would_pick(tmp_path):
    """거르는 기준이 점수보다 좁으면 맞을 항목이 후보에서 먼저 빠진다."""
    from tybot import search_index

    bodies = [
        "iPhone 지급 대장 정리",      # 섞인 대소문자 — 원문 검색 표기 셋으로는 안 잡힌다
        "주간 업무 보고 취합",        # 띄어쓰기
        "PF 대출 약정 변경",
        "비율 50%_초과 항목",          # LIKE 특수문자
        "전혀 상관없는 문장",
    ]
    for query in ("iphone", "주간업무보고", "pf대출", "50%_초과", "상관", "없는말"):
        tokens = search_index.tokens_of(query)
        clause, params = summary_guide.keyword_filter(tokens)
        words = [_unlike(params[key]) for key in sorted(params)]
        by_db = {b for b in bodies if any(w in b.replace(" ", "").lower() for w in words)}
        by_score = {b for b in bodies if search_index.score_line(tokens, query, "", b) > 0}
        assert by_score <= by_db, query
        assert clause.startswith("AND (")


def test_keyword_filter_escapes_like_wildcards_and_caps_words():
    _clause, params = summary_guide.keyword_filter(["a%b", "c_d", "e\\f"])
    assert params == {"kw0": "%a\\%b%", "kw1": "%c\\_d%", "kw2": "%e\\\\f%"}
    # 본문 식이 lower() 라서 패턴도 소문자·공백 제거여야 맞는다.
    assert summary_guide.keyword_filter(["iPhone 대장"])[1] == {"kw0": "%iphone대장%"}
    many = [f"낱말{index}" for index in range(summary_guide.MAX_FILTER_TOKENS + 5)]
    _clause, capped = summary_guide.keyword_filter(many)
    assert len(capped) == summary_guide.MAX_FILTER_TOKENS


def test_no_words_means_no_filter_and_no_database_call(tmp_path):
    assert summary_guide.keyword_filter([]) == ("", {})
    store = _archive(tmp_path)
    conn = _Conn([_row(store)])

    assert summary_guide.expand(store, _ctx(), "   ", conn=conn) == []
    assert conn.sql == []


def test_a_filtered_guide_still_reopens_the_original_under_current_acl(tmp_path):
    """DB 에서 걸러 찾았어도 권한 판정은 그대로 원문 재개봉이 한다."""
    store = _archive(tmp_path)
    conn = _Conn([_row(store)])

    hits = summary_guide.expand(store, _ctx(channels=()), "미수금", conn=conn)
    assert hits == []
