"""채널×일 다이제스트 생성·출력 검사·저장 (B-93 2단계 §7-2). LLM·DB 는 대역이다."""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace

import pytest

from tybot import channel_digest as cd
from tybot import channel_digest_build as build
from tybot import summary_review

DAY = cd.DayInput(
    workspace="mgmt", channel_id="C0PLAN0001", channel="#팀-기획_abb110-경영계획수립",
    day=date(2026, 11, 10),
    lines=("2026-11-10 10:00 담당자: 2026년 경영목표 자료 공유드립니다",),
    attachments=(("계획.pdf", "영업이익 325억 이상, 부채비율 200% 이하"),),
    source_files=(cd.SourceFile("workspaces/mgmt/channels/C0PLAN0001__x/raw/2026-11-10.md", "a" * 64, 1),),
    source_attachments=(cd.SourceAttachment("p.md", "F0PLAN0001", "rev1", "b" * 64),),
)
SOURCE = cd.render_input(DAY).text


def _out(body, terms=()):
    return json.dumps({"body": body, "terms": list(terms)}, ensure_ascii=False)


def test_number_rule_matches_summary_review():
    assert build._NUMBER_RE.pattern == summary_review.NUMBER_RE.pattern


def test_a_good_output_is_kept():
    draft, code = build.check_output(
        _out("2026년 경영목표 자료(영업이익 325억 이상) 공유", ["재무목표", "KPI", "사업계획"]), SOURCE
    )
    assert code == "" and draft.terms == ("재무목표", "KPI", "사업계획")


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("그냥 문장", build.REJECT_SHAPE),
        (json.dumps({"body": 3}), build.REJECT_SHAPE),
        (_out(""), build.REJECT_EMPTY),
        (_out("가" * 2001), build.REJECT_TOO_LONG),
        (_out("경영목표", [f"말{i}" for i in range(41)]), build.REJECT_TOO_MANY_TERMS),
        (_out("영업이익 400억 목표"), build.REJECT_NUMBER),  # 원문에 없는 숫자
        (_out("경영목표", ["2027년"]), build.REJECT_NUMBER),
    ],
)
def test_bad_outputs_are_dropped_not_trimmed(raw, code):
    draft, got = build.check_output(raw, SOURCE)
    assert draft is None and got == code


def test_personal_identifiers_never_reach_the_index():
    draft, code = build.check_output(_out("담당자 연락처 010-1234-5678 공유"), SOURCE + " 010-1234-5678")
    assert code == "" and "1234-5678" not in draft.body


class _Router:
    def __init__(self, text="", boom=None):
        self.text, self.boom, self.calls = text, boom, []

    def complete(self, messages, **kw):
        self.calls.append((messages, kw))
        if self.boom:
            raise self.boom
        return SimpleNamespace(text=self.text, model="claude-haiku-4-5", provider="anthropic", cost_usd=0.001)


def test_generate_calls_once_with_confidential_sensitivity():
    from tybot.gateway.base import Sensitivity

    router = _Router(_out("경영목표 자료 공유", ["재무목표"]))
    outcome = build.generate(DAY, router, model="claude-haiku-4-5")
    assert outcome.draft is not None and outcome.code == ""
    (messages, kw), = router.calls
    assert kw["sensitivity"] == Sensitivity.CONFIDENTIAL and kw["model"] == "claude-haiku-4-5"
    assert "영업이익 325억" in messages[1].content


def test_generate_never_raises():
    outcome = build.generate(DAY, _Router(boom=TimeoutError()), model="m")
    assert outcome.draft is None and outcome.code == "digest_llm_TimeoutError"
    empty = cd.DayInput(workspace="w", channel_id="c", channel="#c", day=date(2026, 1, 1))
    assert build.generate(empty, _Router(), model="m").code == build.REJECT_EMPTY


class _Cursor:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params):
        self.log.append((" ".join(sql.split()), params))

    def fetchall(self):
        return [{"day": date(2026, 11, 10), "revision": "r1"}]


def test_save_inserts_a_new_row_and_marks_older_ones_stale():
    log = []
    conn = SimpleNamespace(cursor=lambda: _Cursor(log))
    outcome = build.Outcome(draft=build.Draft("본문", ("재무목표",)), model="m", provider="anthropic")
    row_id = build.save(conn, DAY, outcome, model="m")
    (insert, ins_params), (stale, st_params) = log
    assert insert.startswith("INSERT INTO channel_day_digest") and "ON CONFLICT (id) DO NOTHING" in insert
    assert ins_params[0] == row_id and ins_params[4] == cd.revision(DAY, model="m")
    files = json.loads(ins_params[7])
    assert files[0]["sha256"] == "a" * 64
    assert stale.startswith("UPDATE channel_day_digest SET stale_at = now()")
    assert st_params[-1] == ins_params[4]  # 새 지문이 아닌 행만 stale


def test_current_revisions_reads_day_to_revision():
    conn = SimpleNamespace(cursor=lambda: _Cursor([]))
    assert build.current_revisions(conn, "mgmt", "C0PLAN0001") == {date(2026, 11, 10): "r1"}
