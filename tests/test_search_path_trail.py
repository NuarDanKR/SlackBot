"""QA 기록의 검색 경로 (B-93) — 질문 하나가 근거를 어느 경로로 찾았는지 남긴다.

거는 것:
1. 전수·색인·색인 장애·색인 0건 네 경로가 각각 정확한 코드로 적힌다
2. 메모장을 열지 않은 호출(콘솔·배치)은 아무것도 모으지 않는다
3. 질문이 바뀌면 이전 질문의 값이 남지 않는다
4. **동시에 처리하는 두 질문의 경로가 섞이지 않는다** — 전역 계수기를 쓰지 않은 이유
5. QARecord 가 검색 경로를 기록 파일에 쓰고, 칸이 없던 옛 기록 모양도 그대로 읽힌다
6. 운영 경로(pilot)가 메모장을 열고 두 기록 자리에 값을 넣는다
"""

from __future__ import annotations

import contextvars
import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from tybot import search_index
from tybot.access import RequestContext
from tybot.archive import store as store_module
from tybot.archive import writer
from tybot.archive.store import ArchiveStore, begin_search_trail, search_trail
from tybot.audit import QALog, QARecord

WS, CH, CHANNEL = "mgmt", "C0PLAN0001", "#팀-기획_abb110-경영계획수립"
# 이름처럼 보이는 한글 낱말은 쓰지 않는다 — 수집 단계의 개인정보 마스킹이 `[이름1]` 로
# 바꿔 검색에 안 걸린다(원칙 5). 영문 약어는 마스킹 대상이 아니다.
QUERY = "ESG"


def _store(root: Path) -> ArchiveStore:
    writer.ingest(
        root,
        workspace=WS,
        channel=CHANNEL,
        channel_id=CH,
        acl=[CHANNEL],
        messages=[
            writer.IncomingMessage(
                datetime(2026, 11, 1, tzinfo=UTC), "담당자", "ESG 점검 일정 공유드립니다"
            ),
            writer.IncomingMessage(
                datetime(2026, 11, 2, tzinfo=UTC), "담당자", "ESG 보고서 초안 올렸습니다"
            ),
        ],
    )
    return ArchiveStore(root)


def _ctx() -> RequestContext:
    return RequestContext(workspace=WS, channels=frozenset({CHANNEL}))


def _isolated(fn):
    """다른 시험이 같은 작업 줄에 남긴 메모장과 섞이지 않게 빈 문맥에서 실행한다."""
    return contextvars.Context().run(fn)


def _one_candidate(store: ArchiveStore):
    """색인이 가리킬 실제 줄 하나. DB 없이 `index` 경로를 만들기 위한 가짜 후보."""
    hit = store.search(QUERY, _ctx(), limit=5, exhaustive=True)[0]
    rel = search_index.rel_path(hit.line.source_path or hit.doc.path, store.root)
    return SimpleNamespace(doc_path=rel, line_no=hit.line.lineno)


def test_exhaustive_is_recorded(tmp_path):
    store = _store(tmp_path)

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_trail()

    assert _isolated(run) == ["exhaustive"]


def test_small_scope_threshold_is_recorded_as_exhaustive(tmp_path):
    store = _store(tmp_path)

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive_max_lines=10_000)
        return search_trail()

    assert _isolated(run) == ["exhaustive"]


def test_index_path_is_recorded(tmp_path, monkeypatch):
    store = _store(tmp_path)
    candidate = _one_candidate(store)
    monkeypatch.setattr(search_index, "candidates", lambda *_a, **_k: [candidate])

    def run():
        begin_search_trail()
        hits = store.search(QUERY, _ctx(), limit=10, exhaustive_max_lines=1)
        return hits, search_trail()

    hits, trail = _isolated(run)
    assert hits
    assert trail == ["index"]


def test_unreadable_index_is_recorded_as_scan_no_index(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(search_index, "candidates", lambda *_a, **_k: None)

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive_max_lines=1)
        return search_trail()

    assert _isolated(run) == ["scan_no_index"]


def test_empty_index_result_is_recorded_as_scan_index_empty(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(search_index, "candidates", lambda *_a, **_k: [])

    def run():
        begin_search_trail()
        hits = store.search(QUERY, _ctx(), limit=10, exhaustive_max_lines=1)
        return hits, search_trail()

    hits, trail = _isolated(run)
    assert hits  # 파일을 다시 훑어서 근거는 찾는다 — 「색인 없음」이 「자료 없음」이 되지 않는다
    assert trail == ["scan_index_empty"]


def test_several_searches_in_one_question_keep_their_order(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(search_index, "candidates", lambda *_a, **_k: None)

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive_max_lines=1)
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_trail()

    assert _isolated(run) == ["scan_no_index", "exhaustive"]


def test_without_begin_nothing_is_collected(tmp_path):
    store = _store(tmp_path)

    def run():
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_trail()

    assert _isolated(run) == []


def test_begin_discards_the_previous_question(tmp_path):
    store = _store(tmp_path)

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        assert search_trail() == ["exhaustive"]
        begin_search_trail()  # 같은 작업 줄이 다음 질문을 받았다
        return search_trail()

    assert _isolated(run) == []


def test_trail_is_bounded(tmp_path):
    store = _store(tmp_path)

    def run():
        begin_search_trail()
        for _ in range(store_module.SEARCH_TRAIL_MAX + 10):
            store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_trail()

    assert len(_isolated(run)) == store_module.SEARCH_TRAIL_MAX


def test_concurrent_questions_do_not_mix_their_paths(tmp_path, monkeypatch):
    """한 질문이 색인으로 가는 동안 다른 질문이 전수로 가도 서로의 기록에 안 들어간다."""
    store = _store(tmp_path)
    monkeypatch.setattr(search_index, "candidates", lambda *_a, **_k: None)
    both_started = threading.Barrier(2, timeout=10)
    seen: dict[str, list[str]] = {}

    def question(name: str, **kwargs):
        begin_search_trail()
        both_started.wait()  # 두 질문이 메모장을 연 채로 동시에 검색한다
        for _ in range(5):
            store.search(QUERY, _ctx(), limit=10, **kwargs)
        seen[name] = search_trail()

    threads = [
        threading.Thread(target=question, args=("exhaustive",), kwargs={"exhaustive": True}),
        threading.Thread(target=question, args=("fallback",), kwargs={"exhaustive_max_lines": 1}),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert seen["exhaustive"] == ["exhaustive"] * 5
    assert seen["fallback"] == ["scan_no_index"] * 5


def _record(**extra) -> QARecord:
    return QARecord.build(
        workspace=WS,
        channel=CHANNEL,
        channel_id=CH,
        user="U1",
        user_name="tester",
        question="질문",
        intent_kind="archive_qa",
        intent_source="rule",
        reason="answered",
        hits=3,
        scope="채널 1개",
        **extra,
    )


def test_qa_record_writes_search_path_and_old_rows_still_read(tmp_path):
    assert _record().search_path == []  # 기본값 — 옛 경로·시험이 칸을 몰라도 된다

    log = QALog(tmp_path, write_md=False)
    log.write(_record(search_path=["index", "exhaustive"]))
    rows = [
        json.loads(line)
        for path in sorted(tmp_path.glob("qa-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["search_path"] == ["index", "exhaustive"]

    # 이 칸이 생기기 전에 쓴 기록 모양. 읽는 쪽은 `row.get` 으로 읽으므로 빈 값이다.
    old_row = {k: v for k, v in rows[0].items() if k != "search_path"}
    assert old_row.get("search_path", []) == []


def test_pilot_opens_the_trail_and_fills_both_record_sites():
    pilot = (Path(__file__).resolve().parents[1] / "src/tybot/slack/pilot.py").read_text(
        encoding="utf-8"
    )
    assert pilot.count("begin_search_trail()") == 1
    # 정상 답변 기록과 오류 기록, 두 자리 모두에서 채운다.
    assert pilot.count("search_path=search_trail()") == 2
    # 메모장은 `_handle_request` 를 부르기 전에 열려야 한다.
    assert pilot.index("begin_search_trail()") < pilot.index("self._handle_request(")
