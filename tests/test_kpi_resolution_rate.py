"""KPI「질문 해결률」 재집계가 **올릴 수 없는 수**를 센다.

KPI 는 사람 평가에 쓰인다. 그래서 「맞게 세는가」 보다 **「딴짓으로 올릴 수 있는가」**
가 더 중요하다. 아래 시험은 전부 그 축이다.

- 어려운 질문을 피하면 → **떨어져야 한다**(분모가 수요이므로)
- 권한 확인 실패 → **분모에 남아야 한다**(묻는 사람에겐 답을 못 받은 것)
- 잡담 → **분모에서 빠져야 한다**(봇이 잘못한 게 없다)
- 근거만 걸리고 출처를 안 붙였으면 → **해결이 아니다**(원칙 2)
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

from kpi_resolution_rate import (  # noqa: E402
    MIN_SAMPLE,
    is_business,
    is_resolved,
    load,
    tally,
    unresolved_reason,
)

KST = timezone(timedelta(hours=9))


def row(**kw) -> dict:
    base = {
        "ts": datetime.now(KST).isoformat(timespec="seconds"),
        "workspace": "tyit",
        "intent_kind": "search",
        "citations": [],
        "hits": 0,
        "reason": "answered",
    }
    return base | kw


def answered(n: int) -> list[dict]:
    return [row(citations=["[전산팀]공지, 문서(2026-10-01)"], hits=2) for _ in range(n)]


def test_avoiding_hard_questions_does_not_raise_the_number():
    """**이 시험이 이 KPI 의 존재 이유다.**

    「근거 없음」 을 늘리는 것은 원칙대로 행동한 것이지만 성과는 아니다.
    분모에서 빠지면 회피가 성과로 보인다.
    """
    good = tally(answered(20))
    avoided = tally(answered(20) + [row(reason="no_hits") for _ in range(20)])
    assert good["resolutionRate"] == 1.0
    assert avoided["resolutionRate"] == pytest.approx(0.5), "회피가 지표를 지켜 줬습니다"
    assert avoided["asked"] == 40, "답 못 한 질문이 분모에서 빠졌습니다"


def test_permission_unverified_stays_in_the_denominator():
    """콘솔 진단과 **일부러 다르다.** 묻는 사람에겐 이것도 답을 못 받은 것이다."""
    result = tally(answered(10) + [row(reason="permission_unverified") for _ in range(10)])
    assert result["resolutionRate"] == pytest.approx(0.5)
    # 같은 자료를 콘솔 방식으로 세면 분모에서 빠져 더 높게 나온다. 그게 맞다.
    assert result["consoleGroundedRate"] == pytest.approx(1.0)
    assert result["consoleGroundedRate"] > result["resolutionRate"]


def test_smalltalk_is_not_counted_against_the_bot():
    """「안녕」 에 답한 것이 지표를 깎으면 안 된다."""
    result = tally(answered(10) + [row(intent_kind="smalltalk", reason="smalltalk")] * 10)
    assert result["asked"] == 10
    assert result["nonBusiness"] == 10
    assert result["resolutionRate"] == 1.0


@pytest.mark.parametrize("kind", ["status", "help", "memory", "out_of_scope", "ingest"])
def test_self_kinds_are_not_business(kind):
    assert not is_business(row(intent_kind=kind))


def test_an_error_record_still_counts_as_demand():
    """예외로 `intent_kind` 를 잃은 줄은 **잡담이 아니다.**

    빼면 오류가 많을수록 KPI 가 좋아진다.
    """
    assert is_business(row(intent_kind="error"))
    result = tally(answered(10) + [row(intent_kind="error", error="TimeoutError")] * 10)
    assert result["asked"] == 20
    assert result["resolutionRate"] == pytest.approx(0.5)


def test_hits_without_citations_is_not_resolved():
    """`hits > 0` 은 「검색이 걸렸다」 일 뿐이다. 출처를 붙여야 해결이다(원칙 2)."""
    assert not is_resolved(row(hits=9, citations=[]))
    assert unresolved_reason(row(hits=9, citations=[])) == "출처 미표기(근거는 있었음)"


def test_blank_citations_do_not_count():
    """빈 문자열만 든 목록을 출처로 세면 한 줄 고쳐서 100% 를 만들 수 있다."""
    assert not is_resolved(row(citations=["", "   "], hits=3))


def test_a_failed_answer_is_not_resolved_even_with_citations():
    assert not is_resolved(row(citations=["문서"], hits=2, error="TimeoutError"))
    assert not is_resolved(
        row(citations=["문서"], hits=2, reason="specialist_unavailable")
    )


def test_a_small_sample_gives_no_number():
    """**0% 가 아니라 「모른다」 다.** 못 본 것과 없는 것은 사람이 할 일이 다르다."""
    result = tally(answered(MIN_SAMPLE - 1))
    assert result["resolutionRate"] is None
    assert result["asked"] == MIN_SAMPLE - 1


def test_workspaces_are_broken_out():
    """전사 평균만 보면 죽은 워크스페이스가 안 보인다."""
    rows = answered(10) + [row(workspace="tec", reason="no_hits") for _ in range(10)]
    result = tally(rows)
    assert result["byWorkspace"]["tyit"]["rate"] == 1.0
    assert result["byWorkspace"]["tec"]["rate"] == 0.0
    assert result["resolutionRate"] == pytest.approx(0.5)


def test_broken_lines_are_counted_not_swallowed(tmp_path):
    """깨진 줄을 조용히 버리면 **분모가 줄어 KPI 가 오른다.**"""
    now = datetime.now(KST)
    path = tmp_path / f"qa-{now:%Y-%m}.jsonl"
    path.write_text(
        json.dumps(row()) + "\n{깨진 줄\n" + json.dumps(row(ts="")) + "\n",
        encoding="utf-8",
    )
    rows, read, broken = load(tmp_path, now - timedelta(days=90))
    assert len(rows) == 1
    assert broken == 2, "깨진 줄과 시각 없는 줄을 세지 않았습니다"
    assert read == [path.name]


def test_rows_outside_the_window_are_dropped(tmp_path):
    now = datetime.now(KST)
    old = now - timedelta(days=200)
    (tmp_path / f"qa-{now:%Y-%m}.jsonl").write_text(
        json.dumps(row()) + "\n", encoding="utf-8"
    )
    (tmp_path / f"qa-{old:%Y-%m}.jsonl").write_text(
        json.dumps(row(ts=old.isoformat(timespec="seconds"))) + "\n", encoding="utf-8"
    )
    rows, read, _ = load(tmp_path, now - timedelta(days=90))
    assert len(rows) == 1
    assert f"qa-{old:%Y-%m}.jsonl" not in read, "기간 밖 월 파일을 읽었습니다"


def test_a_missing_directory_fails_loudly(tmp_path):
    """없는 경로를 **0건으로 세지 않는다.** 0% KPI 를 보고하게 된다."""
    with pytest.raises(SystemExit, match="감사 기록"):
        load(tmp_path / "없음", datetime.now(KST))


def test_the_run_writes_nothing(tmp_path):
    """감사 기록을 건드리면 안 된다. 읽기 전용이라고 적어 뒀으니 지켜야 한다."""
    now = datetime.now(KST)
    path = tmp_path / f"qa-{now:%Y-%m}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in answered(12)), encoding="utf-8")
    before = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()
    }

    tally(load(tmp_path, now - timedelta(days=90))[0])

    after = {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in tmp_path.iterdir()
    }
    assert before == after, "감사 기록이 바뀌었습니다"
