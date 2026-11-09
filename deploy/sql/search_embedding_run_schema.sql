-- B-12: embedding index run ledger.
--
-- One row per bounded index run (console, timer or CLI). The console reads this
-- to show what ran, what it cost and why it stopped. It holds counts and fixed
-- codes only: no source body, question, vector, credential or provider message.
--
-- Rebuildable? No -- this is history. Losing it loses the cost trail, so back it
-- up with the other audit tables.

BEGIN;

CREATE TABLE IF NOT EXISTS search_embedding_index_run (
    id                 bigserial PRIMARY KEY,
    model_revision     text NOT NULL
                       CHECK (model_revision ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
    trigger            text NOT NULL CHECK (trigger IN ('console', 'timer', 'cli')),
    requested_by       text NOT NULL CHECK (btrim(requested_by) <> ''),
    state              text NOT NULL DEFAULT 'running'
                       CHECK (state IN ('running', 'succeeded', 'failed')),
    -- Fixed ASCII code, never an exception message (those can carry paths or
    -- echoed provider input).
    error_code         text NOT NULL DEFAULT ''
                       CHECK (error_code ~ '^[a-z0-9_]{0,80}$'),
    stopped_reason     text NOT NULL DEFAULT ''
                       CHECK (stopped_reason ~ '^[a-z0-9_]{0,80}$'),
    inventory_count    integer CHECK (inventory_count >= 0),
    unchanged_count    integer CHECK (unchanged_count >= 0),
    requested_count    integer CHECK (requested_count >= 0),
    published_count    integer CHECK (published_count >= 0),
    embed_calls        integer CHECK (embed_calls >= 0),
    embedded_chars     bigint  CHECK (embedded_chars >= 0),
    embedded_chunks    integer CHECK (embedded_chunks >= 0),
    -- NULL means "unknown", not free. The provider may not report usage.
    cost_usd           numeric CHECK (cost_usd >= 0),
    -- Outcome code -> count, e.g. {"published": 40, "policy_denied": 3}.
    outcomes           jsonb NOT NULL DEFAULT '{}'::jsonb
                       CHECK (jsonb_typeof(outcomes) = 'object'),
    started_at         timestamptz NOT NULL DEFAULT now(),
    finished_at        timestamptz,
    CHECK (state = 'running' OR finished_at IS NOT NULL),
    CHECK (state <> 'failed' OR error_code <> '')
);

CREATE INDEX IF NOT EXISTS search_embedding_index_run_recent
    ON search_embedding_index_run (started_at DESC);

-- A run that died without finishing must not block the next one forever; the
-- runner treats "running" rows older than its timeout as abandoned.
CREATE INDEX IF NOT EXISTS search_embedding_index_run_running
    ON search_embedding_index_run (started_at) WHERE state = 'running';

COMMENT ON TABLE search_embedding_index_run IS
    'B-12 index run history. Counts and fixed codes only; no body, question or vector.';

-- Closed by default, like every other embedding table. A ledger that anyone
-- can read tells them which workspaces are indexed and what that costs; one
-- anyone can write lets a wrong row hide a run that actually happened.
REVOKE ALL ON TABLE search_embedding_index_run FROM PUBLIC;

-- The bot/console role writes this table. Ownership does not carry grants.
--
-- **No DELETE.** This is history, not a cache: the runtime appends a row and
-- closes it, and nothing in the running system has a reason to remove one.
-- Retention is an operator decision made with the other audit tables, not
-- something a bug in the runner can do by accident.
DO $$
DECLARE
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        GRANT SELECT, INSERT, UPDATE
            ON TABLE search_embedding_index_run
            TO tyslackai;
        -- The sequence is found through `pg_depend`, not spelled out as
        -- `<table>_id_seq`. A table that already existed when this file first
        -- ran may own a differently named sequence, and then a hardcoded name
        -- grants nothing -- `INSERT` fails at the first run with a permission
        -- error that names the sequence, not this file.
        FOR seq_name IN
            SELECT quote_ident(n.nspname) || '.' || quote_ident(s.relname)
              FROM pg_class s
              JOIN pg_namespace n ON n.oid = s.relnamespace
              JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
              JOIN pg_class t ON t.oid = d.refobjid
             WHERE s.relkind = 'S'
               AND t.relname = 'search_embedding_index_run'
        LOOP
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE ' || seq_name || ' TO tyslackai';
        END LOOP;
    END IF;
END $$;

COMMIT;
