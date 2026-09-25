-- 워크스페이스 하나 · 서비스 여럿
--
-- 결정: 2026-09-25 오너 확정 (§2·§3·§5)
-- 설계: docs/design/archiving-bot-decisions-2026-09-25.md
--
-- ## 왜 workspace 를 봇마다 만들지 않는가
--
-- Archiving Bot 은 **별도 Slack 앱**이라 토큰이 따로 있다. 그렇다고 워크스페이스를
-- 하나 더 만들면 `#팀_자금` 채널이 두 워크스페이스에 존재하게 되고, ACL·한도·
-- 아카이브 경로가 전부 갈라진다. 권한이 두 곳에 있으면 한 곳만 고치는 날이 오고,
-- 그날 **한쪽에서만 새어 나간다**(절대 원칙 3).
--
-- 슬랙 워크스페이스는 하나다. 거기 붙는 **서비스**가 여럿일 뿐이다.
--
-- | service | 무엇 | 토큰 |
-- |---|---|---|
-- | `master` | TYBot. 사용자 신원·권한·전달 | 있다 |
-- | `archiver` | Archiving Bot. 원문·첨부 수집 | 있다(별도 앱) |
-- | `hermes_direct` | PF 직접 호출. **공존 기간에만** | 있다 |
--
-- **최종 Hermes specialist 는 여기 없다.** 그쪽은 Slack 토큰을 갖지 않고, 접근
-- 범위는 `specialist_workspace` 가 정한다. 전문 봇이 Slack 에 직접 붙으면 우리가
-- 권한을 판정할 자리가 사라진다 — 계약이 「우리가 필터한 텍스트만 준다」 인데
-- 자기 토큰이 있으면 그 계약이 무의미해진다(CLAUDE.md 봇 체계).
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 서비스 연결
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS workspace_service (
    workspace       text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    service         text NOT NULL
                    CHECK (service IN ('master', 'archiver', 'hermes_direct')),
    state           text NOT NULL DEFAULT 'disabled'
                    CHECK (state IN ('enabled', 'disabled', 'error')),
    error           text,
    -- Slack 이 말해 주는 신원. 사람이 적는 값이 아니라 `auth.test` 결과다.
    --
    -- 왜 저장하나: 토큰을 잘못 붙이면 **다른 워크스페이스에 수집한다.** 그건
    -- 오류가 아니라 유출이고, 붙일 때 확인하지 않으면 한참 뒤에 발견된다.
    team_id         text NOT NULL DEFAULT '',
    bot_user_id     text NOT NULL DEFAULT '',
    -- 마지막 신원 검사. `identity_ok` 가 NULL 이면 **아직 확인 안 했다** —
    -- false(확인했고 틀렸다)와 구분한다.
    identity_ok     boolean,
    identity_error  text NOT NULL DEFAULT '',
    identity_checked_at timestamptz,
    note            text NOT NULL DEFAULT '',
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL DEFAULT '',
    PRIMARY KEY (workspace, service)
);

-- 오류 상태가 아닌데 오류 문구가 남아 있으면, 화면이 「정상인데 빨간 글씨」 를
-- 보여 준다. 그 화면을 본 사람은 무엇을 믿어야 할지 모른다.
ALTER TABLE workspace_service DROP CONSTRAINT IF EXISTS workspace_service_error_only_when_error;
ALTER TABLE workspace_service ADD CONSTRAINT workspace_service_error_only_when_error
    CHECK (state = 'error' OR error IS NULL);

-- **enabled 인데 신원 검사를 통과하지 않은 서비스는 없다.**
-- 검사 전에 켤 수 있으면 토큰을 잘못 붙인 채로 수집이 시작된다.
ALTER TABLE workspace_service DROP CONSTRAINT IF EXISTS workspace_service_enabled_needs_identity;
ALTER TABLE workspace_service ADD CONSTRAINT workspace_service_enabled_needs_identity
    CHECK (state <> 'enabled' OR (identity_ok IS TRUE AND team_id <> '' AND bot_user_id <> ''));

-- 같은 워크스페이스에서 두 서비스가 **같은 봇 사용자**일 수 없다.
-- 같으면 별도 앱이 아니라 같은 앱을 두 번 등록한 것이고, 그 상태로 Socket Mode 를
-- 두 곳에서 열면 이벤트를 양쪽이 받는다(중복 답변·비용 2배, CLAUDE.md 금지사항).
CREATE UNIQUE INDEX IF NOT EXISTS workspace_service_distinct_bot_user
    ON workspace_service (workspace, bot_user_id)
    WHERE bot_user_id <> '';

CREATE INDEX IF NOT EXISTS workspace_service_by_service
    ON workspace_service (service, state);

COMMENT ON TABLE workspace_service IS
    '워크스페이스에 붙는 서비스. Hermes specialist 는 여기 없다 — Slack 토큰이 없고 '
    '접근 범위는 specialist_workspace 가 정한다.';


-- ---------------------------------------------------------------------------
-- 2. 서비스별 토큰
-- ---------------------------------------------------------------------------
-- `workspace_secret` 과 같은 규칙이다 — 암호문만 담고, **복호화 조회 API 를
-- 만들지 않는다.** 봇이 기동 시 한 번 복호화해 메모리에 들고, 콘솔은 mask 만 읽는다.
CREATE TABLE IF NOT EXISTS workspace_service_secret (
    workspace   text NOT NULL,
    service     text NOT NULL,
    kind        text NOT NULL CHECK (kind IN ('bot', 'app')),
    ciphertext  bytea NOT NULL,
    -- 화면에 보여 줄 가린 값. 예: xoxb-4821…9f0c
    mask        text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    updated_by  text NOT NULL,
    PRIMARY KEY (workspace, service, kind),
    FOREIGN KEY (workspace, service)
        REFERENCES workspace_service (workspace, service) ON DELETE CASCADE
);

COMMENT ON TABLE workspace_service_secret IS
    '서비스별 봇/앱 토큰. 평문 저장 금지, 복호화 조회 API 금지. 콘솔은 mask 만 읽는다.';

-- Archiver 런타임은 자기 서비스 토큰만 시작할 때 읽는다. 표 SELECT를 주지 않고
-- SECURITY DEFINER 함수로 열을 제한해 master/Hermes 토큰이 같은 계정에 보이지 않게 한다.
CREATE OR REPLACE FUNCTION archiver_runtime_config(requested_workspace text)
RETURNS TABLE (
    state text,
    team_id text,
    bot_user_id text,
    master_bot_user_id text,
    bot_ciphertext bytea,
    app_ciphertext bytea
)
LANGUAGE sql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT a.state,
           a.team_id,
           a.bot_user_id,
           coalesce(m.bot_user_id, ''),
           bot.ciphertext,
           app.ciphertext
      FROM workspace_service a
      JOIN workspace_service_secret bot
        ON bot.workspace = a.workspace AND bot.service = a.service AND bot.kind = 'bot'
      JOIN workspace_service_secret app
        ON app.workspace = a.workspace AND app.service = a.service AND app.kind = 'app'
      LEFT JOIN workspace_service m
        ON m.workspace = a.workspace AND m.service = 'master'
     WHERE a.workspace = requested_workspace
       AND a.service = 'archiver'
       AND a.state = 'enabled'
       AND a.identity_ok IS TRUE
       AND m.identity_ok IS TRUE
       AND coalesce(m.bot_user_id, '') <> ''
$$;

REVOKE ALL ON FUNCTION archiver_runtime_config(text) FROM PUBLIC;


-- ---------------------------------------------------------------------------
-- 3. 기존 토큰을 master 서비스로 옮긴다
-- ---------------------------------------------------------------------------
-- **복사이고 이동이 아니다.** `workspace_secret` 을 지우지 않는다 — 봇이 아직
-- 그쪽을 읽고 있고, 지우면 다음 기동에서 전 워크스페이스가 뜨지 않는다.
-- 읽는 쪽을 옮긴 뒤 별도 단계에서 정리한다.
--
-- 여러 번 돌려도 안전하다. 이미 옮긴 것은 건드리지 않는다 — 콘솔에서 master
-- 토큰을 새로 넣었는데 재적용이 옛 값으로 되돌리면, 그건 조용한 롤백이다.
INSERT INTO workspace_service (workspace, service, state, note, updated_by)
SELECT DISTINCT w.key, 'master', 'disabled',
       'workspace_secret 에서 이관됨. 신원 검사 뒤 enabled 로 켠다', 'migration'
  FROM workspace w
  JOIN workspace_secret s ON s.workspace = w.key
ON CONFLICT (workspace, service) DO NOTHING;

INSERT INTO workspace_service_secret
    (workspace, service, kind, ciphertext, mask, updated_at, updated_by)
SELECT s.workspace, 'master', s.kind, s.ciphertext, s.mask, s.updated_at, s.updated_by
  FROM workspace_secret s
  JOIN workspace_service ws
    ON ws.workspace = s.workspace AND ws.service = 'master'
ON CONFLICT (workspace, service, kind) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 4. archive_path 를 정본 경로로 맞춘다
-- ---------------------------------------------------------------------------
-- 실제 원문은 `<ARCHIVE_DIR>/workspaces/<key>/channels/...` 에 있다
-- (`writer.channel_dir`). 그런데 등록 때 `<ARCHIVE_DIR>/<key>` 로 적어 두었다.
--
-- 이 값은 화면에만 쓰이므로 봇은 멀쩡히 돌았다. 대신 **그 화면을 보고 서버에
-- 들어간 사람이 빈 디렉터리를 본다.** 「아카이브가 비었다」 로 읽히는 종류의
-- 거짓말이고, 그때 사람은 수집이 죽은 줄 안다.
--
-- 옛 모양(`<root>/<key>`)인 행만 고친다. 사람이 손으로 다른 경로를 넣었을 수도
-- 있고, 그건 이 migration 이 판단할 일이 아니다.
UPDATE workspace
   SET archive_path = regexp_replace(archive_path, '/([^/]+)$', '/workspaces/\1')
 WHERE archive_path <> ''
   AND archive_path NOT LIKE '%/workspaces/%'
   AND archive_path LIKE '%/' || key;


-- ---------------------------------------------------------------------------
-- 5. 권한
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE'
                ' workspace_service, workspace_service_secret TO tyslackai';
    END IF;

    -- archiver 는 시크릿 표를 직접 못 읽는다. 위 함수로 자기 서비스 토큰만 받는다.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_archiver') THEN
        REVOKE ALL PRIVILEGES ON TABLE workspace_service_secret FROM tybot_archiver;
        REVOKE ALL PRIVILEGES ON TABLE workspace_service FROM tybot_archiver;
        -- 자기 서비스 행은 읽어야 한다 — 어느 워크스페이스에 붙는지, 신원이
        -- 무엇이어야 하는지를 확인한다. 그 표에 토큰은 없다.
        EXECUTE 'GRANT SELECT ON TABLE workspace_service TO tybot_archiver';
        EXECUTE 'GRANT EXECUTE ON FUNCTION archiver_runtime_config(text) TO tybot_archiver';
    END IF;
END
$$;

COMMIT;
