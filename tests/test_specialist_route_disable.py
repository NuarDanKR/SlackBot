"""배정이 빠진 워크스페이스의 라우트는 **꺼진다.**

결정: 2026-09-28 QA · inventory §7-7.

`specialist_store` 는 배정을 저장할 때 전량 `DELETE` 후 재삽입한다. 라우트는 별도
표라 그 삭제에 딸려 가지 않는다 — 그래서 남는다. 그대로 두면 나중에 같은
워크스페이스에 **다시 배정하는 순간 과거의 `active` 가 되살아난다.** 뗀 사람은
껐다고 알고, 다시 붙인 사람은 켠 적이 없다. 아무 오류도 안 난다.

SQL 이 맞는지는 격리 DB 시험이 본다(`test_schema_isolated_db`). 여기서는 **부르는가**
와 **무엇을 넘기는가**를 본다.
"""

from __future__ import annotations

import ast
from pathlib import Path

from tybot.console import specialist_store

SOURCE = Path(specialist_store.__file__).read_text(encoding="utf-8")


class _Cur:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.rowcount = 1

    def execute(self, sql, params=()) -> None:
        self.calls.append((" ".join(sql.split()), tuple(params)))


def test_the_remaining_workspaces_are_passed_as_a_list():
    """남는 배정을 안 넘기면 **켜져 있어야 할 라우트까지** 꺼진다."""
    cur = _Cur()

    specialist_store.disable_routes_outside(cur, "hermes", ["tyit", "pf"], "dan")

    (sql, params) = cur.calls[0]
    assert params == ("dan", "hermes", ["tyit", "pf"])
    assert "route_mode = 'disabled'" in sql
    assert "NOT (workspace = ANY(%s))" in sql


def test_it_does_not_delete_rows():
    """지우면 언제 누가 껐는지가 사라진다. 콘솔 역할에는 DELETE 권한도 없다."""
    cur = _Cur()

    specialist_store.disable_routes_outside(cur, "hermes", [], "dan")

    (sql, _) = cur.calls[0]
    assert "DELETE" not in sql.upper()
    assert sql.upper().startswith("UPDATE SPECIALIST_ROUTE")


def test_already_disabled_rows_are_left_alone():
    """이미 꺼진 행을 다시 쓰면 `updated_by` 가 바뀌어 **누가 껐는지**가 흐려진다."""
    cur = _Cur()

    specialist_store.disable_routes_outside(cur, "hermes", ["tyit"], "dan")

    (sql, _) = cur.calls[0]
    assert "route_mode <> 'disabled'" in sql


def test_the_assignment_save_calls_it():
    """호출이 빠지면 이 모듈 전체가 무의미하다. **부르는지**를 본다."""
    tree = ast.parse(SOURCE)
    decide_request = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "decide_request"
    )
    called = {
        node.func.id for node in ast.walk(decide_request)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }

    assert "disable_routes_outside" in called


def test_it_runs_after_the_reassignment():
    """재삽입보다 먼저 끄면, 방금 다시 배정한 워크스페이스까지 대상이 된다."""
    body = SOURCE[SOURCE.index("DELETE FROM specialist_workspace"):]
    insert = body.index("INSERT INTO specialist_workspace")
    disable = body.index("disable_routes_outside(")

    assert insert < disable
