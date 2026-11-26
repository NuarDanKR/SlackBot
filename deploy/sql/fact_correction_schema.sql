-- 사람이 남긴 사실 정정(2026-11-20 오너 결정).
-- 개인 기억(user_memory)과 달리 **원문 좌표를 지금 열 수 있는 모든 사람**의 관련 답변에
-- 정정자·날짜·정정 전후 값과 함께 표시된다. 근거가 아니다 — 원문을 바꾸지 않는다.
-- 좌표(evidence_refs)가 비어 있으면 누가 봐도 되는지 정할 수 없으므로 행을 받지 않는다.

BEGIN;

CREATE TABLE IF NOT EXISTS fact_correction (
    id                bigserial PRIMARY KEY,
    workspace         text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    corrector_id      text NOT NULL CHECK (btrim(corrector_id) <> ''),
    corrector_name    text NOT NULL DEFAULT '',
    subject           text NOT NULL DEFAULT '' CHECK (char_length(subject) <= 60),
    before_value      text NOT NULL CHECK (btrim(before_value) <> '' AND char_length(before_value) <= 40),
    after_value       text NOT NULL CHECK (btrim(after_value) <> '' AND char_length(after_value) <= 40),
    body              text NOT NULL CHECK (btrim(body) <> '' AND char_length(body) <= 300),
    normalized        text NOT NULL CHECK (btrim(normalized) <> ''),
    search_terms      text[] NOT NULL DEFAULT '{}'
                      CHECK (cardinality(search_terms) <= 8),
    source_record_id  text NOT NULL CHECK (btrim(source_record_id) <> ''),
    source_channel_id text NOT NULL DEFAULT '',
    evidence_refs     jsonb NOT NULL
                      CHECK (jsonb_typeof(evidence_refs) = 'array'
                             AND jsonb_array_length(evidence_refs) > 0),
    masked            boolean NOT NULL DEFAULT false,
    mask_types        text[] NOT NULL DEFAULT '{}',
    mask_revision     text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    -- NULL = 영구(2026-11-26 오너 결정).
    expires_at        timestamptz,
    last_shown_at     timestamptz,
    show_count        integer NOT NULL DEFAULT 0 CHECK (show_count >= 0),
    revoked_at        timestamptz,
    revoked_by        text NOT NULL DEFAULT '',
    revoke_reason     text NOT NULL DEFAULT '' CHECK (char_length(revoke_reason) <= 300),
    CHECK (revoked_at IS NULL OR btrim(revoked_by) <> '')
);

-- 이미 만든 표: 만료를 영구로 풀고 기존 행도 영구로 바꾼다(멱등).
ALTER TABLE fact_correction ALTER COLUMN expires_at DROP NOT NULL;
UPDATE fact_correction SET expires_at = NULL
    WHERE revoked_at IS NULL AND expires_at IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS fact_correction_unique_live
    ON fact_correction (workspace, source_record_id, corrector_id, normalized)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS fact_correction_live
    ON fact_correction (workspace, expires_at DESC)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS fact_correction_by_corrector
    ON fact_correction (workspace, corrector_id, created_at DESC);

REVOKE ALL ON TABLE fact_correction FROM PUBLIC;

DO $$
DECLARE
    sequence_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE fact_correction TO tyslackai;
        SELECT pg_get_serial_sequence('fact_correction', 'id') INTO sequence_name;
        IF sequence_name IS NOT NULL THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE ' || sequence_name || ' TO tyslackai';
        END IF;
    END IF;
END
$$;

COMMENT ON TABLE fact_correction IS
    '사람이 남긴 사실 정정. 원문 좌표를 지금 열 수 있는 사람에게만 정정자·날짜와 함께 보인다. 근거 아님.';

COMMIT;
