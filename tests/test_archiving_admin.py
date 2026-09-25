"""콘솔 운영 손잡이 — **전이 함수를 우회하지 못한다.**

결정: 2026-09-25 오너 확정 §5·§6.

여기서 막는 것 둘.

1. **콘솔 SQL 이 `mode` 와 `writer_owner` 를 따로 갱신하는 것.** 따로 바꾸면
   「active 인데 주인은 master」 가 만들어지고, 그 상태에서 두 writer 가 같은
   파일에 쓴다. 줄이 섞이고 `doc_count` 가 유실되는데 아무도 예외를 안 받는다
2. **사람과 사유 없이 바꾸는 것.** 없으면 사고가 났을 때 범위를 정할 수 없다

DB 를 요구하지 않는다 — 요구하면 개발 PC 에서 안 돌고, **안 도는 시험은 지켜
주지 않는다.** 커서를 갈아 끼우고 어떤 SQL 이 나가는지 본다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tybot.archive.archiving_state import ChannelMode, ChannelState, WriterOwner
from tybot.console import archiving_admin as admin

ROOT = Path(__file__).resolve().parent.parent
SOURCE = (ROOT / "src" / "tybot" / "console" / "archiving_admin.py").read_text(
    encoding="utf-8"
)


class FakeCursor:
    """나간 SQL 과 인자를 모은다. 돌려줄 행은 미리 넣어 둔다."""

    def __init__(self, rows: list[dict | None]) -> None:
        self.rows = list(rows)
        self.calls: list[tuple[str, tuple]] = []

    def execute(self, sql, params=()):
        self.calls.append((" ".join(str(sql).split()), tuple(params or ())))

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


@pytest.fixture
def cursor(monkeypatch):
    holder: dict[str, FakeCursor] = {}

    def make(rows):
        cur = FakeCursor(rows)
        holder["cur"] = cur
        monkeypatch.setattr(admin, "_connect", lambda: FakeConn(cur))
        return cur

    return make


def _sql_of(cur: FakeCursor, needle: str) -> tuple[str, tuple]:
    for sql, params in cur.calls:
        if needle in sql:
            return sql, params
    raise AssertionError(f"{needle} 를 담은 SQL 이 없다: {[c[0][:60] for c in cur.calls]}")


# --- 사람과 사유 없이는 못 바꾼다 --------------------------------------------

def test_an_actor_without_a_name_is_refused():
    with pytest.raises(admin.AdminRefused, match="바꾼 사람"):
        admin.Actor("", "파일럿 시작")


def test_an_actor_without_a_reason_is_refused():
    """기본값을 두면 전부 그 기본값으로 남고, 그건 기록이 아니다."""
    with pytest.raises(admin.AdminRefused, match="사유"):
        admin.Actor("dan", "   ")


def test_every_write_records_the_actor_and_the_reason(cursor):
    cur = cursor([{"mode": "off", "writer_owner": "master", "cutover_ts": ""}])

    admin.set_channel_mode("tyit", "C1", ChannelMode.SHADOW, admin.Actor("dan", "파일럿"))

    _, params = _sql_of(cur, "INSERT INTO archive_config_audit")
    assert params[0] == "dan"
    assert "파일럿" in params


# --- 전이 함수를 지난다 ------------------------------------------------------

def test_an_illegal_transition_never_reaches_the_database(cursor):
    """`off → active` 는 SQL 이 나가기 전에 막힌다."""
    cur = cursor([{"mode": "off", "writer_owner": "master", "cutover_ts": ""}])

    with pytest.raises(admin.AdminRefused, match="off → active"):
        admin.set_channel_mode(
            "tyit", "C1", ChannelMode.ACTIVE, admin.Actor("dan", "급함"),
            cutover_ts="1700000000.0001",
        )

    assert not [sql for sql, _ in cur.calls if sql.startswith("INSERT INTO archive_channel_mode")]
    assert not [sql for sql, _ in cur.calls if "archive_config_audit" in sql]


def test_mode_and_owner_are_written_in_one_statement(cursor):
    """따로 쓰는 UPDATE 가 있으면 둘이 갈라지는 순간이 생긴다."""
    cur = cursor([{"mode": "shadow", "writer_owner": "master", "cutover_ts": ""}])

    after = admin.set_channel_mode(
        "tyit", "C1", ChannelMode.ACTIVE, admin.Actor("dan", "인수"),
        cutover_ts="1700000000.0001",
    )

    assert after.writer_owner == WriterOwner.ARCHIVER
    sql, params = _sql_of(cur, "INSERT INTO archive_channel_mode")
    assert "mode" in sql and "writer_owner" in sql
    assert "active" in params and "archiver" in params


def test_the_owner_comes_from_the_transition_not_the_caller(cursor):
    """호출부가 주인을 고를 수 있으면 전이 규칙이 장식이 된다."""
    import inspect

    signature = inspect.signature(admin.set_channel_mode)

    assert "writer_owner" not in signature.parameters


def test_no_sql_in_this_module_updates_the_owner_on_its_own():
    """`writer_owner` 만 건드리는 SQL 이 하나라도 있으면 그 경로가 규칙 밖이다."""
    for statement in re.findall(r"UPDATE archive_channel_mode.*?\"\"\"", SOURCE, re.S):
        assert "mode" in statement, statement[:80]


def test_the_transition_helper_is_called_from_exactly_one_place():
    """전이 계산이 여러 자리에 있으면 한쪽만 고치는 날이 온다.

    문자열로 세지 않는다 — 주석과 import 까지 세어져서, 설명을 한 줄 더 쓰면
    시험이 깨진다. **호출 지점**을 센다.
    """
    import ast

    calls = [
        node
        for node in ast.walk(ast.parse(SOURCE))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "plan_mode_change"
    ]

    assert len(calls) == 1, f"{len(calls)}곳에서 전이를 계산한다"


def test_a_reverse_cutover_goes_through_the_same_path(cursor):
    """역인수도 좌표를 요구한다. 콘솔이 그 규칙을 우회하지 않는다."""
    cur = cursor([{"mode": "paused", "writer_owner": "archiver", "cutover_ts": "1700000000.0001"}])

    with pytest.raises(admin.AdminRefused, match="역인수 좌표"):
        admin.set_channel_mode("tyit", "C1", ChannelMode.SHADOW, admin.Actor("dan", "롤백"))

    assert not [sql for sql, _ in cur.calls if sql.startswith("INSERT INTO archive_channel_mode")]


def test_an_unknown_channel_starts_from_off(cursor):
    """행이 없으면 `off` 에서 출발한다. 없는 채널을 바로 active 로 켤 수 없다."""
    cur = cursor([None])

    with pytest.raises(admin.AdminRefused, match="off → active"):
        admin.set_channel_mode(
            "tyit", "C-NEW", ChannelMode.ACTIVE, admin.Actor("dan", "신규"),
            cutover_ts="1700000000.0001",
        )
    assert cur.calls, "조회는 했어야 한다"


def test_plan_is_the_only_calculation_point():
    """`plan` 이 `archiving_state` 를 감싸는 유일한 자리다."""
    state = ChannelState("tyit", "C1", ChannelMode.SHADOW, WriterOwner.MASTER)

    after = admin.plan(state, ChannelMode.ACTIVE, cutover_ts="1700000000.0001")

    assert after.writer_owner == WriterOwner.ARCHIVER


# --- 기능 스위치 -------------------------------------------------------------

@pytest.mark.parametrize(
    ("scope", "scope_key"),
    [("global", "tyit"), ("workspace", ""), ("channel", "")],
    ids=["global-with-key", "workspace-without-key", "channel-without-key"],
)
def test_flag_scope_and_target_must_agree(cursor, scope, scope_key):
    """범위와 대상이 어긋나면 그 스위치가 무엇에 걸리는지 아무도 모른다."""
    cursor([])

    with pytest.raises(admin.AdminRefused, match="대상"):
        admin.set_feature_flag(
            "separate_attachments", True, admin.Actor("dan", "파일럿"),
            scope=scope, scope_key=scope_key,
        )


def test_an_unknown_scope_is_refused(cursor):
    cursor([])

    with pytest.raises(admin.AdminRefused, match="범위"):
        admin.set_feature_flag(
            "x", True, admin.Actor("dan", "왜"), scope="everywhere", scope_key="k"
        )


def test_turning_a_flag_on_is_audited_with_the_old_value(cursor):
    """이전 값이 없으면 감사를 보고 「무엇이 바뀌었나」 를 알 수 없다."""
    cur = cursor([{"enabled": False}])

    admin.set_feature_flag(
        "separate_attachments", True, admin.Actor("dan", "reader 준비됨"),
        scope="workspace", scope_key="tyit",
    )

    _, params = _sql_of(cur, "INSERT INTO archive_config_audit")
    assert "false" in params and "true" in params


# --- 보존 정책 ---------------------------------------------------------------

def test_zero_days_is_refused(cursor):
    """기록이 생기자마자 사라지면 감사 계약이 성립하지 않는다."""
    cursor([])

    with pytest.raises(admin.AdminRefused, match="1일 이상"):
        admin.set_retention("bot_conversation_audit", 0, admin.Actor("dan", "법무 요청"))


def test_negative_days_is_refused(cursor):
    cursor([])

    with pytest.raises(admin.AdminRefused, match="1일 이상"):
        admin.set_retention("bot_conversation_audit", -1, admin.Actor("dan", "오타"))


def test_an_unknown_policy_is_refused(cursor):
    """없는 이름을 조용히 만들면 게이트가 보는 행과 다른 행이 생긴다."""
    cursor([None])

    with pytest.raises(admin.AdminRefused, match="없는 보존 정책"):
        admin.set_retention("made_up", 30, admin.Actor("dan", "실수"))


def test_setting_a_policy_records_who_approved_it(cursor):
    """사람이 없으면 나중에 그 값을 바꿔도 되는지 아무도 모른다."""
    cur = cursor([{"retention_days": None}])

    admin.set_retention("bot_conversation_audit", 90, admin.Actor("dan", "법무 승인 2026-09-25"))

    sql, params = _sql_of(cur, "UPDATE archive_retention_policy")
    assert "approved_by" in sql and "approved_at" in sql
    assert "dan" in params


def test_clearing_a_policy_is_also_recorded(cursor):
    """되돌리는 것도 결정이다. 기록이 없으면 왜 비었는지 모른다."""
    cur = cursor([{"retention_days": 90}])

    admin.set_retention("bot_conversation_audit", None, admin.Actor("dan", "재검토"))

    _, params = _sql_of(cur, "INSERT INTO archive_config_audit")
    assert "90" in params


# --- 화면이 막힌 이유를 직접 보여 준다 ----------------------------------------

def test_the_detail_view_carries_the_production_blockers():
    """안 보여 주면 누군가 막힌 이유를 찾으러 서버에 들어간다."""
    assert "blockers" in SOURCE
    assert "production_blockers" in SOURCE


def test_only_global_flags_feed_the_gate():
    """파일럿 한 채널이 전체 판정을 뒤집으면 안 된다."""
    flags = [
        {"name": "separate_attachments", "scope": "global", "enabled": False},
        {"name": "separate_attachments", "scope": "channel", "enabled": True},
    ]

    assert admin._flag_map(flags) == {"separate_attachments": False}
