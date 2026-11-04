"""B-12 writer checks that require a real, disposable PostgreSQL database.

Set ``TEST_DATABASE_URL`` to a dedicated database whose name is explicitly
marked as a test database.  The suite refuses operational and measurement DBs.
"""
from __future__ import annotations

import hashlib
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

from tybot.archive.chunking import Chunk
from tybot.embedding_index_store import (
    DocumentKey,
    EmbeddingIndexWriter,
    IndexPublishError,
)
from tybot.embedding_search import SearchScope, candidates

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_schema_isolated as verify  # noqa: E402

DSN = os.environ.get("TEST_DATABASE_URL", "").strip()
pytestmark = pytest.mark.skipif(
    not DSN,
    reason=(
        "TEST_DATABASE_URL is absent; the pgvector dimension trigger, the "
        "incomplete-revision guard, idempotent republication, the one-current "
        "index under concurrency, mid-publication rollback, uninterrupted "
        "current coverage, and cross-workspace candidate isolation are all "
        "UNVERIFIED in this session"
    ),
)


def _unreachable_schema_report(connection) -> str | None:
    """Report why ``CREATE TABLE`` has nowhere to land, before applying the schema.

    PostgreSQL drops a schema from ``search_path`` silently when the role has no
    ``USAGE`` on it, so a missing schema and a missing grant surface as the same
    ``InvalidSchemaName: no schema has been selected to create in`` -- raised in
    the middle of a 167-line schema file, naming neither the database, the role,
    nor the path.  Say all three instead.

    The recovery SQL names the role through ``quote_ident`` so that it stays
    valid for a role whose name is mixed case or carries punctuation -- a bare
    ``GRANT ... TO My-Role`` is a syntax error, and the person running it would
    read that as a second, unrelated fault.
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT current_user, quote_ident(current_user), current_database(), "
            "current_setting('search_path')"
        )
        user, quoted_user, database, path = cur.fetchone()
        cur.execute(
            "SELECT n, has_schema_privilege(n, 'CREATE')"
            "  FROM unnest(current_schemas(true)) AS n"
        )
        reachable = cur.fetchall()
        cur.execute(
            "SELECT nspname FROM pg_namespace"
            " WHERE nspname !~ '^pg_' AND nspname <> 'information_schema'"
            " ORDER BY nspname"
        )
        present = [row[0] for row in cur.fetchall()]
    if any(may_create for _, may_create in reachable):
        return None
    return (
        f"role '{user}' cannot CREATE in any schema on the search_path of "
        f"database '{database}', so deploy/sql/search_embedding_schema.sql "
        "has nowhere to land.\n"
        f"  search_path       = {path}\n"
        f"  reachable schemas = {[name for name, _ in reachable]}\n"
        f"  schemas present   = {present}\n"
        "A schema the role cannot USAGE is removed from search_path silently, "
        "so an absent schema and an absent grant raise the same error. "
        "On the disposable test database only:\n"
        "  CREATE SCHEMA IF NOT EXISTS public;\n"
        f"  GRANT USAGE, CREATE ON SCHEMA public TO {quoted_user};\n"
        f"  -- or: ALTER SCHEMA public OWNER TO {quoted_user};"
    )


@pytest.fixture(scope="module")
def conn():
    refusal = verify.operational_db_refusal(DSN)
    if refusal:
        pytest.fail(f"refusing a non-isolated TEST_DATABASE_URL: {refusal}")
    psycopg = pytest.importorskip("psycopg")
    connection = psycopg.connect(DSN, autocommit=True)
    try:
        unreachable = _unreachable_schema_report(connection)
        if unreachable:
            pytest.fail(unreachable)
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
    result = _publish(EmbeddingIndexWriter(conn), conn,
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
        _publish(EmbeddingIndexWriter(conn), conn,
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
    old = _publish(writer, conn, _key("old", model, doc=doc), "old", [_chunk("old")], [[0.1, 0.2]])
    new = _publish(writer, conn, _key("new", model, doc=doc), "new", [_chunk("new")], [[0.2, 0.3]])

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


# ---------------------------------------------------------------------------
# 실제 PostgreSQL 에서만 알 수 있는 것들
#
# 아래는 전부 **DB 가 지키는 약속**이다. 파이썬 쪽 `EmbeddingIndexWriter` 가
# 같은 것을 한 번 더 보지만, 그 검사만 믿으면 다른 경로(손으로 고치는 SQL,
# 앞으로 생길 재색인 잡, 동시에 도는 두 프로세스)가 표를 깨뜨릴 수 있다.
# 그래서 **writer 를 건너뛰고** 표에 직접 쓰는 시험이 섞여 있다.
# ---------------------------------------------------------------------------


def _versions(connection) -> dict[str, str]:
    with connection.cursor() as cur:
        cur.execute("SELECT version()")
        server = str(cur.fetchone()[0])
        cur.execute("SELECT extversion FROM pg_extension WHERE extname='vector'")
        row = cur.fetchone()
    return {"postgresql": server, "pgvector": str(row[0]) if row else ""}


@contextmanager
def _second_connection():
    """같은 DB 로 가는 **별도 연결.** 동시성은 연결이 둘이어야 보인다."""
    import psycopg

    connection = psycopg.connect(DSN, autocommit=True)
    try:
        yield connection
    finally:
        connection.close()


@contextmanager
def _injected_trigger(connection, name: str, body: str, args: tuple[str, ...]):
    """시험 동안만 다는 고장 주입 트리거.

    중간 실패를 바깥에서 만들 방법이 없어서 DB 안에서 만든다. 이름을 `zz_` 로
    시작하는 이유는 PostgreSQL 이 같은 시점의 트리거를 **이름 순서로** 돌리기
    때문이다 — 스키마의 검사 트리거가 먼저 돌고 그 다음에 이것이 돈다.

    들어갈 때 **먼저 지우고** 만든다. 앞선 실행이 `finally` 를 못 돌린 채 끊기면
    (Ctrl-C · 연결 끊김 · 프로세스 종료) 같은 이름의 트리거가 남고, 다음 실행은
    `CREATE TRIGGER` 에서 `DuplicateObject` 로 죽는다. 그때 화면에 보이는 것은
    「이미 있다」 뿐이라, 남아 있는 트리거가 **이 표의 모든 UPDATE 에 고장을 섞고
    있다**는 사실은 안 보인다 — 그 상태에서는 다른 시험들도 같이 틀어진다.
    """
    function = f"zz_pytest_{name}"
    # 트리거 인자는 `CREATE TRIGGER` 안이라 바인딩이 안 되고 리터럴로 들어간다.
    # 값은 우리가 만든 uuid 경로뿐이지만, 그 전제를 **여기서 지킨다.**
    assert all(arg.isascii() and "'" not in arg and "\\" not in arg for arg in args)
    placeholders = ", ".join(f"'{arg}'" for arg in args)
    with connection.cursor() as cur:
        cur.execute(
            f"DROP TRIGGER IF EXISTS {function} ON archive_search_document_revision"
        )
        cur.execute(
            f"CREATE OR REPLACE FUNCTION {function}() RETURNS trigger "
            f"LANGUAGE plpgsql AS $pytest$ BEGIN {body} RETURN NEW; END $pytest$"
        )
        cur.execute(
            f"CREATE TRIGGER {function} BEFORE UPDATE ON "
            f"archive_search_document_revision FOR EACH ROW "
            f"EXECUTE FUNCTION {function}({placeholders})"
        )
    try:
        yield
    finally:
        with connection.cursor() as cur:
            cur.execute(
                f"DROP TRIGGER IF EXISTS {function} "
                "ON archive_search_document_revision"
            )
            cur.execute(f"DROP FUNCTION IF EXISTS {function}()")


def _trigger_exists(connection, name: str) -> bool:
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid"
            " WHERE NOT t.tgisinternal AND t.tgname=%s"
            "   AND c.relname='archive_search_document_revision'",
            (name,),
        )
        return int(cur.fetchone()[0]) > 0


def _lock_pair() -> tuple[int, int]:
    """`pg_advisory_lock(int4,int4)` 용 키 한 쌍. 다른 실행과 안 겹치게 무작위."""
    raw = uuid.uuid4().int
    return (raw % 2_000_000_000 + 1, (raw >> 64) % 2_000_000_000 + 1)


def _advisory_waiters(connection, classid: int, objid: int) -> int:
    """그 키를 **기다리고 있는** 백엔드 수.

    `pg_locks` 는 커밋과 무관하게 지금 보인다 — 다른 연결의 트랜잭션 안을
    들여다볼 수 있는 몇 안 되는 창이다. 두 정수형 advisory lock 은
    `objsubid=2` 로 들어간다(bigint 한 개짜리는 1).
    """
    with connection.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM pg_locks WHERE locktype='advisory'"
            " AND classid=%s AND objid=%s AND objsubid=2 AND NOT granted",
            (classid, objid),
        )
        return int(cur.fetchone()[0])


_SNAPSHOT = object()


def _publish(writer, connection, key, text, chunks, vectors, *, expected=_SNAPSHOT):
    """올바른 호출자와 같은 모양으로 발행한다.

    writer 는 이제 **무엇을 대체하는지**를 받아야 발행한다. 기본값은
    지금 현역인 판을 읽어 넣는다 — 색인 잡이 시작할 때 보는 것과 같다.
    거절을 보려는 시험만 `expected` 를 명시한다.
    """
    if expected is _SNAPSHOT:
        found = _current_ids(connection, key)
        expected = found[0] if found else None
    return _publish(writer, conn, key, text, chunks, vectors, expected_current=expected)


def _manifest(connection, key: DocumentKey, *, state: str = "building") -> int:
    """writer 를 거치지 않고 매니페스트 한 줄을 만든다."""
    with connection.cursor() as cur:
        cur.execute(
            """INSERT INTO archive_search_document_revision
                   (workspace,channel_id,doc_path,source_revision,
                    chunker_revision,model_revision,state)
               VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
            (
                key.workspace,
                key.channel_id,
                key.doc_path,
                key.source_revision,
                key.chunker_revision,
                key.model_revision,
                state,
            ),
        )
        return int(cur.fetchone()[0])


def _vector(dimensions: int) -> str:
    return "[" + ",".join("0.1" for _ in range(dimensions)) + "]"


def _insert_chunk(connection, document_id: int, dimensions: int, chunk_no: int = 0):
    with connection.cursor() as cur:
        cur.execute(
            """INSERT INTO archive_search_chunk
                   (document_revision_id,chunk_no,start_offset,end_offset,
                    text_sha256,embedding)
               VALUES (%s,%s,%s,%s,%s,%s::vector)""",
            (
                document_id,
                chunk_no,
                chunk_no * 10,
                chunk_no * 10 + 5,
                hashlib.sha256(f"c{chunk_no}".encode()).hexdigest(),
                _vector(dimensions),
            ),
        )


def _current_ids(connection, key: DocumentKey) -> list[int]:
    with connection.cursor() as cur:
        cur.execute(
            """SELECT id FROM archive_search_document_revision
                WHERE workspace=%s AND channel_id=%s AND doc_path=%s
                  AND model_revision=%s AND is_current ORDER BY id""",
            (key.workspace, key.channel_id, key.doc_path, key.model_revision),
        )
        return [int(row[0]) for row in cur.fetchall()]


def test_the_database_reports_its_version(conn, record_property):
    """인계 문서에 적을 값. 이 시험이 **버전을 기록하는 자리**다."""
    found = _versions(conn)
    record_property("postgresql", found["postgresql"])
    record_property("pgvector", found["pgvector"])
    print(f"\nPostgreSQL: {found['postgresql']}\npgvector:   {found['pgvector']}")

    assert found["pgvector"], "pgvector 확장이 없으면 이 표들은 만들어질 수 없다"


# --- 1. 차원 불일치는 DB 가 막는다 --------------------------------------------


def test_a_wrong_dimension_chunk_is_refused_by_the_trigger(conn, model):
    """**writer 를 건너뛰고** 표에 직접 넣는다. DB 가 스스로 막아야 한다."""
    psycopg = pytest.importorskip("psycopg")
    key = _key("alpha", model)
    document_id = _manifest(conn, key)

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        _insert_chunk(conn, document_id, dimensions=3)  # 모델은 2차원이다

    assert "embedding_dimension_mismatch" in str(caught.value)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM archive_search_chunk WHERE document_revision_id=%s",
            (document_id,),
        )
        assert cur.fetchone()[0] == 0


def test_a_right_dimension_chunk_passes_the_same_trigger(conn, model):
    """막는 쪽만 보면 「전부 막는 트리거」 도 통과한다."""
    key = _key("alpha", model)
    document_id = _manifest(conn, key)

    _insert_chunk(conn, document_id, dimensions=2)

    with conn.cursor() as cur:
        cur.execute(
            """SELECT vector_dims(embedding) FROM archive_search_chunk
                WHERE document_revision_id=%s""",
            (document_id,),
        )
        assert cur.fetchone()[0] == 2


def test_changing_a_chunk_to_a_wrong_dimension_is_refused(conn, model):
    """`UPDATE` 도 같은 문을 지난다. 넣을 때만 보면 고쳐서 넣으면 된다."""
    psycopg = pytest.importorskip("psycopg")
    key = _key("alpha", model)
    document_id = _manifest(conn, key)
    _insert_chunk(conn, document_id, dimensions=2)

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE archive_search_chunk SET embedding=%s::vector
                    WHERE document_revision_id=%s""",
                (_vector(5), document_id),
            )

    assert "embedding_dimension_mismatch" in str(caught.value)


def test_the_writer_refuses_a_wrong_dimension_before_the_database_does(conn, model):
    """문이 둘이다. 파이썬 쪽이 먼저 막고, 못 막으면 DB 가 막는다."""
    with pytest.raises(IndexPublishError) as caught:
        _publish(EmbeddingIndexWriter(conn), conn,
            _key("alpha", model), "alpha", [_chunk("alpha")], [[0.1, 0.2, 0.3]]
        )

    assert "embedding_dimension_mismatch" in str(caught.value)


# --- 2. 미완료 revision 은 current 가 될 수 없다 -------------------------------


def test_a_manifest_without_its_chunks_cannot_become_current(conn, model):
    """「1개라고 적힌」 매니페스트에 chunk 가 없으면 current 가 못 된다."""
    psycopg = pytest.importorskip("psycopg")
    key = _key("alpha", model)
    document_id = _manifest(conn, key)

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE archive_search_document_revision
                      SET state='ready', is_current=true, chunk_count=1,
                          indexed_at=now()
                    WHERE id=%s""",
                (document_id,),
            )

    assert "embedding_chunk_count_mismatch" in str(caught.value)
    assert _current_ids(conn, key) == []


def test_a_building_revision_cannot_be_flagged_current(conn, model):
    psycopg = pytest.importorskip("psycopg")
    key = _key("alpha", model)
    document_id = _manifest(conn, key)

    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE archive_search_document_revision
                      SET is_current=true, indexed_at=now() WHERE id=%s""",
                (document_id,),
            )

    assert _current_ids(conn, key) == []


def test_a_current_revision_must_carry_its_indexed_at(conn, model):
    """`indexed_at` 이 비면 **언제 만든 색인인지 모른다.** 모르면 current 가 아니다."""
    psycopg = pytest.importorskip("psycopg")
    key = _key("alpha", model)
    document_id = _manifest(conn, key)
    _insert_chunk(conn, document_id, dimensions=2)

    with pytest.raises(psycopg.errors.CheckViolation):
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE archive_search_document_revision
                      SET state='ready', is_current=true, chunk_count=1
                    WHERE id=%s""",
                (document_id,),
            )

    assert _current_ids(conn, key) == []


# --- 3. 같은 revision 은 멱등, 좌표가 다르면 거절 -------------------------------


def test_republishing_the_same_revision_changes_nothing(conn, model):
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    key = _key("alpha", model, doc=doc)
    writer = EmbeddingIndexWriter(conn)
    first = _publish(writer, conn, key, "alpha", [_chunk("alpha")], [[0.1, 0.2]])

    second = _publish(writer, conn, key, "alpha", [_chunk("alpha")], [[0.9, 0.8]])

    assert second.document_revision_id == first.document_revision_id
    assert second.already_current is True
    assert second.chunk_count == first.chunk_count
    with conn.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM archive_search_document_revision
                WHERE workspace=%s AND channel_id=%s AND doc_path=%s""",
            (key.workspace, key.channel_id, key.doc_path),
        )
        assert cur.fetchone()[0] == 1
        cur.execute(
            "SELECT count(*) FROM archive_search_chunk WHERE document_revision_id=%s",
            (first.document_revision_id,),
        )
        assert cur.fetchone()[0] == 1
    assert _current_ids(conn, key) == [first.document_revision_id]


def test_the_same_revision_with_other_coordinates_is_refused(conn, model):
    """본문이 같은데 자른 자리가 다르다. 둘 중 무엇이 그 revision 인지 알 수 없다."""
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    key = _key("alphabeta", model, doc=doc)
    writer = EmbeddingIndexWriter(conn)
    first = _publish(writer, conn,
        key, "alphabeta", [_chunk("alphabeta")], [[0.1, 0.2]]
    )

    with pytest.raises(IndexPublishError) as caught:
        _publish(writer, conn,
            key,
            "alphabeta",
            [_chunk("alpha"), _chunk("beta", start=5)],
            [[0.1, 0.2], [0.3, 0.4]],
        )

    assert "embedding_revision_conflict" in str(caught.value)
    with conn.cursor() as cur:
        cur.execute(
            """SELECT chunk_no,start_offset,end_offset FROM archive_search_chunk
                WHERE document_revision_id=%s ORDER BY chunk_no""",
            (first.document_revision_id,),
        )
        assert cur.fetchall() == [(0, 0, 9)]
    assert _current_ids(conn, key) == [first.document_revision_id]


def test_the_same_revision_with_a_shifted_offset_is_refused(conn, model):
    """개수가 같아도 자리가 다르면 거절한다 — 개수만 보면 통과한다."""
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    key = _key("alphabeta", model, doc=doc)
    writer = EmbeddingIndexWriter(conn)
    _publish(writer, conn, key, "alphabeta", [_chunk("alphabeta")], [[0.1, 0.2]])

    with pytest.raises(IndexPublishError) as caught:
        _publish(writer, conn,
            key, "alphabeta", [_chunk("lphabeta", start=1)], [[0.1, 0.2]]
        )

    assert "embedding_revision_conflict" in str(caught.value)


# --- 4. 두 연결이 동시에 발행해도 current 는 하나 -------------------------------


def test_two_connections_publishing_the_same_document_leave_one_current(conn, model):
    """**연결이 둘이어야 보이는 것.** 한 연결 안에서는 락이 공짜로 맞는다."""
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    start = threading.Barrier(2)
    outcome: dict[str, BaseException | int] = {}

    def publish(label: str, text: str) -> None:
        try:
            with _second_connection() as own:
                start.wait(timeout=10)
                result = _publish(EmbeddingIndexWriter(own), own,
                    _key(text, model, doc=doc), text, [_chunk(text)], [[0.1, 0.2]]
                )
            outcome[label] = result.document_revision_id
        except BaseException as exc:
            outcome[label] = exc

    threads = [
        threading.Thread(target=publish, args=("a", "alpha")),
        threading.Thread(target=publish, args=("b", "bravo")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads), "발행이 서로를 막았다"
    key = _key("alpha", model, doc=doc)
    current = _current_ids(conn, key)
    assert len(current) == 1, outcome
    # **CAS 가 생긴 뒤로는 하나만 끝까지 간다.** 둘 다 「현역 없음」 을 보고
    # 출발했으므로, 뒤에 락을 받은 쪽은 자기가 본 것과 다른 현역을 만난다.
    # 전에는 둘 다 성공하고 나중 것이 앞의 것을 은퇴시켰다 — 그게 늦은 색인이
    # 새 색인을 덮던 바로 그 경로다.
    done = [value for value in outcome.values() if isinstance(value, int)]
    refused = [value for value in outcome.values() if isinstance(value, BaseException)]
    assert len(done) == 1, outcome
    assert done == current, outcome
    assert [str(exc) for exc in refused] == ["embedding_source_superseded"], outcome


def test_two_connections_publishing_the_same_revision_make_one_row(conn, model):
    """같은 판을 둘이 동시에 올린다. **둘 다 성공하되 행은 하나**여야 한다.

    여기서 걸리면 재색인 잡을 두 번 돌린 날 같은 문서가 두 벌이 된다.

    CAS 가 이것까지 막으면 안 된다. 뒤에 온 쪽이 본 현역은 분명히 자기가 아는
    것과 다르지만, **그 현역이 바로 자기가 올리려던 판**이다. 쓸 것이 없으므로
    거절할 것도 없다 — 거절하면 같은 원문을 집어 든 두 잡이 평범하게 겹쳤을 뿐인
    날에 오류가 난다.
    """
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    start = threading.Barrier(2)
    outcome: dict[str, object] = {}

    def publish(label: str) -> None:
        try:
            with _second_connection() as own:
                start.wait(timeout=10)
                outcome[label] = _publish(EmbeddingIndexWriter(own), own,
                    _key("alpha", model, doc=doc), "alpha", [_chunk("alpha")], [[0.1, 0.2]]
                )
        except BaseException as exc:
            outcome[label] = exc

    threads = [threading.Thread(target=publish, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(thread.is_alive() for thread in threads)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM archive_search_document_revision WHERE doc_path=%s",
            (doc,),
        )
        assert cur.fetchone()[0] == 1
    assert len(_current_ids(conn, _key("alpha", model, doc=doc))) == 1
    results = [value for value in outcome.values() if not isinstance(value, BaseException)]
    assert len(results) == 2, outcome
    assert len({r.document_revision_id for r in results}) == 1
    # 뒤에 온 쪽은 **이미 서 있는 것을 확인만** 한다.
    assert sum(1 for r in results if r.already_current) == 1


def test_a_second_current_row_cannot_be_forced_in(conn, model):
    """동시성 시험이 운 좋게 통과하는 경우를 막는다 — **색인 자체**를 본다."""
    psycopg = pytest.importorskip("psycopg")
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    writer = EmbeddingIndexWriter(conn)
    old = _publish(writer, conn, _key("old", model, doc=doc), "old", [_chunk("old")], [[0.1, 0.2]])
    _publish(writer, conn, _key("new", model, doc=doc), "new", [_chunk("new")], [[0.2, 0.3]])

    with pytest.raises(psycopg.errors.UniqueViolation):
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE archive_search_document_revision
                      SET state='ready', is_current=true, indexed_at=now()
                    WHERE id=%s""",
                (old.document_revision_id,),
            )

    assert len(_current_ids(conn, _key("old", model, doc=doc))) == 1


# --- 5. 중간에 실패하면 전부 되돌린다 -------------------------------------------


def test_a_failure_after_the_retire_rolls_everything_back(conn, model):
    """은퇴는 됐는데 새것이 못 선 상태로 남으면 **그 문서는 검색에서 사라진다.**

    그 자리를 바깥에서 만들 방법이 없어 DB 안에 고장을 심는다.
    """
    psycopg = pytest.importorskip("psycopg")
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    writer = EmbeddingIndexWriter(conn)
    old = _publish(writer, conn, _key("old", model, doc=doc), "old", [_chunk("old")], [[0.1, 0.2]])

    with _injected_trigger(
        conn,
        "fail_ready",
        "IF NEW.is_current AND NEW.state = 'ready' "
        "AND NEW.doc_path = TG_ARGV[0] THEN "
        "RAISE EXCEPTION 'pytest_injected_failure'; END IF;",
        (doc,),
    ):
        with pytest.raises(psycopg.errors.RaiseException):
            _publish(writer, conn,
                _key("new", model, doc=doc), "new", [_chunk("new")], [[0.2, 0.3]]
            )

    with conn.cursor() as cur:
        cur.execute(
            """SELECT id,state,is_current FROM archive_search_document_revision
                WHERE doc_path=%s ORDER BY id""",
            (doc,),
        )
        rows = cur.fetchall()
    # 옛 판은 그대로 현역이고, 새 판은 **흔적도 없다.**
    assert rows == [(old.document_revision_id, "ready", True)]


def test_a_failed_publication_leaves_no_orphan_chunks(conn, model):
    psycopg = pytest.importorskip("psycopg")
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    key = _key("alpha", model, doc=doc)
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM archive_search_chunk")
        before = int(cur.fetchone()[0])

    with pytest.raises(psycopg.errors.UniqueViolation):
        _publish(EmbeddingIndexWriter(conn), conn,
            key, "alpha", [_chunk("alpha"), _chunk("alpha")], [[0.1, 0.2], [0.3, 0.4]]
        )

    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM archive_search_chunk")
        assert int(cur.fetchone()[0]) == before
    assert _current_ids(conn, key) == []


# --- 6. current 가 끊기는 구간이 없다 -------------------------------------------


#: 발행 스레드가 트리거 안으로 들어오기를 기다리는 상한(초). 넉넉히 둔다 —
#: 이 값이 짧으면 느린 서버에서 「못 들어왔다」 가 아니라 **시험이 틀렸다**고
#: 읽히게 된다.
ENTRY_TIMEOUT = 30.0

#: 멈춰 세운 동안 최소 몇 번은 들여다본다. 한 번만 보고 넘어가면 「끊기는
#: 구간이 없다」 가 아니라 「한 순간 괜찮았다」 밖에 증명하지 못한다.
MIN_SAMPLES = 5


def test_the_old_revision_stays_current_until_the_new_one_commits(conn, model):
    """바깥에서 보면 current 가 **한 번도 비지 않는다.**

    발행 트랜잭션 안에서는 은퇴가 먼저 돌지만, 그건 커밋 전이라 다른 연결에
    보이지 않는다. 그 사실을 **다른 연결로 실제로 들여다보며** 확인한다.

    전에는 은퇴 UPDATE 에서 `pg_sleep` 으로 쉬게 하고 시계를 보며 표본을 떴다.
    그 모양은 **트리거에 들어가기도 전을 들여다봐도 통과한다** — 서버가 느리거나
    연결이 늦으면 0.9초가 전부 「아직 시작도 안 한」 구간일 수 있고, 그러면 옛
    판이 현역인 것은 당연해서 아무것도 증명하지 않는다.

    그래서 시계를 버리고 **락으로 세운다.** 이 연결이 advisory lock 을 먼저
    잡고, 트리거가 같은 키를 잡으려다 **막힌다.** `pg_locks` 에 그 대기가
    보이는 순간이 「발행 스레드가 은퇴 UPDATE 한가운데에 있다」 는 증거다 —
    커밋 전이라 다른 어떤 방법으로도 안 보이는 사실이다. 그 상태에서만 표본을
    뜨고, 풀어 줘야 비로소 커밋된다. 쉬는 시간도, 경주도 없다.
    """
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    writer = EmbeddingIndexWriter(conn)
    old = _publish(writer, conn, _key("old", model, doc=doc), "old", [_chunk("old")], [[0.1, 0.2]])
    key = _key("old", model, doc=doc)
    gate_a, gate_b = _lock_pair()
    seen: list[list[int]] = []
    failure: list[BaseException] = []

    def publish_through_the_gate() -> None:
        try:
            with _second_connection() as own:
                _publish(EmbeddingIndexWriter(own), own,
                    _key("new", model, doc=doc), "new", [_chunk("new")], [[0.2, 0.3]]
                )
        except BaseException as exc:  # 스레드 밖으로 그대로 나른다
            failure.append(exc)

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s,%s)", (gate_a, gate_b))
    entered = False
    try:
        with _injected_trigger(
            conn,
            "gated_retire",
            "IF NEW.state = 'retired' AND NEW.doc_path = TG_ARGV[0] THEN "
            "PERFORM pg_advisory_xact_lock(TG_ARGV[1]::int, TG_ARGV[2]::int); "
            "END IF;",
            (doc, str(gate_a), str(gate_b)),
        ):
            worker = threading.Thread(target=publish_through_the_gate)
            worker.start()

            deadline = time.monotonic() + ENTRY_TIMEOUT
            while time.monotonic() < deadline:
                if failure:
                    break
                if _advisory_waiters(conn, gate_a, gate_b):
                    entered = True
                    break
                time.sleep(0.01)
            assert not failure, failure
            assert entered, (
                "발행 스레드가 은퇴 UPDATE 안에서 막힌 것을 못 봤다 — "
                "들여다본 구간이 트리거 바깥이면 이 시험은 아무것도 증명하지 않는다"
            )

            # 여기서부터 발행 스레드는 **한 발짝도 못 나간다.** 표본은 전부
            # 커밋 전이고, 그 사실이 시계가 아니라 락으로 보장된다.
            for _ in range(MIN_SAMPLES):
                seen.append(_current_ids(conn, key))
            assert _advisory_waiters(conn, gate_a, gate_b), (
                "표본을 뜨는 동안 대기가 풀렸다 — 커밋 전이라는 전제가 깨졌다"
            )

            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_unlock(%s,%s)", (gate_a, gate_b))
            worker.join(timeout=ENTRY_TIMEOUT)
            assert not worker.is_alive(), "발행 스레드가 풀어 준 뒤에도 안 끝났다"
    finally:
        with conn.cursor() as cur:
            # 이미 풀었으면 false 를 돌려줄 뿐이고, 안 풀렸으면 여기서 푼다.
            cur.execute("SELECT pg_advisory_unlock_all()")

    assert not failure, failure
    assert len(seen) == MIN_SAMPLES
    # 커밋 전에는 **옛 판이 계속 현역**이다. 비는 순간도, 둘인 순간도 없다.
    assert all(sample == [old.document_revision_id] for sample in seen), seen
    after = _current_ids(conn, key)
    assert len(after) == 1 and after != [old.document_revision_id]


def test_the_injected_trigger_recovers_from_an_aborted_run(conn, model):
    """앞 실행이 트리거를 남기고 죽어도 **다음 실행이 선다.**

    `finally` 가 못 돈 자리를 손으로 만든다 — 같은 이름의 트리거를 미리 걸어
    두고, 그 위에서 평소처럼 고장을 주입한다. 남은 트리거는 이 표의 **모든**
    UPDATE 에서 터지므로, 안 지우면 이 시험뿐 아니라 뒤따르는 시험도 같이
    무너진다.
    """
    psycopg = pytest.importorskip("psycopg")
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    writer = EmbeddingIndexWriter(conn)
    old = _publish(writer, conn, _key("old", model, doc=doc), "old", [_chunk("old")], [[0.1, 0.2]])

    with conn.cursor() as cur:
        cur.execute(
            "CREATE OR REPLACE FUNCTION zz_pytest_aborted() RETURNS trigger "
            "LANGUAGE plpgsql AS $stale$ BEGIN "
            "RAISE EXCEPTION 'pytest_stale_leftover'; END $stale$"
        )
        cur.execute(
            "CREATE TRIGGER zz_pytest_aborted BEFORE UPDATE ON "
            "archive_search_document_revision FOR EACH ROW "
            "EXECUTE FUNCTION zz_pytest_aborted()"
        )
    try:
        assert _trigger_exists(conn, "zz_pytest_aborted")
        with _injected_trigger(
            conn,
            "aborted",
            "IF NEW.is_current AND NEW.state = 'ready' "
            "AND NEW.doc_path = TG_ARGV[0] THEN "
            "RAISE EXCEPTION 'pytest_injected_failure'; END IF;",
            (doc,),
        ):
            with pytest.raises(psycopg.errors.RaiseException) as caught:
                _publish(writer, conn,
                    _key("new", model, doc=doc), "new", [_chunk("new")], [[0.2, 0.3]]
                )
            # 남은 몸통이 아니라 **이번에 심은 몸통**이 돌았다.
            assert "pytest_injected_failure" in str(caught.value)
            assert "pytest_stale_leftover" not in str(caught.value)
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TRIGGER IF EXISTS zz_pytest_aborted "
                "ON archive_search_document_revision"
            )
            cur.execute("DROP FUNCTION IF EXISTS zz_pytest_aborted()")

    # 나올 때 깨끗하다 — 다음 시험이 쓰는 표에 아무것도 안 남는다.
    assert not _trigger_exists(conn, "zz_pytest_aborted")
    assert _current_ids(conn, _key("old", model, doc=doc)) == [old.document_revision_id]


# --- 7. 조회 범위: 빈 범위와 다른 workspace -------------------------------------


@pytest.fixture
def indexed(conn, model):
    """한 워크스페이스에 후보 하나. 조회 범위 시험의 기준점."""
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    key = _key("alpha", model, doc=doc)
    _publish(EmbeddingIndexWriter(conn), conn, key, "alpha", [_chunk("alpha")], [[0.1, 0.2]])
    return key


#: 조회 상한. 한 DB 를 여러 시험이 나눠 쓰므로 기본값(100)으로는 내가 넣은
#: 문서가 상한 밖으로 밀려 **없는 것처럼 보일 수 있다.**
LIMIT = 500


def _fresh():
    """`candidates()` 는 연결을 **닫는다**(`with conn`). 새 연결을 준다."""
    import psycopg

    return psycopg.connect(DSN, row_factory=psycopg.rows.dict_row)


def test_an_empty_channel_scope_never_reaches_the_database(indexed):
    """채널이 없으면 **질의하지 않는다.** 넓게 물어 놓고 거르지 않는다."""
    opened: list[int] = []

    def connect():
        opened.append(1)
        return _fresh()

    found = candidates(
        [0.1, 0.2],
        scope=SearchScope.checked(indexed.workspace, []),
        model_revision=indexed.model_revision,
        connect=connect,
    )

    assert found == []
    assert opened == []


def test_a_candidate_is_found_inside_its_own_scope(indexed):
    found = candidates(
        [0.1, 0.2],
        scope=SearchScope.checked(indexed.workspace, [indexed.channel_id]),
        model_revision=indexed.model_revision,
        limit=LIMIT,
        connect=_fresh,
    )

    assert found is not None
    # 이 시험이 쓰는 DB 에는 다른 시험이 만든 문서도 있다. **내 것이 있는가**를
    # 보지, 「내 것뿐인가」 를 보지 않는다 — 그건 시험 순서에 따라 깨진다.
    assert indexed.doc_path in [c.doc_path for c in found]
    assert all(c.workspace == indexed.workspace for c in found)


def test_another_workspace_cannot_see_the_candidate(indexed):
    """채널 ID 가 같아도 워크스페이스가 다르면 나오지 않는다(원칙 4)."""
    found = candidates(
        [0.1, 0.2],
        scope=SearchScope.checked("other-ws", [indexed.channel_id]),
        model_revision=indexed.model_revision,
        connect=_fresh,
    )

    assert found == []


def test_an_unknown_channel_returns_nothing(indexed):
    found = candidates(
        [0.1, 0.2],
        scope=SearchScope.checked(indexed.workspace, ["C-NOT-MINE"]),
        model_revision=indexed.model_revision,
        connect=_fresh,
    )

    assert found == []


def test_a_retired_revision_is_not_a_candidate(conn, model):
    """새 판이 서면 옛 판은 후보에서 빠진다 — 지운 뜻이 뒤집히지 않는다."""
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    writer = EmbeddingIndexWriter(conn)
    _publish(writer, conn, _key("old", model, doc=doc), "old", [_chunk("old")], [[0.1, 0.2]])
    _publish(writer, conn, _key("new", model, doc=doc), "new", [_chunk("new")], [[0.2, 0.3]])

    found = candidates(
        [0.1, 0.2],
        scope=SearchScope.checked("test-ws", ["C-TEST"]),
        model_revision=model,
        limit=LIMIT,
        connect=_fresh,
    )

    paths = [c.doc_path for c in found or ()]
    assert paths.count(doc) == 1
    revisions = {c.source_revision for c in (found or ()) if c.doc_path == doc}
    assert revisions == {hashlib.sha256(b"new").hexdigest()}


def test_an_inactive_model_revision_yields_no_candidate(conn, model, indexed):
    """모델 판을 내리면 그 판의 후보는 즉시 안 나온다."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE search_embedding_model_revision SET state='retired' WHERE revision=%s",
            (model,),
        )
    try:
        found = candidates(
            [0.1, 0.2],
            scope=SearchScope.checked(indexed.workspace, [indexed.channel_id]),
            model_revision=model,
            connect=_fresh,
        )
    finally:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE search_embedding_model_revision SET state='active' "
                "WHERE revision=%s",
                (model,),
            )

    assert found == []


def test_a_query_of_the_wrong_dimension_yields_no_candidate(indexed):
    """차원이 안 맞는 질의는 거리 계산 없이 빠진다 — 엉뚱한 순위가 아니라 0건이다."""
    found = candidates(
        [0.1, 0.2, 0.3],
        scope=SearchScope.checked(indexed.workspace, [indexed.channel_id]),
        model_revision=indexed.model_revision,
        connect=_fresh,
    )

    assert found == []



# --- 8. 늦게 끝난 옛 색인은 새 current 를 못 밀어낸다 ---------------------------
#
# 전에는 이 자리에 **결함을 고정하는** 시험 두 건이 있었다(옛 판이 그냥 현역이
# 됐다). CAS 가 들어오면서 단언을 안전한 쪽으로 뒤집었다.
# 설계: docs/verification/2026-11-04-stale-index-overwrites-current.md


def _chunks_under(connection, doc: str) -> int:
    with connection.cursor() as cur:
        cur.execute(
            """SELECT count(*) FROM archive_search_chunk c
                 JOIN archive_search_document_revision d
                   ON d.id = c.document_revision_id
                WHERE d.doc_path=%s""",
            (doc,),
        )
        return int(cur.fetchone()[0])


def test_a_never_indexed_stale_revision_is_refused(conn, model):
    """옛 원문을 읽은 색인이 **나중에** 끝나도 현역을 못 가져간다.

    스레드도 경주도 필요 없다. 두 색인 잡이 겹쳐 도는 동안 DB 안에서는
    `pg_advisory_xact_lock(doc)` 이 둘을 **줄 세우므로** 트랜잭션은 안 겹친다.
    위험한 구간은 그 바깥이다 — **원문을 읽은 시점부터 발행까지.** 그 구간을
    접으면 남는 것이 이것이고, 그래서 결정론적으로 재현된다.

    늦게 끝난 잡이 들고 있는 것은 **자기가 출발할 때 본 현역**(여기서는 없음)
    이다. 락을 받고 보니 그 사이 다른 판이 서 있다 — 그러면 손에 든 벡터는 이미
    정본이 아닌 글을 가리킨다. 그대로 올리면 **없는 글이 색인된다.**
    """
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    key = _key("v1", model, doc=doc)
    writer = EmbeddingIndexWriter(conn)

    # 옛 원문을 읽은 잡이 출발한다 — 그때 이 문서에는 현역이 없었다.
    observed_by_the_stale_job = None

    # 새 원문을 읽은 잡이 먼저 끝난다.
    fresh = _publish(writer, conn, _key("v2", model, doc=doc), "v2", [_chunk("v2")], [[0.2, 0.3]])
    assert _current_ids(conn, key) == [fresh.document_revision_id]
    chunks_before = _chunks_under(conn, doc)

    with pytest.raises(IndexPublishError) as caught:
        _publish(
            writer,
            conn,
            _key("v1", model, doc=doc),
            "v1",
            [_chunk("v1")],
            [[0.1, 0.2]],
            expected=observed_by_the_stale_job,
        )

    assert str(caught.value) == "embedding_source_superseded"
    # 새 판이 그대로 현역이다.
    assert _current_ids(conn, key) == [fresh.document_revision_id]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state,is_current FROM archive_search_document_revision WHERE id=%s",
            (fresh.document_revision_id,),
        )
        assert cur.fetchone() == ("ready", True)
        # 거절된 판은 **행도 안 남긴다.**
        cur.execute(
            """SELECT count(*) FROM archive_search_document_revision
                WHERE doc_path=%s AND source_revision=%s""",
            (doc, _key("v1", model, doc=doc).source_revision),
        )
        assert int(cur.fetchone()[0]) == 0
    assert _chunks_under(conn, doc) == chunks_before


def test_an_expectation_of_a_document_that_lost_its_current_is_refused(conn, model):
    """반대 방향도 막는다 — 대체하려던 것이 이미 사라진 경우.

    여기서 통과시키면 「내가 본 것」 이 아무 뜻도 없는 값이 되고, 검사는 한
    방향에만 서 있게 된다.
    """
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    writer = EmbeddingIndexWriter(conn)
    gone = _publish(writer, conn, _key("v0", model, doc=doc), "v0", [_chunk("v0")], [[0.1, 0.2]])
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE archive_search_document_revision "
            "SET is_current=false, state='retired' WHERE id=%s",
            (gone.document_revision_id,),
        )

    with pytest.raises(IndexPublishError) as caught:
        _publish(
            writer,
            conn,
            _key("v1", model, doc=doc),
            "v1",
            [_chunk("v1")],
            [[0.2, 0.3]],
            expected=gone.document_revision_id,
        )

    assert str(caught.value) == "embedding_source_superseded"
    assert _current_ids(conn, _key("v1", model, doc=doc)) == []


def test_the_fresh_job_publishes_over_what_it_actually_saw(conn, model):
    """막는 쪽만 보면 「전부 막는 검사」 도 통과한다. 정상 경로를 같이 둔다."""
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    writer = EmbeddingIndexWriter(conn)
    first = _publish(writer, conn, _key("v1", model, doc=doc), "v1", [_chunk("v1")], [[0.1, 0.2]])
    second = _publish(
        writer,
        conn,
        _key("v2", model, doc=doc),
        "v2",
        [_chunk("v2")],
        [[0.2, 0.3]],
        expected=first.document_revision_id,
    )

    assert not second.already_current
    assert _current_ids(conn, _key("v2", model, doc=doc)) == [second.document_revision_id]


def test_a_returning_retired_revision_is_still_refused(conn, model):
    """CAS 를 통과해도 **전에 본 판**은 멱등 판정에서 막힌다 — 문이 둘이다.

    v0 → v1 을 거친 뒤 v0 을 다시 올린다. 이 잡은 현역(v1)을 제대로 보고
    출발했으므로 CAS 는 통과한다. 그 다음 writer 가 v0 행을 찾고,
    `state<>'ready'` 라서 `embedding_revision_conflict` 을 던진다.

    두 검사가 막는 것이 다르다 — CAS 는 **내가 본 것이 아직 맞는가**, 멱등
    판정은 **이 판이 온전한가**. 한쪽만 두면 나머지 자리가 열린다.
    """
    doc = f"synthetic/{uuid.uuid4().hex}.md"
    writer = EmbeddingIndexWriter(conn)
    first = _publish(writer, conn, _key("v0", model, doc=doc), "v0", [_chunk("v0")], [[0.1, 0.2]])
    second = _publish(writer, conn, _key("v1", model, doc=doc), "v1", [_chunk("v1")], [[0.2, 0.3]])

    with pytest.raises(IndexPublishError) as caught:
        _publish(writer, conn, _key("v0", model, doc=doc), "v0", [_chunk("v0")], [[0.1, 0.2]])

    assert str(caught.value) == "embedding_revision_conflict"
    assert _current_ids(conn, _key("v0", model, doc=doc)) == [second.document_revision_id]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state,is_current FROM archive_search_document_revision WHERE id=%s",
            (first.document_revision_id,),
        )
        assert cur.fetchone() == ("retired", False)
