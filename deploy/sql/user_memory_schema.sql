-- B-91 user-scoped structured feedback memory.
-- Memory is derived data, never archive evidence, and never shared across users.

BEGIN;

CREATE TABLE IF NOT EXISTS user_memory (
    id                bigserial PRIMARY KEY,
    workspace         text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    user_id           text NOT NULL CHECK (btrim(user_id) <> ''),
    kind              text NOT NULL CHECK (kind IN ('preference', 'term', 'claim')),
    body              text NOT NULL CHECK (btrim(body) <> ''),
    normalized        text NOT NULL CHECK (btrim(normalized) <> ''),
    search_terms      text[] NOT NULL DEFAULT '{}'
                      CHECK (cardinality(search_terms) <= 8),
    source_record_id  text NOT NULL CHECK (btrim(source_record_id) <> ''),
    source_channel_id text NOT NULL DEFAULT '',
    source_thread_ts  text NOT NULL DEFAULT '',
    evidence_refs     jsonb NOT NULL DEFAULT '[]'::jsonb
                      CHECK (jsonb_typeof(evidence_refs) = 'array'),
    masked            boolean NOT NULL DEFAULT false,
    mask_types        text[] NOT NULL DEFAULT '{}',
    mask_revision     text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    last_used_at      timestamptz,
    use_count         integer NOT NULL DEFAULT 0 CHECK (use_count >= 0),
    expires_at        timestamptz NOT NULL,
    revoked_at        timestamptz,
    revoked_by        text NOT NULL DEFAULT '',
    CHECK (revoked_at IS NULL OR btrim(revoked_by) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS user_memory_unique_live
    ON user_memory (workspace, user_id, kind, normalized)
    WHERE revoked_at IS NULL;

CREATE INDEX IF NOT EXISTS user_memory_by_user
    ON user_memory (workspace, user_id, kind, expires_at DESC)
    WHERE revoked_at IS NULL;

REVOKE ALL ON TABLE user_memory FROM PUBLIC;

DO $$
DECLARE
    sequence_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE user_memory TO tyslackai;
        SELECT pg_get_serial_sequence('user_memory', 'id') INTO sequence_name;
        IF sequence_name IS NOT NULL THEN
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE ' || sequence_name || ' TO tyslackai';
        END IF;
    END IF;
END
$$;

COMMENT ON TABLE user_memory IS
    'B-91 per-user structured feedback memory. Derived hints only; never archive evidence.';

COMMIT;
