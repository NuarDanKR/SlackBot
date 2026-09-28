-- Slack 라이선스 현황 — 워크스페이스별 할당 수
--
-- 콘솔 > 운영 > 라이선스 현황 화면이 쓴다.
--
-- ## 무엇을 저장하나
-- **할당 라이선스 수만** 저장한다. Slack 에서 가져올 수 없는 값이라 사람이 적는다.
-- 활성 라이선스 수는 저장하지 않고 조회할 때마다 Slack API(`users.list`)로 센다.
-- 저장해 두면 그 숫자가 언제 것인지 알 수 없게 되고, 화면이 옛 숫자를 오늘 숫자처럼
-- 보여 준다.
--
-- 표에 나오는 워크스페이스는 Slack 연동된 곳뿐이다. 연동이 끊긴 워크스페이스의 행은
-- 남아 있어도 화면에 나오지 않는다.
--
-- ## 왜 workspace(key) 를 외래키로 잡지 않나
-- 레지스트리 이전 워크스페이스는 토큰이 환경변수에만 있고 `workspace` 표에 행이 없다.
-- 그런 워크스페이스도 Slack 에 연동돼 있으므로 할당을 적을 수 있어야 한다.
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

CREATE TABLE IF NOT EXISTS slack_license (
    workspace   text PRIMARY KEY CHECK (workspace ~ '^[a-z][a-z0-9-]{1,23}$'),
    allocated   integer NOT NULL DEFAULT 0 CHECK (allocated >= 0),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL DEFAULT ''
);

COMMENT ON TABLE slack_license IS
    'Slack 라이선스 할당(사람 입력). 활성 수는 Slack API 로 조회하고 저장하지 않는다.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE slack_license TO tyslackai';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_console') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE slack_license TO tybot_console';
    END IF;
END
$$;

COMMIT;
