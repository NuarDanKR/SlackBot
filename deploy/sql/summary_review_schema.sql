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
        CHECK (state IN ('pending', 'approved', 'rejected', 'deferred', 'expired', 'superseded')),
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

-- 기존 설치의 CHECK 제약도 갱신한다. `CREATE TABLE IF NOT EXISTS`만 바꾸면 이미
-- 만들어진 서버는 `expired`를 계속 거부한다.
ALTER TABLE summary_review_candidate
    DROP CONSTRAINT IF EXISTS summary_review_candidate_state;
ALTER TABLE summary_review_candidate
    ADD CONSTRAINT summary_review_candidate_state
    CHECK (state IN ('pending', 'approved', 'rejected', 'deferred', 'expired', 'superseded'));

-- 근거 메시지 좌표 (B-56). Canvas의 후보별 출처 링크가 채널이 아니라 그 메시지를
-- 연다. 좌표를 남기기 전에 수집한 후보는 빈 문자열이고, 그때는 채널 링크로 내려간다.
ALTER TABLE summary_review_candidate
    ADD COLUMN IF NOT EXISTS evidence_message_ts text NOT NULL DEFAULT '';

-- 승인 당시 원문 한 줄의 지문. 인용문 포함 여부만 보면 원문 뒤에 정정 문장이
-- 붙어도 같은 근거로 오인한다. 새 후보는 시각·작성자·전체 본문의 지문을 남기고,
-- 지문이 없는 과거 후보는 검색 길잡이로 승격하지 않는다.
ALTER TABLE summary_review_candidate
    ADD COLUMN IF NOT EXISTS evidence_hash text NOT NULL DEFAULT '';

-- 승인 요약이 가리키는 원문이 사라졌거나 좌표가 어긋난 상태 (B-56).
-- 승인 이력을 지우지 않는다 — 검색 길잡이와 기존 승인 요약에서만 빠진다.
ALTER TABLE approved_summary_item
    ADD COLUMN IF NOT EXISTS stale_at timestamptz;
ALTER TABLE approved_summary_item
    ADD COLUMN IF NOT EXISTS stale_reason text NOT NULL DEFAULT '';

-- 검색 길잡이가 매 질문마다 훑는 범위. 살아 있는 항목만 든다.
CREATE INDEX IF NOT EXISTS approved_summary_guide
    ON approved_summary_item (workspace, approved_at DESC)
    WHERE superseded_at IS NULL AND stale_at IS NULL;

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
