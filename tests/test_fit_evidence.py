"""근거 분량 맞추기 — 엑셀은 첫 시트만 답한다(2026-11-09 보고)의 원인.

예전에는 `evidence[:40_000]` 이었다. 엑셀 변환본은 시트가 순서대로 이어져 있어,
4만 자를 넘으면 뒤 시트가 통째로 사라졌고 모델은 그 사실을 몰랐다.
"""
from __future__ import annotations

from tybot.specialist_adapters import MAX_EVIDENCE_CHARS, fit_evidence


def sheet(name: str, rows: int, width: int = 60) -> list[str]:
    head = [f"[2026-11-09 09:00] (C1) 수금계획.xlsx: [시트] {name}",
            f"[2026-11-09 09:00] (C1) 수금계획.xlsx: | 사업장 | {name} 금액 |"]
    body = [f"[2026-11-09 09:00] (C1) 수금계획.xlsx: | 행{i:05d} | {'9' * width} |"
            for i in range(rows)]
    return head + body


def test_short_evidence_is_untouched():
    text = "\n".join(sheet("1월", 5) + sheet("2월", 5))
    assert fit_evidence(text) == text


def test_every_sheet_keeps_its_header_when_the_workbook_is_too_long():
    names = ["1월", "2월", "3월", "4월", "5월"]
    text = "\n".join(line for n in names for line in sheet(n, 400))
    assert len(text) > MAX_EVIDENCE_CHARS

    fitted = fit_evidence(text)

    assert len(fitted) <= MAX_EVIDENCE_CHARS
    for name in names:
        assert f"[시트] {name}" in fitted, name
        assert f"| 사업장 | {name} 금액 |" in fitted, name
    # 예전 방식이었다면 뒤 시트는 없었다
    assert "[시트] 5월" not in text[:MAX_EVIDENCE_CHARS]


def test_small_sheets_are_kept_whole_and_large_ones_share_the_rest():
    text = "\n".join(sheet("요약", 3) + sheet("상세A", 600) + sheet("상세B", 600))
    fitted = fit_evidence(text)
    small = "\n".join(sheet("요약", 3))
    assert small in fitted
    # 큰 두 시트가 같은 몫을 받는다
    after_small = fitted.split("[시트] 상세A")[1]
    first = after_small.split("[시트] 상세B")[0].count("| 행")
    second = after_small.split("[시트] 상세B")[1].count("| 행")
    assert first > 0 and abs(first - second) <= 1


def test_truncation_is_announced_per_sheet_and_up_front():
    text = "\n".join(sheet("1월", 800) + sheet("2월", 800))
    fitted = fit_evidence(text)
    assert fitted.startswith("[근거 분량 안내]")
    assert fitted.count("줄만 실음 — 분량 한도") == 2


def test_lines_are_never_cut_in_half():
    text = "\n".join(sheet("1월", 900) + sheet("2월", 900))
    original = set(text.split("\n"))
    for line in fit_evidence(text).split("\n"):
        if line.startswith(("[근거 분량 안내]", "…(이 구역")):
            continue
        assert line in original


def test_evidence_without_sheets_keeps_the_front():
    text = "\n".join(f"[ts] (C1) 담당: 대화 {i:05d} " + "가" * 80 for i in range(1000))
    fitted = fit_evidence(text)
    assert len(fitted) <= MAX_EVIDENCE_CHARS
    assert "대화 00000" in fitted and "대화 00999" not in fitted


def test_a_single_huge_line_is_cut_not_dropped():
    """줄 하나가 구역 몫보다 길면 그 줄을 잘라 싣는다. 통째로 빼면 버리는 것이다."""
    text = "\n".join(["[시트] 요약", "가" * 30_000, "[시트] 상세", "나" * 30_000])
    fitted = fit_evidence(text)
    assert len(fitted) <= MAX_EVIDENCE_CHARS
    assert fitted.count("가") > 10_000 and fitted.count("나") > 10_000


def test_limit_is_read_at_call_time(monkeypatch):
    import tybot.specialist_adapters as sa

    monkeypatch.setattr(sa, "MAX_EVIDENCE_CHARS", 5_000)
    text = "\n".join(sheet("1월", 200) + sheet("2월", 200))
    assert len(sa.fit_evidence(text)) <= 5_000
