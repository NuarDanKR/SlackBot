from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "deploy/sql/archiver_image_description_schema.sql"


def _sql() -> str:
    return SCHEMA.read_text(encoding="utf-8")


def test_the_schema_is_applied_by_the_canonical_runner() -> None:
    assert "archiver_image_description_schema.sql" in (
        ROOT / "deploy/apply-schema.sh"
    ).read_text(encoding="utf-8")


def test_the_global_policy_starts_off_without_a_model() -> None:
    sql = _sql()
    assert "enabled boolean NOT NULL DEFAULT false" in sql
    assert "VALUES ('', 'initial', 'schema') ON CONFLICT DO NOTHING" in sql
    assert "INSERT INTO archiver_image_model_catalog" not in sql


def test_free_status_needs_a_timestamp_and_revision() -> None:
    sql = _sql()
    assert "NOT zero_cost_verified" in sql
    assert "verified_at IS NOT NULL" in sql
    assert "verification_revision <> ''" in sql
    assert "quota_state = 'available' AND quota_remaining IS NOT NULL" in sql
    assert "AND quota_remaining > 0" in sql
    assert "quota_state IN ('unlimited','unknown') AND quota_remaining IS NULL" in sql


def test_jobs_keep_policy_and_catalog_snapshots_separate() -> None:
    sql = _sql()
    assert "policy_snapshot jsonb NOT NULL" in sql
    assert "catalog_snapshot jsonb NOT NULL" in sql
    assert "prompt_revision text NOT NULL" in sql
    assert "identity_sha256 text NOT NULL UNIQUE" in sql


def test_clearance_is_a_separate_immutable_scope_bound_ledger() -> None:
    sql = _sql()
    table = sql[
        sql.index("CREATE TABLE IF NOT EXISTS archiver_image_description_clearance") :
        sql.index("CREATE TABLE IF NOT EXISTS archiver_image_description_job")
    ]
    for field in (
        "workspace text NOT NULL",
        "channel_id text NOT NULL",
        "file_id text NOT NULL",
        "original_sha256 text NOT NULL",
        "provider text NOT NULL",
        "model text NOT NULL",
        "policy_revision text NOT NULL",
        "visibility text NOT NULL",
        "acl text[] NOT NULL",
        "pii_cleared boolean NOT NULL DEFAULT false",
        "assessed_by text NOT NULL",
        "expires_at timestamptz NOT NULL",
        "reason text NOT NULL",
    ):
        assert field in table
    assert "CHECK (expires_at > assessed_at)" in table
    assert "revoked_by IS NOT NULL AND revoked_by <> ''" in table
    assert "revoke_reason IS NOT NULL AND revoke_reason <> ''" in table
    assert "revoked_at IS NULL OR revoked_at >= assessed_at" in table
    assert "archiver_image_description_approval" not in table
    grants = sql[sql.index("DO $$ BEGIN") :]
    assert "SELECT, INSERT ON TABLE archiver_image_description_clearance" in grants
    assert "UPDATE (revoked_at, revoked_by, revoke_reason)" in grants
    assert "SELECT, INSERT, UPDATE ON TABLE archiver_image_description_clearance" not in grants


def test_transmission_approval_records_reasons_and_only_allows_revocation_updates() -> None:
    sql = _sql()
    table = sql[
        sql.index("CREATE TABLE IF NOT EXISTS archiver_image_description_approval") :
        sql.index("CREATE TABLE IF NOT EXISTS archiver_image_description_clearance")
    ]
    assert "reason text NOT NULL" in table
    assert "revoke_reason text" in table
    assert "image_description_approval_revoke_consistent" in table
    grants = sql[sql.index("DO $$ BEGIN") :]
    assert "SELECT, INSERT ON TABLE archiver_image_description_approval" in grants
    assert "REVOKE UPDATE ON TABLE archiver_image_description_approval" in grants
    assert "UPDATE (revoked_at, revoked_by, revoke_reason)" in grants
    assert "archiver_image_description_policy, archiver_image_description_approval" not in grants


def test_derived_descriptions_are_labeled_bounded_and_immutable_to_the_role() -> None:
    sql = _sql()
    assert "[AI 이미지 설명·검색용]" in sql
    assert "char_length(description) <= 200" in sql
    assert "guard_verdict IN ('allow','withhold_description')" in sql
    assert "guard_codes text[] NOT NULL" in sql
    assert "guard_verdict = 'withhold_description' AND description = ''" in sql
    grants = sql[sql.index("DO $$ BEGIN") :]
    assert "archiver_image_description_revision TO tybot_vision" in grants
    assert "UPDATE ON TABLE archiver_image_description_policy_audit" not in grants
    assert "UPDATE ON TABLE archiver_image_description_revision" not in grants


def test_master_collector_and_public_receive_no_access() -> None:
    sql = _sql()
    assert "REVOKE ALL ON archiver_image_model_catalog" in sql
    assert "archiver_image_description_clearance" in sql
    assert "FROM PUBLIC" in sql
    assert "TO tyslackai" not in sql
    assert "TO tybot_archiver" not in sql
    assert "CREATE ROLE" not in sql
