"""Archiving Bot 스키마가 **선언한 것을 실제로 막는지.**

코드의 상태 전이(`test_archiving_state.py`)는 가는 길을 막는다. 그런데 DB 에는
코드를 거치지 않고 들어오는 길이 늘 있다 — psql, 콘솔의 다른 화면, 나중에 누가
쓸 배치. 그래서 표 자체가 모순을 거절해야 한다.

여기서는 SQL 문을 **파싱해서** 그 선언이 있는지 본다. 진짜 DB 를 요구하면 개발
PC 에서 안 돌고, 안 도는 시험은 지켜 주지 않는다. 대신 「문자열이 들어 있나」 로
보지 않는다 — 주석에 적어 놓기만 해도 통과하기 때문이다.

결정: 2026-09-25 오너 확정.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "deploy" / "sql" / "archiving_schema.sql"
APPLY = ROOT / "deploy" / "apply-schema.sh"


def _sql() -> str:
    """주석을 **지운** SQL. 주석에 적어 둔 것이 선언으로 세어지면 안 된다."""
    text = SCHEMA.read_text(encoding="utf-8")
    return re.sub(r"--[^\n]*", "", text)


@pytest.fixture(scope="module")
def sql() -> str:
    return _sql()


def _constraint(sql: str, name: str) -> str:
    """`ADD CONSTRAINT <name> CHECK (...)` 의 괄호 안을 돌려준다."""
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


# --- 배포 경로에 들어 있나 ---------------------------------------------------

def test_the_schema_is_registered_for_deployment():
    """목록에 없으면 **한 번도 적용되지 않는다.**

    실제로 그래서 콘솔이 `column does not exist` 로 죽었다(2026-09-14).
    파일을 만드는 것과 적용되는 것은 다른 일이다.
    """
    assert "archiving_schema.sql" in APPLY.read_text(encoding="utf-8")


def test_the_schema_is_one_transaction():
    """중간에 실패하면 절반만 선 표가 남는다. 그 상태가 제일 고치기 어렵다."""
    text = _sql()
    assert text.lstrip().startswith("BEGIN;")
    assert text.rstrip().endswith("COMMIT;")


# --- 두 writer 가 동시에 쓰지 못한다 -----------------------------------------

def test_active_channel_must_belong_to_the_archiver(sql):
    """`active` 인데 주인이 master 면 모순이다.

    그 상태에서는 어느 쪽 말을 믿어야 하는지 알 수 없고, 둘 다 쓰면 줄이 섞인다.
    """
    body = _constraint(sql, "archive_channel_mode_owner_matches")

    assert body, "제약이 없다"
    assert "mode" in body and "'active'" in body
    assert "writer_owner" in body and "'archiver'" in body


def test_off_and_shadow_channels_belong_to_master(sql):
    """그림자 수집 중에도 운영 원문은 Master가 쓴다."""
    body = _constraint(sql, "archive_channel_mode_owner_matches")

    assert "'off'" in body and "'shadow'" in body
    assert "'master'" in body


def test_archiver_ownership_requires_a_cutover_coordinate(sql):
    """없으면 「언제부터 이 봇 몫인가」 를 나중에 아무도 모른다."""
    body = _constraint(sql, "archive_channel_mode_cutover_present")

    assert body
    assert "writer_owner" in body and "cutover_ts" in body


def test_writer_owner_is_a_closed_set(sql):
    """제3의 주인이 생기면 「둘 중 하나」 라는 전제가 조용히 깨진다."""
    found = re.search(r"writer_owner\s+text[^,]*?CHECK\s*\(([^)]*)\)", sql, re.S)

    assert found, "writer_owner 에 CHECK 가 없다"
    assert set(re.findall(r"'(\w+)'", found.group(1))) == {"master", "archiver"}


def test_channel_mode_is_a_closed_set(sql):
    found = re.search(r"mode\s+text[^,]*?CHECK\s*\(([^)]*)\)", sql, re.S)

    assert found
    assert set(re.findall(r"'(\w+)'", found.group(1))) == {
        "off", "shadow", "active", "paused"
    }


# --- 첨부가 안 끝났으면 ready 가 아니다 --------------------------------------

def test_ready_requires_every_attachment(sql):
    """오너 결정 §6 을 **표가** 막는다.

    코드 규칙으로만 두면 한 경로가 빠지고, 그 경로만 거짓말한다.
    """
    body = _constraint(sql, "archive_ingest_state_ready_needs_attachments")

    assert body
    assert "'ready'" in body
    assert "attachment_ready" in body and "attachment_total" in body


def test_attachment_counts_cannot_be_nonsense(sql):
    """준비된 수가 전체보다 많으면 위 제약이 통과해 버린다."""
    body = _constraint(sql, "archive_ingest_state_counts_sane")

    assert body
    assert "attachment_ready <= attachment_total" in body


def test_ingest_states_match_the_decision(sql):
    """오너가 정한 일곱 가지. 늘거나 줄면 코드와 표가 갈린다."""
    found = re.search(r"state\s+text[^;]*?CHECK\s*\(state IN \(([^)]*)\)", sql, re.S)

    assert found
    assert set(re.findall(r"'([a-z_]+)'", found.group(1))) == {
        "received", "raw_written", "attachment_pending",
        "ready", "partial", "refused", "failed",
    }


def test_the_python_states_and_the_sql_states_are_the_same():
    """둘이 갈리면 코드가 쓴 값을 표가 거절하거나, 표가 받는 값을 코드가 모른다."""
    from tybot.archive.archiving_state import ChannelMode, IngestState, RevisionKind

    text = _sql()
    for enum, column in (
        (IngestState, r"state\s+text[^;]*?CHECK\s*\(state IN \(([^)]*)\)"),
        (ChannelMode, r"mode\s+text[^,]*?CHECK\s*\(([^)]*)\)"),
        (RevisionKind, r"kind\s+text[^;]*?CHECK\s*\(kind IN \(([^)]*)\)"),
    ):
        found = re.search(column, text, re.S)
        assert found, enum.__name__
        assert set(re.findall(r"'([a-z_]+)'", found.group(1))) == {m.value for m in enum}


# --- revision 은 쌓이고, 앞의 것은 안 바뀐다 ---------------------------------

def test_the_first_revision_must_be_a_create(sql):
    """change 로 시작하면 원본이 없다는 뜻이고, 그때 무엇이 바뀌었는지 못 말한다."""
    body = _constraint(sql, "archive_message_revision_first_is_create")

    assert body
    assert "revision_no > 1" in body and "'create'" in body


def test_revision_order_is_enforced_by_a_trigger(sql):
    """행 하나만 보는 CHECK로는 revision 2부터 넣는 일을 막을 수 없다."""
    assert "CREATE OR REPLACE FUNCTION enforce_archive_message_revision_order()" in sql
    assert "CREATE TRIGGER archive_message_revision_order" in sql
    assert "NEW.revision_no <> latest_no + 1" in sql
    assert "latest_kind = 'redact'" in sql


def test_redacted_rows_keep_no_body_hash(sql):
    """짧은 본문은 사전 대입으로 해시에서 되찾힌다.

    해시를 남기면 「본문을 남기지 않는다」 를 지킨 것이 아니다.
    """
    body = _constraint(sql, "archive_message_revision_redact_is_bare")

    assert body
    assert "'redact'" in body
    assert "body_sha256 = ''" in body
    assert "reason_code <> ''" in body


def test_revision_is_keyed_by_message_and_number(sql):
    """revision 을 덮어쓰는 경로가 열리면 감사가 성립하지 않는다."""
    assert "PRIMARY KEY (workspace, channel_id, message_ts, revision_no)" in sql


def test_search_reads_one_view_not_scattered_sql(sql):
    """조회하는 쪽마다 「최신 고르기」 를 쓰면 한 군데가 지워진 것을 보여 준다."""
    assert "CREATE OR REPLACE VIEW archive_message_current" in sql
    assert "DISTINCT ON (workspace, channel_id, message_ts)" in sql
    assert "revision_no DESC" in sql
    view = sql[sql.index("CREATE OR REPLACE VIEW archive_message_current"):]
    view = view[:view.index(";")]
    assert "kind NOT IN ('delete', 'redact')" in view


# --- 첨부 revision 은 덮어쓰지 않는다 ----------------------------------------

def test_attachment_revision_is_part_of_the_key(sql):
    """키에 없으면 재변환이 옛 변환본을 덮는다.

    덮으면 그 변환본을 인용한 답변의 출처를 눌렀을 때 문장이 없다.
    """
    assert "PRIMARY KEY (workspace, channel_id, file_id, revision)" in sql


def test_superseded_is_a_state_not_a_deletion(sql):
    """더 새 revision 이 나와도 **지우지 않는다.**"""
    found = re.search(
        r"state\s+text[^;]*?CHECK\s*\(state IN \('pending'([^)]*)\)", sql, re.S
    )

    assert found
    assert "superseded" in found.group(0)


def test_attachment_revision_records_all_four_inputs(sql):
    """revision 이 결정적이려면 그 재료가 남아 있어야 한다. 없으면 재현이 안 된다."""
    for column in ("source_sha256", "converter_name", "converter_version", "config_sha256"):
        assert re.search(rf"^\s*{column}\s+text NOT NULL", sql, re.M), column


# --- 거부 기록에 본문이 없다 --------------------------------------------------

def test_refusal_table_stores_no_body(sql):
    """PII 를 막으려고 만든 표가 PII 저장소가 되면 안 된다."""
    block = sql[sql.index("CREATE TABLE IF NOT EXISTS archive_refusal"):]
    block = block[: block.index(");")]

    for banned in ("body", "text_content", "matched", "excerpt"):
        assert banned not in block, f"거부 기록에 {banned} 가 있다"
    assert "reason_code" in block


# --- 보존 정책 ---------------------------------------------------------------

def test_retention_days_is_nullable_so_unset_is_distinguishable(sql):
    """`NULL`(안 정함)과 `0`(즉시 삭제)은 다른 결정이다.

    `NOT NULL DEFAULT 0` 으로 두면 「안 정함」 을 표현할 수 없고, 그러면 게이트가
    막을 것이 없어진다.
    """
    found = re.search(r"retention_days\s+integer([^,]*),", sql)

    assert found
    assert "NOT NULL" not in found.group(1)


def test_a_set_retention_must_name_who_approved_it(sql):
    """사람이 없으면 나중에 그 값을 바꿔도 되는지 아무도 모른다."""
    body = _constraint(sql, "archive_retention_policy_approved")

    assert body
    assert "approved_by" in body and "approved_at" in body


def test_the_two_required_policies_are_seeded(sql):
    """행이 없으면 게이트가 「없음」 을 「통과」 로 읽는다."""
    from tybot.archive.archiving_state import REQUIRED_RETENTION

    for name in REQUIRED_RETENTION:
        assert f"'{name}'" in sql, name


# --- 기능 스위치는 전부 꺼진 채로 들어온다 -----------------------------------

def test_every_feature_flag_defaults_to_off(sql):
    """읽는 쪽이 붙기 전에 켜지면 그 자료가 조용히 답변에서 빠진다."""
    found = re.search(r"enabled\s+boolean\s+NOT NULL\s+DEFAULT\s+(\w+)", sql)

    assert found and found.group(1) == "false"
    # 씨앗 INSERT 가 enabled 를 지정하면 기본값이 무의미해진다.
    seed = sql[sql.index("INSERT INTO archive_feature_flag"):]
    seed = seed[: seed.index(";")]
    assert "enabled" not in seed


def test_attachment_reader_readiness_is_an_explicit_flag(sql):
    assert "('attachment_reader_ready'" in sql


def test_feature_flags_can_be_scoped_without_name_collisions(sql):
    """같은 기능을 전역과 특정 workspace에서 각각 설정할 수 있어야 한다."""
    assert "PRIMARY KEY (name, scope, scope_key)" in sql
    body = _constraint(sql, "archive_feature_flag_scope_valid")
    assert "'global'" in body and "'workspace'" in body and "'channel'" in body
    assert "scope_key" in body


# --- archiver 역할은 최소권한이다 --------------------------------------------

def _archiver_grants(sql: str) -> list[str]:
    block = sql[sql.index("rolname = 'tybot_archiver'"):]
    block = block[: block.index("END IF;")]
    return [" ".join(part.split()) for part in re.findall(r"'([^']*GRANT[^;]*)'", block)] or [
        " ".join(block.split())
    ]


def test_archiver_can_never_delete(sql):
    """아카이빙 봇은 지우는 일을 하지 않는다. 권한이 있으면 언젠가 쓴다."""
    for grant in _archiver_grants(sql):
        assert "DELETE" not in grant, grant


def test_archiver_cannot_change_its_own_mode(sql):
    """봇이 자기 모드를 바꿀 수 있으면 shadow 가 안전장치가 아니게 된다."""
    block = sql[sql.index("rolname = 'tybot_archiver'"):]
    block = block[: block.index("END IF;")]
    settings = re.search(
        r"GRANT SELECT ON TABLE'\s*'\s*([^']*archive_channel_mode[^']*)'", block
    )

    assert settings, "설정 표 GRANT 를 찾지 못했다"
    assert "UPDATE" not in block.split("archive_channel_mode")[0][-80:]


def test_archiver_cannot_forge_config_audit(sql):
    """설정을 바꾸지 못하는 역할이 설정 변경 감사도 만들면 안 된다."""
    block = sql[sql.index("rolname = 'tybot_archiver'"):]
    block = block[: block.index("END IF;")]

    assert "REVOKE ALL PRIVILEGES ON TABLE archive_config_audit" in block
    grants = "\n".join(part for part in block.split("EXECUTE") if "GRANT" in part)
    assert "archive_config_audit" not in grants


def test_append_only_tables_get_no_update(sql):
    """감사가 고쳐지면 감사가 아니다."""
    block = sql[sql.index("rolname = 'tybot_archiver'"):]
    block = block[: block.index("END IF;")]
    for table in ("archive_message_revision", "archive_refusal"):
        line = next(part for part in block.split("EXECUTE") if table in part)
        assert "UPDATE" not in line, f"{table} 에 UPDATE 가 있다"
        assert "INSERT" in line, table


def test_runtime_role_cannot_update_or_delete_append_only_audit(sql):
    block = sql[sql.index("rolname = 'tyslackai'"):]
    block = block[: block.index("END IF;")]
    append_grant = next(
        part for part in block.split("EXECUTE")
        if "archive_message_revision" in part and "GRANT SELECT, INSERT" in part
    )

    assert "UPDATE" not in append_grant
    assert "DELETE" not in append_grant
    assert "SELECT, INSERT" in append_grant
    assert "REVOKE UPDATE, DELETE ON TABLE" in block
    assert "archive_message_revision, archive_refusal, archive_config_audit" in block


def test_archiver_gets_no_secret_tables(sql):
    """시크릿은 저장소 밖이고, 역할 밖이기도 하다(절대 원칙 6)."""
    block = sql[sql.index("rolname = 'tybot_archiver'"):]
    block = block[: block.index("END IF;")]

    for secret in ("workspace_secret", "llm_secret", "harness_file"):
        assert secret not in block, secret
