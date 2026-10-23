"""웹 콘솔 현황판이 **항목과 어긋나지 않는다.**

표를 손으로 적으면 「표에 적힌 상태」 와 「항목에 적힌 상태」 가 두 곳이 되고,
두 곳은 갈라진다. 그래서 표는 `scripts/build_console_board.py` 가 찍고,
손으로 고친 표는 여기서 막는다.

**고장이 조용하기 때문이다** — 표가 틀려도 오류는 안 나고, 사람은 끝난 일을
다시 요청하거나 급한 일을 못 본다. 이 보드가 있는 이유가 바로 그것이다.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
DOC = REPO / "docs" / "web-console-handoff.md"
SCRIPT = REPO / "scripts" / "build_console_board.py"

sys.path.insert(0, str(REPO / "scripts"))

from build_console_board import (  # noqa: E402
    BEGIN,
    END,
    OWNERS,
    STATUS_ORDER,
    BoardError,
    rows,
    table,
)


def _board_text() -> str:
    text = DOC.read_text(encoding="utf-8")
    return text[text.index(BEGIN) + len(BEGIN) : text.index(END)]


def test_the_board_matches_the_items():
    """표를 손으로 고쳤거나 생성을 안 돌렸으면 **여기서 막는다.**"""
    assert table(rows()).strip() in _board_text(), (
        "현황판이 항목과 다릅니다. `python scripts/build_console_board.py` 를 돌려 주세요."
    )


def test_the_check_flag_passes_on_the_committed_tree():
    """`--check` 는 배포 게이트에서 쓰라고 있는 것이다. 지금 통과해야 한다."""
    done = subprocess.run(
        [sys.executable, str(SCRIPT), "--check"],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert done.returncode == 0, done.stderr


def test_every_item_has_a_readable_meta():
    items = rows()
    assert items, "항목이 하나도 없습니다"
    for item in items:
        assert item["owner"] in OWNERS
        assert item["status"] in STATUS_ORDER
        assert item["title"], f"{item['wc']}: 제목이 비었습니다"


def test_numbers_are_never_reused():
    seen = [i["wc"] for i in rows()]
    assert len(seen) == len(set(seen))


def test_every_anchor_link_lands_on_an_item():
    """쪼갰다 합치면서 링크가 앵커로 돌아왔다. 끊기면 여기서 잡는다."""
    text = DOC.read_text(encoding="utf-8")
    anchors = set(re.findall(r'<a id="([^"]+)"></a>', text))
    targets = set(re.findall(r'\]\(#([^)]+)\)', text))
    assert not (targets - anchors), f"없는 자리를 가리킵니다: {sorted(targets - anchors)}"


def test_the_first_meta_cell_wins():
    """본문에도 `| 상태 |` 표가 있는 항목이 있다(WC-14).

    뒤엣것을 읽으면 **조용히 엉뚱한 값**이 보드에 올라간다 — 오류는 안 난다.
    """
    wc14 = next(i for i in rows() if i["wc"] == "WC-14")
    assert wc14["status"] == "완료", "본문 표를 메타로 잘못 읽었습니다"


def test_an_unknown_status_is_refused(tmp_path, monkeypatch):
    """**모르는 상태를 조용히 통과시키지 않는다.**

    통과시키면 그 줄은 표에서 빠지거나 엉뚱한 자리에 붙고, 둘 다 안 보인다.
    """
    import build_console_board as mod

    fake = tmp_path / "doc.md"
    fake.write_text(
        '<a id="wc-99"></a>\n# WC-99. 시험용\n\n| | |\n|---|---|\n'
        "| 상태 | 🟠 거의 다 됨 |\n| 우선 | 보통 |\n| 담당 | 양쪽 |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "DOC", fake)
    with pytest.raises(BoardError, match="모르는 상태"):
        mod.rows()


def test_an_unknown_owner_is_refused(tmp_path, monkeypatch):
    import build_console_board as mod

    fake = tmp_path / "doc.md"
    fake.write_text(
        '<a id="wc-98"></a>\n# WC-98. 시험용\n\n| | |\n|---|---|\n'
        "| 상태 | 🔴 요청됨 |\n| 우선 | 보통 |\n| 담당 | 아무나 |\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "DOC", fake)
    with pytest.raises(BoardError, match="모르는 담당"):
        mod.rows()


def test_anchor_and_heading_number_must_match(tmp_path, monkeypatch):
    import build_console_board as mod

    fake = tmp_path / "doc.md"
    fake.write_text(
        '<a id="wc-98"></a>\n# WC-99. 다른 번호\n\n| | |\n|---|---|\n'
        '| 상태 | 🔴 요청됨 |\n| 우선 | 보통 |\n| 담당 | 양쪽 |\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "DOC", fake)
    with pytest.raises(BoardError, match="앵커와 제목 번호"):
        mod.rows()


def test_a_missing_meta_cell_is_refused(tmp_path, monkeypatch):
    import build_console_board as mod

    fake = tmp_path / "doc.md"
    fake.write_text(
        '<a id="wc-97"></a>\n# WC-97. 시험용\n\n| | |\n|---|---|\n| 상태 | 🔴 요청됨 |\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(mod, "DOC", fake)
    with pytest.raises(BoardError, match="우선"):
        mod.rows()


def test_finished_items_sort_below_the_live_ones():
    """끝난 것이 위에 쌓이면 **지금 할 일이 안 보인다.**"""
    order = [STATUS_ORDER[i["status"]] for i in rows()]
    assert order == sorted(order)
