"""보고서별 근거 예산 — 업로드 순서가 결과를 정하면 안 된다.

설계: `docs/design/document-pipeline-trace-and-report-summary.md` §11·§13·§17

## 재현하는 사례
기간 요약은 채널마다 최근 60줄을 고른다. 그런데 실측 첨부에는 747줄짜리 xlsx 가 있다.
그 한 건이 예산을 통째로 먹으면 나머지 보고서는 첨부 표시까지 밀려나고, 「11건을
종합해줘」 가 「마지막에 올라온 한 건의 꼬리」 가 된다.
"""
from __future__ import annotations

from dataclasses import dataclass

from tybot import document_evidence as de

REPORTS = (
    "[주간업무보고] 2026.09.10_방글라데시 차토그람 하수도.hwp",
    "[주간보고]광명자원회수시설 (26년09월2주차).hwpx",
    "공사팀 업무보고(2026.06월말 기준)_호남고철2-5.hwp",
)


@dataclass
class FakeLine:
    text: str
    ts: str = "2026-09-11 09:00"
    speaker: str = "홍길동"


@dataclass
class FakeDoc:
    workspace: str = "tyit"
    channel: str = "#팀-전산_ABB155-주간보고"


def _listed(name, link="https://slack.example/f/1"):
    return FakeLine(f"[첨부:자동변환] {name} (hwp, 240KB) · <{link}|원본 파일>")


def _body(name, count, prefix="본문"):
    return [FakeLine(f"[첨부추출:{name}] {prefix} {i}") for i in range(count)]


def _pair(doc, lines):
    return [(doc, lines)]


# --- 묶기 ---------------------------------------------------------------------
def test_attachment_lines_group_by_file_name():
    doc = FakeDoc()
    lines = [_listed(REPORTS[0]), *_body(REPORTS[0], 5),
             _listed(REPORTS[1]), *_body(REPORTS[1], 3)]
    got = de.group_documents(_pair(doc, lines))
    assert [d.title for d in got] == [REPORTS[0], REPORTS[1]]
    assert [d.extracted_lines for d in got] == [5, 3]
    assert all(d.is_attachment for d in got)


def test_bracketed_file_names_survive():
    """`[주간업무보고] …hwp` 의 첫 `]` 에서 끊기면 전부 다른 문서로 쪼개진다."""
    doc = FakeDoc()
    got = de.group_documents(_pair(doc, _body(REPORTS[0], 4)))
    assert len(got) == 1
    assert got[0].title == REPORTS[0]


def test_conversation_lines_are_kept_as_their_own_document():
    """대화를 버리면 사람 발언이 사라진다. 문서 수치와 구별해야 할 다른 근거다."""
    doc = FakeDoc()
    lines = [FakeLine("오늘 현장 점검했습니다"), *_body(REPORTS[0], 3)]
    got = de.group_documents(_pair(doc, lines))
    kinds = {d.is_attachment for d in got}
    assert kinds == {True, False}
    talk = next(d for d in got if not d.is_attachment)
    assert talk.extracted_lines == 1


def test_listed_without_body_is_unread_not_absent():
    """「자료가 없다」 와 「읽지 못했다」 는 다른 사실이다."""
    doc = FakeDoc()
    (got,) = de.group_documents(_pair(doc, [_listed(REPORTS[0])]))
    assert got.listed_only
    assert got.unread
    assert got.extracted_lines == 0
    assert got.source_link.startswith("https://")


def test_body_clears_the_listed_only_flag():
    doc = FakeDoc()
    lines = [_listed(REPORTS[0]), *_body(REPORTS[0], 2)]
    (got,) = de.group_documents(_pair(doc, lines))
    assert not got.listed_only
    assert not got.unread


def test_same_name_in_different_channels_is_not_merged():
    a, b = FakeDoc(channel="#A"), FakeDoc(channel="#B")
    got = de.group_documents([(a, _body(REPORTS[0], 2)), (b, _body(REPORTS[0], 2))])
    assert len(got) == 2
    assert len({d.key for d in got}) == 2


# --- 예산: 한 문서가 독점하지 못한다 -------------------------------------------
def test_a_huge_document_does_not_starve_the_others():
    """실측: 747줄짜리 xlsx 한 건이 채널 예산 60줄을 통째로 먹었다."""
    doc = FakeDoc()
    lines = [*_body("거대.xlsx", 747)]
    for name in REPORTS:
        lines += [_listed(name), *_body(name, 40)]
    got = de.allocate(de.group_documents(_pair(doc, lines)), ["보고"])

    picked = {d.title: len(sel) for d, sel in got.selected}
    assert len(picked) == 4
    for name in REPORTS:
        assert picked[name] >= de.MIN_LINES_PER_DOC, name
    assert picked["거대.xlsx"] <= de.MAX_LINES_PER_DOC


def test_every_candidate_gets_at_least_one_line():
    """0줄이면 후보에 있었는데 근거가 없다 — 빠진 것과 같다."""
    doc = FakeDoc()
    lines = []
    for i in range(30):
        lines += _body(f"보고서{i}.hwp", 20)
    got = de.allocate(de.group_documents(_pair(doc, lines)), [], total_lines=40)
    assert got.selected
    assert all(sel for _, sel in got.selected)


def test_upload_order_does_not_decide_the_result():
    """예산이 모자랄 때 **관련도**가 정해야 한다. 순서가 정하면 무관한 답이 된다.

    예산이 넉넉하면 둘 다 상한을 받는다 — 그건 정상이다. 부족할 때를 시험한다.
    """
    doc = FakeDoc()
    early = _body("무관.hwp", 50, prefix="잡담")
    late = _body(REPORTS[0], 50, prefix="기성금 청구")
    got = de.allocate(
        de.group_documents(_pair(doc, early + late)), ["기성금"], total_lines=30
    )
    budgets = {d.title: len(sel) for d, sel in got.selected}
    assert budgets[REPORTS[0]] > budgets["무관.hwp"]
    # 그래도 무관한 문서가 0줄이 되지는 않는다 — 후보였으면 최소 몫이 있다.
    assert budgets["무관.hwp"] >= 1


def test_document_count_is_capped_and_the_rest_is_reported():
    doc = FakeDoc()
    lines = []
    for i in range(25):
        lines += _body(f"보고서{i}.hwp", 10)
    got = de.allocate(de.group_documents(_pair(doc, lines)), [], max_documents=20)
    assert got.used == 20
    assert len(got.dropped) == 5
    assert not got.complete


def test_eleven_reports_all_appear_in_coverage():
    """설계 §17: 11개 fixture 에서 11개가 모두 집계된다."""
    doc = FakeDoc()
    lines = []
    for i in range(11):
        lines += _body(f"주간보고{i}.hwp", 12)
    got = de.allocate(de.group_documents(_pair(doc, lines)), ["주간보고"])
    assert got.candidates == 11
    assert got.used == 11
    assert got.complete
    assert "대상 11건" in de.coverage_line(got)


# --- 고르기: 꼬리를 자르지 않는다 ----------------------------------------------
def test_head_and_tail_both_survive():
    """표는 머리(무슨 값인지)와 꼬리(합계)가 둘 다 필요하다."""
    lines = [FakeLine("[첨부추출:표.xlsx] 구분  금액")]
    lines += [FakeLine(f"[첨부추출:표.xlsx] 항목{i}  {i}00원") for i in range(50)]
    lines.append(FakeLine("[첨부추출:표.xlsx] 합계  5,000원"))
    (item,) = de.group_documents(_pair(FakeDoc(), lines))
    picked = de.pick_lines(item, [], 10)
    texts = [ln.text for ln in picked]
    assert any("구분" in t for t in texts)
    assert any("합계" in t for t in texts)


def test_picked_lines_keep_their_original_order():
    """순서가 섞이면 표가 표로 읽히지 않는다."""
    lines = [FakeLine(f"[첨부추출:표.xlsx] {i}행 100원") for i in range(30)]
    (item,) = de.group_documents(_pair(FakeDoc(), lines))
    picked = de.pick_lines(item, [], 8)
    assert picked == sorted(picked, key=lambda ln: item.lines.index(ln))


def test_question_terms_win_over_position():
    lines = [FakeLine(f"[첨부추출:표.xlsx] 잡담 {i}") for i in range(30)]
    lines.append(FakeLine("[첨부추출:표.xlsx] 기성금 1,200만원"))
    lines += [FakeLine(f"[첨부추출:표.xlsx] 잡담 {i}") for i in range(30, 60)]
    (item,) = de.group_documents(_pair(FakeDoc(), lines))
    picked = de.pick_lines(item, ["기성금"], 5)
    assert any("기성금" in ln.text for ln in picked)


def test_short_documents_are_returned_whole():
    lines = _body("작은.hwp", 3)
    (item,) = de.group_documents(_pair(FakeDoc(), lines))
    assert de.pick_lines(item, [], 40) == lines


def test_zero_budget_returns_nothing():
    (item,) = de.group_documents(_pair(FakeDoc(), _body("x.hwp", 5)))
    assert de.pick_lines(item, [], 0) == []


# --- 확인 범위 (설계 §13) ------------------------------------------------------
def test_unread_files_are_counted_and_named():
    doc = FakeDoc()
    lines = [*_body(REPORTS[0], 10), _listed(REPORTS[1]), _listed(REPORTS[2])]
    got = de.allocate(de.group_documents(_pair(doc, lines)), [])
    assert len(got.unread) == 2
    assert not got.complete
    block = de.coverage_block(got)
    assert "내용 미확인 2건" in block
    assert "확인하지 못한 파일" in block
    assert REPORTS[1] in block


def test_complete_coverage_says_nothing_is_missing():
    got = de.allocate(de.group_documents(_pair(FakeDoc(), _body(REPORTS[0], 10))), [])
    assert got.complete
    assert "확인하지 못한 파일" not in de.coverage_block(got)


def test_coverage_of_nothing_is_honest():
    got = de.allocate([], [])
    assert got.candidates == 0
    assert got.used == 0
    assert "대상 0건" in de.coverage_line(got)


def test_unread_documents_are_not_given_evidence_lines():
    """읽지 못한 문서가 근거에 들어가면 파일명으로 내용을 지어내게 된다."""
    doc = FakeDoc()
    got = de.allocate(de.group_documents(_pair(doc, [_listed(REPORTS[0])])), [])
    assert got.selected == []
    assert len(got.unread) == 1
