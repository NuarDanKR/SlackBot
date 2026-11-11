-- 승인 요약 키워드 색인과 벡터 상태 (B-89)
--
-- 승인 요약(`approved_summary_item`)은 원문이 아니라 **원문을 찾는 길잡이**다(B-56).
-- 이 파일은 그 길잡이를 질문 낱말로 DB 에서 바로 거르게 한다. 예전에는 최근 500건을
-- 읽어 파이썬에서 점수를 매겼고, 501번째부터는 아무리 맞아도 길잡이가 되지 못했다.
--
-- 답변 근거는 여전히 요약 문장이 아니다. 거른 항목이 가리키는 원문 좌표를 요청자의
-- **현재 권한**으로 다시 연다(`summary_guide.expand` → `ArchiveStore.resolve_refs`).
--
-- summary_review_schema.sql 뒤에 적용한다(그 표에 색인·열을 더한다). 멱등.

-- ---------------------------------------------------------------------------
-- 1. 키워드 색인
-- ---------------------------------------------------------------------------
-- 식은 `summary_guide.SUMMARY_COMPACT_BODY` 에서 별칭(`a.`)만 뺀 것과 **글자까지
-- 같아야** 한다. 다르면 오류 없이 전체 스캔이 된다(tests/test_summary_index_schema.py).
--
-- 원문 색인(index_schema.sql)과 같은 이유로 pg_bigm 을 쓴다 — 2글자 한국어 낱말(기성·
-- 타설)을 덮는 것은 bigm 뿐이다. 없으면 pg_trgm 으로 일단 깔고 넘어간다.
--     sudo -u postgres psql -p 55432 -d tyslackai -c "CREATE EXTENSION pg_bigm;"
--
-- 부분 색인 조건은 길잡이 조회의 WHERE 와 같은 두 열이다. 대체·어긋난 항목은 길잡이가
-- 읽지 않으므로 색인에 둘 이유가 없다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_bigm') THEN
        DROP INDEX IF EXISTS approved_summary_body_trgm_fallback;
        CREATE INDEX IF NOT EXISTS approved_summary_body_bigm
            ON approved_summary_item USING gin ((lower(replace(body, ' ', ''))) gin_bigm_ops)
            WHERE superseded_at IS NULL AND stale_at IS NULL;
    ELSE
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_trgm';
        CREATE INDEX IF NOT EXISTS approved_summary_body_trgm_fallback
            ON approved_summary_item USING gin ((lower(replace(body, ' ', ''))) gin_trgm_ops)
            WHERE superseded_at IS NULL AND stale_at IS NULL;
        RAISE WARNING 'pg_bigm 이 없어 승인 요약 색인을 pg_trgm 으로 대체했다. 2글자 낱말은 인덱스를 타지 못한다.';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. 벡터 상태
-- ---------------------------------------------------------------------------
-- 임베딩(B-12)은 provider 승인 전까지 만들 수 없다. 그동안 키워드 색인만으로 길잡이가
-- 돌고, 모든 항목은 `parked` 로 남는다. 승인 뒤 같은 항목(같은 derived revision)에
-- 벡터를 더하면서 `pending` → `ready`/`failed` 로 옮긴다.
--
-- **`parked` 는 실패가 아니다.** 「벡터가 없다」 를 오류로 세면 승인 전 기간 내내
-- 경보가 울리고, 사람은 그 경보를 끄는 법부터 배운다.
ALTER TABLE approved_summary_item
    ADD COLUMN IF NOT EXISTS vector_state text NOT NULL DEFAULT 'parked';
ALTER TABLE approved_summary_item
    ADD COLUMN IF NOT EXISTS vector_revision text NOT NULL DEFAULT '';
ALTER TABLE approved_summary_item
    DROP CONSTRAINT IF EXISTS approved_summary_item_vector_state;
ALTER TABLE approved_summary_item
    ADD CONSTRAINT approved_summary_item_vector_state
    CHECK (vector_state IN ('parked', 'pending', 'ready', 'failed'));

COMMENT ON COLUMN approved_summary_item.vector_state IS
    'B-89. 임베딩 provider 승인 전에는 parked. 키워드 색인과 무관하다.';
