"""supervisor 스키마가 **선언한 것을 실제로 막는지.**

결정: 2026-09-29 supervisor 작업지시서 §3.2·§4.2.

코드의 상태 규칙(`test_supervisor_state`)은 가는 길을 막는다. DB 에는 코드를 거치지
않고 들어오는 길이 늘 있다 — psql, 다른 화면, 나중에 누가 쓸 배치. 그래서 표
자체가 모순을 거절해야 한다.

SQL 을 **파싱해서** 본다. 주석에 적어 놓기만 해도 통과하면 안 되므로 주석은 지운다.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "deploy" / "sql" / "archiver_supervisor_schema.sql"
APPLY = ROOT / "deploy" / "apply-schema.sh"
VERIFIER = ROOT / "scripts" / "verify_schema_isolated.py"
GATE = ROOT / "src" / "tybot" / "console" / "release_gate.py"


def _sql() -> str:
    return re.sub(r"--[^\n]*", "", SCHEMA.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def sql() -> str:
    return _sql()


def _constraint(sql: str, name: str) -> str:
    start = sql.find(f"ADD CONSTRAINT {name}")
    if start < 0:
        return ""
    check = sql.find("CHECK", start)
    if check < 0:
        return ""
    depth, out = 0, []
    for ch in sql[check:]:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        if depth >= 1:
            out.append(ch)
    return " ".join("".join(out).split())


def _column(sql: str, table: str, column: str) -> str:
    start = sql.find(f"CREATE TABLE IF NOT EXISTS {table} (")
    if start < 0:
        return ""
    depth, out = 0, []
    for ch in sql[start + len(f"CREATE TABLE IF NOT EXISTS {table}"):]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
    for piece in re.split(r",(?![^()]*\))", "".join(out)):
        cleaned = " ".join(piece.split()).lstrip("( ")
        if cleaned.startswith(f"{column} "):
            return cleaned
    return ""


# --- 배포·검증 경로 -------------------------------------------------------------

def test_the_schema_is_registered_everywhere():
    """목록에 없으면 적용도 검증도 안 된다(2026-09-14 실측)."""
    assert "archiver_supervisor_schema.sql" in APPLY.read_text(encoding="utf-8")
    assert "archiver_supervisor_schema.sql" in VERIFIER.read_text(encoding="utf-8")
    assert "archiver_supervisor_schema.sql" in GATE.read_text(encoding="utf-8")


def test_it_runs_after_the_tables_it_references():
    order = [
        line.split("#")[0].strip()
        for line in APPLY.read_text(encoding="utf-8").splitlines()
    ]
    order = [name for name in order if name.endswith(".sql")]

    assert order.index("archiver_supervisor_schema.sql") > order.index("console_schema.sql")


def test_the_schema_is_one_transaction():
    text = _sql()
    assert text.lstrip().startswith("BEGIN;")
    assert text.rstrip().endswith("COMMIT;")


# --- 희망과 관측 ----------------------------------------------------------------

def test_desired_and_observed_are_separate_columns(sql):
    """한 칸에 합치면 「켜라고 했는데 안 켜졌다」 를 표현할 자리가 없다."""
    assert _column(sql, "archiver_workspace_runtime", "desired_mode")
    assert _column(sql, "archiver_workspace_runtime", "observed_state")


def test_the_states_are_closed_sets(sql):
    desired = _column(sql, "archiver_workspace_runtime", "desired_mode")
    observed = _column(sql, "archiver_workspace_runtime", "observed_state")

    assert set(re.findall(r"'(\w+)'", desired)) == {"off", "shadow", "live"}
    assert set(re.findall(r"'(\w+)'", observed)) == {
        "stopped", "starting", "running", "degraded", "error",
    }


def test_a_switched_off_workspace_cannot_be_running(sql):
    """둘 중 하나가 거짓인 상태를 표가 허용하면 화면이 그걸 보여 준다."""
    body = _constraint(sql, "archiver_runtime_off_is_not_running")

    assert body
    assert "desired_mode" in body and "'off'" in body
    assert "observed_state" in body


def test_an_error_code_cannot_outlive_the_error(sql):
    body = _constraint(sql, "archiver_runtime_error_only_when_error")

    assert body
    assert "'error'" in body and "error_code" in body


def test_the_generation_is_a_number_not_a_timestamp(sql):
    """시계가 다르면 갱신을 놓치고, worker 는 옛 설정으로 계속 돈다."""
    generation = _column(sql, "archiver_workspace_runtime", "generation")

    assert "bigint" in generation


# --- cursor ----------------------------------------------------------------------

def test_realtime_and_history_cursors_are_separate(sql):
    """소급이 실시간보다 뒤처진 구간이 곧 「아직 안 메운 곳」 이다."""
    assert _column(sql, "archive_channel_cursor", "last_realtime_ts")
    assert _column(sql, "archive_channel_cursor", "last_history_ts")


def test_the_cursor_remembers_when_slack_said_wait(sql):
    assert _column(sql, "archive_channel_cursor", "retry_after")


# --- 작업 ------------------------------------------------------------------------

def test_job_states_distinguish_nothing_from_failure(sql):
    state = _column(sql, "archive_backfill_job", "state")

    assert set(re.findall(r"'(\w+)'", state)) == {
        "queued", "running", "partial", "succeeded", "failed", "cancelled",
    }


def test_counts_are_kept_apart(sql):
    """하나로 합치면 「500건 처리」 가 400건 중복이었다는 사실을 가린다."""
    for column in (
        "found_count", "written_count", "duplicate_count", "refused_count",
        "failed_count",
    ):
        assert _column(sql, "archive_backfill_job", column), column


def test_a_job_requires_a_person_and_a_reason(sql):
    """소급은 되돌릴 수 없다. 누가 왜 돌렸는지 없으면 나중에 범위를 못 정한다."""
    assert "requested_by  text NOT NULL" in sql
    assert "reason        text NOT NULL" in sql


def test_a_finished_job_has_a_finish_time(sql):
    """없으면 화면이 「도는 중」 으로 보여 주고, 사람은 멈춘 작업을 계속 기다린다."""
    body = _constraint(sql, "archive_backfill_job_finished")

    assert body
    assert "finished_at IS NOT NULL" in body


def test_dry_run_is_the_default(sql):
    """기본이 실제 실행이면, 확인하려던 사람이 실행한다."""
    assert "dry_run       boolean NOT NULL DEFAULT true" in sql


# --- 권한 ------------------------------------------------------------------------

def test_the_archiver_cannot_write_its_own_desired_mode(sql):
    """프로세스가 자기 희망을 적으면 콘솔에서 끈 것이 되살아난다."""
    # SQL 이 여러 줄 문자열로 쪼개져 있다. 따옴표와 줄바꿈을 걷고 본다.
    flat = re.sub(r"'\s*'", "", " ".join(sql.split()))
    grant = re.search(
        r"GRANT UPDATE \(([^)]*)\) ON TABLE archiver_workspace_runtime", flat,
    )

    assert grant, "열 단위 UPDATE 권한이 없다"
    columns = grant.group(1)
    assert "desired_mode" not in columns
    assert "observed_state" in columns and "heartbeat_at" in columns


def test_nobody_can_delete_the_records(sql):
    """작업 기록을 지우면 「그때 무엇을 돌렸나」 가 사라진다."""
    assert sql.count("REVOKE DELETE ON TABLE") >= 2
    assert "GRANT SELECT, INSERT, UPDATE, DELETE" not in sql
