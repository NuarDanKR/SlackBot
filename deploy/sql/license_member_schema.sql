-- 라이선스 보유자 명단(직접 입력) · 회수 기록
--
-- 콘솔 > 운영 > 라이선스 현황에서 워크스페이스 줄을 펼쳐 사번으로 추가·삭제한다.
--
-- ## 언제 쓰나
-- - Slack 연동 워크스페이스인데 **봇 토큰이 없는** 곳(`workspace` 열)
-- - Slack 에 연동되지 않아 직접 추가한 워크스페이스(`manual_id` 열 → `slack_license_manual`)
-- 봇 토큰이 있는 워크스페이스는 Slack `users.list` 가 명단의 정답이므로 여기에 적지 않는다
-- (서버가 거절한다). 두 출처가 섞이면 어느 쪽이 맞는지 아무도 모른다.
--
-- 명단이 한 명이라도 있으면 그 워크스페이스의 활성 수는 **명단 인원 수**로 센다.
--
-- ## 사번으로 건다
-- 사람은 사번으로 적는다(`employee` 외래키). 이름·조직은 조회 때 `employee` 에서 읽는다 —
-- 적어 두면 인사이동을 따라가지 못한다. 직접 추가한 워크스페이스를 지우면 명단도 지워진다.
--
-- ## 회수 기록 (`license_release`)
-- 「이 사람 라이선스를 뺀다」 를 사유와 함께 남긴다. 지우지 않는다.
-- - 직접 입력 명단: 기록을 남기고 명단에서 뺀다(활성 수가 바로 준다)
-- - Slack 명단: 콘솔은 Slack 계정을 끄지 못한다. 기록만 남기고, Slack 에서 실제로 비활성화될
--   때까지 화면에 「Slack 비활성화 대기」 로 보인다. 사라지면 「완료」 다
-- 이름·사번·워크스페이스 이름은 기록 시점의 사본이다 — 직원·워크스페이스가 지워져도 남는다.
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

CREATE TABLE IF NOT EXISTS license_member (
    id          bigserial PRIMARY KEY,
    workspace   text CHECK (workspace ~ '^[a-z][a-z0-9-]{1,23}$'),
    manual_id   bigint REFERENCES slack_license_manual(id) ON DELETE CASCADE,
    emp_no      text NOT NULL REFERENCES employee(emp_no) ON DELETE RESTRICT,
    added_by    text NOT NULL CHECK (btrim(added_by) <> ''),
    added_at    timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT license_member_one_target CHECK ((workspace IS NULL) <> (manual_id IS NULL))
);

COMMENT ON TABLE license_member IS
    '봇 토큰이 없거나 직접 추가한 워크스페이스의 라이선스 보유자(사번). 있으면 활성 수 = 인원 수.';

CREATE UNIQUE INDEX IF NOT EXISTS license_member_ws_emp
    ON license_member (workspace, emp_no) WHERE workspace IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS license_member_manual_emp
    ON license_member (manual_id, emp_no) WHERE manual_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS license_release (
    id            bigserial PRIMARY KEY,
    uid           text NOT NULL CHECK (uid ~ '^(ws:[a-z][a-z0-9-]{1,23}|id:[0-9]+)$'),
    label         text NOT NULL DEFAULT '',
    source        text NOT NULL CHECK (source IN ('slack', 'manual')),
    slack_user    text,
    emp_no        text,
    person_name   text NOT NULL DEFAULT '',
    org_name      text NOT NULL DEFAULT '',
    reason        text NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 500),
    released_by   text NOT NULL CHECK (btrim(released_by) <> ''),
    released_at   timestamptz NOT NULL DEFAULT now(),
    cancelled_at  timestamptz,
    cancelled_by  text,
    CONSTRAINT license_release_who CHECK (slack_user IS NOT NULL OR emp_no IS NOT NULL),
    CONSTRAINT license_release_cancel CHECK ((cancelled_at IS NULL) = (cancelled_by IS NULL))
);

COMMENT ON TABLE license_release IS
    '라이선스 회수 기록(사유·처리자). 지우지 않는다. Slack 명단은 기록만 — 계정 비활성화는 Slack 에서.';

-- Slack 계정 하나에 열린 회수 기록은 하나다.
CREATE UNIQUE INDEX IF NOT EXISTS license_release_one_open
    ON license_release (uid, slack_user) WHERE slack_user IS NOT NULL AND cancelled_at IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, DELETE ON TABLE license_member TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE license_member_id_seq TO tyslackai';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE license_release TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE license_release_id_seq TO tyslackai';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_console') THEN
        EXECUTE 'GRANT SELECT, INSERT, DELETE ON TABLE license_member TO tybot_console';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE license_member_id_seq TO tybot_console';
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE license_release TO tybot_console';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE license_release_id_seq TO tybot_console';
    END IF;
END
$$;

COMMIT;
