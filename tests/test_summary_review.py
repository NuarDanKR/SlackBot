import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot import summary_review as sr


def _source(text="공사기간은 2026-09-01부터 2026-12-31까지입니다"):
    return [sr.SourceLine("2026-09-15 09:00", "홍길동", text, "a.md:10")]


def test_candidate_requires_an_exact_original_quote():
    raw = json.dumps({"candidates": [{"kind": "new_issue", "current_text": "",
        "proposed_text": "공사기간은 2026-09-01부터 2026-12-31까지입니다",
        "evidence_quote": "원문에 없는 내용", "evidence_at": "거짓", "evidence_author": "거짓"}]})
    assert sr.parse_proposals(raw, _source()) == []


def test_source_identity_comes_from_archive_not_model():
    quote = _source()[0].text
    raw = json.dumps({"candidates": [{"kind": "number_or_schedule", "current_text": "",
        "proposed_text": quote, "evidence_quote": quote,
        "evidence_at": "모델이 만든 시각", "evidence_author": "모델이 만든 사람"}]})
    got = sr.parse_proposals(raw, _source())
    assert got[0].evidence_at == "2026-09-15 09:00"
    assert got[0].evidence_author == "홍길동"


def test_a_number_not_in_evidence_is_rejected():
    quote = "공사기간은 2026-09-01부터입니다"
    raw = json.dumps({"candidates": [{"kind": "number_or_schedule", "current_text": "",
        "proposed_text": "공사기간은 2026-09-02부터입니다", "evidence_quote": quote}]})
    assert sr.parse_proposals(raw, _source(quote)) == []


def test_a_numeric_new_issue_is_still_reviewed_as_a_number():
    quote = "신규 계약금액은 1,200억원입니다"
    raw = json.dumps({"candidates": [{"kind": "new_issue", "current_text": "",
        "proposed_text": quote, "evidence_quote": quote}]})

    got = sr.parse_proposals(raw, _source(quote))

    assert got[0].kind == "number_or_schedule"


def test_model_cannot_invent_the_current_approved_text():
    quote = "새 일정은 2026-09-15입니다"
    raw = json.dumps({"candidates": [{"kind": "number_or_schedule",
        "current_text": "기존 일정은 2026-09-14입니다", "proposed_text": quote,
        "evidence_quote": quote}]})
    assert sr.parse_proposals(raw, _source(quote), approved=[]) == []


def test_candidate_text_is_extractive_not_a_new_claim():
    quote = "착공일은 2026-09-15입니다"
    raw = json.dumps({"candidates": [{"kind": "new_issue", "current_text": "",
        "proposed_text": "착공일은 2026-09-15로 확정됐습니다", "evidence_quote": quote}]})
    assert sr.parse_proposals(raw, _source(quote)) == []


def test_invalid_json_fails_closed():
    with pytest.raises(sr.SummaryReviewError):
        sr.parse_proposals("not-json", _source())


def test_json_code_fence_is_tolerated_but_still_verified():
    quote = _source()[0].text
    raw = "```json\n" + json.dumps({"candidates": [{"kind": "new_issue",
        "current_text": "", "proposed_text": quote, "evidence_quote": quote}]}) + "\n```"
    assert sr.parse_proposals(raw, _source())[0].proposed_text == quote


def test_review_blocks_have_nonempty_candidate_ids():
    rows = [{"id": "123", "kind": "new_issue", "current_text": "", "proposed_text": "새 일정",
             "evidence_author": "홍길동", "evidence_at": "2026-09-15", "evidence_quote": "새 일정이 확정됐습니다"}]
    blocks = sr.candidate_blocks("#공지", rows)
    actions = next(x for x in blocks if x["type"] == "actions")
    assert {x["action_id"] for x in actions["elements"]} == {sr.ACTION_APPROVE, sr.ACTION_REJECT, sr.ACTION_DEFER}
    assert all(x["value"] for x in actions["elements"])


def test_numeric_candidates_are_first_and_show_values_to_verify():
    rows = [
        {"id": "issue", "kind": "new_issue", "current_text": "",
         "proposed_text": "신규 쟁점이 있습니다", "evidence_author": "김현장",
         "evidence_at": "2026-09-15", "evidence_quote": "신규 쟁점이 있습니다"},
        {"id": "number", "kind": "number_or_schedule", "current_text": "",
         "proposed_text": "공정률은 62.5%, 금액은 1,200억원입니다",
         "evidence_author": "홍길동", "evidence_at": "2026-09-16",
         "evidence_quote": "공정률은 62.5%, 금액은 1,200억원입니다"},
    ]

    blocks = sr.candidate_blocks("#현장", rows)
    sections = [
        block["text"]["text"] for block in blocks if block["type"] == "section"
    ]

    assert "숫자·금액·비율·날짜 확인 1건" in sections[0]
    assert "*확인할 값* `62.5%` · `1,200억원`" in sections[1]
    assert "신규 쟁점" in sections[2]


def test_reject_modal_requires_correction():
    blocks = {b["block_id"]: b for b in sr.reject_modal("123")["blocks"]}
    assert blocks["correction"]["element"]["min_length"] == sr.MIN_CORRECTION
    # 「틀린 부분」 분류도 필수다 — 분류가 없으면 같은 실수를 세어 볼 수 없다.
    assert blocks["wrong_part"]["element"]["type"] == "static_select"


def test_defer_is_until_tomorrow():
    assert sr.default_defer_date(date(2026, 9, 15)) == date(2026, 9, 16)


def test_prompt_budget_does_not_cut_new_originals():
    source = _source("중요 원문")
    body = sr.prompt_input(approved=["가" * (sr.MAX_APPROVED_CHARS + 100)], source=source)
    assert "중요 원문" in body
    assert len(body) <= sr.MAX_SOURCE_CHARS


def test_first_generation_only_looks_back_one_day():
    class FakeStore:
        conn = SimpleNamespace(rollback=lambda: None)
        def cursor(self, *_): return ""
        def start_at(self, *_): return "2020-01-01 00:00"
        def may_attempt(self, *_): return True
        def approved(self, *_): return []
        def save_run(self, **_): raise AssertionError("오래된 원문은 후보가 되면 안 된다")
    old = SimpleNamespace(ts="2026-09-10 09:00", speaker="홍길동", text="오래된 원문",
                          lineno=1, source_path=Path("old.md"))
    archive = SimpleNamespace(docs=lambda: [SimpleNamespace(
        workspace="ws", channel_id="C1", raw_lines=[old], path=Path("old.md"))])
    assert sr.generate_channel(
        FakeStore(), archive, workspace="ws", channel_id="C1", channel_name="#채널",
        complete=lambda *_: "should not run", now=sr.datetime(2026, 9, 15, tzinfo=sr.KST),
    ) == 0


def test_channel_source_is_scoped_and_never_reads_bot_lines():
    human = SimpleNamespace(ts="2026-09-15 09:00", speaker="홍길동", text="사람 원문",
                            lineno=10, source_path=Path("2026-09-15.md"))
    bot = SimpleNamespace(ts="2026-09-15 09:01", speaker="TYBot", text="봇 요약",
                          lineno=11, source_path=Path("2026-09-15.md"))
    docs = [SimpleNamespace(workspace="ws", channel_id="C1", raw_lines=[human, bot],
                            path=Path("fallback.md")),
            SimpleNamespace(workspace="ws", channel_id="C2", raw_lines=[human],
                            path=Path("other.md"))]
    got = sr.channel_source(SimpleNamespace(docs=lambda: docs), "ws", "C1", "")
    assert [line.text for line in got] == ["사람 원문"]


def test_schema_keeps_approved_summaries_out_of_raw_archive():
    sql = Path("deploy/sql/summary_review_schema.sql").read_text(encoding="utf-8")
    assert "summary_review_candidate" in sql
    assert "approved_summary_item" in sql
    assert "raw_line" not in sql
    assert "sent.recipient=%s" in Path("src/tybot/summary_review.py").read_text(encoding="utf-8")
    assert "enable --now tybot-review-dm.timer" in Path("deploy/update.sh").read_text(encoding="utf-8")
