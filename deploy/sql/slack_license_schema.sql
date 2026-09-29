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
-- 연동 워크스페이스의 행은 연동이 끊기면 남아 있어도 화면에 나오지 않는다.
--
-- ## 직접 추가한 워크스페이스 (`slack_license_manual`)
-- 아직 Slack 에 연동되지 않은 워크스페이스는 사람이 화면에서 추가한다. 활성 수를 Slack 에서
-- 읽을 수 없으므로 할당과 활성을 **둘 다** 사람이 적는다. 삭제할 수 있는 것도 이 표의 행뿐이다.
--
-- 연동 표(`slack_license`)와 나눈 이유: 그 표는 이미 배포됐고 키가 워크스페이스 키다.
-- 키가 없는 행을 섞으려면 기존 표의 제약을 바꿔야 하는데, 이 파일은 `CREATE ... IF NOT
-- EXISTS` 라 **이미 만들어진 표에는 바뀐 정의가 적용되지 않는다.** 새 표는 그냥 생긴다.
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

CREATE TABLE IF NOT EXISTS slack_license_manual (
    id          bigserial PRIMARY KEY,
    label       text NOT NULL UNIQUE CHECK (label <> '' AND length(label) <= 80),
    allocated   integer NOT NULL DEFAULT 0 CHECK (allocated >= 0),
    active      integer NOT NULL DEFAULT 0 CHECK (active >= 0),
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text NOT NULL DEFAULT '',
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL DEFAULT ''
);

COMMENT ON TABLE slack_license_manual IS
    'Slack 에 연동되지 않아 사람이 추가한 워크스페이스. 할당·활성 모두 사람 입력.';

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE slack_license TO tyslackai';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE slack_license_manual TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE slack_license_manual_id_seq TO tyslackai';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_console') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE slack_license TO tybot_console';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE slack_license_manual TO tybot_console';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE slack_license_manual_id_seq TO tybot_console';
    END IF;
END
$$;

COMMIT;
