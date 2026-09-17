import json
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path
from types import SimpleNamespace

import pytest

from tybot import summary_review as sr
from tybot.evidence_refs import content_hash


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

    assert "숫자·금액·비율·날짜 포함 1건" in sections[0]
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


def test_contract_asks_for_non_numeric_daily_context_too():
    prompt = sr.contract_prompt()
    assert "진행 상황" in prompt
    assert "후속 조치" in prompt
    assert "숫자가 없는" in prompt


def test_first_generation_only_looks_back_one_day():
    class FakeStore:
        conn = SimpleNamespace(rollback=lambda: None)
        def cursor(self, *_): return ""
        def start_at(self, *_): return "2020-01-01 00:00"
        def source_run_seen(self, *_):
            return False

        def advance(self, *_):
            pass

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


def test_manual_generation_bypasses_the_failed_digest_backoff():
    class FakeStore:
        conn = SimpleNamespace(rollback=lambda: None)

        def cursor(self, *_):
            return ""

        def start_at(self, *_):
            return "2026-09-15 00:00"

        def source_run_seen(self, *_):
            return False

        def advance(self, *_):
            pass

        def may_attempt(self, *_):
            return False

        def approved(self, *_):
            return []

        def save_run(self, **kwargs):
            return len(kwargs["proposals"])

    line = SimpleNamespace(
        ts="2026-09-16 09:00",
        speaker="홍길동",
        text="공정률은 62.5%입니다",
        lineno=1,
        source_path=Path("2026-09-16.md"),
        message_ts="1757980800.000001",
    )
    archive = SimpleNamespace(docs=lambda: [SimpleNamespace(
        workspace="ws", channel_id="C1", raw_lines=[line], path=Path("fallback.md")
    )])
    proposal = json.dumps({"candidates": [{
        "kind": "number_or_schedule",
        "current_text": "",
        "proposed_text": line.text,
        "evidence_quote": line.text,
    }]})

    got = sr.generate_channel(
        FakeStore(), archive, workspace="ws", channel_id="C1", channel_name="#채널",
        complete=lambda *_: proposal,
        now=sr.datetime(2026, 9, 17, tzinfo=sr.KST),
        force=True,
    )

    assert got == 1


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
    assert "'expired'" in sql
    assert "evidence_hash" in sql


def test_the_evidence_coordinate_comes_from_the_archive_not_the_model():
    """출처 링크가 모델 출력이면 사람이 확인하러 간 자리에 그 문장이 없다(B-56)."""
    quote = "공정률은 62.5%입니다"
    source = [sr.SourceLine(
        "2026-09-15 09:00", "홍길동", quote, "2026-09-15.md:10",
        message_ts="1758012345.123456",
    )]
    raw = json.dumps({"candidates": [{"kind": "number_or_schedule", "current_text": "",
        "proposed_text": quote, "evidence_quote": quote,
        "evidence_message_ts": "9999999999.000000"}]})

    got = sr.parse_proposals(raw, source)

    assert got[0].evidence_message_ts == "1758012345.123456"
    assert got[0].evidence_hash == content_hash(
        "2026-09-15 09:00", "홍길동", quote
    )


def test_message_link_needs_both_channel_and_coordinate():
    assert sr.message_link("C1", "1758012345.123456") == (
        "https://slack.com/archives/C1/p1758012345123456"
    )
    assert sr.message_link("C1", "") == ""
    assert sr.message_link("", "1758012345.123456") == ""


# --- 무엇이 실제로 일어났는가 (2026-09-17) -------------------------------------
# 콘솔은 「실행 완료」만 보이고 아무도 DM을 못 받았다. 종료 코드 0이 「보낼 것이
# 없었다」와 「보냈다」를 같은 글자로 만들었기 때문이다. run() 이 채널마다 사유를
# 남기는지 실제 호출 경로로 확인한다.
class _OutcomeStore:
    def __init__(self, *, rows, reviewers, already, generated=True):
        self._rows = rows
        self._reviewers = reviewers
        self._already = already
        self._generated = generated
        self.delivered = []
        self.stamped = []

    def __call__(self, conn):
        return self

    def expire_unconfirmed(self, workspace, channel_id, on):
        return 0

    def generated_on(self, workspace, channel_id, on):
        return self._generated

    def pending(self, workspace, channel_id, on):
        return list(self._rows)

    def reviewer_recipients(self, workspace, channel_id):
        return list(self._reviewers)

    def artifact_rows(self, artifact_id):
        return list(self._rows)

    def delivery_sent(self, artifact_id, recipient):
        return recipient in self._already

    def record_delivery(self, artifact_id, recipient, **kwargs):
        if kwargs.get("state", "sent") == "sent":
            self.delivered.append(recipient)

    def mark_delivered(self, candidate_ids):
        self.stamped += list(candidate_ids)


class _OutcomeClient:
    def __init__(self):
        self.posted = []

    def conversations_open(self, users):
        return {"channel": {"id": f"D{users}"}}

    def chat_postMessage(self, **kwargs):
        self.posted.append(kwargs["channel"])
        return {"ts": "1.0"}


def _outcome_run(
    monkeypatch, *, rows, reviewers, already, resend=False, force_generate=False,
    deliver_only=False, generated=True, generate_only=False,
):
    from types import SimpleNamespace

    from tybot import summary_review as sr

    store = _OutcomeStore(rows=rows, reviewers=reviewers, already=already,
                          generated=generated)
    monkeypatch.setattr(sr, "Store", store)
    monkeypatch.setattr(
        sr, "_canvas_round",
        lambda *a, **k: {"artifact_id": "A1", "permalink": "https://x"},
    )
    monkeypatch.setattr(sr, "canvas_review_blocks", lambda **k: [])
    client = _OutcomeClient()
    result = sr.run(
        SimpleNamespace(rollback=lambda: None),
        {"tyit": client},
        archive=None,
        channels=[("tyit", "C1", "주간보고", dt_time.min)],
        complete=lambda *a, **k: "",
        now=datetime(2026, 9, 17, 18, 0, tzinfo=sr.KST),
        resend=resend,
        force_generate=force_generate,
        deliver_only=deliver_only,
        generate_only=generate_only,
    )
    return result, client, store


def test_run_says_no_candidates_instead_of_silently_finishing(monkeypatch):
    result, client, _store = _outcome_run(monkeypatch, rows=[], reviewers=["U1"], already=set())

    assert result.sent == 0
    assert client.posted == []
    assert [row.code for row in result.outcomes] == ["no-candidates"]


def test_run_says_no_reviewer_when_nobody_is_registered(monkeypatch):
    result, _client, _store = _outcome_run(
        monkeypatch, rows=[{"id": 1}], reviewers=[], already=set()
    )

    assert [row.code for row in result.outcomes] == ["no-reviewer"]


def test_run_says_already_sent_rather_than_reporting_success(monkeypatch):
    result, client, _store = _outcome_run(
        monkeypatch, rows=[{"id": 1}], reviewers=["U1"], already={"U1"}
    )

    assert result.sent == 0
    assert client.posted == []
    assert [row.code for row in result.outcomes] == ["already-sent"]


def test_resend_pushes_to_a_reviewer_who_already_got_today(monkeypatch):
    """운영자가 직접 「다시 보내기」를 켠 회차에만 오늘 이력을 넘어선다."""
    result, client, _store = _outcome_run(
        monkeypatch, rows=[{"id": 1}], reviewers=["U1"], already={"U1"}, resend=True
    )

    assert result.sent == 1
    assert client.posted == ["DU1"]
    assert [row.code for row in result.outcomes] == ["sent"]


def test_manual_run_checks_new_source_even_after_an_earlier_run_today(monkeypatch):
    """수동 버튼 뒤 새 대화가 생겼다면 `generated_on` 때문에 놓치면 안 된다."""
    from tybot import summary_review as sr

    called = []
    monkeypatch.setattr(
        sr,
        "generate_channel",
        lambda *args, **kwargs: called.append(
            (kwargs["channel_id"], kwargs["force"])
        ) or 0,
    )

    result, _client, _store = _outcome_run(
        monkeypatch,
        rows=[],
        reviewers=["U1"],
        already=set(),
        force_generate=True,
    )

    assert called == [("C1", True)]
    # 읽을 원문이 없었다는 뜻이다. 「후보가 없다」 로 뭉치면 소급이 답인지 모른다.
    assert [row.code for row in result.outcomes] == ["no-new-source"]


def test_scheduled_run_keeps_the_once_per_day_generation_lock(monkeypatch):
    from tybot import summary_review as sr

    monkeypatch.setattr(
        sr,
        "generate_channel",
        lambda *args, **kwargs: pytest.fail("scheduled run regenerated today's source"),
    )

    _outcome_run(monkeypatch, rows=[], reviewers=["U1"], already=set())


# --- 소급 검토 (B-58) ---------------------------------------------------------
# 수집은 됐는데 검토 후보가 한 번도 안 만들어진 구간이 있었다. 최초 회차가
# 「검토자 지정 이후」와 「최근 하루」로 두 번 좁혀지고 커서는 앞으로만 간다.
def _line(ts, text, lineno):
    return SimpleNamespace(ts=ts, speaker="홍길동", text=text, lineno=lineno,
                           source_path=Path("archive.md"), message_ts="")


def _archive(lines):
    return SimpleNamespace(docs=lambda: [SimpleNamespace(
        workspace="ws", channel_id="C1", raw_lines=list(lines),
        path=Path("fallback.md"),
    )])


def test_a_channel_collected_before_the_reviewer_was_named_is_not_reviewable_without_backfill():
    """이게 「수집은 됐는데 검토 DM이 안 온다」의 원인이다."""
    archive = _archive([_line("2026-06-01 09:00", "6월 원문", 1)])

    # 최초 회차 규칙: 검토자 지정 시각 이후만 본다.
    assert sr.channel_source(archive, "ws", "C1", "", start_at="2026-09-01 00:00") == []
    # 소급은 같은 원문을 본다.
    got = sr.channel_source(archive, "ws", "C1", sr.backfill_watermark())
    assert [line.text for line in got] == ["6월 원문"]


def test_backfill_since_includes_the_named_day():
    archive = _archive([
        _line("2026-05-31 23:59", "전날", 1),
        _line("2026-06-01 00:00", "그날 0시", 2),
        _line("2026-06-02 09:00", "다음날", 3),
    ])

    got = sr.channel_source(archive, "ws", "C1", sr.backfill_watermark("2026-06-01"))

    assert [line.text for line in got] == ["그날 0시", "다음날"]


def test_backfill_estimate_counts_everything_not_one_round(monkeypatch):
    """자른 목록으로 세면 몇 달치가 늘 「한 회차」로 보인다."""
    monkeypatch.setattr(sr, "MAX_NEW_SOURCE_CHARS", 100)
    lines = [_line(f"2026-06-{day:02d} 09:00", "가" * 60, day) for day in range(1, 11)]

    found = sr.backfill_estimate(_archive(lines), workspace="ws", channel_id="C1")

    assert found.lines == 10
    assert found.rounds > 1
    assert found.first_at == "2026-06-01 09:00"
    assert found.last_at == "2026-06-10 09:00"


class _BackfillStore:
    """커서가 회차마다 전진하는지 보려고 실제 커서 값을 흉내 낸다."""

    def __init__(self, seen=False):
        self.watermark = "2026-09-17 00:00|z"
        self.rewound_to = None
        self.rounds = 0
        self.seen = seen

    def cursor(self, workspace, channel_id):
        return self.watermark

    def rewind(self, workspace, channel_id, watermark):
        self.rewound_to = watermark
        self.watermark = watermark

    def start_at(self, workspace, channel_id):
        return "2026-09-01 00:00"

    def approved(self, workspace, channel_id):
        return []

    def source_run_seen(self, *_):
        return self.seen

    def advance(self, workspace, channel_id, watermark, digest):
        self.watermark = watermark

    def may_attempt(self, *args, **kwargs):
        return True

    def save_run(self, **kwargs):
        self.watermark = kwargs["watermark"]
        self.rounds += 1
        return len(kwargs["proposals"])


def _backfill(monkeypatch, *, lines, rounds=10, resume=False, since=""):
    monkeypatch.setattr(sr, "MAX_NEW_SOURCE_CHARS", 90)
    store = _BackfillStore()
    done = sr.backfill_channel(
        store, _archive(lines), workspace="ws", channel_id="C1", channel_name="#채널",
        complete=lambda *_: json.dumps({"candidates": []}),
        now=datetime(2026, 9, 17, tzinfo=sr.KST),
        since=since, max_rounds=rounds, resume=resume,
    )
    return store, done


def test_backfill_walks_the_whole_history_in_rounds(monkeypatch):
    lines = [_line(f"2026-06-{day:02d} 09:00", "가" * 50, day) for day in range(1, 8)]

    store, done = _backfill(monkeypatch, lines=lines)

    assert store.rewound_to == sr.EPOCH_WATERMARK
    assert done.rounds > 1
    assert done.exhausted
    # 마지막 줄까지 읽었다 — 커서가 그 줄을 가리킨다.
    assert store.watermark.startswith("2026-06-07 09:00")


def test_backfill_stops_at_the_round_cap_and_says_it_is_not_done(monkeypatch):
    """회차마다 LLM 을 한 번 부른다. 무제한이면 채널 하나가 비용 한도를 다 쓴다."""
    lines = [_line(f"2026-06-{day:02d} 09:00", "가" * 50, day) for day in range(1, 20)]

    _store, done = _backfill(monkeypatch, lines=lines, rounds=2)

    assert done.rounds == 2
    assert not done.exhausted


def test_backfill_resume_does_not_rewind_the_cursor(monkeypatch):
    """되돌리기는 「이미 본 구간을 다시 본다」다. 이어 가기는 그걸 하지 않는다."""
    lines = [_line("2026-06-01 09:00", "6월 원문", 1)]

    store, _done = _backfill(monkeypatch, lines=lines, resume=True)

    assert store.rewound_to is None


def test_a_delivered_candidate_is_stamped_through_the_real_send_path(monkeypatch):
    """폐기는 이 기록만 보고 판단한다. 배선이 빠지면 보여 준 후보가 영원히 안 죽는다."""
    result, _client, store = _outcome_run(
        monkeypatch, rows=[{"id": "cand-1"}], reviewers=["U1"], already=set()
    )

    assert result.sent == 1
    assert store.stamped == ["cand-1"]


# --- 「새 후보가 없다」를 셋으로 가른다 (2026-09-17) ---------------------------
def test_no_new_source_says_backfill_is_the_answer():
    stats = sr.GenerateStats(lines=0)

    assert sr._empty_reason(stats) == sr.OUTCOME_NO_SOURCE
    assert "소급" in sr.OUTCOME_LABELS[sr.OUTCOME_NO_SOURCE]


def test_source_read_but_nothing_passed_verification_is_a_different_reason():
    """소급해도 결과가 같다 — 원문은 읽었는데 대조에서 다 떨어진 것이다."""
    stats = sr.GenerateStats(lines=120, proposed=4, accepted=0)

    assert sr._empty_reason(stats) == sr.OUTCOME_NO_ACCEPTED
    assert sr._empty_detail(stats) == "원문 120줄 · 요약기 제안 4건 · 대조 통과 0건"


def test_a_skipped_generation_keeps_the_plain_reason():
    assert sr._empty_reason(None) == sr.OUTCOME_NO_CANDIDATES
    assert sr._empty_detail(None) == ""


def test_the_real_run_reports_why_nothing_was_generated(monkeypatch):
    """배선 확인 — 통계가 run() 까지 오지 않으면 화면은 다시 한 문장만 보인다."""
    def _generate(*args, **kwargs):
        stats = kwargs["stats"]
        stats.lines, stats.proposed, stats.accepted = 90, 3, 0
        return 0

    monkeypatch.setattr(sr, "generate_channel", _generate)
    result, _client, _store = _outcome_run(
        monkeypatch, rows=[], reviewers=["U1"], already=set(), force_generate=True,
    )

    assert [row.code for row in result.outcomes] == ["no-accepted-candidate"]
    assert result.outcomes[0].as_dict()["detail"] == "원문 90줄 · 요약기 제안 3건 · 대조 통과 0건"


def test_parse_proposals_reports_how_many_it_threw_away():
    stats = sr.GenerateStats()
    raw = json.dumps({"candidates": [
        {"kind": "new_issue", "current_text": "", "proposed_text": "원문에 없는 문장",
         "evidence_quote": "원문에 없는 문장"},
        {"kind": "number_or_schedule", "current_text": "",
         "proposed_text": "공사기간은 2026-09-01부터 2026-12-31까지입니다",
         "evidence_quote": "공사기간은 2026-09-01부터 2026-12-31까지입니다"},
    ]})

    got = sr.parse_proposals(raw, _source(), stats=stats)

    assert stats.proposed == 2
    assert stats.accepted == len(got) == 1


def test_generate_channel_fills_the_stats_it_was_given():
    """배선 확인 — `parse_proposals` 까지 통계가 가지 않으면 화면은 다시 침묵한다."""
    class FakeStore:
        conn = SimpleNamespace(rollback=lambda: None)

        def cursor(self, *_):
            return ""

        def start_at(self, *_):
            return "2026-09-15 00:00"

        def source_run_seen(self, *_):
            return False

        def advance(self, *_):
            pass

        def may_attempt(self, *_):
            return True

        def approved(self, *_):
            return []

        def save_run(self, **kwargs):
            return len(kwargs["proposals"])

    line = SimpleNamespace(ts="2026-09-16 09:00", speaker="홍길동",
                           text="공정률은 62.5%입니다", lineno=1,
                           source_path=Path("2026-09-16.md"), message_ts="")
    archive = SimpleNamespace(docs=lambda: [SimpleNamespace(
        workspace="ws", channel_id="C1", raw_lines=[line], path=Path("f.md"))])
    raw = json.dumps({"candidates": [
        {"kind": "number_or_schedule", "current_text": "",
         "proposed_text": line.text, "evidence_quote": line.text},
        {"kind": "new_issue", "current_text": "",
         "proposed_text": "원문에 없는 문장", "evidence_quote": "원문에 없는 문장"},
    ]})
    stats = sr.GenerateStats()

    sr.generate_channel(
        FakeStore(), archive, workspace="ws", channel_id="C1", channel_name="#채널",
        complete=lambda *_: raw, now=datetime(2026, 9, 17, tzinfo=sr.KST),
        force=True, stats=stats,
    )

    assert stats.lines == 1
    assert stats.proposed == 2
    assert stats.accepted == 1


# --- 같은 원문을 두 번 요약하지 않는다 (B-59) ---------------------------------
def test_an_already_summarized_segment_costs_nothing_the_second_time(monkeypatch):
    """소급은 커서를 되돌린다. 막지 않으면 같은 구간에 돈이 두 번 나간다."""
    calls = []
    store = _BackfillStore(seen=True)
    store.watermark = sr.EPOCH_WATERMARK
    stats = sr.GenerateStats()
    line = _line("2026-06-01 09:00", "6월 원문", 1)

    got = sr.generate_channel(
        store, _archive([line]), workspace="ws", channel_id="C1", channel_name="#채널",
        complete=lambda *a: calls.append(a) or "{}",
        now=datetime(2026, 9, 17, tzinfo=sr.KST), force=True, stats=stats,
    )

    assert calls == []          # LLM 을 부르지 않았다
    assert got == 0
    assert stats.reused
    # 커서는 전진한다 — 안 그러면 소급이 그 구간에서 영원히 맴돈다.
    assert store.watermark.startswith("2026-06-01 09:00")


def test_a_second_backfill_walks_the_history_without_calling_the_model(monkeypatch):
    monkeypatch.setattr(sr, "MAX_NEW_SOURCE_CHARS", 90)
    lines = [_line(f"2026-06-{day:02d} 09:00", "가" * 50, day) for day in range(1, 8)]
    store = _BackfillStore(seen=True)
    calls = []

    done = sr.backfill_channel(
        store, _archive(lines), workspace="ws", channel_id="C1", channel_name="#채널",
        complete=lambda *a: calls.append(a) or "{}",
        now=datetime(2026, 9, 17, tzinfo=sr.KST), max_rounds=10,
    )

    assert calls == []
    assert done.exhausted
    assert store.watermark.startswith("2026-06-07 09:00")


# --- 만든 후보만 다시 보낸다 (B-59) -------------------------------------------
def test_deliver_only_never_calls_the_model(monkeypatch):
    """요약은 됐는데 DM 만 실패한 회차를 복구한다. 생성 버튼을 다시 누르게 하면
    같은 원문에 돈이 또 나간다."""
    calls = []
    monkeypatch.setattr(sr, "generate_channel",
                        lambda *a, **k: calls.append(1) or 0)

    # 생성 잠금이 안 걸린 상태다 — 막는 것은 `deliver_only` 하나뿐이어야 한다.
    result, client, _store = _outcome_run(
        monkeypatch, rows=[{"id": "c1"}], reviewers=["U1"], already=set(),
        deliver_only=True, generated=False,
    )

    assert calls == []
    assert result.sent == 1
    assert client.posted == ["DU1"]


def test_deliver_only_with_nothing_pending_says_so():
    assert sr._empty_reason(None, deliver_only=True) == sr.OUTCOME_NOTHING_PENDING


def test_the_result_says_how_many_candidates_are_waiting(monkeypatch):
    """이미 만든 것이 아직 아무에게도 안 갔다 — 그건 LLM 없이 복구할 수 있다."""
    result, _client, _store = _outcome_run(
        monkeypatch, rows=[{"id": "c1"}, {"id": "c2"}], reviewers=["U1"],
        already={"U1"},
    )

    outcome = result.outcomes[0]
    assert outcome.code == "already-sent"
    assert outcome.pending == 2
    assert outcome.as_dict()["pending"] == 2


# --- 생성 요약(abstract) 후보 (B-60 1단계) ------------------------------------
# 푸는 것은 「제안 == 인용」 한 줄뿐이다. 나머지 검증은 form 과 무관하게 그대로 돈다.
_ABSTRACT_SOURCE = [
    sr.SourceLine("2026-09-15 09:00", "홍길동",
                  "공사기간은 2026-09-01부터 2026-12-31까지입니다", "a.md:10"),
    sr.SourceLine("2026-09-15 10:00", "홍길동",
                  "공정률은 62.5%로 집계되었습니다", "a.md:11"),
]


def _candidate(**over):
    base = {
        "kind": "new_issue",
        "form": "abstract",
        "current_text": "",
        "proposed_text": "공사기간과 공정률을 한 문장으로 정리했습니다",
        "evidence_quote": "공정률은 62.5%로 집계되었습니다",
    }
    base.update(over)
    return json.dumps({"candidates": [base]})


def test_an_abstract_candidate_need_not_repeat_the_quote():
    """같은 사실이 여러 날 나오면 추출만으로는 그 수만큼 후보가 된다."""
    got = sr.parse_proposals(_candidate(), _ABSTRACT_SOURCE)

    assert len(got) == 1
    assert got[0].form == sr.FORM_ABSTRACT
    assert got[0].proposed_text != got[0].evidence_quote


def test_an_abstract_candidate_cannot_invent_a_number():
    """문장은 모델이 써도 되지만 **사실은 원문에서만 온다.**"""
    got = sr.parse_proposals(
        _candidate(proposed_text="공정률은 75%로 집계되었습니다"), _ABSTRACT_SOURCE,
    )

    assert got == []


def test_an_abstract_candidate_may_reuse_a_number_from_its_quote():
    got = sr.parse_proposals(
        _candidate(proposed_text="공정률 62.5% 로 일정에 맞춰 진행 중입니다"),
        _ABSTRACT_SOURCE,
    )

    assert len(got) == 1


def test_an_abstract_candidate_without_a_quote_is_dropped():
    """근거 없는 생성 문장은 환각과 구별되지 않는다."""
    assert sr.parse_proposals(_candidate(evidence_quote=""), _ABSTRACT_SOURCE) == []


def test_an_abstract_candidate_whose_quote_is_not_in_the_source_is_dropped():
    got = sr.parse_proposals(
        _candidate(evidence_quote="원문에 없는 문장입니다"), _ABSTRACT_SOURCE,
    )

    assert got == []


def test_an_unknown_form_falls_back_to_the_stricter_one():
    """모르면 더 엄격한 쪽. 생성문으로 통과시키면 검증이 한 겹 조용히 사라진다."""
    for form in ("", "freeform", "ABSTRACTIVE", None):
        got = sr.parse_proposals(_candidate(form=form), _ABSTRACT_SOURCE)
        assert got == [], form


def test_a_quote_candidate_is_judged_exactly_as_before():
    got = sr.parse_proposals(
        _candidate(form="quote", proposed_text="공정률은 62.5%로 집계되었습니다"),
        _ABSTRACT_SOURCE,
    )

    assert len(got) == 1
    assert got[0].form == sr.FORM_QUOTE


def test_a_missing_form_column_is_written_as_quote():
    """옛 후보가 소급으로 생성문이 되면, 사람이 「원문 그대로」로 믿고 승인한 것이
    다른 종류가 된다."""
    sql = Path("deploy/sql/summary_review_schema.sql").read_text(encoding="utf-8")

    assert "ADD COLUMN IF NOT EXISTS form text NOT NULL DEFAULT 'quote'" in sql
    assert "CHECK (form IN ('quote', 'abstract'))" in sql


def test_generate_only_makes_candidates_and_sends_nothing(monkeypatch):
    """검토자가 없어도 후보는 만든다. 보내는 것만 멈춘다(B-62)."""
    monkeypatch.setattr(sr, "generate_channel", lambda *a, **k: 2)
    result, client, store = _outcome_run(
        monkeypatch, rows=[{"id": "c1"}, {"id": "c2"}], reviewers=[], already=set(),
        generate_only=True, generated=False,
    )

    assert client.posted == []
    assert store.stamped == []          # 보여 준 적 없으니 폐기 대상이 아니다
    assert result.sent == 0
    assert result.skipped == 0          # 건너뛴 것이 아니라 여기까지가 이 회차다
    outcome = result.outcomes[0]
    assert outcome.code == "generated-only"
    assert outcome.pending == 2


def test_generate_only_stops_before_asking_who_the_reviewers_are(monkeypatch):
    """검토자 조회까지 가면 「검토자 없음」 으로 기록되고, 그건 사실이 아니다 —
    이 회차는 애초에 보내는 회차가 아니다."""
    monkeypatch.setattr(sr, "generate_channel", lambda *a, **k: 1)
    result, _client, store = _outcome_run(
        monkeypatch, rows=[{"id": "c1"}], reviewers=["U1"], already=set(),
        generate_only=True, generated=False,
    )

    assert result.outcomes[0].code == "generated-only"
    assert store.delivered == []
