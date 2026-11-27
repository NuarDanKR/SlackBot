"""QA 기록의 검색 줄 수·검색 시간 (B-93) — 15만 줄 문턱을 숫자로 조정할 재료.

거는 것:
1. 검색마다 「볼 수 있던 줄 수」와 「검색만 걸린 시간」이 경로와 같은 순서로 남는다
2. 줄 수는 전수·색인·VIP(전수 강제) 어느 경로에서도 남는다(문턱을 안 쓰는 경로 포함)
3. 메모장을 열지 않은 호출은 모으지 않고, 질문이 바뀌면 이전 값이 남지 않는다
4. 동시에 처리하는 두 질문의 숫자가 섞이지 않는다
5. QARecord 가 기록 파일에 쓰고, 콘솔 상세가 믿을 수 있는 값만 내보낸다
6. 운영 경로(pilot)가 두 기록 자리에 값을 넣는다
"""

from __future__ import annotations

import contextvars
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tybot import search_index
from tybot.access import RequestContext
from tybot.archive import store as store_module
from tybot.archive import writer
from tybot.archive.store import (
    ArchiveStore,
    begin_search_trail,
    search_stats,
    search_trail,
)
from tybot.audit import QALog, QARecord
from tybot.console import answer_records

ROOT = Path(__file__).resolve().parents[1]
WS, CH, CHANNEL = "mgmt", "C0PLAN0001", "#팀-기획_abb110-경영계획수립"
# 이름처럼 보이는 한글 낱말은 쓰지 않는다 — 수집 단계의 마스킹이 `[이름1]` 로 바꿔 검색에 안
# 걸린다(원칙 5). 영문 약어는 대상이 아니다.
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
    return contextvars.Context().run(fn)


def _visible_lines(store: ArchiveStore) -> int:
    """정답: 이 질문자가 볼 수 있는 문서의 원문 줄 수를 시험이 직접 센다."""
    return sum(len(doc.raw_lines) for doc in store._visible_channel_docs(_ctx()))


class _Clock:
    """`time.perf_counter` 대역 — 호출할 때마다 정해 둔 순서대로 시각을 준다."""

    def __init__(self, *values: float):
        self._values = list(values)

    def __call__(self) -> float:
        return self._values.pop(0)


# --- 1·2. 줄 수와 시간이 경로와 함께 남는다 ----------------------------------------


def test_exhaustive_by_threshold_records_the_visible_lines(tmp_path):
    store = _store(tmp_path)
    expected = _visible_lines(store)
    assert expected > 0

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive_max_lines=10_000)
        return search_trail(), search_stats()

    path, stats = _isolated(run)
    assert path == ["exhaustive"]
    assert [s["visible_lines"] for s in stats] == [expected]


def test_forced_exhaustive_still_records_lines(tmp_path):
    """VIPBot 은 문턱 없이 전수로 가지만 줄 수는 남아야 한다(문턱 조정의 비교 기준)."""
    store = _store(tmp_path)
    expected = _visible_lines(store)

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_stats()

    assert [s["visible_lines"] for s in _isolated(run)] == [expected]


def test_index_path_records_the_lines_above_the_threshold(tmp_path, monkeypatch):
    store = _store(tmp_path)
    expected = _visible_lines(store)
    hit = store.search(QUERY, _ctx(), limit=5, exhaustive=True)[0]
    rel = search_index.rel_path(hit.line.source_path or hit.doc.path, store.root)
    candidate = type("C", (), {"doc_path": rel, "line_no": hit.line.lineno})()
    monkeypatch.setattr(search_index, "candidates", lambda *_a, **_k: [candidate])

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive_max_lines=1)
        return search_trail(), search_stats()

    path, stats = _isolated(run)
    assert path == ["index"]
    assert stats[0]["visible_lines"] == expected  # 문턱(1)보다 큰 실제 줄 수가 그대로 남는다


def test_elapsed_time_is_the_search_only_in_milliseconds(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(store_module.time, "perf_counter", _Clock(10.0, 10.25))

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_stats()

    assert [s["ms"] for s in _isolated(run)] == [250]


def test_real_clock_gives_a_non_negative_integer(tmp_path):
    store = _store(tmp_path)

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_stats()

    (stat,) = _isolated(run)
    assert isinstance(stat["ms"], int) and stat["ms"] >= 0


def test_several_searches_keep_stats_aligned_with_the_path(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(search_index, "candidates", lambda *_a, **_k: None)
    monkeypatch.setattr(store_module.time, "perf_counter", _Clock(0.0, 0.1, 5.0, 5.4))

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive_max_lines=1)
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_trail(), search_stats()

    path, stats = _isolated(run)
    assert path == ["scan_no_index", "exhaustive"]
    assert [s["ms"] for s in stats] == [100, 400]  # 검색마다 자기 시간


def test_a_search_without_tokens_leaves_no_pair(tmp_path):
    """토큰이 없어 일찍 돌아온 검색은 경로도 숫자도 남기지 않는다 — 두 목록이 어긋나지 않는다."""
    store = _store(tmp_path)

    def run():
        begin_search_trail()
        store.search("?!", _ctx(), limit=10)
        return search_trail(), search_stats()

    assert _isolated(run) == ([], [])


# --- 3. 메모장 수명 ----------------------------------------------------------------


def test_without_begin_nothing_is_collected(tmp_path):
    store = _store(tmp_path)

    def run():
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_stats()

    assert _isolated(run) == []


def test_begin_discards_the_previous_questions_stats(tmp_path):
    store = _store(tmp_path)

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        assert len(search_stats()) == 1
        begin_search_trail()
        return search_stats()

    assert _isolated(run) == []


def test_stats_are_bounded_like_the_path(tmp_path):
    store = _store(tmp_path)

    def run():
        begin_search_trail()
        for _ in range(store_module.SEARCH_TRAIL_MAX + 10):
            store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        return search_trail(), search_stats()

    path, stats = _isolated(run)
    assert len(path) == len(stats) == store_module.SEARCH_TRAIL_MAX


def test_returned_stats_are_a_copy(tmp_path):
    store = _store(tmp_path)

    def run():
        begin_search_trail()
        store.search(QUERY, _ctx(), limit=10, exhaustive=True)
        search_stats()[0]["ms"] = 999_999  # 받은 쪽이 고쳐도 메모장은 그대로
        return search_stats()[0]["ms"]

    assert _isolated(run) < 999_999


# --- 4. 동시 질문 ------------------------------------------------------------------


def test_concurrent_questions_do_not_mix_their_numbers(tmp_path):
    """한 질문은 전체를 보고(줄 수 N), 다른 질문은 빈 범위를 본다(줄 수 0). 섞이면 둘이 같아진다."""
    store = _store(tmp_path)
    total = _visible_lines(store)
    assert total > 0
    both_started = threading.Barrier(2, timeout=10)
    seen: dict[str, list[int]] = {}

    def question(name: str, channels):
        begin_search_trail()
        both_started.wait()
        for _ in range(5):
            store.search(QUERY, _ctx(), limit=10, exhaustive=True, channels=channels)
        seen[name] = [s["visible_lines"] for s in search_stats()]

    threads = [
        threading.Thread(target=question, args=("all", None)),
        threading.Thread(target=question, args=("none", frozenset({"#없는-채널"}))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert seen["all"] == [total] * 5
    assert seen["none"] == [0] * 5


# --- 5. 기록 파일과 콘솔 -----------------------------------------------------------


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


def test_qa_record_writes_search_stats_and_old_rows_still_read(tmp_path):
    assert _record().search_stats == []

    stats = [{"visible_lines": 120_000, "ms": 800}, {"visible_lines": 5, "ms": 3}]
    log = QALog(tmp_path, write_md=False)
    log.write(_record(search_path=["exhaustive", "index"], search_stats=stats))
    rows = [
        json.loads(line)
        for path in sorted(tmp_path.glob("qa-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["search_stats"] == stats

    old_row = {k: v for k, v in rows[0].items() if k != "search_stats"}
    assert answer_records.search_stats(old_row) == []


def _row(**over):
    row = {"record_id": "a" * 32, "ts": "2026-11-27T10:00:00+09:00", "workspace": "mgmt",
           "user": "U1", "question": "q", "answer": "a", "hits": 40}
    row.update(over)
    return row


def test_console_passes_the_numbers_in_order_with_camel_case_keys():
    row = _row(search_stats=[{"visible_lines": 120_000, "ms": 800}, {"visible_lines": 5, "ms": 0}])
    expected = [{"visibleLines": 120_000, "ms": 800}, {"visibleLines": 5, "ms": 0}]
    assert answer_records.search_stats(row) == expected
    assert answer_records.detail(row, [])["searchStats"] == expected


def test_console_old_rows_have_no_stats():
    row = _row()
    assert "search_stats" not in row
    assert answer_records.search_stats(row) == []
    assert answer_records.detail(row, [])["searchStats"] == []


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "x",
        {"a": 1},
        3,
        [None],
        ["x"],
        [{"visible_lines": "5", "ms": 1}],
        [{"visible_lines": 5}],
        [{"visible_lines": -1, "ms": 1}],
        [{"visible_lines": 5, "ms": -1}],
        [{"visible_lines": True, "ms": 1}],
        [{"visible_lines": 5, "ms": 1.5}],
    ],
)
def test_console_untrusted_values_become_an_empty_list(bad):
    assert answer_records.search_stats(_row(search_stats=bad)) == []


def test_console_one_bad_item_drops_the_whole_list():
    """일부만 남기면 경로와 짝이 어긋나 엉뚱한 줄에 숫자가 붙는다."""
    row = _row(search_stats=[{"visible_lines": 5, "ms": 1}, "깨짐", {"visible_lines": 9, "ms": 2}])
    assert answer_records.search_stats(row) == []


def test_console_stats_are_capped_like_the_recorder():
    row = _row(search_stats=[{"visible_lines": 1, "ms": 1}] * 40)
    assert len(answer_records.search_stats(row)) == 16


def test_console_stats_stay_when_the_body_is_hidden():
    row = _row(search_stats=[{"visible_lines": 7, "ms": 4}], citations=["인사안.xlsx"])
    row[answer_records.BOT_KEY] = "vipbot"
    hidden = answer_records.detail(row, [], show_body=False)
    assert hidden["bodyHidden"] is True and hidden["question"] == ""
    assert hidden["searchStats"] == [{"visibleLines": 7, "ms": 4}]


# --- 6. 운영 경로 ------------------------------------------------------------------


def test_pilot_fills_stats_at_both_record_sites():
    pilot = (ROOT / "src/tybot/slack/pilot.py").read_text(encoding="utf-8")
    assert pilot.count("search_stats=search_stats()") == 2
    assert pilot.count("search_path=search_trail()") == 2
    assert pilot.index("begin_search_trail()") < pilot.index("self._handle_request(")
