-- Hermes 채널 요약 후보와 승인된 파생 요약 (B-37).
-- 원문 아카이브와 검색 색인에는 절대 쓰지 않는다.

BEGIN;

CREATE TABLE IF NOT EXISTS summary_review_cursor (
    workspace       text NOT NULL,
    channel_id      text NOT NULL,
    watermark       text NOT NULL DEFAULT '',
    source_digest   text NOT NULL DEFAULT '',
    failed_digest   text NOT NULL DEFAULT '',
    retry_after     timestamptz,
    checked_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id)
);

CREATE TABLE IF NOT EXISTS summary_review_candidate (
    id              uuid PRIMARY KEY,
    workspace       text NOT NULL,
    channel_id      text NOT NULL,
    channel_name    text NOT NULL DEFAULT '',
    run_date        date NOT NULL,
    source_digest   text NOT NULL,
    kind            text NOT NULL,
    current_text    text NOT NULL DEFAULT '',
    proposed_text   text NOT NULL,
    evidence_quote  text NOT NULL,
    evidence_at     text NOT NULL,
    evidence_author text NOT NULL,
    evidence_locator text NOT NULL,
    state           text NOT NULL DEFAULT 'pending',
    defer_until     date,
    created_at      timestamptz NOT NULL DEFAULT now(),
    decided_at      timestamptz,
    decided_by      text,
    correction      text NOT NULL DEFAULT '',
    CONSTRAINT summary_review_candidate_kind
        CHECK (kind IN ('number_or_schedule', 'new_issue', 'closed_issue')),
    CONSTRAINT summary_review_candidate_state
        CHECK (state IN ('pending', 'approved', 'rejected', 'deferred', 'superseded')),
    CONSTRAINT summary_review_candidate_unique
        UNIQUE (workspace, channel_id, source_digest, kind, proposed_text)
);

CREATE INDEX IF NOT EXISTS summary_review_candidate_pending
    ON summary_review_candidate (workspace, channel_id, state, defer_until, created_at);

-- 승인된 요약은 원문이 아닌 파생 데이터다. 후보와 1:1로 남겨 승인 이력을 덮어쓰지 않는다.
CREATE TABLE IF NOT EXISTS approved_summary_item (
    candidate_id uuid PRIMARY KEY REFERENCES summary_review_candidate(id),
    workspace    text NOT NULL,
    channel_id   text NOT NULL,
    kind         text NOT NULL,
    body          text NOT NULL,
    approved_by   text NOT NULL,
    approved_at   timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz,
    superseded_by uuid REFERENCES summary_review_candidate(id)
);

CREATE INDEX IF NOT EXISTS approved_summary_channel
    ON approved_summary_item (workspace, channel_id, approved_at DESC)
    WHERE superseded_at IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE '
                'summary_review_cursor, summary_review_candidate, approved_summary_item '
                'TO tyslackai';
    END IF;
END
$$;

COMMIT;
