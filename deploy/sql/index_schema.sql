-- TYBot 인덱서·조직 스키마 (PostgreSQL 16+)
--
-- 적용:  psql -U <사용자> -d tyslackai -f deploy/sql/index_schema.sql
-- 여러 번 실행해도 안전하다(IF NOT EXISTS).
--
-- 먼저 슈퍼유저로 한 번 실행해야 하는 것:
--     sudo -u postgres psql -d tyslackai -c "CREATE EXTENSION pg_bigm;"
-- pg_bigm 은 trusted 확장이 아니라 일반 계정이 만들 수 없다.
--
-- ## 대전제 — DB 는 진실이 아니다
-- 원문의 진실은 `/var/lib/tybot/archive` 의 MD 파일이다. 이 스키마의 `raw_line` 은
-- 매 질문마다 수천 개 MD 를 읽지 않으려고 두는 **캐시**이고, 언제든 MD 에서 재빌드된다.
-- 출처 표기는 항상 `doc_path` + `line_no` 로 MD 를 다시 가리킨다.
--
-- | 성격 | 테이블 | DROP 되면 |
-- |---|---|---|
-- | 재빌드 가능 | raw_line, channel | MD 에서 다시 만든다 |
-- | 원본이 Oracle | org_unit, employee | 다음 동기화에서 복구된다 |
-- | 백업 필수 | user_identity, audit_query, sync_run | 복구 불가 |

BEGIN;

-- ===========================================================================
-- 1. 조직 (Oracle 이 원본, 우리는 읽기 복제)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS org_unit (
    code        text PRIMARY KEY,
    name        text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('hq', 'team', 'site', 'project')),
    parent_code text REFERENCES org_unit(code),
    -- 퇴직자·폐지조직을 **삭제하지 않고** 이 값으로 끈다.
    -- 지우면 권한 판정이 조용히 넓어지거나 좁아진다.
    active      boolean NOT NULL DEFAULT true,
    synced_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT org_unit_no_self_parent CHECK (parent_code IS NULL OR parent_code <> code)
);

-- 계열사 경계. 그룹웨어 확인 결과 조직이 TY(태영건설)·SUB01/03/06·SPC01~24 로 나뉜다.
-- 계열사 자료가 서로 보이면 안 되므로 조직코드만으로 판단하지 않고 이 값으로 한 번 더 막는다.
-- 이미 만들어진 DB 에도 붙도록 ALTER 로 추가한다(여러 번 실행해도 안전).
ALTER TABLE org_unit ADD COLUMN IF NOT EXISTS company_code text;
-- 그룹웨어 원본 경로(`ORGROOT;TY;ABB300;ABB340;`). 분류가 틀렸을 때 되짚는 근거로 남긴다.
ALTER TABLE org_unit ADD COLUMN IF NOT EXISTS org_path text;

CREATE INDEX IF NOT EXISTS org_unit_parent ON org_unit (parent_code);
CREATE INDEX IF NOT EXISTS org_unit_active ON org_unit (active) WHERE active;
CREATE INDEX IF NOT EXISTS org_unit_company ON org_unit (company_code);

COMMENT ON TABLE org_unit IS
    'Oracle V_TYSLACK_ORG 의 복제. 여기서 직접 고치지 않는다 — 다음 동기화에서 덮인다.';
COMMENT ON COLUMN org_unit.kind IS
    '그룹웨어에 구분 컬럼이 없어 이름·코드로 추정한 값이다. 권한 판정은 parent_code 트리로 한다.';

CREATE TABLE IF NOT EXISTS employee (
    emp_no    text PRIMARY KEY,
    name      text NOT NULL,
    email     text,
    org_code  text REFERENCES org_unit(code),
    position  text,
    active    boolean NOT NULL DEFAULT true,
    synced_at timestamptz NOT NULL DEFAULT now()
);

-- 이메일은 대소문자를 구분하지 않는다. Slack 프로필 이메일과 맞추는 열쇠다.
CREATE UNIQUE INDEX IF NOT EXISTS employee_email_key
    ON employee (lower(email)) WHERE email IS NOT NULL;
CREATE INDEX IF NOT EXISTS employee_org ON employee (org_code) WHERE active;

COMMENT ON TABLE employee IS
    'Oracle V_TYSLACK_EMP 의 복제. 비밀번호·휴대폰·주소·생년월일은 뷰에서 제외한다.';

-- Slack 사용자 ↔ 사번. **Oracle 이 아니라 우리가 만드는 데이터라 백업 대상이다.**
CREATE TABLE IF NOT EXISTS user_identity (
    workspace   text NOT NULL,
    slack_user  text NOT NULL,
    emp_no      text REFERENCES employee(emp_no),
    -- 어떻게 확인했는지 남긴다. 'manual' 로 붙인 매핑은 나중에 근거를 물을 수 있어야 한다.
    verified_by text NOT NULL CHECK (verified_by IN ('email_match', 'otp', 'sso', 'manual')),
    verified_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, slack_user)
);

CREATE INDEX IF NOT EXISTS user_identity_emp ON user_identity (emp_no);

-- 동기화 이력. 실패를 추적하려고 남긴다(재빌드 불가).
CREATE TABLE IF NOT EXISTS sync_run (
    id         bigserial PRIMARY KEY,
    source     text NOT NULL CHECK (source IN ('oracle_pull', 'snapshot_push')),
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at   timestamptz,
    ok         boolean,
    org_rows   integer,
    emp_rows   integer,
    message    text
);

CREATE INDEX IF NOT EXISTS sync_run_recent ON sync_run (started_at DESC);

-- ===========================================================================
-- 2. 채널 → 조직 매핑
-- ===========================================================================

CREATE TABLE IF NOT EXISTS channel (
    workspace   text NOT NULL,
    name        text NOT NULL,
    org_code    text REFERENCES org_unit(code),
    -- 이 채널 자료를 어디까지 넘길지. 문서의 share_with 와 별개로 채널 단위 기본값이다.
    share_level text NOT NULL DEFAULT 'org_internal'
                CHECK (share_level IN ('private', 'org_internal', 'company', 'public')),
    -- 채널명에서 조직코드를 못 뽑았을 때의 사유. 사람이 보정할 목록이 된다.
    parse_note  text,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, name)
);

CREATE INDEX IF NOT EXISTS channel_org ON channel (org_code);
-- 조직코드를 못 뽑은 채널 = 사람이 손봐야 하는 목록
CREATE INDEX IF NOT EXISTS channel_unmapped ON channel (workspace) WHERE org_code IS NULL;

-- ===========================================================================
-- 3. 원문 라인 인덱스 (MD 에서 재빌드 가능)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS raw_line (
    id          bigserial PRIMARY KEY,
    workspace   text NOT NULL,
    channel     text NOT NULL,
    -- 출처 표기의 근거. 답변은 항상 이 경로로 MD 를 다시 가리킨다.
    doc_path    text NOT NULL,
    line_no     integer NOT NULL,
    spoken_at   timestamptz NOT NULL,
    speaker     text NOT NULL,
    body        text NOT NULL,
    -- 같은 줄을 다시 색인해도 중복되지 않게 하는 열쇠(멱등 재색인).
    content_sha text NOT NULL,
    indexed_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (doc_path, line_no, content_sha)
);

-- 한국어 부분일치 검색.
--
-- **왜 pg_trgm 이 아니라 pg_bigm 인가** — 실제 DB 실측(2026-08-31, 행 2만,
-- 같은 데이터에 인덱스만 바꿔 끼워 비교. `python scripts/check_search_index.py --compare`):
--
--   검색어      pg_bigm            pg_trgm
--   기성(2자)   인덱스  0.30ms     전체 스캔  8.06ms
--   타설(2자)   인덱스  0.21ms     전체 스캔  8.75ms
--   결재(2자)   인덱스  0.21ms     전체 스캔  7.48ms
--   기성률(3자) 인덱스  0.23ms     인덱스     0.12ms
--   김해외동(4자) 인덱스 0.32ms    인덱스     0.13ms
--
-- pg_trgm 은 세 글자 묶음으로 색인해 **2글자 검색어에 인덱스를 쓰지 못한다.**
-- 우리 쓰임에서 2글자 명사는 흔하다(기성·타설·결재·예산·공정·검측).
-- pg_bigm 은 두 글자 묶음이라 그 구간을 덮는다.
--
-- 3글자 이상에서는 pg_trgm 이 약간 빠르다(0.12 vs 0.23ms). 그 차이는 밀리초 단위이고,
-- 2글자에서 갈리는 차이는 40배다. 그래서 pg_bigm 하나로 간다 — 둘 다 걸면 쓰기 비용만 는다.
--
-- pg_bigm 은 trusted 확장이 아니라 슈퍼유저만 만들 수 있다. 아직 안 만들어졌으면
-- 이 파일 전체가 그 줄에서 멈추므로, 없으면 pg_trgm 으로 **일단 깔고 넘어간다**.
-- 대체 인덱스는 3글자 이상만 덮는다 — 2글자 검색은 여전히 전체 스캔이다.
-- 그래서 임시방편이고, 슈퍼유저로 아래를 실행한 뒤 이 파일을 다시 돌려야 한다.
--     sudo -u postgres psql -d tyslackai -c "CREATE EXTENSION pg_bigm;"
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'pg_bigm') THEN
        -- 대체 인덱스가 남아 있으면 치운다. 둘 다 두면 쓰기 비용만 두 배가 된다.
        DROP INDEX IF EXISTS raw_line_trgm_fallback;
        CREATE INDEX IF NOT EXISTS raw_line_bigm
            ON raw_line USING gin (body gin_bigm_ops);
    ELSE
        CREATE INDEX IF NOT EXISTS raw_line_trgm_fallback
            ON raw_line USING gin (body gin_trgm_ops);
        RAISE WARNING 'pg_bigm 이 없어 pg_trgm 으로 대체했다. 2글자 한국어 검색(기성·타설)은 인덱스를 타지 못한다. 슈퍼유저로 CREATE EXTENSION pg_bigm; 실행 후 이 파일을 다시 적용할 것.';
    END IF;
END $$;

-- 기간 요약은 최근 것부터 훑는다.
CREATE INDEX IF NOT EXISTS raw_line_recent ON raw_line (workspace, channel, spoken_at DESC);
-- 문서 단위 재색인·삭제용
CREATE INDEX IF NOT EXISTS raw_line_doc ON raw_line (doc_path);

COMMENT ON TABLE raw_line IS
    'MD 원문의 캐시. 언제든 재빌드 가능. 출처는 doc_path+line_no 로 MD 를 가리킨다.';

-- ===========================================================================
-- 4. 질의 감사 (재빌드 불가 — 백업 대상)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS audit_query (
    id         bigserial PRIMARY KEY,
    asked_at   timestamptz NOT NULL DEFAULT now(),
    slack_user text NOT NULL,
    workspace  text NOT NULL,
    channel    text NOT NULL,
    question   text NOT NULL,
    -- 권한 판정 결과(허용된 채널 목록). 나중에 "왜 이게 보였나"를 되짚는 근거다.
    scope      jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- 실제 근거로 쓴 doc_path/line_no
    sources    jsonb NOT NULL DEFAULT '[]'::jsonb,
    model      text,
    cost_usd   numeric(10, 5)
);

CREATE INDEX IF NOT EXISTS audit_query_recent ON audit_query (asked_at DESC);
CREATE INDEX IF NOT EXISTS audit_query_user ON audit_query (slack_user, asked_at DESC);

COMMIT;

-- ===========================================================================
-- 조직 트리 조회 — 재귀 CTE
-- ===========================================================================
-- 어떤 조직의 하위 전체(자기 포함). 권한 상속 판정에 쓴다.
--
--   WITH RECURSIVE sub AS (
--     SELECT code FROM org_unit WHERE code = $1 AND active
--     UNION ALL
--     SELECT o.code FROM org_unit o JOIN sub ON o.parent_code = sub.code WHERE o.active
--   )
--   SELECT code FROM sub;
--
-- 상위 전체(자기 포함)는 parent_code 를 거꾸로 타면 된다.
-- **비활성 조직에서 끊는다** — 폐지된 조직 아래를 계속 상속하면 권한이 남는다.
