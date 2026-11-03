-- 콘솔 계정 자동 관리 — Slack 을 쓰는 재직자에게 게스트 계정을 주고, Slack 이 지워지거나
-- 퇴직하면 계정을 멈춘다.
--
-- 실행: 콘솔 > 사용자 · 봇 권한 > 「계정 동기화」(미리 보기 → 적용), 또는 타이머
--       `tybot-account-sync.timer`(배치 관리에서 켠다).
-- 코드: src/tybot/console/account_sync.py
--
-- ## 규칙
-- - 자동으로 만들고 멈추는 것은 **이 동기화가 만든 계정(`managed_by = 'slack_sync'`)뿐**이다.
--   사람이 「콘솔 사용자 관리」 에서 만든 계정(관리자·개발자 포함)은 건드리지 않고,
--   퇴직자면 경고로만 알린다. 서비스 계정·관리자가 Slack 이 없다는 이유로 잠기면 안 된다.
-- - **지우지 않고 멈춘다**(`active = false`, `stopped_at`, `stop_reason`). 감사 기록이 이메일을
--   가리키고 있고, 다시 Slack 을 쓰면 같은 계정을 되살린다.
-- - 자동 계정은 비밀번호가 없다(`password_hash = ''`). 인증은 빈 해시를 로그인 대상에서
--   뺀다(`auth.load_accounts`). 로그인은 Slack 로그인이 생기면 열린다.
-- - Slack 조회가 실패한 워크스페이스가 하나라도 있으면 그 회차는 **멈추기를 하지 않는다.**
--   못 읽은 것을 「없다」 로 읽으면 멀쩡한 사람이 잠긴다.
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

ALTER TABLE console_user ADD COLUMN IF NOT EXISTS managed_by text
    CHECK (managed_by IS NULL OR managed_by = 'slack_sync');
ALTER TABLE console_user ADD COLUMN IF NOT EXISTS emp_no text;
ALTER TABLE console_user ADD COLUMN IF NOT EXISTS stopped_at timestamptz;
ALTER TABLE console_user ADD COLUMN IF NOT EXISTS stop_reason text
    CHECK (stop_reason IS NULL OR stop_reason IN ('retired', 'slack_gone'));

CREATE INDEX IF NOT EXISTS console_user_managed ON console_user (managed_by)
    WHERE managed_by IS NOT NULL;

-- Slack 계정이 지금 살아 있는가. `user_identity` 는 매핑만 들고 Slack 에서 지워져도
-- 남는다(백업 대상이라 지우지 않는다). 그래서 살아 있는지는 여기 따로 적는다.
CREATE TABLE IF NOT EXISTS slack_presence (
    workspace   text NOT NULL,
    slack_user  text NOT NULL,
    active      boolean NOT NULL,
    checked_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, slack_user)
);

-- 동기화 회차. 무엇을 만들고 멈췄는지 이름 없이 건수와 사번만 남긴다.
CREATE TABLE IF NOT EXISTS account_sync_run (
    id                 bigserial PRIMARY KEY,
    started_at         timestamptz NOT NULL DEFAULT now(),
    actor              text NOT NULL CHECK (btrim(actor) <> ''),
    created            integer NOT NULL DEFAULT 0,
    reactivated        integer NOT NULL DEFAULT 0,
    stopped            integer NOT NULL DEFAULT 0,
    workspaces_changed integer NOT NULL DEFAULT 0,
    linked             integer NOT NULL DEFAULT 0,
    failed_workspaces  text[] NOT NULL DEFAULT '{}',
    stops_skipped      boolean NOT NULL DEFAULT false,
    detail             jsonb NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS account_sync_run_recent ON account_sync_run (started_at DESC);

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE slack_presence TO tyslackai';
        EXECUTE 'GRANT SELECT, INSERT ON TABLE account_sync_run TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE account_sync_run_id_seq TO tyslackai';
    END IF;
END
$$;

COMMIT;
