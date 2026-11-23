-- B-81 후속. 이미지 OCR 전사 revision 과 사진 검색 투영 (2026-11-12 오너 결정)
--
-- 코드: src/tybot/archive/image_transcript.py, image_description_store.py,
--       src/tybot/image_guide.py
--
-- archiver_image_description_schema.sql 뒤에 적용한다(그 표의 job·revision 을 참조한다).
-- 이 파일을 적용해도 모델이 켜지지 않는다. 정책 OFF·timer 비활성 기본은 그대로다.
--
-- ## 두 표를 나누는 이유
--
-- 1. archiver_image_transcript_revision — **Vision 전용 원장.** 설명 revision 과 같은
--    job 에서 나왔지만 다른 표·다른 라벨이다. 전사를 설명 칸에 섞지 않는다.
-- 2. image_search_entry — **검색 투영.** Master 가 읽는 유일한 이미지 파생 표다.
--    Vision 원장(job·scope·카탈로그·policy)을 Master 에 열지 않으려고 따로 둔다
--    (check_schema_drift.py 는 Vision 표에 Master 권한이 있으면 drift 로 본다).
--    안전 판정을 통과한 검색 텍스트와 provenance 좌표만 담는다.

BEGIN;

CREATE TABLE IF NOT EXISTS archiver_image_transcript_revision (
    id text PRIMARY KEY,
    identity_sha256 text NOT NULL UNIQUE CHECK (identity_sha256 ~ '^[a-f0-9]{64}$'),
    job_id text NOT NULL UNIQUE REFERENCES archiver_image_description_job(id),
    -- 같은 호출에서 나온 설명 revision. 설명이 저장될 때만 전사도 저장한다.
    description_revision_id text NOT NULL
        REFERENCES archiver_image_description_revision(id),
    workspace text NOT NULL CHECK (workspace <> ''),
    channel_id text NOT NULL CHECK (channel_id <> ''),
    file_id text NOT NULL CHECK (file_id <> ''),
    original_sha256 text NOT NULL CHECK (original_sha256 ~ '^[a-f0-9]{64}$'),
    image_sha256 text NOT NULL CHECK (image_sha256 ~ '^[a-f0-9]{64}$'),
    prompt_revision text NOT NULL CHECK (prompt_revision <> ''),
    model text NOT NULL CHECK (model <> ''),
    provider text NOT NULL CHECK (provider <> ''),
    source text NOT NULL DEFAULT 'vision_llm' CHECK (source = 'vision_llm'),
    status text NOT NULL CHECK (status IN ('transcribed','uncertain')),
    label text NOT NULL DEFAULT '[AI 텍스트 인식·검색용]'
        CHECK (label = '[AI 텍스트 인식·검색용]'),
    -- 마스킹(sensitive_mask)과 최종 관문(pii_screen)을 지난 줄만.
    body text NOT NULL CHECK (body <> '' AND char_length(body) <= 8200),
    line_count integer NOT NULL CHECK (line_count BETWEEN 1 AND 200),
    masked_count integer NOT NULL DEFAULT 0 CHECK (masked_count >= 0),
    uncertainty text[] NOT NULL DEFAULT '{}',
    guard_codes text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (status <> 'uncertain' OR cardinality(uncertainty) > 0)
);
CREATE INDEX IF NOT EXISTS archiver_image_transcript_revision_source
    ON archiver_image_transcript_revision(workspace, channel_id, file_id, created_at);

-- 검색 투영. kind 마다 revision 한 줄. 본문은 **이미 안전 판정을 지난 것만** 들어온다:
--   description — 가드가 allow 한 설명 + 허용 목록 태그(보류된 설명은 태그만)
--   transcript  — 마스킹·최종 관문을 지난 전사
CREATE TABLE IF NOT EXISTS image_search_entry (
    id text PRIMARY KEY,
    kind text NOT NULL CHECK (kind IN ('description','transcript')),
    revision_id text NOT NULL UNIQUE,
    workspace text NOT NULL CHECK (workspace <> ''),
    channel_id text NOT NULL CHECK (channel_id <> ''),
    file_id text NOT NULL CHECK (file_id <> ''),
    original_sha256 text NOT NULL CHECK (original_sha256 ~ '^[a-f0-9]{64}$'),
    provider text NOT NULL CHECK (provider <> ''),
    model text NOT NULL CHECK (model <> ''),
    prompt_revision text NOT NULL CHECK (prompt_revision <> ''),
    status text NOT NULL CHECK (status IN ('described','uncertain','transcribed')),
    label text NOT NULL CHECK (label IN ('[AI 이미지 설명·검색용]','[AI 텍스트 인식·검색용]')),
    body text NOT NULL CHECK (body <> '' AND char_length(body) <= 8200),
    uncertainty text[] NOT NULL DEFAULT '{}',
    -- 임베딩(B-12)은 provider 승인 전까지 만들지 않는다. parked 는 실패가 아니다.
    vector_state text NOT NULL DEFAULT 'parked'
        CHECK (vector_state IN ('parked','pending','ready','failed')),
    vector_revision text NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((kind = 'description' AND label = '[AI 이미지 설명·검색용]'
                                 AND status IN ('described','uncertain')) OR
           (kind = 'transcript' AND label = '[AI 텍스트 인식·검색용]'
                                AND status IN ('transcribed','uncertain')))
);
CREATE INDEX IF NOT EXISTS image_search_entry_source
    ON image_search_entry(workspace, channel_id, file_id, created_at);

-- 키워드 색인. 식은 `image_guide.ENTRY_COMPACT_BODY` 에서 별칭만 뺀 것과 글자까지 같다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_bigm') THEN
        DROP INDEX IF EXISTS image_search_entry_body_trgm_fallback;
        CREATE INDEX IF NOT EXISTS image_search_entry_body_bigm
            ON image_search_entry USING gin ((lower(replace(body, ' ', ''))) gin_bigm_ops);
    ELSE
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_trgm';
        CREATE INDEX IF NOT EXISTS image_search_entry_body_trgm_fallback
            ON image_search_entry USING gin ((lower(replace(body, ' ', ''))) gin_trgm_ops);
        RAISE WARNING 'pg_bigm 이 없어 사진 검색 색인을 pg_trgm 으로 대체했다. 2글자 낱말은 인덱스를 타지 못한다.';
    END IF;
END $$;

REVOKE ALL ON archiver_image_transcript_revision, image_search_entry FROM PUBLIC;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_vision') THEN
        -- 원장은 append-only. 투영도 worker 가 쓰기만 한다.
        GRANT SELECT, INSERT ON TABLE archiver_image_transcript_revision TO tybot_vision;
        GRANT SELECT, INSERT ON TABLE image_search_entry TO tybot_vision;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        -- Master 는 **투영만** 읽는다. Vision 원장에는 권한을 주지 않는다.
        GRANT SELECT ON TABLE image_search_entry TO tyslackai;
    END IF;
END $$;

COMMENT ON TABLE archiver_image_transcript_revision IS
    'B-81 AI OCR 전사 revision(Vision 전용). 설명 revision 과 분리. 마스킹 뒤 본문만.';
COMMENT ON TABLE image_search_entry IS
    'B-81 사진 검색 투영(Master 읽기 전용). 안전 판정을 지난 텍스트와 원본 좌표만. 근거 아님.';

COMMIT;
