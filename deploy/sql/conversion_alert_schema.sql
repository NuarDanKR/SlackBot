-- 첨부 변환 실패·부분 누락 알림의 발송 기록 (B-45 §6)
--
-- 설계: docs/design/operational-warning-recovery-and-answer-progress.md
--
-- **파일명도 본문도 넣지 않는다.** 여기 있는 것은 좌표와 개수, 그리고 같은
-- 상태를 다시 보내지 않기 위한 지문(`dedupe_key`)뿐이다. 알림 기록이 업무
-- 내용의 사본이 되면, 그 표가 새 유출 경로가 된다.
--
-- 적용:
--   sudo cat /opt/tybot/deploy/sql/conversion_alert_schema.sql \
--     | sudo -u postgres psql -p 55432 -d tyslackai -f -

BEGIN;

CREATE TABLE IF NOT EXISTS conversion_alert_sent (
    workspace   text NOT NULL,
    channel_id  text NOT NULL,
    recipient   text NOT NULL,
    -- 파일 목록과 각 파일의 **상태**로 만든 지문. 상태가 바뀌면 새 키가 되어
    -- 다시 알린다 — 실패가 부분 성공이 되거나 파일이 늘면 알릴 값이 있다.
    dedupe_key  text NOT NULL,
    file_count  integer NOT NULL DEFAULT 0,
    sent_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id, recipient, dedupe_key)
);

CREATE INDEX IF NOT EXISTS conversion_alert_sent_recent
    ON conversion_alert_sent (sent_at DESC);

-- 권한. **표만 만들고 GRANT 를 안 주면 봇에게는 그 표가 없는 것과 같다**
-- (2026-09-14 실측). `information_schema` 는 권한 필터가 걸린 뷰라 안 보인다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE'
                ' conversion_alert_sent TO tyslackai';
    END IF;
END
$$;

COMMENT ON TABLE conversion_alert_sent IS
    '변환 알림 발송 기록. 파일명·본문 금지 — 좌표와 개수, 상태 지문만.';

COMMIT;
