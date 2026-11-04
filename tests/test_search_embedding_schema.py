from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "deploy/sql/search_embedding_schema.sql"


def sql() -> str:
    return SCHEMA.read_text(encoding="utf-8")


def test_schema_is_applied_immediately_after_the_base_index_schema():
    apply = (ROOT / "deploy/apply-schema.sh").read_text(encoding="utf-8")
    listed = []
    inside = False
    for raw in apply.splitlines():
        if not inside:
            inside = raw.strip().startswith("FILES=(")
            continue
        line = raw.split("#", 1)[0].strip()
        if line == ")":
            break
        if line:
            listed.append(line)
    assert listed.index("index_schema.sql") < listed.index("search_embedding_schema.sql")
    assert listed.index("search_embedding_schema.sql") < listed.index("console_schema.sql")


def test_pgvector_and_all_revision_tables_are_declared():
    body = sql()
    assert "CREATE EXTENSION IF NOT EXISTS vector" in body
    for table in (
        "search_embedding_model_revision",
        "archive_search_document_revision",
        "archive_search_chunk",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in body


def test_chunks_store_coordinates_hash_and_vector_but_not_source_text():
    body = sql()
    chunk = body[body.index("CREATE TABLE IF NOT EXISTS archive_search_chunk") :]
    chunk = chunk[: chunk.index(");")]
    for field in ("start_offset", "end_offset", "text_sha256", "embedding"):
        assert field in chunk
    for forbidden in (" body ", " question ", " answer ", " source_text "):
        assert forbidden not in chunk.lower()


def test_only_ready_current_document_revisions_can_be_current():
    body = sql()
    assert "NOT is_current OR (state = 'ready' AND indexed_at IS NOT NULL)" in body
    assert "archive_search_document_one_current" in body
    assert "WHERE is_current" in body


def test_model_chunker_and_source_revisions_are_all_bound():
    body = sql()
    for field in ("source_revision", "chunker_revision", "model_revision"):
        assert field in body
    assert "REFERENCES search_embedding_model_revision(revision)" in body
    assert "A-Za-z0-9._:=-" in body


def test_database_rejects_wrong_dimensions_and_incomplete_current_revisions():
    body = sql()
    assert "vector_dims(NEW.embedding) <> expected_dimensions" in body
    assert "embedding_dimension_mismatch" in body
    assert "actual_chunks <> NEW.chunk_count" in body
    assert "embedding_chunk_count_mismatch" in body
    assert "BEFORE INSERT OR UPDATE OF document_revision_id, embedding" in body
    assert "BEFORE INSERT OR UPDATE OF is_current, state, chunk_count, indexed_at" in body


def test_public_has_no_access_and_only_the_existing_app_role_is_granted_access():
    body = sql()
    for table in (
        "search_embedding_model_revision",
        "archive_search_document_revision",
        "archive_search_chunk",
    ):
        assert f"REVOKE ALL ON TABLE {table} FROM PUBLIC" in body
    assert "TO tyslackai" in body
    assert "TO tybot_archiver" not in body


def test_schema_does_not_create_an_ann_index_before_a_model_dimension_is_chosen():
    body = sql().lower()
    assert "using hnsw" not in body
    assert "using ivfflat" not in body
