-- 요약 검토 Canvas 회차(B-50). 설계: docs/design/summary-review-canvas.md §4
--
-- Canvas 는 Slack API 로 만들고 상태는 여기 남긴다. **두 곳이 하나의 트랜잭션이
-- 아니다.** 그래서 Artifact 행을 먼저 잡고 Canvas 를 만든다 — 순서를 뒤집으면
-- API 가 만들고 응답만 유실됐을 때 같은 회차의 Canvas 가 두 개 생긴다.
--
-- Canvas 본문은 여기 복제하지 않는다. 본문은 Slack 에 있고, 여기에는 좌표만 둔다.

BEGIN;

CREATE TABLE IF NOT EXISTS summary_review_artifact (
    id               uuid PRIMARY KEY,
    workspace        text NOT NULL,
    channel_id       text NOT NULL,
    channel_name     text NOT NULL DEFAULT '',
    review_date      date NOT NULL,
    source_digest    text NOT NULL,
    state            text NOT NULL DEFAULT 'creating',
    canvas_id        text,
    -- Slack 내부 링크. DB 에는 둬도 되지만 **로그·감사에는 남기지 않는다.**
    canvas_permalink text,
    content_hash     text NOT NULL,
    error_code       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    ready_at         timestamptz,
    decided_at       timestamptz,
    CONSTRAINT summary_review_artifact_state
        CHECK (state IN ('creating', 'ready', 'partial', 'completed', 'failed', 'ambiguous')),
    -- 같은 회차를 두 번 만들지 않는다. 재실행의 멱등성이 이 제약 하나에 걸려 있다.
    CONSTRAINT summary_review_artifact_round
        UNIQUE (workspace, channel_id, review_date, source_digest)
);

CREATE INDEX IF NOT EXISTS summary_review_artifact_open
    ON summary_review_artifact (workspace, state, created_at DESC);

-- Canvas 의 **번호와 후보를 고정한다.** `pending()` 을 다시 조회한 순서에 기대면
-- 재시도 때 3번이 다른 후보가 되고, 사람이 "3번 틀렸다" 고 한 것이 엉뚱한 후보에
-- 적용된다.
CREATE TABLE IF NOT EXISTS summary_review_artifact_candidate (
    artifact_id  uuid NOT NULL REFERENCES summary_review_artifact(id),
    candidate_id uuid NOT NULL REFERENCES summary_review_candidate(id),
    position     integer NOT NULL,
    PRIMARY KEY (artifact_id, candidate_id),
    CONSTRAINT summary_review_artifact_position UNIQUE (artifact_id, position)
);

-- 결정 권한의 근거. 날짜 단위 발송 이력(`review_digest_sent`)이 아니라 **이 회차에
-- 실제로 보냈는가**로 본다. 이력은 날짜만 알고 어느 Canvas 였는지 모른다.
CREATE TABLE IF NOT EXISTS summary_review_delivery (
    artifact_id uuid NOT NULL REFERENCES summary_review_artifact(id),
    recipient   text NOT NULL,
    dm_channel  text,
    message_ts  text,
    state       text NOT NULL DEFAULT 'pending',
    sent_at     timestamptz,
    error_code  text,
    PRIMARY KEY (artifact_id, recipient),
    CONSTRAINT summary_review_delivery_state
        CHECK (state IN ('pending', 'sent', 'failed'))
);

CREATE INDEX IF NOT EXISTS summary_review_delivery_recipient
    ON summary_review_delivery (recipient, state);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE '
                'summary_review_artifact, summary_review_artifact_candidate, '
                'summary_review_delivery TO tyslackai';
    END IF;
END
$$;

COMMIT;
