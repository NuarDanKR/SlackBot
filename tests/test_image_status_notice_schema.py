"""B-100 상태 표 스키마 — 선언 대조와 **진짜 PostgreSQL** 시험.

선언 시험은 늘 돈다. DB 시험은 격리 DB 가 있을 때만 돈다:

    TEST_DATABASE_URL=postgresql://…/tybot_schema_test python -m pytest \\
        tests/test_image_status_notice_schema.py

**운영 DB 를 주지 마십시오.** 시작할 때 상태 표를 TRUNCATE 한다. 이름 검사로 운영 DB 는
코드가 거부한다(`operational_db_refusal`).
"""

from __future__ import annotations

import os
import pathlib
import re
from datetime import UTC, datetime, timedelta

import pytest

from tybot.archive import image_status_notice as notices

ROOT = pathlib.Path(__file__).resolve().parents[1]
SQL = ROOT / "deploy" / "sql"
SCHEMA = SQL / "image_status_notice_schema.sql"
DSN = os.environ.get("TEST_DATABASE_URL", "").strip()


def test_the_schema_is_applied_after_the_policy_table_it_reads():
    order = (ROOT / "deploy" / "apply-schema.sh").read_text(encoding="utf-8")
    assert order.index("archiver_image_description_schema.sql") < order.index(
        "image_status_notice_schema.sql"
    )


def test_archiver_gets_a_yes_no_function_not_the_vision_policy_table():
    text = SCHEMA.read_text(encoding="utf-8")
    assert "SECURITY DEFINER" in text and "SET search_path = public, pg_temp" in text
    assert "REVOKE ALL ON FUNCTION image_notice_vision_enabled(text) FROM PUBLIC" in text
    grants = [line.strip() for line in text.splitlines() if line.strip().startswith("GRANT")]
    assert not any("archiver_image_description" in line for line in grants)
    assert any("tybot_archiver" in line and "image_status_notice" in line for line in grants)
    assert any("tybot_vision" in line and "image_status_notice" in line for line in grants)
    # 행을 지우는 권한은 누구에게도 없다 — 상태는 고쳐 가며 남는다.
    assert not any("DELETE" in line for line in grants)


ARCHIVER_COLUMNS = {
    "slack_eligible", "reply_ts", "delivered_version", "delivery_state",
    "attempt_version", "attempts", "last_attempt_at", "next_attempt_at", "error_code",
}


def test_vision_can_write_only_the_phase_columns():
    """worker·콘솔(tybot_vision)은 reply_ts·delivery_state 같은 Archiver 칸을 못 고친다."""
    text = " ".join(SCHEMA.read_text(encoding="utf-8").split())
    assert "REVOKE ALL ON TABLE image_status_notice FROM tybot_vision" in text
    assert "UPDATE ON TABLE image_status_notice TO tybot_vision" not in text
    columns = {}
    for kind in ("INSERT", "UPDATE"):
        found = re.search(
            kind + r" \(([^)]*)\) ON TABLE image_status_notice TO tybot_vision", text
        )
        assert found, kind
        columns[kind] = {c.strip() for c in found.group(1).split(",")}
        assert not columns[kind] & ARCHIVER_COLUMNS, kind
    assert "version" in columns["UPDATE"] and "phase" in columns["UPDATE"]
    assert (
        "GRANT EXECUTE ON FUNCTION image_notice_resend(text, text, text, text) TO tybot_vision"
        in text
    )


def test_phase_and_delivery_values_match_the_code():
    text = SCHEMA.read_text(encoding="utf-8")
    phases = re.search(r"phase text NOT NULL CHECK \(phase IN \((.*?)\)\)", text, re.S)
    assert phases is not None
    assert set(re.findall(r"'([a-z_]+)'", phases.group(1))) == set(notices.PHASES)
    assert "CHECK (delivery_state <> 'delivered' OR reply_ts <> '')" in text


# --- 진짜 PostgreSQL ------------------------------------------------------------

db = pytest.mark.skipif(
    not DSN,
    reason="TEST_DATABASE_URL 이 없어 실제 DB 검증을 건너뜁니다(행·CHECK·잠금은 미검증).",
)


def _refuse_operational_db() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_schema_isolated import operational_db_refusal

    refusal = operational_db_refusal(DSN)
    if refusal:
        pytest.fail(f"운영 DB 로 보여 시작하지 않습니다: {refusal}", pytrace=False)


@pytest.fixture
def conn():
    _refuse_operational_db()
    psycopg = pytest.importorskip("psycopg", reason="psycopg 미설치")
    from psycopg.rows import dict_row

    connection = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    try:
        with connection.cursor() as cur:
            # 폐기 DB 전용: 열 권한을 실제 역할로 재려고 역할을 만들고, 표는 새 판으로 다시 만든다.
            cur.execute(
                "DO $$ BEGIN "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='tybot_vision') "
                "THEN CREATE ROLE tybot_vision NOLOGIN; END IF; "
                "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='tybot_archiver') "
                "THEN CREATE ROLE tybot_archiver NOLOGIN; END IF; END $$"
            )
            cur.execute("DROP TABLE IF EXISTS image_status_notice")
            for name in (
                "archiver_image_description_schema.sql",
                "archiver_image_transcript_schema.sql",
                "image_status_notice_schema.sql",
            ):
                cur.execute((SQL / name).read_text(encoding="utf-8"))
            cur.execute("TRUNCATE image_status_notice")
        yield connection
    finally:
        connection.close()


KEY = notices.NoticeKey("tyit", "C12345678", "1794000000.000100", "F12345678")


class Slack:
    def __init__(self):
        self.posts = 0
        self.updates = 0

    def conversations_info(self, *, channel):
        return {"channel": {"is_member": True}}

    def chat_postMessage(self, **_kw):
        self.posts += 1
        return {"ts": "1794000001.000001"}

    def chat_update(self, **_kw):
        self.updates += 1
        return {"ok": True}


@db
def test_records_advance_once_and_deliver_as_one_comment(conn):
    repo = notices.PostgresNoticeRepo(conn)
    slack = Slack()
    accepted = notices.NoticeRow(
        key=KEY,
        phase=notices.ACCEPTED,
        payload=notices.receipt_payload(
            name="a.png", conversion_state="succeeded", has_text=True, vision_enabled=True
        ),
    )
    assert repo.record(accepted) is True
    assert repo.record(accepted) is False
    notices.deliver_pending(slack, repo, "tyit", allowed_channels={"C12345678"})
    assert repo.record(notices.NoticeRow(key=KEY, phase=notices.PROCESSING)) is True
    notices.deliver_pending(slack, repo, "tyit", allowed_channels={"C12345678"})
    notices.deliver_pending(slack, repo, "tyit", allowed_channels={"C12345678"})

    (row,) = repo.recent("tyit")
    assert (slack.posts, slack.updates) == (1, 1)
    assert row.version == row.delivered_version == 2 and row.delivery_state == "delivered"
    assert row.payload["file"] == "a.png"  # 접수 때 이름을 이어받았다


@db
def test_failed_delivery_is_kept_and_can_be_resent(conn):
    repo = notices.PostgresNoticeRepo(conn)
    repo.record(notices.NoticeRow(key=KEY, phase=notices.ACCEPTED))
    notices.deliver_pending(Slack(), repo, "tyit", allowed_channels=frozenset())
    (row,) = repo.recent("tyit")
    assert row.delivery_state == "failed_delivery" and row.error_code == "unregistered_channel"
    assert repo.resend(KEY) is True
    assert repo.pending("tyit")


@db
def test_the_schema_refuses_a_delivered_row_without_a_comment(conn):
    import psycopg

    repo = notices.PostgresNoticeRepo(conn)
    repo.record(notices.NoticeRow(key=KEY, phase=notices.ACCEPTED))
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        cur.execute("UPDATE image_status_notice SET delivery_state='delivered'")


@db
def test_retry_schedule_is_stored(conn):
    repo = notices.PostgresNoticeRepo(conn)
    repo.record(notices.NoticeRow(key=KEY, phase=notices.ACCEPTED))
    (row,) = repo.pending("tyit")
    assert repo.begin_attempt(row) == 1
    repo.mark_retry(row, "ratelimited", datetime.now(UTC) + timedelta(hours=1))
    assert repo.pending("tyit") == []
    (row,) = repo.recent("tyit")
    assert row.delivery_state == "retrying" and row.error_code == "ratelimited"


@db
def test_the_vision_flag_function_follows_global_and_workspace_rows(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT image_notice_vision_enabled('tyit') AS on")
        assert cur.fetchone()["on"] is False


def _set_role(conn, role: str) -> None:
    with conn.cursor() as cur:
        cur.execute("SET ROLE " + role if role else "RESET ROLE")


@db
def test_vision_records_state_but_cannot_touch_delivery_columns(conn):
    import psycopg

    _set_role(conn, "tybot_vision")
    try:
        repo = notices.PostgresNoticeRepo(conn)
        # 과거 이미지: worker 만 적는다. 행은 생기지만 Slack 대상이 아니다.
        assert repo.record(notices.NoticeRow(key=KEY, phase=notices.PROCESSING)) is True
        assert repo.record(
            notices.NoticeRow(
                key=KEY, phase=notices.COMPLETED, payload={"search_published": True}
            )
        ) is True
        assert repo.pending("tyit") == []
        for column, value in (
            ("reply_ts", "'1.1'"),
            ("delivery_state", "'delivered'"),
            ("slack_eligible", "true"),
            ("attempts", "9"),
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.cursor() as cur:
                cur.execute(f"UPDATE image_status_notice SET {column}={value}")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.cursor() as cur:
            cur.execute(
                "INSERT INTO image_status_notice (workspace, channel_id, message_ts, file_id, "
                "phase, slack_eligible) VALUES ('tyit','C12345678','1.000001','F99999999', "
                "'accepted', true)"
            )
        # 재전송 함수는 부를 수 있다. 대상이 아닌 행(소급 이미지)은 되돌리지 않는다.
        assert repo.resend(KEY) is False
    finally:
        _set_role(conn, "")


@db
def test_a_receipt_after_worker_completion_keeps_the_result(conn):
    repo = notices.PostgresNoticeRepo(conn)
    _set_role(conn, "tybot_vision")
    try:
        repo.record(
            notices.NoticeRow(key=KEY, phase=notices.COMPLETED, payload={"description": "현장"})
        )
    finally:
        _set_role(conn, "")
    _set_role(conn, "tybot_archiver")
    try:
        assert repo.record(
            notices.NoticeRow(
                key=KEY, phase=notices.ACCEPTED, payload={"file": "a.png", "ocr": "text"}
            )
        ) is True
        (row,) = repo.pending("tyit")
    finally:
        _set_role(conn, "")
    assert row.phase == "completed" and row.slack_eligible
    assert row.payload == {"description": "현장", "file": "a.png", "ocr": "text"}


@db
def test_a_concurrent_first_insert_is_merged_not_lost(conn):
    """접수와 worker 완료가 동시에 첫 행을 만든다 — 늦은 쪽은 충돌 뒤 다시 잠가 합친다."""
    import threading

    import psycopg
    from psycopg.rows import dict_row

    other = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    result: dict = {}
    try:
        holder = other.transaction()
        holder.__enter__()
        with other.cursor() as cur:  # 접수가 먼저 INSERT 하고 아직 커밋 전
            cur.execute(
                "INSERT INTO image_status_notice (workspace, channel_id, message_ts, file_id, "
                "phase, payload, slack_eligible) VALUES (%s,%s,%s,%s,'accepted','{}',true)",
                ("tyit", KEY.channel_id, KEY.message_ts, KEY.file_id),
            )

        def worker():
            result["ok"] = notices.PostgresNoticeRepo(conn).record(
                notices.NoticeRow(key=KEY, phase=notices.COMPLETED, payload={"x": 1})
            )

        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=1)
        assert thread.is_alive()  # 고유키 충돌에서 접수 커밋을 기다린다
        holder.__exit__(None, None, None)
        thread.join(timeout=10)
    finally:
        other.close()
    assert result == {"ok": True}
    (row,) = notices.PostgresNoticeRepo(conn).recent("tyit")
    assert row.phase == "completed" and row.slack_eligible and row.version == 2
