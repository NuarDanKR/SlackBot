-- 콘솔 임시 비밀번호 — TYBot DM 으로 발급하고, 첫 로그인에서 바꾸게 한다.
--
-- 코드: src/tybot/console/password_issue.py
--
-- ## 규칙
-- - 임시 비밀번호는 **해시만** 저장한다(`console_user.password_hash`). 원문은 DM 한 번뿐이다.
-- - `must_change_password` 인 계정은 비밀번호 로그인으로 세션을 받지 못한다. 먼저 바꿔야 한다.
-- - 임시 비밀번호는 72시간, 5번 틀리면 즉시 무효. 다시 받으려면 관리자가 재발급한다.
-- - 발급은 관리자가 버튼을 눌렀을 때만 한다. 계정 동기화 타이머는 발급하지 않는다.
-- - 비밀번호를 바꾸거나 기한이 지나면 TYBot 이 그 DM 을 Slack 에서 지운다(`dm_deleted_at`).
--   봇 메시지라 아카이브에는 원래 실리지 않지만(원칙 1), Slack 에도 남기지 않는다.
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

ALTER TABLE console_user ADD COLUMN IF NOT EXISTS must_change_password boolean NOT NULL DEFAULT false;
ALTER TABLE console_user ADD COLUMN IF NOT EXISTS temp_expires_at timestamptz;
ALTER TABLE console_user ADD COLUMN IF NOT EXISTS temp_failed integer NOT NULL DEFAULT 0
    CHECK (temp_failed >= 0);
ALTER TABLE console_user ADD COLUMN IF NOT EXISTS password_changed_at timestamptz;

-- 발급 기록. 비밀번호 원문은 없다 — 누가 언제 누구에게 어디로 보냈는지만.
CREATE TABLE IF NOT EXISTS console_password_issue (
    id            bigserial PRIMARY KEY,
    email         text NOT NULL REFERENCES console_user(email) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('initial', 'reset')),
    issued_by     text NOT NULL CHECK (btrim(issued_by) <> ''),
    issued_at     timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    workspace     text NOT NULL,
    dm_channel    text NOT NULL,
    dm_ts         text NOT NULL,
    -- 바꿨거나(changed) 기한이 지났거나(expired) 다시 발급해(replaced) 더 쓰지 않는다.
    closed_at     timestamptz,
    close_reason  text CHECK (close_reason IS NULL OR close_reason IN
                              ('changed', 'expired', 'replaced', 'locked')),
    dm_deleted_at timestamptz,
    dm_delete_error text
);

CREATE INDEX IF NOT EXISTS console_password_issue_open
    ON console_password_issue (email) WHERE closed_at IS NULL;
CREATE INDEX IF NOT EXISTS console_password_issue_dm_pending
    ON console_password_issue (closed_at) WHERE closed_at IS NOT NULL AND dm_deleted_at IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE console_password_issue TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE console_password_issue_id_seq TO tyslackai';
    END IF;
END
$$;

COMMIT;
