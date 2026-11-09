"""검색 후보 선택 — 「최근 500줄 컷」 이 맞는 줄을 버리지 않는가.

2026-11-09 실측: 낱말 중 하나라도 든 줄을 최근 순으로 500줄 자르던 때, 「자금 보고」 는
두 낱말을 모두 가진 2,635줄 중 **0줄**이 후보에 남았다. 흔한 낱말 하나가 그날 하루
치로 창을 다 채웠기 때문이다.

DB 없이 SQL 을 흉내 내는 작은 가짜로 순서를 확인한다.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

import pytest

from tybot import search_index as si

BASE = datetime(2026, 11, 9, 9)


class Line:
    def __init__(self, n, body, *, days_ago, speaker="담당자", channel="C1"):
        self.doc_path = f"ws/{channel}/{n}.md"
        self.line_no = n
        self.body = body
        self.speaker = speaker
        self.channel = channel
        self.spoken_at = BASE - timedelta(days=days_ago, minutes=n)


class FakeCursor:
    """이 모듈이 만드는 두 문장만 이해한다: 모두 맞음 / 일부만 맞음."""

    def __init__(self, lines):
        self.lines = lines
        self.executed: list[tuple[str, list]] = []
        self._rows: list = []

    def execute(self, sql, params):
        self.executed.append((sql, list(params)))
        assert "ILIKE" not in sql, "ILIKE 는 pg_bigm 인덱스를 못 탄다"
        assert sql.count("%s") == len(params), "값은 전부 바인딩이어야 한다"
        groups = self._groups(sql, params)
        channels = next(p for p in params if isinstance(p, list))
        limit = params[-1]
        rows = [ln for ln in self.lines if ln.channel in channels]
        if "NOT (" in sql:
            rows = [ln for ln in rows if 0 < self._hits(ln, groups) < len(groups)]
            rows.sort(key=lambda ln: (-self._hits(ln, groups), -ln.spoken_at.timestamp()))
        else:
            rows = [ln for ln in rows if self._hits(ln, groups) == len(groups)]
            rows.sort(key=lambda ln: -ln.spoken_at.timestamp())
        self._rows = [
            {"doc_path": ln.doc_path, "line_no": ln.line_no, "spoken_at": ln.spoken_at.isoformat()}
            for ln in rows[:limit]
        ]

    @staticmethod
    def _groups(sql, params):
        """낱말마다 그 낱말의 표기들(LIKE 패턴)을 묶는다."""
        patterns = [p for p in params if isinstance(p, str)]
        words = sorted({p.strip("%").lower() for p in patterns})
        return [[p.strip("%") for p in patterns if p.strip("%").lower() == w] for w in words]

    @staticmethod
    def _hits(line, groups):
        # 본문은 띄어쓰기를 지운 식으로 찾는다(`si.COMPACT_BODY`).
        body = line.body.replace(" ", "")
        return sum(
            1 for forms in groups
            if any(f in body or f in line.speaker for f in forms)
        )

    def fetchall(self):
        return list(self._rows)


def test_lines_with_every_word_beat_newer_lines_with_one_word():
    # 최근 600줄은 「보고」 만 가졌고, 「자금」·「보고」 를 다 가진 줄은 한 달 전이다.
    lines = [Line(i, "주간 보고 올립니다", days_ago=0) for i in range(600)]
    lines.append(Line(9001, "자금 집행 보고서 첨부", days_ago=30))
    cur = FakeCursor(lines)
    got = si._ranked_candidates(cur, ["자금", "보고"], ["C1"], 500)
    assert got[0].line_no == 9001
    assert len(got) == 500


def test_partial_matches_fill_only_the_remaining_slots_by_match_count():
    lines = [
        Line(1, "설계 변경 현장 협의", days_ago=40),
        Line(2, "설계 변경 요청", days_ago=1),
        Line(3, "현장 안전", days_ago=0),
    ]
    cur = FakeCursor(lines)
    got = si._ranked_candidates(cur, ["설계", "변경", "현장"], ["C1"], 3)
    # 모두 맞는 줄 → 두 낱말 → 한 낱말. 최근성은 같은 일치 수 안에서만 본다.
    assert [c.line_no for c in got] == [1, 2, 3]
    assert len(cur.executed) == 2
    assert cur.executed[1][1][-1] == 2  # 남은 자리만 요청한다


def test_no_second_query_when_every_word_already_fills_the_limit():
    lines = [Line(i, "기성 현장", days_ago=i) for i in range(10)]
    cur = FakeCursor(lines)
    got = si._ranked_candidates(cur, ["기성", "현장"], ["C1"], 5)
    assert len(got) == 5 and len(cur.executed) == 1


def test_single_word_needs_one_query():
    cur = FakeCursor([Line(1, "하자", days_ago=0)])
    si._ranked_candidates(cur, ["하자"], ["C1"], 500)
    assert len(cur.executed) == 1


def test_channels_outside_the_permitted_list_never_come_back():
    lines = [Line(1, "자금 보고", days_ago=0, channel="SECRET"), Line(2, "자금 보고", days_ago=5)]
    got = si._ranked_candidates(FakeCursor(lines), ["자금", "보고"], ["C1"], 500)
    assert [c.line_no for c in got] == [2]


def test_speaker_counts_as_a_match():
    lines = [Line(1, "공사비 확정", days_ago=3, speaker="김부장")]
    got = si._ranked_candidates(FakeCursor(lines), ["김부장", "공사비"], ["C1"], 500)
    assert [c.line_no for c in got] == [1]


def test_english_abbreviation_is_found_in_upper_case():
    lines = [Line(1, "PF 대출 약정", days_ago=10)]
    cur = FakeCursor(lines)
    got = si._ranked_candidates(cur, si.tokens_of("pf 대출"), ["C1"], 500)
    assert [c.line_no for c in got] == [1]
    assert "%PF%" in cur.executed[0][1] and "%pf%" in cur.executed[0][1]


@pytest.mark.parametrize("query,expected", [
    ("현장의 기성률은", ["현장", "기성률"]),
    ("사무실에서 회의록을", ["사무실", "회의록"]),
    ("대표이사가 결재", ["대표이사", "결재"]),
    # 남는 말이 1자면 떼지 않는다 — 「회의」 를 「회」 로 만들면 아무 줄이나 맞는다
    ("회의 결과", ["회의", "결과"]),
    ("협의", ["협의"]),
    # 숫자가 섞이면 조사로 보지 않는다
    ("5천만", ["5천만"]),
    ("PF는", ["pf는"]),
])
def test_tokens_drop_trailing_particles_only_when_a_real_word_remains(query, expected):
    assert si.tokens_of(query) == expected


def test_stripping_particles_only_widens():
    """뗀 말은 원래 어절의 앞부분이다 — 원래 어절이 든 줄은 그대로 찾힌다."""
    for word in ["현장의", "기성률은", "사무실에서", "공사비가", "고속도로"]:
        stem = si.tokens_of(word)[0]
        assert word.startswith(stem) and len(stem) >= 2


def test_a_compound_word_finds_the_spaced_spelling():
    """「주간업무보고」 로 물어도 본문의 「주간 업무보고」 를 찾는다 (실측 +49%)."""
    lines = [
        Line(1, "주간 업무보고 첨부합니다", days_ago=3),
        Line(2, "하 도 급 계 약 현 황", days_ago=5),
    ]
    got = si._ranked_candidates(FakeCursor(lines), ["주간업무보고"], ["C1"], 500)
    assert [c.line_no for c in got] == [1]
    got = si._ranked_candidates(FakeCursor(lines), ["하도급"], ["C1"], 500)
    assert [c.line_no for c in got] == [2]


def test_the_body_expression_matches_the_index_expression():
    """식이 인덱스와 다르면 오류 없이 전체 스캔이 된다. 스키마와 글자까지 대조한다."""
    from pathlib import Path

    schema = (Path(__file__).resolve().parents[1] / "deploy/sql/index_schema.sql").read_text(
        encoding="utf-8"
    )
    assert f"(({si.COMPACT_BODY}) gin_bigm_ops)" in schema
    assert f"(({si.COMPACT_BODY}) gin_trgm_ops)" in schema
    cur = FakeCursor([])
    si._ranked_candidates(cur, ["자금", "보고"], ["C1"], 10)
    sql = cur.executed[0][0]
    assert f"{si.COMPACT_BODY} LIKE %s" in sql
    assert "body LIKE" not in sql.replace(f"{si.COMPACT_BODY} LIKE", "")


def test_score_ignores_spaces_but_not_across_speaker_and_body():
    tokens = si.tokens_of("주간업무보고")
    assert si.score_line(tokens, "주간업무보고", "담당자", "주간 업무보고 첨부") >= 1
    # 발화자 끝과 본문 앞이 붙어 없는 낱말이 생기지 않는다.
    assert si.score_line(["부장공"], "부장공", "김부장", "공사비 확정") == 0
    # 띄어쓰기까지 같은 줄이 구절 가점으로 앞선다.
    exact = si.score_line(si.tokens_of("주간 업무보고"), "주간 업무보고", "a", "주간 업무보고")
    loose = si.score_line(si.tokens_of("주간 업무보고"), "주간 업무보고", "a", "주간업무보고")
    assert exact > loose > 0


def test_candidate_sql_never_interpolates_values():
    cur = FakeCursor([])
    si._ranked_candidates(cur, ["자금'; DROP TABLE raw_line; --", "보고"], ["C1"], 10)
    for sql, _ in cur.executed:
        assert "DROP" not in sql and not re.search(r"'%", sql)
