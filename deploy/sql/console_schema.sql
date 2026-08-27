-- TYBot 관리 콘솔 스키마 (PostgreSQL 16)
--
-- 적용:  psql -U tybot -d tybot -f deploy/sql/console_schema.sql
-- 이 파일은 여러 번 실행해도 안전하다(IF NOT EXISTS).
--
-- ## 대전제 — DB 는 진실이 아니다
-- 원문의 진실은 `/var/lib/tybot/archive` 의 MD 파일이다. DB 는 매 질문마다 수천 개 MD 를
-- 읽지 않으려고 두는 파생물이다(`docs/design/db-and-acl.md`).
--
-- | 성격 | 테이블 | DROP 되면 |
-- |---|---|---|
-- | 재빌드 가능 | archive_doc, archive_course, usage_daily | MD·감사기록에서 다시 만든다 |
-- | 백업 필수 | workspace, workspace_secret, harness_*, deploy_*, archive_read_audit, anomaly | 복구 불가 |
--
-- 재빌드 가능한 테이블에는 사람이 만든 정보를 넣지 않는다. 넣는 순간 이 성질이 깨진다.

BEGIN;

-- ===========================================================================
-- 1. 워크스페이스 등록 (`.env` 편집을 대체한다)
-- ===========================================================================

-- 키는 아카이브 디렉터리 이름과 문서 프론트매터에 함께 쓰인다. 그래서 바꿀 수 없다.
-- 정규식은 콘솔 화면의 검사와 같은 규칙이다.
CREATE TABLE IF NOT EXISTS workspace (
    key           text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9-]{1,23}$'),
    label         text NOT NULL CHECK (label <> ''),
    -- root: 산하 워크스페이스 자료를 공유 표시와 무관하게 열람하고,
    --       자기 워크스페이스 안에서 채널 멤버십 필터를 받지 않는다.
    role          text NOT NULL DEFAULT 'member' CHECK (role IN ('root', 'member')),
    -- 등록 오류는 이 워크스페이스만 멈춘다. 다른 봇은 계속 뜬다.
    state         text NOT NULL DEFAULT 'enabled'
                  CHECK (state IN ('enabled', 'disabled', 'error')),
    error         text,
    -- 워크스페이스별 일 사용 상한. 전체 합산 상한과 별도로 이 값이 먼저 걸린다.
    limit_usd     numeric(10, 2) NOT NULL DEFAULT 2 CHECK (limit_usd >= 0),
    archive_path  text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    created_by    text NOT NULL,
    CONSTRAINT error_only_when_error CHECK (state = 'error' OR error IS NULL)
);

-- 크로스 워크스페이스 열람 화이트리스트(단방향). 이건 '넘어갈 수 있는 후보'만 정한다.
-- 실제로 무엇이 넘어가는지는 읽는 쪽이 root 인지, 문서에 share_with 가 있는지로 갈린다.
CREATE TABLE IF NOT EXISTS workspace_readable (
    reader  text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    target  text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    PRIMARY KEY (reader, target),
    CONSTRAINT no_self_read CHECK (reader <> target)
);

-- 시크릿은 암호화해서만 담는다. **복호화 조회 API 는 만들지 않는다.**
-- 봇 프로세스가 기동 시 한 번 복호화해 메모리에 들고, 콘솔은 mask 만 읽는다.
CREATE TABLE IF NOT EXISTS workspace_secret (
    workspace   text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    kind        text NOT NULL CHECK (kind IN ('bot', 'app')),
    ciphertext  bytea NOT NULL,
    -- 화면에 보여 줄 가린 값. 예: xoxb-4821…9f0c
    mask        text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    PRIMARY KEY (workspace, kind)
);

COMMENT ON TABLE workspace_secret IS
    '봇/앱 토큰. 평문 저장 금지, 복호화 조회 API 금지. 콘솔은 mask 만 읽는다.';

-- ===========================================================================
-- 2. 콘솔 사용자와 권한
-- ===========================================================================

-- 접속 경로와 무관하게 사용자 식별은 필요하다 — 누가 승인했는지 남겨야 하기 때문이다.
CREATE TABLE IF NOT EXISTS console_user (
    email       text PRIMARY KEY,
    name        text NOT NULL,
    -- owner 만 승인할 수 있다. member 는 요청만 올린다.
    role        text NOT NULL DEFAULT 'member' CHECK (role IN ('owner', 'member')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    last_seen   timestamptz
);

-- member 가 다룰 수 있는 워크스페이스. owner 는 이 표와 무관하게 전체를 본다.
CREATE TABLE IF NOT EXISTS console_user_workspace (
    email      text NOT NULL REFERENCES console_user(email) ON DELETE CASCADE,
    workspace  text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    PRIMARY KEY (email, workspace)
);

-- ===========================================================================
-- 3. 봇 하네싱 (규칙 MD)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS harness_file (
    id          bigserial PRIMARY KEY,
    workspace   text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    path        text NOT NULL,
    title       text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('rules', 'workflow', 'glossary', 'prompt')),
    content     text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    UNIQUE (workspace, path)
);

-- 반영 전 이전 내용을 남긴다. 되돌리기가 '이력에서 골라 다시 반영'으로 끝나게 하려고.
CREATE TABLE IF NOT EXISTS harness_version (
    id           bigserial PRIMARY KEY,
    file_id      bigint NOT NULL REFERENCES harness_file(id) ON DELETE CASCADE,
    content      text NOT NULL,
    replaced_at  timestamptz NOT NULL DEFAULT now(),
    replaced_by  text NOT NULL
);

CREATE TABLE IF NOT EXISTS harness_request (
    id             bigserial PRIMARY KEY,
    workspace      text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    path           text NOT NULL,
    requester      text NOT NULL,
    requested_at   timestamptz NOT NULL DEFAULT now(),
    -- 요청자가 직접 쓴 변경 이유. 승인자가 판단할 근거이므로 빈 값을 막는다.
    reason         text NOT NULL CHECK (length(btrim(reason)) >= 5),
    before_content text NOT NULL,
    after_content  text NOT NULL,
    added          integer NOT NULL DEFAULT 0,
    removed        integer NOT NULL DEFAULT 0,
    -- [{"id":"schema","label":"문서 형식","state":"pass","detail":"..."}, ...]
    checks         jsonb NOT NULL DEFAULT '[]'::jsonb,
    state          text NOT NULL DEFAULT 'awaiting_checks'
                   CHECK (state IN ('awaiting_checks', 'awaiting_approval', 'blocked',
                                    'approved', 'rejected')),
    approver       text,
    decided_at     timestamptz,
    CONSTRAINT approver_only_when_decided
        CHECK ((state IN ('approved', 'rejected')) = (approver IS NOT NULL))
);

-- 한 파일에 대기 중인 편집은 하나만. 두 사람이 같은 파일을 고치면 나중 것이 앞선 것을 덮는다.
CREATE UNIQUE INDEX IF NOT EXISTS harness_request_one_pending
    ON harness_request (workspace, path)
    WHERE state IN ('awaiting_checks', 'awaiting_approval', 'blocked');

-- ===========================================================================
-- 4. 배포 승인
-- ===========================================================================

CREATE TABLE IF NOT EXISTS deploy_request (
    id                  bigserial PRIMARY KEY,
    workspace           text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    requester           text NOT NULL,
    requested_at        timestamptz NOT NULL DEFAULT now(),
    repo                text NOT NULL,
    branch              text NOT NULL,
    commit_sha          text NOT NULL,
    commit_title        text NOT NULL,
    author              text NOT NULL,
    -- fast-forward 가 아니면 반영하지 않는다(force·rebase 금지).
    fast_forward        boolean NOT NULL,
    -- [{"path":"src/...","added":34,"removed":6}, ...]
    files               jsonb NOT NULL DEFAULT '[]'::jsonb,
    checks              jsonb NOT NULL DEFAULT '[]'::jsonb,
    state               text NOT NULL DEFAULT 'awaiting_checks'
                        CHECK (state IN ('awaiting_checks', 'awaiting_approval', 'blocked',
                                         'approved', 'applying', 'live', 'rejected',
                                         'rolled_back')),
    -- 승인은 10분간 유효하다. 지나면 재승인이 필요하다.
    approval_expires_at timestamptz,
    approver            text,
    decided_at          timestamptz
);

CREATE INDEX IF NOT EXISTS deploy_request_open
    ON deploy_request (workspace, requested_at DESC)
    WHERE state IN ('awaiting_checks', 'awaiting_approval', 'blocked', 'approved', 'applying');

-- 추가만 되는 이력. 콘솔에서 지울 수 없어야 하므로 UPDATE·DELETE 권한을 주지 않는다(6절).
CREATE TABLE IF NOT EXISTS deploy_event (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL DEFAULT now(),
    workspace   text NOT NULL,
    commit_sha  text,
    actor       text NOT NULL,
    action      text NOT NULL CHECK (action IN ('요청', '승인', '반려', '적용', '롤백')),
    note        text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS deploy_event_recent ON deploy_event (at DESC);

-- ===========================================================================
-- 5. 수집 현황 · 사용량 (재빌드 가능한 파생물)
-- ===========================================================================

-- MD 파일 1개에 대응. `ArchiveStore` 가 읽은 결과를 캐시한다.
-- 원문 본문은 담지 않는다 — 진실은 MD 이고, 열람은 파일에서 직접 읽는다.
CREATE TABLE IF NOT EXISTS archive_doc (
    workspace         text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    channel           text NOT NULL,
    path              text NOT NULL,
    lines             integer NOT NULL DEFAULT 0,
    bytes             bigint NOT NULL DEFAULT 0,
    attachment_lines  integer NOT NULL DEFAULT 0,
    last_ingested_at  timestamptz,
    visibility        text NOT NULL DEFAULT 'private'
                      CHECK (visibility IN ('public', 'private')),
    acl               text[] NOT NULL DEFAULT '{}',
    share_with        text[] NOT NULL DEFAULT '{}',
    -- 형식 검사 실패 사유. NULL 이 아니면 답변 근거로 쓰이지 않는다.
    schema_error      text,
    scanned_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, path)
);

-- 수집 추이 그래프의 한 칸(하루). 없는 날은 행이 없다 = 화면의 '수집 없음'.
CREATE TABLE IF NOT EXISTS archive_course (
    workspace  text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    day        date NOT NULL,
    lines      integer NOT NULL DEFAULT 0,
    PRIMARY KEY (workspace, day)
);

-- 질의응답 1건. 감사기록(JSONL)에서 옮겨 담는다. **질문·답변 본문은 담지 않는다.**
CREATE TABLE IF NOT EXISTS usage_call (
    id          bigserial PRIMARY KEY,
    at          timestamptz NOT NULL,
    workspace   text NOT NULL,
    intent      text NOT NULL,
    source      text NOT NULL CHECK (source IN ('llm', 'regex', 'cmd')),
    reason      text NOT NULL,
    hits        integer NOT NULL DEFAULT 0,
    model       text,
    cost_usd    numeric(12, 6) NOT NULL DEFAULT 0,
    elapsed_ms  integer NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS usage_call_recent ON usage_call (at DESC);
CREATE INDEX IF NOT EXISTS usage_call_by_workspace ON usage_call (workspace, at DESC);

COMMENT ON TABLE usage_call IS
    '질문 문장과 답변 본문은 담지 않는다. 의도·근거건수·모델·비용만.';

-- 일별 집계. 기준선(최근 14일 같은 시각 중위값) 계산과 화면 표시에 쓴다.
CREATE TABLE IF NOT EXISTS usage_daily (
    day        date NOT NULL,
    workspace  text NOT NULL,
    calls      integer NOT NULL DEFAULT 0,
    cost_usd   numeric(12, 6) NOT NULL DEFAULT 0,
    PRIMARY KEY (day, workspace)
);

CREATE TABLE IF NOT EXISTS anomaly (
    id           bigserial PRIMARY KEY,
    workspace    text NOT NULL,
    kind         text NOT NULL CHECK (kind IN ('spike', 'limit', 'loop', 'stalled')),
    detected_at  timestamptz NOT NULL DEFAULT now(),
    -- 기준선 대비 배수. 3.0 이면 평소의 세 배.
    factor       numeric(6, 2) NOT NULL DEFAULT 0,
    headline     text NOT NULL,
    detail       text NOT NULL DEFAULT '',
    state        text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'ack', 'breaker')),
    decided_by   text,
    decided_at   timestamptz
);

CREATE INDEX IF NOT EXISTS anomaly_open ON anomaly (detected_at DESC) WHERE state = 'open';

-- ===========================================================================
-- 6. 원문 열람 기록 (지울 수 없어야 한다)
-- ===========================================================================

-- 수집 문서 원문을 열 때마다 한 줄. 콘솔에서 삭제할 수 없다.
CREATE TABLE IF NOT EXISTS archive_read_audit (
    id         bigserial PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      text NOT NULL,
    workspace  text NOT NULL,
    path       text NOT NULL
);

CREATE INDEX IF NOT EXISTS archive_read_audit_recent ON archive_read_audit (at DESC);

COMMENT ON TABLE archive_read_audit IS
    '원문 열람 기록. 콘솔 DB 역할에 UPDATE/DELETE 를 주지 않는다.';

COMMIT;

-- ===========================================================================
-- 권한 — 콘솔은 이력을 지울 수 없어야 한다
-- ===========================================================================
-- 아래는 역할을 만든 뒤 한 번 실행한다(역할 이름은 운영 환경에 맞춰 바꾼다).
--
--   CREATE ROLE tybot_console LOGIN PASSWORD '...';
--   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO tybot_console;
--   -- 추가만 되는 표에서 수정·삭제를 회수한다
--   REVOKE UPDATE, DELETE ON deploy_event, archive_read_audit FROM tybot_console;
--   GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO tybot_console;
--
-- 봇 프로세스는 별도 역할로 돌린다. 시크릿을 읽어야 하지만 승인 관련 표는 건드릴 필요가 없다.
--
--   CREATE ROLE tybot_bot LOGIN PASSWORD '...';
--   GRANT SELECT ON workspace, workspace_readable, workspace_secret, harness_file TO tybot_bot;
--   GRANT INSERT ON usage_call TO tybot_bot;
--   GRANT SELECT, INSERT, UPDATE, DELETE ON archive_doc, archive_course TO tybot_bot;
