"""전문 봇 도구 — 우리 아카이브 위에서 우리 권한으로 (2026-09-11).

Hermes 를 읽어 보니 값이 프롬프트가 아니라 **찾는 방식**에 있었다 — 검색하고,
읽고, 모자라면 다시 검색한다. 그 루프를 프롬프트 한 장으로는 못 옮긴다.

그래서 도구는 우리가 만들고 설명문·규칙만 가져왔다. 여기서 고정하는 것은
**권한이 한 곳에 남는가** 다. 도구가 `ctx` 를 우회하면 그 순간 전문 봇이
아카이브를 통째로 읽는 것과 같아진다.
"""
from __future__ import annotations

from dataclasses import dataclass

from tybot import specialist_tools as tools
from tybot.access import RequestContext


@dataclass
class FakeLine:
    ts: str
    speaker: str
    text: str


@dataclass
class FakeDoc:
    channel: str
    channel_id: str
    raw_lines: list
    workspace: str = "tyit"


@dataclass
class FakeHit:
    doc: FakeDoc
    line: FakeLine


class FakeStore:
    """`search` 와 `visible_docs` 만 흉내 낸다 — 도구가 쓰는 것이 그 둘뿐이다."""

    def __init__(self, docs, *, hidden=()):
        self._docs = list(docs)
        self._hidden = set(hidden)
        self.search_calls: list[str] = []

    @staticmethod
    def _require_ctx(ctx) -> None:
        if not isinstance(ctx, RequestContext):
            raise AssertionError("권한 컨텍스트 없이 아카이브를 읽으려 했다")

    def visible_docs(self, ctx):
        # 실제 store 와 같은 계약: ctx 로 걸러 돌려준다.
        self._require_ctx(ctx)
        return [d for d in self._docs if d.channel not in self._hidden]

    def search(self, query, ctx, *, limit=20, channels=None):
        # **가짜도 ctx 를 요구해야 한다.** 무시하면 도구가 ctx 를 버려도
        # 테스트가 통과한다 — 실제로 그렇게 한 번 새는 것을 못 잡았다.
        self._require_ctx(ctx)
        self.search_calls.append(query)
        out = []
        for doc in self.visible_docs(ctx):
            if channels is not None and doc.channel not in channels:
                continue
            for line in doc.raw_lines:
                # 실제 ArchiveStore는 OR 후보를 가져온 뒤 일치 낱말 수로 순위를
                # 매긴다. 일부 일치 폴백을 테스트하려면 가짜도 같은 계약이어야 한다.
                if any(word in line.text for word in query.split()):
                    out.append(FakeHit(doc, line))
        return out[:limit]


def _store(hidden=()):
    public = FakeDoc(
        channel="#팀-전산_ABB110-주간회의", channel_id="C1",
        raw_lines=[
            FakeLine("2026-09-01 09:00", "홍길동", "3공구 기성금 3.2억 지급"),
            FakeLine("2026-09-02 10:00", "김철수", "[첨부추출:가정산서.xlsx] 합계 3.2억"),
        ],
    )
    secret = FakeDoc(
        channel="#팀-임원_ABB999-비공개", channel_id="C9",
        raw_lines=[FakeLine("2026-09-01 09:00", "임원", "3공구 매각 검토 중")],
    )
    return FakeStore([public, secret], hidden=hidden)


def _box(store, *, live=None, here=""):
    return tools.ToolBox(
        store=store,
        ctx=RequestContext(workspace="tyit", channels=frozenset({"C1"})),
        live_fetch=live,
        here=here,
    )


# --- 권한이 한 곳에 남는가 ----------------------------------------------------
def test_search_only_returns_what_the_store_allows():
    """도구가 ACL 을 다시 구현하지 않는다. `store.search(query, ctx)` 가 유일한 판정이다."""
    box = _box(_store(hidden={"#팀-임원_ABB999-비공개"}))

    got = box.run("search", {"query": "3공구"})

    assert "기성금" in got
    assert "매각" not in got, "가려야 할 채널 내용이 나왔다"


def test_read_channel_cannot_open_a_hidden_channel():
    box = _box(_store(hidden={"#팀-임원_ABB999-비공개"}))

    got = box.run("read_channel", {"channel": "#팀-임원_ABB999-비공개"})

    assert "권한이 없습니다" in got
    assert "매각" not in got


def test_read_document_cannot_reach_a_hidden_channel():
    box = _box(_store(hidden={"#팀-임원_ABB999-비공개"}))

    got = box.run("read_document", {"name": "매각"})

    assert "매각 검토" not in got


def test_the_context_is_not_an_argument():
    """`ctx` 를 인자로 받으면 그 자리가 곧 권한 우회다."""
    import inspect

    for name in ("_search", "_read_channel", "_read_document", "_fetch_recent"):
        sig = inspect.signature(getattr(tools.ToolBox, name))
        assert list(sig.parameters) == ["self", "args"], name


def test_the_tools_never_read_files_directly():
    """전문 봇이 아카이브 파일을 직접 읽으면 권한 판정이 둘이 되고, 갈리면
    오류 없이 새어 나간다."""
    import inspect

    source = inspect.getsource(tools)

    for leaked in ("open(", "read_text", "read_bytes", "Path(", "glob("):
        assert leaked not in source, f"도구가 파일을 직접 만진다: {leaked}"


# --- 검색 규칙 (Hermes 에서 가져온 것) ---------------------------------------
def test_an_empty_result_reports_counts_and_allows_bounded_retry():
    """0건이면 동의어 탐색을 허용하되 동일 검색 반복은 막는다."""
    box = _box(_store())

    got = box.run("search", {"query": "존재하지 않는 낱말"})

    assert "낱말별" in got
    assert "동의어" in got
    assert "같은 검색은 반복하지" in got


def test_the_search_description_carries_the_bounded_retry_rule():
    """설명문에서 동의어 재검색과 반복 제한을 함께 전달한다."""
    assert "동의어" in tools.SEARCH_DESCRIPTION
    assert "같은 검색을 반복하지" in tools.SEARCH_DESCRIPTION


def test_where_narrows_to_a_channel():
    box = _box(_store())

    got = box.run("search", {"query": "3공구", "where": "전산"})

    assert "기성금" in got
    assert "매각" not in got


def test_where_rejects_an_ambiguous_channel_name():
    docs = [
        FakeDoc("#팀-전산_ABB110-주간회의", "C1", [FakeLine("1", "홍", "기성 1억")]),
        FakeDoc("#팀-전산_ABB110-월간회의", "C2", [FakeLine("2", "김", "기성 2억")]),
    ]
    box = _box(FakeStore(docs))

    got = box.run("search", {"query": "기성", "where": "팀-전산"})

    assert "여러 채널과 일치" in got
    assert "1억" not in got
    assert "2억" not in got


def test_where_narrows_before_the_global_result_limit():
    other = FakeDoc(
        "#팀-전산_ABB110-다른채널",
        "C2",
        [FakeLine(str(i), "홍", "기성 자료") for i in range(tools.MAX_SEARCH_HITS)],
    )
    wanted = FakeDoc(
        "#팀-전산_ABB110-주간회의",
        "C1",
        [FakeLine("99", "김", "기성 지정채널")],
    )
    box = _box(FakeStore([other, wanted]))

    got = box.run("search", {"query": "기성", "where": "주간회의"})

    assert "지정채널" in got
    assert "다른채널" not in got


def test_where_keeps_zero_result_diagnostics_inside_the_channel():
    other = FakeDoc(
        "#팀-전산_ABB110-다른채널",
        "C2",
        [FakeLine("1", "홍", "특수표현 있음")],
    )
    wanted = FakeDoc(
        "#팀-전산_ABB110-주간회의",
        "C1",
        [FakeLine("2", "김", "일반 내용")],
    )
    box = _box(FakeStore([other, wanted]))

    got = box.run(
        "search",
        {"query": "특수표현 없음", "where": "주간회의"},
    )

    assert "특수표현 0건" in got


def test_search_separates_documents_from_human_conversation():
    box = _box(_store())

    got = box.run("search", {"query": "3.2억"})

    assert got.index("## 문서·첨부") < got.index("[첨부추출:가정산서.xlsx]")
    assert got.index("## 사람 대화") < got.index("기성금 3.2억 지급")


def test_search_marks_partial_term_matches():
    box = _box(_store())

    got = box.run("search", {"query": "3공구 존재하지않음"})

    assert "(1/2 낱말)" in got


# --- 무엇을 열었는지 기억한다 -------------------------------------------------
def test_touched_records_the_documents_used():
    """출처는 마스터가 붙인다. 모델이 본문에 적은 것을 믿고 만들면 그게 환각이다."""
    box = _box(_store())

    box.run("search", {"query": "기성금"})

    assert [d.channel for d in box.touched.documents] == ["#팀-전산_ABB110-주간회의"]
    assert [h.line.text for h in box.touched.evidence_hits] == [
        "3공구 기성금 3.2억 지급"
    ]


def test_touched_does_not_duplicate():
    """같은 문서를 두 도구가 건드려도 한 번만 센다 — 출처가 두 줄로 나가면
    사람이 서로 다른 근거인 줄 안다."""
    box = _box(_store())

    # `기성금` 은 전산 채널에만 있다. `3공구` 는 두 채널에 다 있어서
    # 이 성질을 검증하지 못한다.
    box.run("search", {"query": "기성금"})
    box.run("read_channel", {"channel": "팀-전산"})

    assert [d.channel for d in box.touched.documents] == ["#팀-전산_ABB110-주간회의"]


def test_touched_does_not_record_lines_beyond_the_tool_output_limit():
    """잘려 모델에게 전달되지 않은 뒤쪽 줄은 답변 근거가 아니다."""
    doc = FakeDoc(
        channel="#팀-전산_ABB110-주간회의", channel_id="C1",
        raw_lines=[
            FakeLine("1", "홍", "공통 " + "가" * 20_000),
            FakeLine("2", "김", "공통 " + "나" * 20_000),
        ],
    )
    box = _box(FakeStore([doc]))

    box.run("search", {"query": "공통"})

    assert [hit.line.ts for hit in box.touched.evidence_hits] == ["1", "2"]

    # 세 번째 줄은 30,000자 뒤라 모델에게 전달되지 않는다.
    doc.raw_lines.append(FakeLine("3", "박", "공통 " + "다" * 20_000))
    box = _box(FakeStore([doc]))
    box.run("search", {"query": "공통"})
    assert [hit.line.ts for hit in box.touched.evidence_hits] == ["1", "2"]


def test_search_group_headers_count_toward_the_touched_output_limit(monkeypatch):
    """그룹 제목 뒤로 잘린 줄은 내부 근거 목록에도 들어가면 안 된다."""
    monkeypatch.setattr(tools, "MAX_TOOL_CHARS", 100)
    docs = [
        FakeDoc(
            "#문서채널",
            "C1",
            [FakeLine("1", "홍", "[첨부추출:x] 공통 " + "가" * 50)],
        ),
        FakeDoc(
            "#대화채널",
            "C2",
            [FakeLine("2", "김", "공통 대화")],
        ),
    ]
    box = _box(FakeStore(docs))

    got = box.run("search", {"query": "공통"})

    assert "이하 생략" in got
    assert [hit.line.ts for hit in box.touched.evidence_hits] == ["1"]


# --- 실시간 조회 --------------------------------------------------------------
def _live(messages):
    def fetch(channel_id, limit):
        return messages[:limit]
    return fetch


def test_live_fetch_is_absent_unless_enabled():
    """도구를 주지 않으면 모델이 부를 수 없다. 프롬프트로 막는 것보다 확실하다."""
    names = {t.name for t in tools.specs(live=False)}

    assert "fetch_recent_slack" not in names
    assert {"search", "read_channel", "read_document"} <= names


def test_live_fetch_respects_the_same_acl():
    """Slack 을 직접 물으면 아카이브 ACL 을 우회한다 — 이 도구를 더하면서
    가장 쉽게 생기는 구멍이다."""
    box = _box(
        _store(hidden={"#팀-임원_ABB999-비공개"}),
        live=_live([{"ts": "1", "speaker": "임원", "text": "매각 확정"}]),
    )

    got = box.run("fetch_recent_slack", {"channel": "#팀-임원_ABB999-비공개"})

    assert "권한이 없습니다" in got
    assert "매각 확정" not in got


def test_live_lines_are_marked():
    """출처를 아카이브 문서로 붙이면 그 문서에는 없는 내용이 되고, 사람이
    확인하러 갔을 때 찾지 못한다."""
    box = _box(_store(), live=_live([
        {"ts": "1", "speaker": "홍길동", "text": "방금 결정됐습니다",
         "permalink": "https://ty.slack.com/archives/C1/p1"},
    ]))

    got = box.run("fetch_recent_slack", {"channel": "팀-전산"})

    assert tools.LIVE_MARK in got
    assert box.touched.used_live
    assert box.touched.live_permalinks == ["https://ty.slack.com/archives/C1/p1"]
    assert box.touched.live_messages == [("tyit", "C1", "1")]


def test_bot_messages_are_never_fetched():
    """실으면 우리 답이 다음 답의 근거가 되고, 한 번 잘못 말한 숫자가 굳는다."""
    box = _box(_store(), live=_live([
        {"ts": "1", "speaker": "TYBot", "text": "기성금은 9.9억입니다", "is_bot": True},
        {"ts": "2", "speaker": "홍길동", "text": "확인했습니다"},
    ]))

    got = box.run("fetch_recent_slack", {"channel": "팀-전산"})

    assert "9.9억" not in got
    assert "확인했습니다" in got


def test_live_fetch_defaults_to_the_asking_channel():
    box = _box(_store(), live=_live([{"ts": "1", "speaker": "홍", "text": "네"}]),
               here="#팀-전산_ABB110-주간회의")

    got = box.run("fetch_recent_slack", {})

    assert "네" in got


def test_live_fetch_is_capped():
    many = [{"ts": str(i), "speaker": "홍", "text": f"줄{i}"} for i in range(200)]
    box = _box(_store(), live=_live(many), here="팀-전산")

    got = box.run("fetch_recent_slack", {"limit": 999})

    assert got.count("줄") <= tools.MAX_LIVE_MESSAGES + 1


# --- 실패해도 답변을 끊지 않는다 ----------------------------------------------
def test_a_broken_tool_returns_text_not_an_exception():
    """도구가 터지면 모델은 그것을 모른 채 답을 만든다. 오류도 문자열로 줘야
    모델이 다른 길을 찾는다."""
    class Boom(FakeStore):
        def search(self, query, ctx, *, limit=20):
            raise RuntimeError("index down")

    box = _box(Boom([]))

    got = box.run("search", {"query": "x"})

    assert "도구 오류" in got


def test_an_unknown_tool_is_reported():
    box = _box(_store())

    assert "알 수 없는 도구" in box.run("nope", {})


def test_a_huge_result_is_clipped():
    """컨텍스트를 넘겨 호출이 통째로 실패하면 답이 아예 안 나간다."""
    big = FakeDoc(
        channel="#팀-전산_ABB110-주간회의", channel_id="C1",
        raw_lines=[FakeLine("t", "홍", "가" * 200) for _ in range(500)],
    )
    box = _box(FakeStore([big]))

    got = box.run("read_channel", {"channel": "팀-전산", "limit": 400})

    assert len(got) <= tools.MAX_TOOL_CHARS + 200
    assert "이하 생략" in got
