"""B-81 전송 실패(영수증 없음)를 **진짜 PostgreSQL 에서** 끝까지 건다 (f86542f 독립 검증).

`tests/test_image_description_store.py` 는 SQL 을 실행하지 않는 대역이라, 다음은 그쪽에서
답할 수 없다 — 실제 행이 어떻게 남는가, 스키마 CHECK 가 그 행을 받는가, 같은 원본을 다시
예약하면 DB 가 막는가.

거는 것(timeout·429 처럼 provider 영수증이 없는 호출 실패):
1. `dispatching` 작업이 `failed` 로 닫힌다(`finished_at` 포함)
2. `actual_model`·`actual_provider` 는 NULL — 영수증 없이 「실제로 그 모델이 답했다」 고 적지 않는다
3. 설명·전사 revision 0건, `image_search_entry` 0건
4. 무료 카탈로그가 자동으로 막힌다(`zero_cost_verified=false`·`quota_state='unknown'`) —
   비용을 모르는 호출 뒤에 다음 사진을 또 보내면 「무료라서 켠 기능」 이 과금될 수 있다
5. 같은 원본은 다시 예약되지 않는다 — 사람이 카탈로그를 다시 검증한 뒤에도 `duplicate_job`
   (한 번 나간 호출을 재청구하지 않는다)

실행(전용 격리 DB 만):
    TEST_DATABASE_URL=postgresql://…/tybot_schema_test python -m pytest \\
        tests/test_image_description_transport_failure_db.py

**운영 DB 를 주지 마십시오.** 시작할 때 B-81 표를 비운다(TRUNCATE). 이름 검사로 운영 DB 는
코드가 거부한다(`operational_db_refusal`).
"""
from __future__ import annotations

import os
import pathlib
from datetime import UTC, datetime, timedelta

import pytest

from tybot.archive.image_description import (
    ERROR_MODEL_ERROR,
    ERROR_MODEL_TIMEOUT,
    PROMPT_REVISION,
    STATUS_FAILED,
    ImageDescription,
    ImageInput,
    ImageUsage,
)
from tybot.archive.image_description_policy import ImageDescriptionApproval
from tybot.archive.image_description_store import ImageDescriptionDenied, ImageDescriptionStore
from tybot.archive.image_model_catalog import FreeQuotaState, ImageModelCatalogEntry
from tybot.gateway.base import Sensitivity

DSN = os.environ.get("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN,
    reason=(
        "TEST_DATABASE_URL 이 없어 실제 DB 검증을 건너뜁니다. 대역 시험은 SQL 을 실행하지 "
        "않으므로 이 세션에서 행·CHECK·UNIQUE 는 검증되지 않았습니다."
    ),
)

SQL = pathlib.Path(__file__).resolve().parents[1] / "deploy" / "sql"
SCHEMAS = ("archiver_image_description_schema.sql", "archiver_image_transcript_schema.sql")
TABLES = (
    "image_search_entry",
    "archiver_image_transcript_revision",
    "archiver_image_description_revision",
    "archiver_image_description_job",
    "archiver_image_description_clearance",
    "archiver_image_description_approval",
    "archiver_image_description_policy_audit",
    "archiver_image_description_policy",
    "archiver_image_model_catalog",
)

MODEL, PROVIDER, REGION = "free-vision-v1", "approved-provider", "asia-northeast3"
WS, CH, FILE = "tyit", "C12345678", "F12345678"
CHECKS = {
    "source_verified": True,
    "access_verified": True,
    "pii_blocked": False,
    "bot_generated": False,
}


def _refuse_operational_db() -> None:
    """운영 DB 실행을 **코드로** 막는다(`verify_schema_isolated.operational_db_refusal`).

    이 파일은 B-81 표를 TRUNCATE 한다. 이름에 schema_test·schema_check·scratch 가 없거나,
    설정 파일·DATABASE_URL 이 가리키는 DB 면 시작하지 않는다.
    """
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
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
            for name in SCHEMAS:
                cur.execute((SQL / name).read_text(encoding="utf-8"))
            cur.execute(f"TRUNCATE {', '.join(TABLES)} CASCADE")
            # TRUNCATE 가 스키마의 전역 정책 씨앗 행('', 'initial')까지 지운다 — 다시 심는다.
            for name in SCHEMAS:
                cur.execute((SQL / name).read_text(encoding="utf-8"))
        yield connection
    finally:
        connection.close()


def _catalog(*, verified_at: datetime, revision: str = "v1") -> ImageModelCatalogEntry:
    return ImageModelCatalogEntry(
        model=MODEL,
        provider=PROVIDER,
        supports_vision=True,
        zero_cost_verified=True,
        verified_at=verified_at,
        verification_revision=revision,
        max_sensitivity=Sensitivity.PUBLIC,
        allowed_regions=frozenset({REGION}),
        credential_ready=True,
        quota_state=FreeQuotaState.AVAILABLE,
        quota_remaining=100,
    )


def _image(original: str = "a" * 64, payload: bytes = b"\x89PNG-test-image") -> ImageInput:
    return ImageInput(WS, CH, FILE, original, payload, "image/png")


def _ready(conn) -> tuple[ImageDescriptionStore, str]:
    """정책(global+workspace)·카탈로그·승인까지. 정책 revision 을 돌려준다."""
    store = ImageDescriptionStore(conn)
    now = datetime.now(UTC)
    global_rev = store.save_policy(
        "",
        expected_revision="initial",
        enabled=True,
        config={
            "model": MODEL,
            "provider": PROVIDER,
            "region": REGION,
            "sensitivity": "public",
            "catalog_verification_ttl_seconds": 86_400,
            "max_image_bytes": 5_000_000,
            "max_output_tokens": 256,
            "timeout_seconds": 30,
            "max_concurrency": 2,
            "daily_calls": 10,
        },
        actor="tester",
    )
    local_rev = store.save_policy(
        WS,
        expected_revision=None,
        enabled=True,
        config={"max_concurrency": 2, "daily_calls": 10},
        actor="tester",
    )
    store.save_catalog(
        _catalog(verified_at=now - timedelta(minutes=1)), expected_revision=None, actor="tester"
    )
    return store, f"{global_rev}:{local_rev}"


def _approve(store, policy_revision: str, *, approval_id: str, original: str) -> None:
    store.approve(
        ImageDescriptionApproval(
            approval_id=approval_id,
            workspace=WS,
            channel=CH,
            file_id=FILE,
            original_sha256=original,
            policy_revision=policy_revision,
            prompt_revision=PROMPT_REVISION,
            model=MODEL,
            provider=PROVIDER,
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ),
        actor="tester",
        reason="test",
    )


def _reserve(store, image, approval_id):
    return store.reserve(
        image,
        approval_id=approval_id,
        prompt_revision=PROMPT_REVISION,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
        checks=CHECKS,
    )


def _transport_failure(image: ImageInput, error_code: str) -> ImageDescription:
    """`describe_image` 가 timeout·429 에서 돌려주는 모양 — provider 영수증이 없다."""
    return ImageDescription(
        status=STATUS_FAILED,
        workspace=image.workspace,
        channel=image.channel,
        file_id=image.file_id,
        original_sha256=image.original_sha256,
        prompt_revision=PROMPT_REVISION,
        description="",
        tags=(),
        error_code=error_code,
        usage=ImageUsage(model=MODEL, elapsed_ms=30_000),
    )


def _one(conn, sql: str, args=()):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()


@pytest.mark.parametrize("error_code", [ERROR_MODEL_TIMEOUT, ERROR_MODEL_ERROR])
def test_a_transport_failure_closes_the_job_and_publishes_nothing(conn, error_code):
    store, revision = _ready(conn)
    image = _image()
    _approve(store, revision, approval_id="approval-1", original=image.original_sha256)
    receipt = _reserve(store, image, "approval-1")
    grant = store.begin_dispatch(receipt.dispatch_id, image, checks=CHECKS)
    # 정책 민감도가 계약 값으로 provider 경계까지 간다(f86542f + 2026-11-19 오너 결정).
    assert grant.sensitivity is Sensitivity.INTERNAL  # public 정책 → 계약 값(일반 업무)

    assert store.finish_and_publish(receipt.dispatch_id, _transport_failure(image, error_code)) == ""

    job = _one(conn, "SELECT * FROM archiver_image_description_job WHERE id=%s", (receipt.dispatch_id,))
    # 1. dispatching → failed, 끝난 시각이 있다
    assert job["state"] == "failed" and job["finished_at"] is not None
    assert job["error_code"] == f"{error_code},cost_unverified"
    # 2. 영수증이 없으면 실제 모델·provider 를 적지 않는다
    assert job["actual_model"] is None and job["actual_provider"] is None
    assert job["actual_usd"] is None
    assert job["input_tokens"] is None and job["output_tokens"] is None
    # 3. 파생물 0건
    for table in (
        "archiver_image_description_revision",
        "archiver_image_transcript_revision",
        "image_search_entry",
    ):
        assert _one(conn, f"SELECT count(*) AS n FROM {table}")["n"] == 0, table
    # 4. 무료 카탈로그 자동 차단
    catalog = _one(
        conn,
        "SELECT * FROM archiver_image_model_catalog WHERE provider=%s AND model=%s",
        (PROVIDER, MODEL),
    )
    assert catalog["zero_cost_verified"] is False
    assert catalog["quota_state"] == "unknown" and catalog["quota_remaining"] is None
    assert catalog["updated_by"] == "runtime-cost-guard"


def test_the_blocked_catalog_stops_the_next_image_before_any_job(conn):
    store, revision = _ready(conn)
    first = _image()
    _approve(store, revision, approval_id="approval-1", original=first.original_sha256)
    receipt = _reserve(store, first, "approval-1")
    store.begin_dispatch(receipt.dispatch_id, first, checks=CHECKS)
    store.finish_and_publish(receipt.dispatch_id, _transport_failure(first, ERROR_MODEL_TIMEOUT))

    second = _image(original="c" * 64, payload=b"\x89PNG-another-image")
    _approve(store, revision, approval_id="approval-2", original=second.original_sha256)
    with pytest.raises(ImageDescriptionDenied) as denied:
        _reserve(store, second, "approval-2")
    assert denied.value.args[0] in {"zero_cost_not_verified", "quota_unknown"}
    assert _one(conn, "SELECT count(*) AS n FROM archiver_image_description_job")["n"] == 1


def test_the_same_original_is_never_reserved_again_even_after_reverification(conn):
    store, revision = _ready(conn)
    image = _image()
    _approve(store, revision, approval_id="approval-1", original=image.original_sha256)
    receipt = _reserve(store, image, "approval-1")
    store.begin_dispatch(receipt.dispatch_id, image, checks=CHECKS)
    store.finish_and_publish(receipt.dispatch_id, _transport_failure(image, ERROR_MODEL_ERROR))

    # 차단된 카탈로그에서는 같은 원본도 막힌다.
    with pytest.raises(ImageDescriptionDenied):
        _reserve(store, image, "approval-1")

    # 사람이 카탈로그를 다시 검증해도 **같은 원본**은 다시 나가지 않는다(재청구 금지).
    store.save_catalog(
        _catalog(verified_at=datetime.now(UTC) - timedelta(seconds=5), revision="v2"),
        expected_revision="v1",
        actor="reviewer",
    )
    with pytest.raises(ImageDescriptionDenied) as denied:
        _reserve(store, image, "approval-1")
    assert denied.value.args[0] == "duplicate_job"
    assert _one(conn, "SELECT count(*) AS n FROM archiver_image_description_job")["n"] == 1


def test_the_schema_rejects_a_second_job_for_the_same_identity(conn):
    """코드 검사를 건너뛰어도 DB 가 막는다(UNIQUE identity_sha256)."""
    psycopg = pytest.importorskip("psycopg")
    store, revision = _ready(conn)
    image = _image()
    _approve(store, revision, approval_id="approval-1", original=image.original_sha256)
    receipt = _reserve(store, image, "approval-1")
    job = _one(conn, "SELECT * FROM archiver_image_description_job WHERE id=%s", (receipt.dispatch_id,))
    with pytest.raises(psycopg.errors.UniqueViolation), conn.cursor() as cur:
        cur.execute(
            """INSERT INTO archiver_image_description_job
            (id,identity_sha256,approval_id,workspace,policy_revision,prompt_revision,
             scope,policy_snapshot,catalog_snapshot,max_output_tokens,state,expires_at)
            VALUES ('dup',%s,%s,%s,%s,%s,'{}'::jsonb,'{}'::jsonb,'{}'::jsonb,256,'reserved',
                    now() + interval '10 minutes')""",
            (
                job["identity_sha256"], job["approval_id"], WS,
                job["policy_revision"], job["prompt_revision"],
            ),
        )
