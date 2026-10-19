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

REVOKE ALL ON archiver_vision_policy, archiver_vision_approval, archiver_vision_job FROM PUBLIC;
REVOKE ALL ON archiver_vision_policy_audit FROM PUBLIC;
-- No automatic role creation or collector/Master access. Provision a dedicated
-- service DSN separately before any future worker activation.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_vision') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE archiver_vision_policy,
            archiver_vision_approval, archiver_vision_job TO tybot_vision;
        GRANT SELECT, INSERT ON TABLE archiver_vision_policy_audit TO tybot_vision;
    END IF;
END $$;
COMMIT;
