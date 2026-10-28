"""스키마의 상한 숫자와 **코어 상수가 같은가.** DB 없이 판다.

## 왜 따로 있나

`tests/test_image_description_schema.py` 는 SQL 에 문자열
`char_length(description) <= 200` 이 있는지 본다. 그 시험은 **상수가 바뀌어도
통과한다** — 리터럴 `200` 이 양쪽에 따로 적혀 있어 둘이 갈라져도 아무도 모른다.

실제로 묶는 대조는 `tests/test_image_description_store_db.py` 에 하나 있는데
`TEST_DATABASE_URL` 이 있어야 돌고, 실제 DB 검증을 패키지 밖에서 돌리면
`skip` 된다(2026-10-28 실측). 즉 **평상시에는 아무도 안 재고 있다.**

여기서는 SQL 에서 숫자를 **파싱해** 상수와 비교한다. DB·DSN 없이 돈다.
"""
from __future__ import annotations

import pathlib
import re

from tybot.archive.image_description import MAX_DESCRIPTION_CHARS

SQL_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "deploy" / "sql" / "archiver_image_description_schema.sql"
)

#: `description` 칸의 길이 CHECK. 공백은 자유롭게 두되 **대상 칸 이름은 고정**이다
#: — 다른 칸의 상한을 집어 오면 대조가 엉뚱한 숫자를 보증한다.
_CAP = re.compile(
    r"char_length\s*\(\s*description\s*\)\s*<=\s*(\d+)",
    re.IGNORECASE,
)


def _caps() -> list[int]:
    return [int(found) for found in _CAP.findall(SQL_PATH.read_text(encoding="utf-8"))]


def test_the_schema_actually_caps_the_description():
    """없으면 상한이 없는 것이다 — 통과가 아니라 실패여야 한다."""
    assert _caps(), f"description 길이 CHECK 를 못 찾았다: {SQL_PATH.name}"


def test_only_one_place_states_the_description_cap():
    """두 군데면 **갈라질 수 있다.** 하나로 두고 그 하나를 상수에 묶는다."""
    found = _caps()
    assert len(found) == 1, found


def test_the_schema_cap_equals_the_core_constant():
    """리터럴을 양쪽에 반복하지 않는다 — 숫자를 **파싱해** 비교한다."""
    assert _caps()[0] == MAX_DESCRIPTION_CHARS


def test_the_comparison_would_notice_a_drift():
    """대조가 실제로 무는지 확인한다.

    이 시험이 없으면 정규식이 아무것도 못 잡는 상태에서도 위 시험들이
    조용히 지나갈 수 있다 — 그때 `_caps()` 는 빈 목록이고, 빈 목록끼리는
    언제나 같다.
    """
    said = _CAP.findall("description text NOT NULL CHECK (char_length(description) <= 7)")
    assert [int(item) for item in said] == [7]
    assert _CAP.findall("CHECK (char_length(label) <= 200)") == []
