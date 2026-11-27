"""QA 기록의 요약 스캔 숫자 (B-93) — 채널당 상한(기본 60줄)에 요약이 얼마나 잘리는지 잰다.

요약(`AnswerEngine.summarize`)은 `store.search` 를 거치지 않는 기간 스캔이라 검색 경로·검색 줄 수
기록에는 나오지 않는다. 60줄 값에는 정해 둔 근거가 없어서, 바꾸기 전에 숫자가 필요하다.

거는 것:
1. 기간 스캔 요약이 「읽은 줄 → 기간 안 줄 → 쓴 줄」, 상한에 걸린 채널 수, 스캔 시간을 남긴다
2. 숫자는 요약의 **동작을 바꾸지 않는다**(같은 근거 블록·같은 건수)
3. 기간 스캔이 아닌 요약(문서 집합·좁혀 묻는 후속)은 숫자를 남기지 않는다
4. 메모장이 없으면 모으지 않고, 질문이 바뀌면 이전 값이 남지 않으며, 동시 질문이 섞이지 않는다
5. QARecord 가 기록 파일에 쓰고, 콘솔이 믿을 수 있는 값만 내보낸다
6. 운영 경로(pilot)가 두 기록 자리에 값을 넣는다
"""

from __future__ import annotations

import contextvars
import json
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot import answer as answer_module
from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive import store as store_module
from tybot.archive import writer
from tybot.archive.store import ArchiveStore, begin_search_trail, summary_stats
from tybot.audit import QALog, QARecord
from tybot.console import answer_records
from tybot.gateway.base import LLMResponse, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router

ROOT = Path(__file__).resolve().parents[1]
WS = "mgmt"
BIG, BIG_ID = "#팀-기획_abb110-경영계획수립", "C0PLAN0001"
SMALL, SMALL_ID = "#팀-재무_abb120-자금관리", "C0FUND0001"
RECENT_BIG, OLD_BIG, RECENT_SMALL = 100, 10, 5  # 큰 채널은 상한(60)을 넘는다


class _Fake:
    name = "anthropic"

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        return LLMResponse("정리 결과", spec.model, self.name, 200, 40, 0.001)


def _engine(root: Path, **kw) -> AnswerEngine:
    router = Router(
        providers={"anthropic": _Fake()},
        registry={
            "claude-sonnet-5": ModelSpec(
                "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
            )
        },
        cost_guard=CostGuard(10.0),
    )
    return AnswerEngine(
        ArchiveStore(root), router, allow_master_business_answers=True, **kw
    )


def _ingest(root: Path, channel: str, channel_id: str, *, recent: int, old: int = 0) -> None:
    now = datetime.now(UTC)
    # 영문 약어만 쓴다 — 이름처럼 보이는 한글 낱말은 수집 단계 마스킹이 `[이름1]` 로 바꾼다.
    messages = [
        writer.IncomingMessage(now - timedelta(minutes=i), "담당자", f"ESG 항목 {i}")
        for i in range(recent)
    ] + [
        writer.IncomingMessage(now - timedelta(days=30, minutes=i), "담당자", f"ESG 옛 항목 {i}")
        for i in range(old)
    ]
    writer.ingest(
        root, workspace=WS, channel=channel, channel_id=channel_id, acl=[channel], messages=messages
    )


@pytest.fixture
def root(tmp_path):
    _ingest(tmp_path, BIG, BIG_ID, recent=RECENT_BIG, old=OLD_BIG)
    _ingest(tmp_path, SMALL, SMALL_ID, recent=RECENT_SMALL)
    return tmp_path


def _ctx(*channels: str) -> RequestContext:
    return RequestContext(workspace=WS, channels=frozenset(channels or {BIG, SMALL}))


def _run(fn):
    """다른 시험이 남긴 메모장과 섞이지 않게 빈 문맥에서 실행한다."""
    return contextvars.Context().run(fn)


def _visible(root: Path, ctx: RequestContext) -> int:
    return sum(len(d.raw_lines) for d in ArchiveStore(root).visible_docs(ctx))


# --- 1. 숫자 -----------------------------------------------------------------------


def test_period_scan_records_the_funnel_and_the_cap(root):
    engine = _engine(root)
    ctx = _ctx()
    total = _visible(root, ctx)
    assert total == RECENT_BIG + OLD_BIG + RECENT_SMALL

    def run():
        begin_search_trail()
        engine.summarize(ctx, days=7)
        return summary_stats()

    (stat,) = _run(run)
    assert stat["visible_lines"] == total
    assert stat["window_lines"] == RECENT_BIG + RECENT_SMALL  # 30일 전 줄은 기간 밖
    assert stat["used_lines"] == 60 + RECENT_SMALL  # 큰 채널만 60줄에 잘린다
    assert stat["channels"] == 2
    assert stat["capped_channels"] == 1
    assert stat["ms"] >= 0


def test_nothing_is_capped_when_the_limit_is_larger(root):
    engine = _engine(root, max_lines_per_channel=500)

    def run():
        begin_search_trail()
        engine.summarize(_ctx(), days=7)
        return summary_stats()

    (stat,) = _run(run)
    assert stat["used_lines"] == stat["window_lines"] == RECENT_BIG + RECENT_SMALL
    assert stat["capped_channels"] == 0


def test_a_channel_exactly_at_the_limit_is_not_counted_as_capped(root):
    engine = _engine(root, max_lines_per_channel=RECENT_BIG)

    def run():
        begin_search_trail()
        engine.summarize(_ctx(), days=7)
        return summary_stats()

    (stat,) = _run(run)
    assert stat["used_lines"] == RECENT_BIG + RECENT_SMALL
    assert stat["capped_channels"] == 0  # 잘린 줄이 없으면 「잘린 채널」 이 아니다


def test_all_time_widens_the_window(root):
    engine = _engine(root)

    def run():
        begin_search_trail()
        engine.summarize(_ctx(), all_time=True)
        return summary_stats()

    (stat,) = _run(run)
    assert stat["window_lines"] == RECENT_BIG + OLD_BIG + RECENT_SMALL


def test_a_topic_that_matches_nothing_records_zero_window_but_still_the_scan(root):
    engine = _engine(root)
    ctx = _ctx()

    def run():
        begin_search_trail()
        engine.summarize(ctx, days=7, terms=["ZZQX"])
        return summary_stats()

    (stat,) = _run(run)
    assert stat["visible_lines"] == _visible(root, ctx)  # 읽기는 했다
    assert stat["window_lines"] == stat["used_lines"] == stat["channels"] == 0


def test_the_permitted_scope_is_what_is_counted(root):
    """권한이 좁으면 읽은 줄도 줄어든다 — 줄 수는 질문자마다 다르다."""
    engine = _engine(root)
    ctx = _ctx(SMALL)

    def run():
        begin_search_trail()
        engine.summarize(ctx, days=7)
        return summary_stats()

    (stat,) = _run(run)
    assert stat["visible_lines"] == _visible(root, ctx) == RECENT_SMALL
    assert stat["channels"] == 1


def test_scan_time_is_milliseconds_of_the_scan_only(root, monkeypatch):
    engine = _engine(root)
    ticks = iter([5.0, 5.25])
    monkeypatch.setattr(
        answer_module, "time", SimpleNamespace(perf_counter=lambda: next(ticks))
    )

    def run():
        begin_search_trail()
        engine.summarize(_ctx(), days=7)
        return summary_stats()

    (stat,) = _run(run)
    assert stat["ms"] == 250


# --- 2. 동작은 그대로 --------------------------------------------------------------


def test_recording_does_not_change_what_the_summary_returns(root):
    """메모장이 있든 없든 근거 건수와 출처는 같다 — 숫자는 곁에서 셀 뿐이다."""
    engine = _engine(root)
    ctx = _ctx()

    plain = _run(lambda: engine.summarize(ctx, days=7))

    def with_trail():
        begin_search_trail()
        return engine.summarize(ctx, days=7)

    traced = _run(with_trail)
    assert plain.reason == traced.reason == "answered"
    assert plain.hit_count == traced.hit_count == 60 + RECENT_SMALL
    assert plain.citations == traced.citations


# --- 3. 기간 스캔이 아닌 요약 ------------------------------------------------------


def test_scoped_follow_up_summary_records_nothing(root):
    """좁혀 묻는 후속(`evidence_hits`)은 기간 스캔으로 돌아가지 않는다 — 숫자도 없다."""
    engine = _engine(root)

    def run():
        begin_search_trail()
        engine.summarize(_ctx(), days=7, evidence_hits=[])
        return summary_stats()

    assert _run(run) == []


def test_document_set_summary_records_nothing(root):
    """문서 집합 요약은 채널당 N줄이 아니라 문서 몫이다 — 이 숫자의 대상이 아니다."""
    engine = _engine(root)

    def run():
        begin_search_trail()
        engine.summarize(_ctx(), days=7, document_query=["경영계획"], terms=["ESG"])
        return summary_stats()

    assert _run(run) == []


# --- 4. 메모장 수명·동시 질문 -------------------------------------------------------


def test_without_begin_nothing_is_collected(root):
    engine = _engine(root)

    def run():
        engine.summarize(_ctx(), days=7)
        return summary_stats()

    assert _run(run) == []


def test_begin_discards_the_previous_question(root):
    engine = _engine(root)

    def run():
        begin_search_trail()
        engine.summarize(_ctx(), days=7)
        assert len(summary_stats()) == 1
        begin_search_trail()
        return summary_stats()

    assert _run(run) == []


def test_the_pad_is_bounded(root):
    engine = _engine(root)

    def run():
        begin_search_trail()
        for _ in range(store_module.SEARCH_TRAIL_MAX + 5):
            engine.summarize(_ctx(), days=7)
        return summary_stats()

    assert len(_run(run)) == store_module.SEARCH_TRAIL_MAX


def test_returned_stats_are_a_copy(root):
    engine = _engine(root)

    def run():
        begin_search_trail()
        engine.summarize(_ctx(), days=7)
        summary_stats()[0]["used_lines"] = 999_999
        return summary_stats()[0]["used_lines"]

    assert _run(run) == 60 + RECENT_SMALL


def test_concurrent_questions_do_not_mix_their_numbers(root):
    engine = _engine(root)
    barrier = threading.Barrier(2, timeout=10)
    seen: dict[str, list[dict[str, int]]] = {}

    def question(name: str, ctx: RequestContext):
        begin_search_trail()
        barrier.wait()
        for _ in range(4):
            engine.summarize(ctx, days=7)
        seen[name] = summary_stats()

    threads = [
        threading.Thread(target=question, args=("big", _ctx(BIG))),
        threading.Thread(target=question, args=("small", _ctx(SMALL))),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert [s["window_lines"] for s in seen["big"]] == [RECENT_BIG] * 4
    assert [s["window_lines"] for s in seen["small"]] == [RECENT_SMALL] * 4
    assert [s["capped_channels"] for s in seen["big"]] == [1] * 4
    assert [s["capped_channels"] for s in seen["small"]] == [0] * 4


# --- 5. 기록 파일과 콘솔 -----------------------------------------------------------

STAT = {
    "visible_lines": 124_000, "window_lines": 480, "used_lines": 120,
    "channels": 8, "capped_channels": 3, "ms": 90,
}
STAT_CAMEL = {
    "visibleLines": 124_000, "windowLines": 480, "usedLines": 120,
    "channels": 8, "cappedChannels": 3, "ms": 90,
}


def _record(**extra) -> QARecord:
    return QARecord.build(
        workspace=WS, channel=BIG, channel_id=BIG_ID, user="U1", user_name="tester",
        question="질문", intent_kind="summary", intent_source="rule", reason="answered",
        hits=3, scope="채널 2개", **extra,
    )


def test_qa_record_writes_summary_stats_and_old_rows_still_read(tmp_path):
    assert _record().summary_stats == []
    log = QALog(tmp_path, write_md=False)
    log.write(_record(summary_stats=[STAT]))
    rows = [
        json.loads(line)
        for path in sorted(tmp_path.glob("qa-*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert rows[0]["summary_stats"] == [STAT]
    old_row = {k: v for k, v in rows[0].items() if k != "summary_stats"}
    assert answer_records.summary_stats(old_row) == []


def _row(**over):
    row = {"record_id": "a" * 32, "ts": "2026-11-27T10:00:00+09:00", "workspace": "mgmt",
           "user": "U1", "question": "q", "answer": "a", "hits": 120}
    row.update(over)
    return row


def test_console_passes_the_numbers_with_camel_case_keys():
    row = _row(summary_stats=[STAT])
    assert answer_records.summary_stats(row) == [STAT_CAMEL]
    assert answer_records.detail(row, [])["summaryStats"] == [STAT_CAMEL]


def test_console_old_rows_and_non_summary_rows_have_no_numbers():
    row = _row()
    assert answer_records.summary_stats(row) == []
    assert answer_records.detail(row, [])["summaryStats"] == []


@pytest.mark.parametrize(
    "bad",
    [
        None, "x", {"a": 1}, 3, [None], ["x"],
        [{**STAT, "ms": "1"}],
        [{**STAT, "used_lines": -1}],
        [{**STAT, "channels": True}],
        [{**STAT, "ms": 1.5}],
        [{k: v for k, v in STAT.items() if k != "capped_channels"}],  # 키 하나 빠짐
    ],
)
def test_console_untrusted_values_become_an_empty_list(bad):
    assert answer_records.summary_stats(_row(summary_stats=bad)) == []


def test_console_one_bad_item_drops_the_whole_list():
    assert answer_records.summary_stats(_row(summary_stats=[STAT, "깨짐"])) == []


def test_console_numbers_are_capped_like_the_recorder():
    assert len(answer_records.summary_stats(_row(summary_stats=[STAT] * 40))) == 16


def test_console_numbers_stay_when_the_body_is_hidden():
    row = _row(summary_stats=[STAT], citations=["인사안.xlsx"])
    row[answer_records.BOT_KEY] = "vipbot"
    hidden = answer_records.detail(row, [], show_body=False)
    assert hidden["bodyHidden"] is True and hidden["question"] == ""
    assert hidden["summaryStats"] == [STAT_CAMEL]


def test_console_keys_match_what_the_recorder_writes():
    """기록하는 키와 콘솔이 읽는 키가 어긋나면(한쪽만 고치면) 콘솔은 조용히 빈 목록이 된다."""
    assert set(answer_records._SUMMARY_KEYS) == set(store_module.SUMMARY_STAT_KEYS)
    assert set(answer_records._SUMMARY_CAMEL) == set(store_module.SUMMARY_STAT_KEYS)


# --- 6. 운영 경로 ------------------------------------------------------------------


def test_pilot_fills_summary_stats_at_both_record_sites():
    pilot = (ROOT / "src/tybot/slack/pilot.py").read_text(encoding="utf-8")
    assert pilot.count("summary_stats=summary_stats()") == 2
    assert pilot.index("begin_search_trail()") < pilot.index("self._handle_request(")
