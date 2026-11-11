-- 변환 런타임 정책 — rhwp 앱 ID 와 야간 재점검 숫자의 정본 (B-89 후속)
--
-- 코드: src/tybot/conversion_policy.py
--
-- **행을 미리 넣지 않는다.** 기본값 행이 있으면 「아무도 정하지 않은 값」 이 정본이
-- 된다. 행이 없으면 코드는 위임 상태를 「모른다」 로 두고 HWP 직접 변환과 야간
-- apply 를 막는다. 첫 행은 콘솔 「변환 정책」 에서 사람이 저장한다.
--
-- 시크릿은 없다. Slack 앱 ID(공개 식별자)와 숫자뿐이다.
--
-- 적용: sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

CREATE TABLE IF NOT EXISTS conversion_runtime_policy (
    -- 한 행뿐이다. 워크스페이스별 값이 아니다 — rhwp 앱 ID 는 배포된 앱 하나로 같다.
    id               smallint PRIMARY KEY CHECK (id = 1),
    -- 빈 배열 = 위임 꺼짐(TYBot 이 HWP 를 직접 변환). **행이 없음과 다르다.**
    rhwp_app_ids     text[] NOT NULL
        CHECK (array_position(rhwp_app_ids, NULL) IS NULL
               AND coalesce(array_length(rhwp_app_ids, 1), 0) <= 10),
    recheck_limit    integer NOT NULL CHECK (recheck_limit BETWEEN 0 AND 1000),
    rhwp_grace_hours integer NOT NULL CHECK (rhwp_grace_hours BETWEEN 1 AND 168),
    revision         bigint NOT NULL CHECK (revision > 0),
    updated_by       text NOT NULL CHECK (updated_by <> ''),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- append-only. 바꿀 때마다 한 행. 값·직전 값·실행자·사유만 담는다.
CREATE TABLE IF NOT EXISTS conversion_runtime_policy_audit (
    id               bigserial PRIMARY KEY,
    revision         bigint NOT NULL UNIQUE CHECK (revision > 0),
    rhwp_app_ids     text[] NOT NULL,
    recheck_limit    integer NOT NULL,
    rhwp_grace_hours integer NOT NULL,
    previous         jsonb CHECK (previous IS NULL OR jsonb_typeof(previous) = 'object'),
    updated_by       text NOT NULL CHECK (updated_by <> ''),
    reason           text NOT NULL DEFAULT '',
    recorded_at      timestamptz NOT NULL DEFAULT now()
);

REVOKE ALL ON conversion_runtime_policy, conversion_runtime_policy_audit FROM PUBLIC;

-- 권한. **표만 만들고 GRANT 를 안 주면 봇에게는 그 표가 없는 것과 같다**
-- (2026-09-14 실측). 그러면 코드는 `table_missing` 으로 읽고 위임 상태를 모른다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        -- 콘솔·봇·변환 잡. 감사 표는 INSERT 만 — UPDATE·DELETE 가 있으면 감사가 아니다.
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE conversion_runtime_policy TO tyslackai';
        EXECUTE 'GRANT SELECT, INSERT ON TABLE conversion_runtime_policy_audit TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE conversion_runtime_policy_audit_id_seq TO tyslackai';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_archiver') THEN
        -- 수집기는 rhwp PDF 를 받을지만 판단한다. 읽기만.
        EXECUTE 'GRANT SELECT ON TABLE conversion_runtime_policy TO tybot_archiver';
    END IF;
END
$$;

COMMENT ON TABLE conversion_runtime_policy IS
    '변환 런타임 정책 정본(B-89). 행 없음 = 모름(HWP 직접 변환·야간 apply 차단). 빈 배열 = 위임 꺼짐.';
COMMENT ON TABLE conversion_runtime_policy_audit IS
    '변환 정책 변경 감사(append-only). 값·직전 값·실행자·사유만.';

COMMIT;
