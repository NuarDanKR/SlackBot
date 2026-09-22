-- ===========================================================================
-- PF 운영 콘솔 (`/pf/`) 스키마
-- ===========================================================================
--
-- 설계: docs/design/pf-hermes-owner-plan.md §8, docs/design/pf-console.md
--
-- ## 왜 표가 따로인가
-- `/pf/` 는 프금팀 Hermes 를 보는 화면이고, TYBot 콘솔과 **프로세스·계정·env·쿠키·
-- DB role 이 전부 다르다.** 같은 표에 얹으면 그 분리가 권한 한 줄로 무너진다 —
-- TYBot admin 이 자동으로 PF 를 보게 되고, PF 계정이 TYBot 질문·원문에 닿는다.
--
-- 그래서 **PF 는 자기 표만 본다.** PF DB role 은 아래 표에만 권한이 있고,
-- `workspace` · `usage_call` · `archive_doc` · `specialist_*` 같은 TYBot 표에는
-- 권한이 없다. 권한이 없으면 실수로 짠 쿼리도 실패한다 — 코드 규칙보다 강하다.
--
-- ## 계정은 공유하되 권한은 공유하지 않는다
-- 회사 이메일은 같은 사람을 가리키므로 `console_user` 를 다시 만들지 않는다. 다만
-- **PF 를 볼 수 있는지는 `console_user_service` 행이 정한다.** TYBot 에서 admin 이어도
-- 이 표에 행이 없으면 `/pf/` 는 아무것도 보여 주지 않는다(§8.2).
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh
-- ===========================================================================

-- ---------------------------------------------------------------------------
-- 1. 관리 대상 서비스
-- ---------------------------------------------------------------------------
--
-- 오늘은 `pf-hermes` 한 줄뿐이다. 그래도 표로 두는 이유: API 가 service key 를
-- **body 로 받지 않기** 위해서다(§8.3). 화면이 고를 수 있는 것은 이 표에 있고
-- 그 사람에게 권한이 있는 것뿐이며, 그 밖의 값은 조회 자체가 안 된다.
CREATE TABLE IF NOT EXISTS managed_service (
    key         text PRIMARY KEY
                CHECK (key ~ '^[a-z][a-z0-9-]{1,39}$'),
    label       text NOT NULL,
    -- 'active' 운영 중 · 'shadow' 관찰 중(쓰기 비활성) · 'disabled' 사용 안 함
    state       text NOT NULL DEFAULT 'shadow'
                CHECK (state IN ('active', 'shadow', 'disabled')),
    -- 업무 소유자(프금팀)와 인프라 소유자(우리). §7.1 의 계약을 화면에 띄우기 위한 것.
    business_owner text NOT NULL DEFAULT '',
    infra_owner    text NOT NULL DEFAULT '',
    -- 상태 파일이 있는 곳. 콘솔은 **이 값으로만** 파일을 찾는다. 요청이 경로를
    -- 지정할 수 있으면 그것은 임의 파일 읽기다.
    state_dir   text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE managed_service IS
    'PF 콘솔이 다루는 서비스 allowlist. API 는 이 표에 없는 key 를 받지 않는다.';

INSERT INTO managed_service (key, label, state, state_dir)
VALUES ('pf-hermes', '프금팀 Hermes', 'shadow', '/var/lib/tybot-subbots/pf-hermes')
ON CONFLICT (key) DO NOTHING;   -- 사람이 고친 값을 배포가 되돌리지 않는다

-- ---------------------------------------------------------------------------
-- 2. 서비스 권한 (RBAC)
-- ---------------------------------------------------------------------------
--
-- 역할 네 가지(§8.2).
--   viewer     상태·비용·감사 조회
--   operator   viewer + 운영 action (오늘은 열지 않는다)
--   developer  viewer + release 제출 (오늘은 열지 않는다)
--   approver   viewer + release 승인 (오늘은 열지 않는다)
--
-- **오늘 열리는 것은 조회뿐이다.** 그래도 역할을 지금 넣는 이유: 나중에 열 때
-- 역할을 새로 만들면 그 사이에 들어온 계정이 전부 한 등급 위로 떠 버린다.
--
-- 개발자는 자기 release 를 승인할 수 없다(§8.2). 한 사람이 두 역할을 가질 수는
-- 있지만, 승인 시점에 요청자와 승인자가 같은지는 release 표가 따로 본다.
CREATE TABLE IF NOT EXISTS console_user_service (
    email      text NOT NULL,
    service    text NOT NULL REFERENCES managed_service(key) ON DELETE CASCADE,
    role       text NOT NULL
               CHECK (role IN ('viewer', 'operator', 'developer', 'approver')),
    granted_by text NOT NULL DEFAULT '',
    granted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (email, service, role)
);

CREATE INDEX IF NOT EXISTS console_user_service_by_email
    ON console_user_service (email);

COMMENT ON TABLE console_user_service IS
    'PF 서비스 권한. 이 표에 행이 없으면 TYBot admin 이라도 /pf/ 에서 아무것도 못 본다.';

-- ---------------------------------------------------------------------------
-- 3. 감사 (append only)
-- ---------------------------------------------------------------------------
--
-- 조회만 여는 오늘도 감사를 먼저 만든다. 나중에 action 을 열 때 감사가 같이
-- 만들어지면, 그 사이의 조작은 **아무 데도 안 남는다.**
--
-- 본문·시크릿은 담지 않는다(§10). `detail` 은 사람이 읽을 짧은 문장이고,
-- 값이 필요하면 식별자만 적는다.
CREATE TABLE IF NOT EXISTS pf_audit_event (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      text NOT NULL,
    service    text NOT NULL DEFAULT '',
    action     text NOT NULL,
    outcome    text NOT NULL DEFAULT 'succeeded'
               CHECK (outcome IN ('succeeded', 'failed', 'denied')),
    detail     text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS pf_audit_event_recent ON pf_audit_event (at DESC);
CREATE INDEX IF NOT EXISTS pf_audit_event_by_service
    ON pf_audit_event (service, at DESC);

COMMENT ON TABLE pf_audit_event IS
    '/pf/ 감사. append only — UPDATE·DELETE 권한을 주지 않는다. 본문·시크릿 금지.';

-- ---------------------------------------------------------------------------
-- 4. 권한 — PF role 은 TYBot 표를 못 본다
-- ---------------------------------------------------------------------------
--
-- 역할을 만든 뒤 한 번 실행한다(운영 환경에 맞춰 이름을 바꾼다).
--
--   CREATE ROLE tybot_pf_console LOGIN PASSWORD '...';
--
-- 아래 DO 블록이 권한을 준다. **기본은 아무 권한도 없는 상태**이고, 여기 적힌
-- 표에만 필요한 만큼 준다. PostgreSQL 은 기본적으로 다른 역할의 표를 못 읽으므로
-- 「TYBot 표 권한을 회수한다」 가 아니라 「애초에 주지 않는다」 가 맞다.
--
-- 다만 `PUBLIC` 에 권한이 붙어 있으면 그것으로 새어 나가므로, PF role 을 만들 때
-- 그 표들에 대해 명시적으로 확인한다. `scripts/check_pf_isolation.py` 가 검사한다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_pf_console') THEN
        -- 읽기: 계정 확인에 필요한 최소 열과 PF 자기 표
        EXECUTE 'GRANT SELECT ON TABLE managed_service, console_user_service'
                ' TO tybot_pf_console';
        -- 감사: 넣기만 한다. 고치거나 지울 수 없다.
        EXECUTE 'GRANT SELECT, INSERT ON TABLE pf_audit_event TO tybot_pf_console';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE pf_audit_event_id_seq'
                ' TO tybot_pf_console';

        -- 로그인에 필요한 열만. `console_user` 전체를 주면 TYBot 쪽 역할·워크스페이스
        -- 배정까지 읽힌다 — PF 가 알 필요가 없고, 알면 조직 구조가 드러난다.
        EXECUTE 'GRANT SELECT (email, name, password_hash, active)'
                ' ON TABLE console_user TO tybot_pf_console';
    END IF;
END
$$;

-- 관리 콘솔(TYBot 쪽)에서 PF 권한 행을 관리할 수 있어야 한다 — 누구에게 PF 를
-- 열어 줄지는 인프라 소유자의 일이다. 반대 방향(PF 가 TYBot 을 보는 것)은 없다.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE'
                ' managed_service, console_user_service, pf_audit_event TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE pf_audit_event_id_seq TO tyslackai';
    END IF;
END
$$;
