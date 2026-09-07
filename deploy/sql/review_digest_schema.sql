-- 검토 하루치 발송 이력 (B-37 3단계 · 첨부 검수)
--
-- 설계: docs/design/summary-review.md
--
-- **왜 필요한가** — 타이머는 1분마다 돈다. 어디까지 보냈는지 남기지 않으면 같은
-- 사람에게 같은 목록이 하루 종일 간다. 그러면 사람이 그 DM 을 끈다.
--
-- **왜 이 키인가** — `(워크스페이스, 채널, 받는 사람, 날짜, 종류)`.
-- 받는 사람을 키에 넣는 이유는 한 채널에 검토자가 여럿일 수 있어서고,
-- 종류를 넣는 이유는 첨부 검수와 요약 후보가 **같은 시각에 각각** 가기 때문이다.
-- 요약 후보 생성(2단계)이 생기면 `kind='summary'` 로 이 테이블에 얹는다 —
-- 각자 이력 테이블을 기르면 검토자가 하루에 DM 을 두 번 받는 것을 아무도 못 잡는다.
--
-- **파일명·본문은 넣지 않는다.** 남기는 것은 식별자·건수·시각이다.

BEGIN;

CREATE TABLE IF NOT EXISTS review_digest_sent (
    workspace    text NOT NULL,
    -- Slack 채널 ID. 이름이 아니다 — 이름은 바뀐다.
    channel_id   text NOT NULL,
    -- 받는 Slack 사용자 ID.
    recipient    text NOT NULL,
    -- 어느 날 몫인가(KST 기준 날짜).
    digest_date  date NOT NULL,
    -- 'attachment' | 'summary'
    kind         text NOT NULL,
    -- 그날 건별로 보인 건수. 밀린 건수는 담지 않는다(그날의 사실이 아니다).
    item_count   integer NOT NULL DEFAULT 0,
    sent_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id, recipient, digest_date, kind),
    CONSTRAINT review_digest_kind CHECK (kind IN ('attachment', 'summary'))
);

COMMENT ON TABLE review_digest_sent IS
    '검토자에게 하루치를 보낸 이력. 하루에 한 번을 보장하는 멱등 키다.';

-- "어제 검토 DM 이 나갔나" 를 채널 없이 묻는 경로(헬스 체크·콘솔).
CREATE INDEX IF NOT EXISTS review_digest_by_date
    ON review_digest_sent (digest_date, kind);

COMMIT;
