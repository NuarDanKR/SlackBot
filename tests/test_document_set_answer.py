"""문서 집합 요약 — 보고서 여러 건을 묻는 질문이 문서 단위로 처리되는가.

설계: `docs/design/document-pipeline-trace-and-report-summary.md` §9·§11·§13

## 재현하는 사례
「여태까지 수집된 주간 보고 회의 관련 내용을 종합해줘」 에 봇이 「회의록이나 논의가
없다」 고 답했다. 추적해 보니 파일은 원문에 **멀쩡히 들어가 있었다**. 원인은 그 뒤였다 —
기간이 7일로 줄고, 채널당 60줄 자르기에서 보고서들이 서로를 밀어냈다.
"""
from __future__ import annotations

import datetime as dt
import json

import pytest

from tybot.access import RequestContext
from tybot.answer import AnswerEngine
from tybot.archive.store import ArchiveStore
from tybot.gateway.base import LLMResponse, Message, ModelSpec, Sensitivity
from tybot.gateway.cost import CostGuard
from tybot.gateway.router import Router
from tybot.intent import CLASSIFIER_MODEL, classify_by_rule

TODAY = dt.date.today()
OLD = TODAY - dt.timedelta(days=90)

REPORTS = (
    "[주간업무보고] 2026.09.10_방글라데시 차토그람 하수도.hwp",
    "[주간보고]광명자원회수시설 (26년09월2주차).hwpx",
    "공사팀 업무보고(2026.06월말 기준)_호남고철2-5.hwp",
)
CHANNEL = "#팀-전산_ABB155-주간보고"


def _doc(lines: list[str]) -> str:
    head = (
        "---\n"
        "workspace: tyit\n"
        f'channel: "{CHANNEL}"\n'
        "visibility: private\n"
        f"acl: [{CHANNEL}]\n"
        "last_ingested: 2026-09-11T17:00+09:00\n"
        "---\n\n"
        "## 원문\n\n"
    )
    return head + "".join(lines)


def _listed(name, when=TODAY):
    return f"> [{when} 09:00] 홍길동: [첨부:자동변환] {name} (hwp, 240KB) · <https://x|원본 파일>\n"


def _body(name, count, when=TODAY, prefix="진척률"):
    return [
        f"> [{when} 09:0{i % 10}] 홍길동: [첨부추출:{name}] {prefix} {i}0% 기성금 {i}억\n"
        for i in range(count)
    ]


class FakeProvider:
    name = "anthropic"

    def __init__(self):
        self.calls: list[list[Message]] = []

    def complete(self, spec, messages, *, max_tokens=1024, temperature=0.0):
        if "라우터" in messages[0].content:
            got = classify_by_rule(messages[1].content)
            return LLMResponse(
                json.dumps(
                    {"kind": got.kind, "days": got.days, "terms": got.terms},
                    ensure_ascii=False,
                ),
                spec.model, self.name, 200, 30, 0.0002,
            )
        self.calls.append(list(messages))
        return LLMResponse("종합 결과", spec.model, self.name, 300, 60, 0.002)


@pytest.fixture
def engine(tmp_path):
    def _build(lines: list[str]):
        path = (
            tmp_path / "workspaces" / "tyit" / "channels" / "C1__weekly" / "raw"
            / "2026-09-11.md"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_doc(lines), encoding="utf-8")
        fake = FakeProvider()
        router = Router(
            providers={"anthropic": fake},
            registry={
                "claude-sonnet-5": ModelSpec(
                    "claude-sonnet-5", "anthropic", 3.0, 15.0, Sensitivity.CONFIDENTIAL
                ),
                CLASSIFIER_MODEL: ModelSpec(
                    CLASSIFIER_MODEL, "anthropic", 1.0, 5.0, Sensitivity.CONFIDENTIAL
                ),
            },
            cost_guard=CostGuard(10.0),
        )
        return AnswerEngine(ArchiveStore(tmp_path), router), fake

    return _build


def _ctx():
    return RequestContext(workspace="tyit", channels=frozenset({CHANNEL}))


QUESTION = "여태까지 수집된 주간 보고 회의 관련 내용을 종합해줘"


def _evidence(fake) -> str:
    return fake.calls[0][1].content


# --- 기간: 「여태까지」 는 7일이 아니다 ------------------------------------------
#
# 90일 전 보고서가 7일 창에서 잘리면, 파일이 아카이브에 있어도 근거에 못 들어온다.
def test_all_time_question_reaches_old_documents(engine):
    lines = [_listed(REPORTS[0], OLD), *_body(REPORTS[0], 6, OLD)]
    eng, fake = engine(lines)
    ans = eng.respond(QUESTION, _ctx())
    assert ans.reason == "answered"
    assert REPORTS[0] in _evidence(fake)


def test_a_seven_day_question_does_not_reach_them(engine):
    """대조군. 범위를 넓힌 것이 「여태까지」 때문임을 보인다."""
    lines = [_listed(REPORTS[0], OLD), *_body(REPORTS[0], 6, OLD)]
    eng, _ = engine(lines)
    ans = eng.respond("이번주 진행 상황 정리해줘", _ctx())
    assert ans.reason == "no_hits"


# --- 예산: 한 보고서가 나머지를 굶기지 않는다 ----------------------------------
def test_every_report_appears_in_the_evidence(engine):
    """실측: 747줄짜리 첨부 한 건이 채널 예산 60줄을 통째로 먹었다."""
    lines = [*_body("거대.xlsx", 300)]
    for name in REPORTS:
        lines += [_listed(name), *_body(name, 30)]
    eng, fake = engine(lines)
    ans = eng.respond(QUESTION, _ctx())

    assert ans.reason == "answered"
    evidence = _evidence(fake)
    for name in REPORTS:
        assert name in evidence, name


def test_blocks_are_titled_by_document_not_channel(engine):
    """모델이 보고서별로 읽어야 답변에서도 보고서를 구별해 쓴다."""
    lines = []
    for name in REPORTS:
        lines += [_listed(name), *_body(name, 10)]
    eng, fake = engine(lines)
    eng.respond(QUESTION, _ctx())
    evidence = _evidence(fake)
    assert f"### {REPORTS[0]}" in evidence


# --- 확인 범위 (설계 §13) ------------------------------------------------------
def test_unread_files_are_reported_not_ignored(engine):
    """「자료가 없다」 와 「파일은 있으나 읽지 못했다」 는 다른 사실이다."""
    lines = [_listed(REPORTS[0]), *_body(REPORTS[0], 10), _listed(REPORTS[1])]
    eng, _ = engine(lines)
    ans = eng.respond(QUESTION, _ctx())
    assert "확인 범위" in ans.text
    assert "내용 미확인 1건" in ans.text
    assert REPORTS[1] in ans.text


def test_coverage_is_written_by_code_not_the_model(engine):
    """건수는 코드가 센 사실이다. 모델에게 맡기면 근거 없이 바뀐다."""
    lines = []
    for name in REPORTS:
        lines += [_listed(name), *_body(name, 8)]
    eng, fake = engine(lines)
    ans = eng.respond(QUESTION, _ctx())
    # 모델은 "종합 결과" 만 돌려준다. 범위 줄은 그 뒤에 코드가 붙인다.
    assert fake.calls
    assert ans.text.startswith("종합 결과")
    assert "대상 3건" in ans.text


def test_complete_coverage_does_not_list_missing_files(engine):
    lines = []
    for name in REPORTS:
        lines += [_listed(name), *_body(name, 8)]
    eng, _ = engine(lines)
    ans = eng.respond(QUESTION, _ctx())
    assert "확인하지 못한 파일" not in ans.text


# --- 회귀: 일반 요약은 그대로 --------------------------------------------------
def test_plain_summary_still_works(engine):
    """문서 종류가 없는 질문은 예전 경로다. 여기서 `no_hits` 가 되면 회귀다."""
    lines = [f"> [{TODAY} 09:15] 홍길동: 3공구 골조 마무리\n"]
    eng, fake = engine(lines)
    ans = eng.respond("이번주 진행 상황 정리해줘", _ctx())
    assert ans.reason == "answered"
    assert "3공구 골조" in _evidence(fake)


def test_specific_fact_question_is_not_a_document_set(engine):
    lines = [f"> [{TODAY} 09:15] 홍길동: 김해외동 기성금 3억 2천만원 청구 완료\n"]
    eng, _ = engine(lines)
    ans = eng.respond("김해외동 기성금 얼마야", _ctx())
    assert ans.reason == "answered"
    assert "확인 범위" not in ans.text


def test_citations_are_still_attached(engine):
    lines = [_listed(REPORTS[0]), *_body(REPORTS[0], 10)]
    eng, _ = engine(lines)
    ans = eng.respond(QUESTION, _ctx())
    assert ans.citations
