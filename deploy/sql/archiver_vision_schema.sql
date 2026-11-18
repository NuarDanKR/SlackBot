-- B-80. Dormant backend only: no worker, credentials, content or activation.
BEGIN;

CREATE TABLE IF NOT EXISTS archiver_vision_policy (
    workspace text PRIMARY KEY,
    revision text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    config jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(config) = 'object'),
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- All mutations lock this row first, including OFF/revocation and settlement.
INSERT INTO archiver_vision_policy(workspace, revision, updated_by)
VALUES ('', 'initial', 'schema') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS archiver_vision_policy_audit (
    revision text PRIMARY KEY,
    workspace text NOT NULL,
    enabled boolean NOT NULL,
    config jsonb NOT NULL CHECK (jsonb_typeof(config) = 'object'),
    updated_by text NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS archiver_vision_approval (
    id text PRIMARY KEY,
    workspace text NOT NULL CHECK (workspace <> ''),
    scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'object'),
    expires_at timestamptz NOT NULL,
    approved_by text NOT NULL CHECK (approved_by <> ''),
    revoked_at timestamptz,
    revoked_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS archiver_vision_job (
    id text PRIMARY KEY,
    identity_sha256 text NOT NULL UNIQUE CHECK (identity_sha256 ~ '^[a-f0-9]{64}$'),
    approval_id text NOT NULL REFERENCES archiver_vision_approval(id),
    workspace text NOT NULL CHECK (workspace <> ''),
    policy_revision text NOT NULL,
    scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'object'),
    policy_snapshot jsonb NOT NULL CHECK (jsonb_typeof(policy_snapshot) = 'object'),
    max_output_tokens integer NOT NULL CHECK (max_output_tokens > 0),
    reserved_usd numeric NOT NULL CHECK (reserved_usd > 0 AND reserved_usd < 'Infinity'::numeric),
    actual_usd numeric CHECK (actual_usd >= 0 AND actual_usd < 'Infinity'::numeric),
    state text NOT NULL CHECK (state IN ('reserved', 'dispatching', 'settled', 'unknown', 'cancelled')),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    finished_at timestamptz,
    CHECK ((state = 'settled') = (actual_usd IS NOT NULL)),
    CHECK (state NOT IN ('dispatching', 'unknown', 'settled') OR dispatched_at IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS archiver_vision_job_budget
    ON archiver_vision_job(workspace, created_at);

-- B-80 운영 연결(2026-11-19): 페이지 전사 결과는 **파생 revision** 이다. 원문 MD·원본 파일을
-- 고치지 않는다. 한 job(= 한 페이지 한 번의 전송)에 결과는 하나뿐이다(job_id UNIQUE).
-- 본문은 마스킹 뒤 최종 관문을 지난 것만 들어간다. 실패·보류 결과는 본문 없이 코드만 남긴다.
CREATE TABLE IF NOT EXISTS archiver_vision_page_revision (
    id text PRIMARY KEY,
    job_id text NOT NULL UNIQUE REFERENCES archiver_vision_job(id),
    workspace text NOT NULL CHECK (workspace <> ''),
    channel_id text NOT NULL CHECK (channel_id <> ''),
    file_id text NOT NULL CHECK (file_id <> ''),
    original_sha256 text NOT NULL CHECK (original_sha256 ~ '^[a-f0-9]{64}$'),
    page_number integer NOT NULL CHECK (page_number > 0),
    page_basis text NOT NULL,
    rendered_sha256 text NOT NULL CHECK (rendered_sha256 ~ '^[a-f0-9]{64}$'),
    renderer_version text NOT NULL,
    image_sha256 text NOT NULL CHECK (image_sha256 ~ '^[a-f0-9]{64}$'),
    model text NOT NULL,
    provider text NOT NULL,
    status text NOT NULL CHECK (status IN ('extracted', 'uncertain', 'failed', 'withheld')),
    body text NOT NULL DEFAULT '',
    unreadable text[] NOT NULL DEFAULT '{}',
    codes text[] NOT NULL DEFAULT '{}',
    masked_count integer NOT NULL DEFAULT 0 CHECK (masked_count >= 0),
    label text NOT NULL DEFAULT '[AI 문서 전사·파생]' CHECK (label = '[AI 문서 전사·파생]'),
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((status IN ('extracted', 'uncertain')) = (body <> ''))
);
CREATE INDEX IF NOT EXISTS archiver_vision_page_revision_file
    ON archiver_vision_page_revision(workspace, file_id, page_number);
REVOKE ALL ON archiver_vision_page_revision FROM PUBLIC;

-- Master 가 **읽기만** 하는 검색 투영(B-81 `image_search_entry` 와 같은 원칙).
-- Vision 원장(job·scope·정책)은 Master 에 열지 않는다. 여기에는 성공 전사(extracted·uncertain)의
-- 마스킹된 본문과 원본 좌표·provenance 만 들어간다. 원본 MD 는 고치지 않는다.
-- 답변에 쓸 때는 요청자의 **현재 권한**으로 그 채널·파일을 다시 확인한다(원칙 3).
-- revision 하나에 투영 하나(revision_id UNIQUE) — 소급 투영을 몇 번 돌려도 같다.
CREATE TABLE IF NOT EXISTS pdf_page_search_entry (
    id text PRIMARY KEY,
    revision_id text NOT NULL UNIQUE REFERENCES archiver_vision_page_revision(id),
    workspace text NOT NULL CHECK (workspace <> ''),
    channel_id text NOT NULL CHECK (channel_id <> ''),
    file_id text NOT NULL CHECK (file_id <> ''),
    page_number integer NOT NULL CHECK (page_number > 0),
    original_sha256 text NOT NULL CHECK (original_sha256 ~ '^[a-f0-9]{64}$'),
    renderer_version text NOT NULL,
    provider text NOT NULL CHECK (provider <> ''),
    model text NOT NULL CHECK (model <> ''),
    status text NOT NULL CHECK (status IN ('extracted', 'uncertain')),
    label text NOT NULL DEFAULT '[AI 문서 전사·파생]' CHECK (label = '[AI 문서 전사·파생]'),
    body text NOT NULL CHECK (body <> '' AND char_length(body) <= 20000),
    unreadable text[] NOT NULL DEFAULT '{}',
    vector_state text NOT NULL DEFAULT 'parked'
        CHECK (vector_state IN ('parked', 'pending', 'ready', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pdf_page_search_entry_source
    ON pdf_page_search_entry(workspace, channel_id, file_id, page_number);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_bigm') THEN
        DROP INDEX IF EXISTS pdf_page_search_entry_body_trgm_fallback;
        CREATE INDEX IF NOT EXISTS pdf_page_search_entry_body_bigm
            ON pdf_page_search_entry USING gin ((lower(replace(body, ' ', ''))) gin_bigm_ops);
    ELSIF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm') THEN
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS pdf_page_search_entry_body_trgm_fallback
            ON pdf_page_search_entry USING gin ((lower(replace(body, ' ', ''))) gin_trgm_ops);
    END IF;
END $$;
REVOKE ALL ON pdf_page_search_entry FROM PUBLIC;

REVOKE ALL ON archiver_vision_policy, archiver_vision_approval, archiver_vision_job FROM PUBLIC;
REVOKE ALL ON archiver_vision_policy_audit FROM PUBLIC;
-- No automatic role creation or collector/Master access. Provision a dedicated
-- service DSN separately before any future worker activation.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_vision') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE archiver_vision_policy,
            archiver_vision_approval, archiver_vision_job TO tybot_vision;
        GRANT SELECT, INSERT ON TABLE archiver_vision_policy_audit TO tybot_vision;
        GRANT SELECT, INSERT ON TABLE archiver_vision_page_revision TO tybot_vision;
        GRANT SELECT, INSERT ON TABLE pdf_page_search_entry TO tybot_vision;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        -- Master 는 검색 투영만 읽는다. 원장·전사 revision 은 읽지 않는다.
        GRANT SELECT ON TABLE pdf_page_search_entry TO tyslackai;
    END IF;
END $$;
COMMIT;
