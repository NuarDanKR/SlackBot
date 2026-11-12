-- 채널×일 다이제스트 (B-93 2단계)
--
-- 설계: docs/design/channel-day-digest.md
--
-- 다이제스트는 **검색 길잡이**다. 본문(body·terms)은 답변 근거·출처로 나가지 않는다.
-- 질문이 다이제스트와 맞으면 `source_files`·`source_attachments` 가 가리키는 원문을
-- 요청자의 **현재 권한**으로 다시 열고, 근거는 그 원문 줄이다(원칙 1·3).
--
-- 한 행은 **한 채널·하루**만 묶는다(파생 조건 5). 원문 지문(`revision`)이 바뀌면 이전
-- 행은 `stale_at` 이 찍히고 새 행이 생긴다 — 덮어쓰지 않는다(무엇이 언제 바뀌었는지 남긴다).
--
-- 권한:
--   tybot_digest  (NOLOGIN 그룹) — 배치가 쓴다. 로그인 계정은 3단계(배치)에서 붙인다
--   tyslackai     (Master)        — 검색에서 **읽기만** 한다
--
-- 멱등. apply-schema.sh 가 bot_source_schema.sql 뒤에 적용한다(그 표를 읽을 권한을 준다).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_digest') THEN
        CREATE ROLE tybot_digest NOLOGIN;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS channel_day_digest (
    id                 text PRIMARY KEY,
    workspace          text NOT NULL,
    channel_id         text NOT NULL,
    day                date NOT NULL,                 -- KST 기준 하루
    revision           text NOT NULL,                 -- 입력 지문(channel_digest.revision)
    body               text NOT NULL,                 -- 검색용 요약. 근거 아님
    terms              text[] NOT NULL DEFAULT '{}',  -- 같은 뜻 다른 말(경영목표·KPI 등)
    source_files       jsonb NOT NULL,                -- [{path, sha256, lines}]
    source_attachments jsonb NOT NULL DEFAULT '[]',   -- [{path, file_id, revision, sha256}]
    model              text NOT NULL,
    provider           text NOT NULL,
    prompt_revision    text NOT NULL,
    stale_at           timestamptz,
    stale_reason       text,
    vector_state       text NOT NULL DEFAULT 'parked',
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT channel_day_digest_once UNIQUE (workspace, channel_id, day, revision),
    CONSTRAINT channel_day_digest_body_len CHECK (char_length(body) BETWEEN 1 AND 2000),
    CONSTRAINT channel_day_digest_terms_len CHECK (cardinality(terms) <= 40),
    CONSTRAINT channel_day_digest_vector_state
        CHECK (vector_state IN ('parked', 'pending', 'ready', 'failed'))
);

COMMENT ON TABLE channel_day_digest IS
    'B-93 2단계. 채널×일 검색 길잡이. 본문은 근거가 아니다 — 원문 좌표를 현재 권한으로 다시 연다.';

-- 현재 판 조회: 같은 (workspace, channel_id, day) 에서 stale 아닌 행.
CREATE INDEX IF NOT EXISTS channel_day_digest_current
    ON channel_day_digest (workspace, channel_id, day)
    WHERE stale_at IS NULL;

-- 키워드 색인. 원문 색인과 같은 이유로 pg_bigm 우선(2글자 한국어 낱말), 없으면 pg_trgm.
-- 식은 `channel_digest_guide.DIGEST_COMPACT_TEXT` 와 글자까지 같아야 한다(4단계에서 고정).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_bigm') THEN
        DROP INDEX IF EXISTS channel_day_digest_text_trgm_fallback;
        CREATE INDEX IF NOT EXISTS channel_day_digest_text_bigm
            ON channel_day_digest USING gin
            ((lower(replace(body || ' ' || array_to_string(terms, ' '), ' ', ''))) gin_bigm_ops)
            WHERE stale_at IS NULL;
    ELSE
        EXECUTE 'CREATE EXTENSION IF NOT EXISTS pg_trgm';
        CREATE INDEX IF NOT EXISTS channel_day_digest_text_trgm_fallback
            ON channel_day_digest USING gin
            ((lower(replace(body || ' ' || array_to_string(terms, ' '), ' ', ''))) gin_trgm_ops)
            WHERE stale_at IS NULL;
        RAISE WARNING 'pg_bigm 이 없어 다이제스트 색인을 pg_trgm 으로 대체했다. 2글자 낱말은 인덱스를 타지 못한다.';
    END IF;
END $$;

GRANT SELECT, INSERT ON channel_day_digest TO tybot_digest;
-- 배치는 stale 표시만 고친다. 본문·좌표는 고치지 않는다(새 행을 만든다).
GRANT UPDATE (stale_at, stale_reason, vector_state) ON channel_day_digest TO tybot_digest;
GRANT SELECT ON channel_day_digest TO tyslackai;

-- 배치는 대상 채널(봇 근거 채널)을 읽는다. 필요한 열만.
GRANT SELECT (bot, workspace, channel_id, removed_at) ON bot_source_channel TO tybot_digest;
