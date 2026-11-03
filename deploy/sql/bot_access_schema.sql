-- 봇 사용 권한 — 사람(사번) 단위로 「이 봇을 쓸 수 있다」 를 적는 표
--
-- 콘솔 > 사용자 화면에서 관리자가 주고 회수한다. 첫 사용처는 vipbot(임원용)이다.
--
-- ## 규칙
-- - **적힌 사람만 쓴다.** 줄이 없으면 거절이다. 조직·직책으로 넓게 주는 길은 일부러
--   만들지 않았다 — 임원 대상이라 사람 수가 적고, 조직 개편 때 권한이 조용히
--   넓어지거나 좁아지는 일을 막는다.
-- - **지우지 않고 회수한다.** 언제 누가 왜 주고 거뒀는지가 남아야 한다.
-- - 같은 사람·같은 봇에 열린 줄(회수 안 된 줄)은 하나뿐이다. 만료된 열린 줄은
--   다시 줄 때 같은 트랜잭션에서 「만료」 로 회수한다(`console/user_directory.py`).
-- - 판정은 `tybot.bot_access.may_use_bot` 한 곳에서만 한다. 봇마다 따로 만들면
--   규칙이 여러 벌이 되고 한쪽만 느슨해진다.
--
-- ## 사번에 거는 이유
-- Slack 사용자 ID 는 워크스페이스마다 다르다. 한 사람이 7개 워크스페이스에 있어도
-- 권한은 한 번만 주고, 판정할 때 `user_identity` 로 사번을 찾는다. 퇴직(`employee.active`
-- = false)하면 줄이 남아 있어도 판정에서 막힌다.
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

CREATE TABLE IF NOT EXISTS bot_access (
    id             bigserial PRIMARY KEY,
    bot            text NOT NULL CHECK (bot ~ '^[a-z][a-z0-9_-]{1,39}$'),
    emp_no         text NOT NULL REFERENCES employee(emp_no) ON DELETE RESTRICT,
    reason         text NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 500),
    granted_by     text NOT NULL CHECK (btrim(granted_by) <> ''),
    granted_at     timestamptz NOT NULL DEFAULT now(),
    -- NULL 이면 기한 없음.
    expires_at     timestamptz CHECK (expires_at IS NULL OR expires_at > granted_at),
    revoked_at     timestamptz,
    revoked_by     text,
    revoke_reason  text CHECK (revoke_reason IS NULL OR length(revoke_reason) <= 500),
    CONSTRAINT bot_access_revoke_complete CHECK (
        (revoked_at IS NULL AND revoked_by IS NULL AND revoke_reason IS NULL)
        OR (revoked_at IS NOT NULL AND btrim(coalesce(revoked_by, '')) <> ''
            AND btrim(coalesce(revoke_reason, '')) <> '')
    )
);

COMMENT ON TABLE bot_access IS
    '봇 사용 권한(사번 단위). 줄이 없으면 거절. 지우지 않고 revoked_* 로 회수한다.';

-- 열린 줄은 사람·봇마다 하나.
CREATE UNIQUE INDEX IF NOT EXISTS bot_access_one_open
    ON bot_access (bot, emp_no) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS bot_access_emp ON bot_access (emp_no);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE bot_access TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE bot_access_id_seq TO tyslackai';
    END IF;
END
$$;

COMMIT;
