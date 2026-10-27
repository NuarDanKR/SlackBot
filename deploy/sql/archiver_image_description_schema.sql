-- B-81. Dormant image-description policy/catalog/dispatch/derived-index ledger.
-- Applying this file does NOT activate a model or grant the collector/Master access.
BEGIN;

CREATE TABLE IF NOT EXISTS archiver_image_model_catalog (
    provider text NOT NULL CHECK (provider <> ''),
    model text NOT NULL CHECK (model <> ''),
    supports_vision boolean NOT NULL DEFAULT false,
    zero_cost_verified boolean NOT NULL DEFAULT false,
    verified_at timestamptz,
    verification_revision text NOT NULL DEFAULT '',
    max_sensitivity text NOT NULL CHECK (max_sensitivity IN ('public','internal','confidential')),
    allowed_regions text[] NOT NULL DEFAULT '{}',
    credential_ready boolean NOT NULL DEFAULT false,
    quota_state text NOT NULL DEFAULT 'unknown'
        CHECK (quota_state IN ('available','exhausted','unlimited','unknown')),
    quota_remaining bigint CHECK (quota_remaining IS NULL OR quota_remaining >= 0),
    quota_resets_at timestamptz,
    updated_by text NOT NULL CHECK (updated_by <> ''),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider, model),
    CHECK (NOT zero_cost_verified OR
           (verified_at IS NOT NULL AND verification_revision <> '')),
    CHECK ((quota_state = 'available' AND quota_remaining IS NOT NULL
                                      AND quota_remaining > 0) OR
           (quota_state = 'exhausted' AND quota_remaining = 0) OR
           (quota_state IN ('unlimited','unknown') AND quota_remaining IS NULL))
);

CREATE TABLE IF NOT EXISTS archiver_image_description_policy (
    workspace text PRIMARY KEY,
    revision text NOT NULL,
    enabled boolean NOT NULL DEFAULT false,
    config jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(config) = 'object'),
    updated_by text NOT NULL CHECK (updated_by <> ''),
    updated_at timestamptz NOT NULL DEFAULT now()
);
-- All mutations lock the global row first. Empty config + OFF is intentionally unusable.
INSERT INTO archiver_image_description_policy(workspace, revision, updated_by)
VALUES ('', 'initial', 'schema') ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS archiver_image_description_policy_audit (
    revision text PRIMARY KEY,
    workspace text NOT NULL,
    enabled boolean NOT NULL,
    config jsonb NOT NULL CHECK (jsonb_typeof(config) = 'object'),
    updated_by text NOT NULL CHECK (updated_by <> ''),
    recorded_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS archiver_image_description_approval (
    id text PRIMARY KEY,
    workspace text NOT NULL CHECK (workspace <> ''),
    scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'object'),
    expires_at timestamptz NOT NULL,
    approved_by text NOT NULL CHECK (approved_by <> ''),
    revoked_at timestamptz,
    revoked_by text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS archiver_image_description_job (
    id text PRIMARY KEY,
    identity_sha256 text NOT NULL UNIQUE CHECK (identity_sha256 ~ '^[a-f0-9]{64}$'),
    approval_id text NOT NULL REFERENCES archiver_image_description_approval(id),
    workspace text NOT NULL CHECK (workspace <> ''),
    policy_revision text NOT NULL CHECK (policy_revision <> ''),
    prompt_revision text NOT NULL CHECK (prompt_revision <> ''),
    scope jsonb NOT NULL CHECK (jsonb_typeof(scope) = 'object'),
    policy_snapshot jsonb NOT NULL CHECK (jsonb_typeof(policy_snapshot) = 'object'),
    catalog_snapshot jsonb NOT NULL CHECK (jsonb_typeof(catalog_snapshot) = 'object'),
    max_output_tokens integer NOT NULL CHECK (max_output_tokens > 0),
    state text NOT NULL CHECK (state IN (
        'reserved','dispatching','described','uncertain','failed','unknown','cancelled','blocked'
    )),
    error_code text NOT NULL DEFAULT '',
    actual_model text,
    actual_provider text,
    input_tokens integer CHECK (input_tokens IS NULL OR input_tokens >= 0),
    output_tokens integer CHECK (output_tokens IS NULL OR output_tokens >= 0),
    actual_usd numeric CHECK (actual_usd >= 0 AND actual_usd < 'Infinity'::numeric),
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    dispatched_at timestamptz,
    finished_at timestamptz,
    CHECK (state NOT IN ('dispatching','described','uncertain','failed','unknown')
           OR dispatched_at IS NOT NULL),
    CHECK (state NOT IN ('described','uncertain','failed','unknown','cancelled','blocked')
           OR finished_at IS NOT NULL),
    CHECK ((actual_model IS NULL) = (actual_provider IS NULL))
);
CREATE INDEX IF NOT EXISTS archiver_image_description_job_status
    ON archiver_image_description_job(workspace, state, created_at);

CREATE TABLE IF NOT EXISTS archiver_image_description_revision (
    id text PRIMARY KEY,
    identity_sha256 text NOT NULL UNIQUE CHECK (identity_sha256 ~ '^[a-f0-9]{64}$'),
    job_id text NOT NULL UNIQUE REFERENCES archiver_image_description_job(id),
    workspace text NOT NULL CHECK (workspace <> ''),
    channel_id text NOT NULL CHECK (channel_id <> ''),
    file_id text NOT NULL CHECK (file_id <> ''),
    original_sha256 text NOT NULL CHECK (original_sha256 ~ '^[a-f0-9]{64}$'),
    image_sha256 text NOT NULL CHECK (image_sha256 ~ '^[a-f0-9]{64}$'),
    prompt_revision text NOT NULL CHECK (prompt_revision <> ''),
    model text NOT NULL CHECK (model <> ''),
    provider text NOT NULL CHECK (provider <> ''),
    status text NOT NULL CHECK (status IN ('described','uncertain')),
    guard_verdict text NOT NULL CHECK (guard_verdict IN ('allow','withhold_description')),
    guard_codes text[] NOT NULL DEFAULT '{}',
    label text NOT NULL DEFAULT '[AI 이미지 설명·검색용]'
        CHECK (label = '[AI 이미지 설명·검색용]'),
    description text NOT NULL CHECK (char_length(description) <= 200),
    tags text[] NOT NULL DEFAULT '{}',
    uncertainty text[] NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((guard_verdict = 'allow' AND description <> '') OR
           (guard_verdict = 'withhold_description' AND description = '')),
    CHECK (guard_verdict <> 'withhold_description' OR status = 'uncertain')
);
CREATE INDEX IF NOT EXISTS archiver_image_description_revision_source
    ON archiver_image_description_revision(workspace, channel_id, file_id, created_at);

REVOKE ALL ON archiver_image_model_catalog,
    archiver_image_description_policy, archiver_image_description_policy_audit,
    archiver_image_description_approval, archiver_image_description_job,
    archiver_image_description_revision FROM PUBLIC;

-- Optional Vision service role shared with B-80. Policies/tables remain purpose-specific.
-- Never create roles or grant Master/collector here.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_vision') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE archiver_image_model_catalog,
            archiver_image_description_policy, archiver_image_description_approval,
            archiver_image_description_job TO tybot_vision;
        GRANT SELECT, INSERT ON TABLE archiver_image_description_policy_audit,
            archiver_image_description_revision TO tybot_vision;
    END IF;
END $$;
COMMIT;
