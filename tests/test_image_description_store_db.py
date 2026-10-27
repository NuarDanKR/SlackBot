"""B-81 스키마를 **진짜 PostgreSQL 에서** 건다 (2026-10-27).

## 왜 따로 있나

`tests/test_image_description_store.py` 의 `ScriptedConnection` 은 SQL 을
**실행하지 않는다.** 미리 적어 둔 행을 돌려줄 뿐이라, 스키마의 `CHECK`·외래키·
`UNIQUE`·권한이 **한 번도 돌지 않는다.** 저장소 로직을 재는 데는 맞지만
「DB 가 거절하는가」 는 그 대역으로 답할 수 없다.

이 파일은 실제 연결이 있을 때만 돈다. 없으면 **건너뛴 이유를 분명히 남긴다** —
초록으로 보이는데 아무것도 안 잰 상태가 제일 나쁘다.

```
TEST_DATABASE_URL=postgresql://…/tybot_image_test \
    /opt/tybot/.venv/bin/python -m pytest tests/test_image_description_store_db.py
```

**운영 DB 를 주지 마십시오.** 이 파일은 표를 만들고 행을 넣는다. 전용 격리 DB 만
받고, DSN 은 출력·문서에 남기지 않는다.
"""
from __future__ import annotations

import os
import pathlib
import uuid

import pytest

DSN = os.environ.get("TEST_DATABASE_URL", "").strip()

SKIP_WHY = (
    "TEST_DATABASE_URL 이 없어 실제 DB 검증을 건너뜁니다. "
    "스크립트 대역 시험은 SQL 을 실행하지 않으므로 CHECK·외래키·권한은 "
    "이 세션에서 검증되지 않았습니다."
)

pytestmark = pytest.mark.skipif(not DSN, reason=SKIP_WHY)

SQL_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "deploy" / "sql" / "archiver_image_description_schema.sql"
)


@pytest.fixture(scope="module")
def conn():
    psycopg = pytest.importorskip("psycopg", reason="psycopg 미설치")
    connection = psycopg.connect(DSN, autocommit=True)
    try:
        with connection.cursor() as cur:
            cur.execute(SQL_PATH.read_text(encoding="utf-8"))
        yield connection
    finally:
        connection.close()


def _rid() -> str:
    return uuid.uuid4().hex


@pytest.fixture
def job(conn):
    """승인 한 건 + `dispatching` 작업 한 건. 각 시험마다 새로 만든다."""
    approval_id, job_id = _rid(), _rid()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_description_approval
            (id,workspace,scope,expires_at,approved_by)
            VALUES (%s,'tyit','{}'::jsonb, now() + interval '1 hour','tester')""",
            (approval_id,),
        )
        cur.execute(
            """INSERT INTO archiver_image_description_job
            (id,identity_sha256,approval_id,workspace,policy_revision,prompt_revision,
             scope,policy_snapshot,catalog_snapshot,max_output_tokens,state,
             expires_at,dispatched_at)
            VALUES (%s,%s,%s,'tyit','g:l','p1','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
                    256,'dispatching', now() + interval '1 hour', now())""",
            # identity 는 UNIQUE 다. 고정값을 쓰면 **두 번째 시험부터** 픽스처가
            # 터지고, 그 15건은 스키마가 아니라 시험 탓으로 빨개진다(실측).
            (job_id, _rid() + _rid(), approval_id),
        )
    return job_id


def _insert_revision(cur, job_id: str, **over) -> None:
    row = {
        "id": _rid(),
        "identity_sha256": uuid.uuid4().hex + "0" * 32,
        "job_id": job_id,
        "workspace": "tyit",
        "channel_id": "C1",
        "file_id": "F1",
        "original_sha256": "a" * 64,
        "image_sha256": "b" * 64,
        "prompt_revision": "p1",
        "model": "free-vision-v1",
        "provider": "approved-provider",
        "status": "described",
        "guard_verdict": "allow",
        "guard_codes": [],
        "description": "건설현장이 보인다.",
        "tags": ["건설현장"],
        "uncertainty": [],
    }
    row.update(over)
    cur.execute(
        """INSERT INTO archiver_image_description_revision
        (id,identity_sha256,job_id,workspace,channel_id,file_id,original_sha256,
         image_sha256,prompt_revision,model,provider,status,guard_verdict,
         guard_codes,description,tags,uncertainty)
        VALUES (%(id)s,%(identity_sha256)s,%(job_id)s,%(workspace)s,%(channel_id)s,
                %(file_id)s,%(original_sha256)s,%(image_sha256)s,%(prompt_revision)s,
                %(model)s,%(provider)s,%(status)s,%(guard_verdict)s,%(guard_codes)s,
                %(description)s,%(tags)s,%(uncertainty)s)""",
        row,
    )


# --- 파생 색인에 무엇이 들어갈 수 있나 ------------------------------------------


def test_an_allow_row_is_accepted(conn, job):
    with conn.cursor() as cur:
        _insert_revision(cur, job)


def test_a_withhold_row_stores_tags_and_codes_without_a_body(conn, job):
    """설명만 빠진다. **사진이 있다는 사실은 남아야** PII 검토를 할 수 있다."""
    with conn.cursor() as cur:
        _insert_revision(
            cur, job, guard_verdict="withhold_description", status="uncertain",
            description="", guard_codes=["sensitive_uncertainty"],
            uncertainty=["person_visible"],
        )
        cur.execute(
            """SELECT description,tags,uncertainty,guard_codes
            FROM archiver_image_description_revision WHERE job_id=%s""",
            (job,),
        )
        row = cur.fetchone()
    assert row[0] == ""
    assert row[1] == ["건설현장"]
    assert row[2] == ["person_visible"]
    assert row[3] == ["sensitive_uncertainty"]


@pytest.mark.parametrize("verdict", ["block", "", "BLOCK", "allow_all"])
def test_a_blocked_verdict_can_never_be_inserted(conn, job, verdict):
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        _insert_revision(cur, job, guard_verdict=verdict)


@pytest.mark.parametrize("status", ["failed", "blocked", "unknown", ""])
def test_a_non_indexable_status_can_never_be_inserted(conn, job, status):
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        _insert_revision(cur, job, status=status)


def test_an_allow_row_must_carry_a_body(conn, job):
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        _insert_revision(cur, job, description="")


def test_a_withhold_row_must_not_carry_a_body(conn, job):
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        _insert_revision(cur, job, guard_verdict="withhold_description",
                         status="uncertain", description="본문이 남았다.")


def test_a_withhold_row_must_be_uncertain(conn, job):
    """**게이트가 실제로 내는 조합이다.** 리뷰 문서 §발견 1 참고."""
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        _insert_revision(cur, job, guard_verdict="withhold_description",
                         status="described", description="")


def test_the_description_cap_matches_the_core(conn, job):
    from tybot.archive.image_description import MAX_DESCRIPTION_CHARS

    psycopg = pytest.importorskip("psycopg")
    with conn.cursor() as cur:
        _insert_revision(cur, job, description="가" * MAX_DESCRIPTION_CHARS)
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        _insert_revision(cur, job, description="가" * (MAX_DESCRIPTION_CHARS + 1))


def test_the_ai_label_cannot_be_changed(conn, job):
    """파생 색인은 **사람 원문이 아니라는 표시**를 달고만 존재한다(원칙 1)."""
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        cur.execute(
            "UPDATE archiver_image_description_revision SET label=%s WHERE job_id=%s",
            ("현장 기록", job),
        )
        _insert_revision(cur, job)


def test_one_revision_per_job(conn, job):
    psycopg = pytest.importorskip("psycopg")
    with conn.cursor() as cur:
        _insert_revision(cur, job)
    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        _insert_revision(cur, job)


def test_a_revision_needs_a_real_job(conn):
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.cursor() as cur:
        _insert_revision(cur, "no-such-job")


# --- 작업 상태 ---------------------------------------------------------------


def test_a_finished_state_requires_a_finish_time(conn):
    psycopg = pytest.importorskip("psycopg")
    approval_id, job_id = _rid(), _rid()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_description_approval
            (id,workspace,scope,expires_at,approved_by)
            VALUES (%s,'tyit','{}'::jsonb, now() + interval '1 hour','tester')""",
            (approval_id,),
        )
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_description_job
            (id,identity_sha256,approval_id,workspace,policy_revision,prompt_revision,
             scope,policy_snapshot,catalog_snapshot,max_output_tokens,state,
             expires_at,dispatched_at)
            VALUES (%s,%s,%s,'tyit','g:l','p1','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
                    256,'described', now() + interval '1 hour', now())""",
            (job_id, "d" * 64, approval_id),
        )


def test_an_identity_can_only_hold_one_job(conn):
    """같은 bytes 를 **승인만 새로 받아** 다시 보낼 수 없다."""
    psycopg = pytest.importorskip("psycopg")
    identity = "e" * 64
    ids = []
    for _ in range(2):
        approval_id = _rid()
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO archiver_image_description_approval
                (id,workspace,scope,expires_at,approved_by)
                VALUES (%s,'tyit','{}'::jsonb, now() + interval '1 hour','tester')""",
                (approval_id,),
            )
        ids.append(approval_id)
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_description_job
            (id,identity_sha256,approval_id,workspace,policy_revision,prompt_revision,
             scope,policy_snapshot,catalog_snapshot,max_output_tokens,state,expires_at)
            VALUES (%s,%s,%s,'tyit','g:l','p1','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
                    256,'reserved', now() + interval '1 hour')""",
            (_rid(), identity, ids[0]),
        )
    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_description_job
            (id,identity_sha256,approval_id,workspace,policy_revision,prompt_revision,
             scope,policy_snapshot,catalog_snapshot,max_output_tokens,state,expires_at)
            VALUES (%s,%s,%s,'tyit','g:l','p1','{}'::jsonb,'{}'::jsonb,'{}'::jsonb,
                    256,'reserved', now() + interval '1 hour')""",
            (_rid(), identity, ids[1]),
        )


def test_a_zero_quota_must_be_marked_exhausted(conn):
    """**0 을 「모름」 으로 흘려보내면 무제한이 된다.**"""
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_model_catalog
            (provider,model,max_sensitivity,quota_state,quota_remaining,updated_by)
            VALUES ('p','m','confidential','available',0,'tester')"""
        )


def test_available_quota_needs_a_known_positive_remaining_count(conn):
    psycopg = pytest.importorskip("psycopg")
    name = _rid()
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_model_catalog
            (provider,model,max_sensitivity,quota_state,quota_remaining,updated_by)
            VALUES (%s,%s,'confidential','available',NULL,'tester')""",
            (name, name),
        )


def test_verified_unlimited_quota_is_explicit(conn):
    # 이 행은 **실제로 남는다.** 고정 이름이면 재실행이 PK 충돌로 빨개져,
    # 스키마 결함과 구분되지 않는다.
    name = _rid()
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_model_catalog
            (provider,model,max_sensitivity,quota_state,quota_remaining,updated_by)
            VALUES (%s,%s,'confidential','unlimited',NULL,'tester')""",
            (name, name),
        )


def test_zero_cost_needs_a_verification(conn):
    psycopg = pytest.importorskip("psycopg")
    with pytest.raises(psycopg.errors.CheckViolation), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_model_catalog
            (provider,model,max_sensitivity,zero_cost_verified,updated_by)
            VALUES ('p2','m2','confidential',true,'tester')"""
        )


# --- 권한 -------------------------------------------------------------------


@pytest.mark.parametrize("table", [
    "archiver_image_model_catalog",
    "archiver_image_description_policy",
    "archiver_image_description_policy_audit",
    "archiver_image_description_approval",
    "archiver_image_description_job",
    "archiver_image_description_revision",
])
def test_public_holds_no_privilege(conn, table):
    """Master·수집기가 쓰는 역할이 PUBLIC 을 타고 들어오면 안 된다(원칙 3·4)."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM information_schema.role_table_grants
            WHERE table_name=%s AND grantee='PUBLIC'""",
            (table,),
        )
        assert cur.fetchone()[0] == 0


def test_only_the_vision_role_is_granted(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT DISTINCT grantee FROM information_schema.role_table_grants
            WHERE table_name LIKE 'archiver_image%'"""
        )
        grantees = {row[0] for row in cur.fetchall()}
        cur.execute("SELECT current_user")
        owner = cur.fetchone()[0]
    # 소유자(스키마를 적용한 역할)와 선택적 `tybot_vision` 외에는 없어야 한다.
    assert grantees <= {owner, "tybot_vision"}, grantees


def test_the_vision_role_cannot_delete_a_revision(conn):
    """파생 색인은 **불변**이다. 지우면 무엇을 지웠는지가 안 남는다."""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM information_schema.role_table_grants
            WHERE table_name='archiver_image_description_revision'
              AND grantee='tybot_vision' AND privilege_type IN ('DELETE','UPDATE')"""
        )
        assert cur.fetchone()[0] == 0
