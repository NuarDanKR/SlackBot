"""승인 요약 키워드 색인 스키마 (B-89).

색인 식이 코드와 한 글자라도 다르면 오류 없이 전체 스캔이 된다. 여기서는 그 일치와,
임베딩이 없는 동안 모든 항목이 `parked` 로 남는다는 약속을 본다.
"""
from __future__ import annotations

import os
import pathlib
import re

import pytest

from tybot import summary_guide

ROOT = pathlib.Path(__file__).resolve().parent.parent
SQL = (ROOT / "deploy/sql/summary_index_schema.sql").read_text(encoding="utf-8")


def _code(sql: str) -> str:
    return "\n".join(line.split("--", 1)[0] for line in sql.splitlines())


def test_index_expression_matches_the_guide_query_exactly():
    expression = summary_guide.SUMMARY_COMPACT_BODY.replace("a.", "")
    assert expression == "lower(replace(body, ' ', ''))"
    indexes = re.findall(r"USING gin \(\((.+?)\) gin_(?:bigm|trgm)_ops\)", _code(SQL))
    assert indexes == [expression, expression]


def test_partial_index_covers_exactly_what_the_guide_reads():
    code = _code(SQL)
    assert code.count("WHERE superseded_at IS NULL AND stale_at IS NULL") == 2


def test_vectors_stay_parked_until_an_embedding_provider_is_approved():
    code = _code(SQL)
    assert "ADD COLUMN IF NOT EXISTS vector_state text NOT NULL DEFAULT 'parked'" in code
    assert "CHECK (vector_state IN ('parked', 'pending', 'ready', 'failed'))" in code
    # 길잡이는 벡터 상태를 보지 않는다 — 키워드만으로 돈다.
    guide = (ROOT / "src/tybot/summary_guide.py").read_text(encoding="utf-8")
    assert "vector_state" not in guide


def test_schema_is_applied_after_the_table_it_extends():
    files = re.findall(r"^\s+([a-z_]+\.sql)", (ROOT / "deploy/apply-schema.sh").read_text(
        encoding="utf-8"), flags=re.M)
    assert files.index("summary_review_schema.sql") < files.index("summary_index_schema.sql")


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL"), reason="전용 시험 DB 가 없다")
def test_on_a_disposable_database_keyword_lookup_works_and_vectors_are_parked():
    """**폐기 가능한 전용 DB(TEST_DATABASE_URL)에서만** 돈다. 운영 DB 는 쓰지 않는다."""
    import psycopg

    with psycopg.connect(os.environ["TEST_DATABASE_URL"], autocommit=True) as conn:
        schema = "b89_summary_index_test"
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute(f"CREATE SCHEMA {schema}")
        try:
            conn.execute(f"SET search_path TO {schema}, public")
            conn.execute("""
                CREATE TABLE approved_summary_item (
                    candidate_id text PRIMARY KEY, workspace text NOT NULL,
                    body text NOT NULL, superseded_at timestamptz, stale_at timestamptz
                )""")
            conn.execute(SQL)
            conn.execute("""INSERT INTO approved_summary_item (candidate_id, workspace, body)
                            VALUES ('1', 'pilot', 'iPhone 지급 대장'),
                                   ('2', 'pilot', '주간 업무 보고')""")
            clause, params = summary_guide.keyword_filter(["iphone", "주간업무"])
            rows = conn.execute(
                "SELECT candidate_id, vector_state FROM approved_summary_item a "
                "WHERE true " + clause + " ORDER BY candidate_id",
                params,
            ).fetchall()
            assert rows == [("1", "parked"), ("2", "parked")]
        finally:
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
