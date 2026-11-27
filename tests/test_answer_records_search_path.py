"""질문·답변 원본 — QA 기록의 검색 경로를 콘솔 상세로 내보낸다 (B-93).

거는 것:
1. 기록의 `search_path` 가 상세의 `searchPath` 로 그대로 나간다
2. 이 칸이 생기기 전 옛 기록·모양이 틀린 값은 빈 목록이다(화면은 「기록 없음」)
3. 코드만 담기므로 본문이 가려진 VIP 기록에서도 보인다
4. 화면의 한국어 이름표가 기록하는 코드와 어긋나지 않는다 — 코드가 늘면 여기서 걸린다
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from tybot.console import answer_records

ROOT = Path(__file__).resolve().parents[1]


def _row(**over):
    row = {"record_id": "a" * 32, "ts": "2026-11-26T15:35:04+09:00", "workspace": "mgmt",
           "user": "U1", "question": "q", "answer": "a", "hits": 40}
    row.update(over)
    return row


def _detail(row, **kw):
    return answer_records.detail(row, [], **kw)


def test_search_path_is_passed_through_in_order():
    row = _row(search_path=["index", "exhaustive"])
    assert answer_records.search_path(row) == ["index", "exhaustive"]
    assert _detail(row)["searchPath"] == ["index", "exhaustive"]


def test_records_from_before_the_field_have_an_empty_path():
    row = _row()
    assert "search_path" not in row
    assert answer_records.search_path(row) == []
    assert _detail(row)["searchPath"] == []


@pytest.mark.parametrize("bad", [None, "index", {"a": 1}, 3, ("index",), True])
def test_malformed_values_become_an_empty_path(bad):
    assert answer_records.search_path(_row(search_path=bad)) == []


def test_non_string_and_empty_items_are_dropped():
    row = _row(search_path=["index", None, 7, "", {"x": 1}, "exhaustive"])
    assert answer_records.search_path(row) == ["index", "exhaustive"]


def test_the_path_is_capped_like_the_recorder():
    row = _row(search_path=["index"] * 40)
    assert len(answer_records.search_path(row)) == 16


def test_path_stays_when_the_body_is_hidden():
    """코드뿐이라 본문 가림(VIP, 본인이 아닌 검토자)과 무관하게 보인다."""
    row = _row(search_path=["exhaustive"], citations=["인사안.xlsx"])
    row[answer_records.BOT_KEY] = "vipbot"
    hidden = _detail(row, show_body=False)
    assert hidden["bodyHidden"] is True
    assert hidden["question"] == "" and hidden["citations"] == []
    assert hidden["searchPath"] == ["exhaustive"]


def _recorded_codes() -> set[str]:
    source = (ROOT / "src/tybot/archive/store.py").read_text(encoding="utf-8")
    return set(re.findall(r'_note_search_path\("([a-z_]+)"', source))


def test_the_console_names_every_code_the_recorder_can_write():
    codes = _recorded_codes()
    assert codes == {"exhaustive", "index", "scan_no_index", "scan_index_empty"}
    page = (ROOT / "console-web/src/pages/AnswerRecords.tsx").read_text(encoding="utf-8")
    block = page[page.index("SEARCH_PATH_LABEL") :]
    block = block[: block.index("}")]
    named = set(re.findall(r"^\s*([a-z_]+):", block, flags=re.MULTILINE))
    assert named == codes, "화면 이름표와 기록 코드가 어긋났다"
