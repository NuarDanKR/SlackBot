-- 채널 요약 검토자 (B-37)
--
-- 요약은 봇이 **후보만** 만들고 사람이 확정한다. 그 사람을 여기에 둔다.
-- 설계: docs/design/summary-review.md
--
-- **왜 파일이 아니라 DB 인가** — 검토 대상 목록과 결정 이력이 이 값을 참조한다.
-- 채널 소유자와 수정 담당자(`ChannelOwnerStore`)는 현재 JSON 파일에 있다. 검토자는
-- 후보 목록·발송 이력·헬스 체크가 함께 읽고, 여러 프로세스(봇·타이머·콘솔)가 동시에
-- 보므로 먼저 DB에 둔다. 채널 권한 기록도 B-43에서 DB로 이전한다.

BEGIN;

CREATE TABLE IF NOT EXISTS channel_reviewer (
    workspace     text NOT NULL,
    -- Slack 채널 ID. **이름이 아니다** — 채널명은 바뀌고, 바뀌면 검토자가 조용히
    -- 사라진다(`/채널 이름변경` 이 실제로 그렇게 만든다).
    channel_id    text NOT NULL,
    -- 표시용. 이름이 바뀌면 갱신되지만, 판정에 쓰지 않는다.
    channel_name  text NOT NULL DEFAULT '',
    reviewer_user text NOT NULL,
    -- 보낼 시각(KST). 채널 개설자가 정한다.
    send_at       time NOT NULL DEFAULT '08:00',
    -- 사용 중지. 삭제 대신 끈다 — 지우면 언제부터 검토가 멈췄는지 알 수 없다.
    enabled       boolean NOT NULL DEFAULT true,
    set_by        text NOT NULL,
    set_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id, reviewer_user)
);

COMMENT ON TABLE channel_reviewer IS
    '채널별 요약 검토자. 검토자가 없으면 요약을 반영하지 않는다(자동 반영으로 물러서지 않는다).';

-- 발송 시각 훑기. 1분마다 도는 타이머가 "지금 보낼 것" 을 찾는 경로다.
CREATE INDEX IF NOT EXISTS channel_reviewer_due
    ON channel_reviewer (send_at) WHERE enabled;

-- 검토자별 담당 채널. 사람이 그만두면 무엇이 비는지 바로 나온다.
CREATE INDEX IF NOT EXISTS channel_reviewer_person
    ON channel_reviewer (workspace, reviewer_user) WHERE enabled;

-- 권한. 소유자가 다르면 GRANT 는 **자동으로 따라오지 않는다.**
--
-- 표를 `postgres` 로 만들면 소유자도 postgres 가 되고, 봇 역할은 그 표를 못 읽는다.
-- 그런데 `information_schema` 는 권한 필터가 걸린 뷰라서 **표가 아예 안 보이고**,
-- 화면에는 「표가 없다」 는 오류가 나간다. 스키마를 다시 적용해도 달라지지 않는다.
-- `review_digest_sent`(2026-09-11)·`specialist_source`(2026-09-14) 에서 두 번 겪었다.
--
-- 역할이 없는 개발 DB 에서도 적용이 통째로 실패하지 않도록 존재를 확인하고 준다.
DO $$
DECLARE
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE channel_reviewer TO tyslackai';
        -- bigserial 시퀀스도 함께. 없으면 INSERT 만 권한 오류로 죽는다.
        --
        -- `ALL SEQUENCES IN SCHEMA public` 은 쓰지 않는다 — 다른 파일이 만든 남의
        -- 시퀀스까지 건드려서, 적용하는 역할이 그것들의 소유자가 아니면 **파일 전체가
        -- 실패한다.** 이 파일이 만든 표에 딸린 것만 정확히 준다.
        FOR seq_name IN
            SELECT quote_ident(n.nspname) || '.' || quote_ident(s.relname)
              FROM pg_class s
              JOIN pg_namespace n ON n.oid = s.relnamespace
              JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
              JOIN pg_class t ON t.oid = d.refobjid
             WHERE s.relkind = 'S' AND t.relname = ANY(ARRAY['channel_reviewer'])
        LOOP
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE ' || seq_name || ' TO tyslackai';
        END LOOP;
    END IF;
END
$$;

COMMIT;
