"""B-12 writer checks that require a real, disposable PostgreSQL database.

Set ``TEST_DATABASE_URL`` to a dedicated database whose name is explicitly
marked as a test database.  The suite refuses operational and measurement DBs.
"""
from __future__ import annotations

import hashlib
import os
import sys
import uuid
from pathlib import Path

import pytest

from tybot.archive.chunking import Chunk
from tybot.embedding_index_store import DocumentKey, EmbeddingIndexWriter

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_schema_isolated as verify  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN,
    reason=(
        "TEST_DATABASE_URL is absent; pgvector CHECK, trigger, rollback, and "
        "atomic publication are not verified in this session"
    ),
)


@pytest.fixture(scope="module")
def conn():
    refusal = verify.operational_db_refusal(DSN)
    if refusal:
        pytest.fail(f"refusing a non-isolated TEST_DATABASE_URL: {refusal}")
    psycopg = pytest.importorskip("psycopg")
    connection = psycopg.connect(DSN, autocommit=True)
    try:
        with connection.cursor() as cur:
            cur.execute(
                (ROOT / "deploy/sql/search_embedding_schema.sql").read_text(
                    encoding="utf-8"
                )
            )
        yield connection
    finally:
        connection.close()


@pytest.fixture
def model(conn):
    revision = "test-" + uuid.uuid4().hex
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO search_embedding_model_revision
                   (revision,provider,model,dimensions,state,created_by)
               VALUES (%s,'local-test','synthetic',2,'active','pytest')""",
            (revision,),
        )
    return revision


def _chunk(text: str, start: int = 0) -> Chunk:
    return Chunk(
        text=text,
        start=start,
        end=start + len(text),
        kind="text",
        line_start=1,
        line_end=1,
    )


def _key(text: str, model: str, *, doc: str | None = None) -> DocumentKey:
    return DocumentKey(
        workspace="test-ws",
        channel_id="C-TEST",
        doc_path=doc or f"synthetic/{uuid.uuid4().hex}.md",
        source_revision=hashlib.sha256(text.encode()).hexdigest(),
        chunker_revision="chunk-v1:max=1200:ov=120",
        model_revision=model,
    )


def test_writer_publishes_only_a_complete_current_revision(conn, model):
    result = EmbeddingIndexWriter(conn).publish(
        _key("alpha", model), "alpha", [_chunk("alpha")], [[0.1, 0.2]]
    )

    with conn.cursor() as cur:
        cur.execute(
            """SELECT state,is_current,chunk_count,indexed_at IS NOT NULL
                 FROM archive_search_document_revision WHERE id=%s""",
            (result.document_revision_id,),
        )
        assert cur.fetchone() == ("ready", True, 1, True)
        cur.execute(
            """SELECT start_offset,end_offset,text_sha256,vector_dims(embedding)
                 FROM archive_search_chunk WHERE document_revision_id=%s""",
            (result.document_revision_id,),
        )
        assert cur.fetchone() == (0, 5, hashlib.sha256(b"alpha").hexdigest(), 2)


def test_a_failed_chunk_insert_rolls_back_the_manifest(conn, model):
    psycopg = pytest.importorskip("psycopg")
    key = _key("alpha", model)
    with pytest.raises(psycopg.errors.UniqueViolation):
        EmbeddingIndexWriter(conn).publish(
            key,
            "alpha",
            [_chunk("alpha"), _chunk("alpha")],
            [[0.1, 0.2], [0.3, 0.4]],
        )

    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM archive_search_document_revision
                WHERE workspace=%s AND channel_id=%s AND doc_path=%s
                  AND source_revision=%s AND chunker_revision=%s AND model_revision=%s""",
            (
                key.workspace,
                key.channel_id,
                key.doc_path,
                key.source_revision,
                key.chunker_revision,
                key.model_revision,
            ),
        )
        assert cur.fetchone()[0] == 0


def test_publishing_a_new_source_retires_the_previous_current(conn, model):
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    writer = EmbeddingIndexWriter(conn)
    old = writer.publish(_key("old", model, doc=doc), "old", [_chunk("old")], [[0.1, 0.2]])
    new = writer.publish(_key("new", model, doc=doc), "new", [_chunk("new")], [[0.2, 0.3]])

    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,state,is_current FROM archive_search_document_revision
                WHERE id=ANY(%s) ORDER BY id""",
            ([old.document_revision_id, new.document_revision_id],),
        )
        assert cur.fetchall() == [
            (old.document_revision_id, "retired", False),
            (new.document_revision_id, "ready", True),
        ]


def test_the_index_tables_have_no_source_body_column(conn):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT column_name FROM information_schema.columns
                WHERE table_name IN
                  ('archive_search_document_revision','archive_search_chunk')"""
        )
        columns = {row[0] for row in cur.fetchall()}
    assert not {"body", "source_text", "question", "answer"} & columns
