-- 논리 봇 하나 · 연결 여럿
--
-- 설계: docs/design/workspace-service-console-redesign.md §4·§5 (2026-09-28)
-- 1단계 inventory: docs/verification/2026-09-28-console-redesign-inventory.md
--
-- ## 무엇이 바뀌나
--
-- `workspace_service` 는 `master|archiver|hermes_direct` 세 값을 고정했다. 그러면
-- **Hermes 가 콘솔에서 두 개로 보인다** — PF 가 직접 부르는 것과 Master 가 내부로
-- 부르는 것이 다른 봇처럼 놓인다. 봇은 하나이고 다른 것은 **연결 방식**뿐이다.
--
--   bot_catalog            논리 봇이 무엇인가          (master · archiver · hermes · clio)
--   bot_connection         그 봇이 이 워크스페이스에서 Slack 이벤트를 받는가
--   specialist_route       Master 가 그 봇을 내부로 부르는가
--
-- `master_internal` 을 `bot_connection` 에 **가짜 행으로 넣지 않는다**(§4.3). 그쪽
-- 정본은 라우트 표다. 가짜 행을 만들면 Slack 토큰 칸이 비어 있는 연결이 생기고,
-- 그 순간 「토큰 없는 연결」 과 「토큰을 아직 안 넣은 연결」 을 구분할 수 없다.
--
-- ## 옛 표를 지우지 않는다
--
-- `workspace_service`·`workspace_service_secret` 은 그대로 둔다. Archiver 런타임이
-- 아직 그쪽을 읽고 있고(`archiver_runtime_config`), 지우면 다음 기동에서 수집이
-- 멈춘다. 여기서는 **복사만** 한다. 읽는 쪽을 옮기는 것은 나중 단계이고, 옛 표
-- 정리는 그 뒤의 별도 승인 작업이다(§5.9).
--
-- 여러 번 돌려도 안전하다. 이미 새 표에 값이 있으면 **옛 값으로 덮지 않는다** —
-- 콘솔에서 새로 넣은 토큰을 재적용이 옛 값으로 되돌리면 그건 조용한 롤백이다.
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 논리 봇
-- ---------------------------------------------------------------------------
-- **네 값만 허용하는 CHECK 를 만들지 않는다**(§4.2). 승인된 전문 봇이 늘어날 때
-- 스키마를 고쳐야 하면, 고치는 사람이 없는 날 봇이 안 늘어난다.
CREATE TABLE IF NOT EXISTS bot_catalog (
    key                  text PRIMARY KEY CHECK (key ~ '^[a-z][a-z0-9-]{1,31}$'),
    display_name         text NOT NULL CHECK (btrim(display_name) <> ''),
    category             text NOT NULL
                         CHECK (category IN ('orchestrator', 'collector', 'specialist')),
    owner_team           text NOT NULL DEFAULT '',
    -- Slack 앱으로 설치될 수 있나. Hermes 는 PF 공존 기간에만 true 다.
    slack_connectable    boolean NOT NULL DEFAULT false,
    -- Master 가 내부 호출로 부를 수 있나. Master 자신은 false 다.
    internally_invokable boolean NOT NULL DEFAULT false,
    state                text NOT NULL DEFAULT 'active'
                         CHECK (state IN ('active', 'retired')),
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE bot_catalog IS
    '논리 봇 정체성. specialist runtime 상태를 여기 복제하지 않는다 — 그쪽 정본은 '
    'specialist_bot 과 specialist_deployment 다.';

-- 초기 네 봇. **멱등하다** — 이미 있으면 사람이 고친 값을 덮지 않는다.
-- `Hermes Direct` 행은 만들지 않는다(§13).
INSERT INTO bot_catalog
    (key, display_name, category, owner_team, slack_connectable, internally_invokable)
VALUES
    ('master',   'TYBot Master', 'orchestrator', 'Slack AI TFT', true,  false),
    ('archiver', 'Archiving Bot', 'collector',   'Slack AI TFT', true,  false),
    ('hermes',   'Hermes',        'specialist',  'Slack AI TFT', true,  true),
    ('clio',     'Clio',          'specialist',  'Slack AI TFT', false, true)
ON CONFLICT (key) DO NOTHING;

-- 이미 등록된 **다른 전문 봇**도 들여온다.
--
-- 네 봇만 seed 하면, 승인받아 쓰고 있던 전문 봇이 카탈로그에 없다. 그 상태에서
-- `specialist_route` 는 배정을 backfill 하는데 화면은 그 봇을 모른다 — 「어느 봇의
-- 라우트인지 모르는 행」 이 생기고, 그건 사람이 지우기도 어렵다.
--
-- `internally_invokable` 은 true 다. 전문 봇은 Master 내부 호출로만 불린다 —
-- Slack 에 직접 붙으면 우리가 권한을 판정할 자리가 사라진다(CLAUDE.md 봇 체계).
-- Hermes 의 PF 직접 연결은 공존 기간의 예외이고 위에서 이미 true 로 적었다.
--
-- 표시 이름은 그 봇이 쓰던 이름을 그대로 쓴다. 비어 있으면 key 로 대신한다 —
-- `display_name` 이 비면 화면에 이름 없는 행이 보인다.
-- **`state` 는 옮기지 않는다.** 여기 `active`/`retired` 는 봇의 정체성이 살아
-- 있는가이고, `specialist_bot.state` 는 지금 켜져 있는가다. 복제하면 두 값이
-- 갈리는 날이 오고, 그날 어느 쪽이 참인지 고를 근거가 없다(§4.2).
INSERT INTO bot_catalog
    (key, display_name, category, owner_team, slack_connectable, internally_invokable)
SELECT s.key,
       coalesce(nullif(btrim(s.name), ''), s.key),
       'specialist',
       '',
       false,
       true
  FROM specialist_bot s
ON CONFLICT (key) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 2. Slack 연결
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bot_connection (
    id                       bigserial PRIMARY KEY,
    workspace                text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    bot_key                  text NOT NULL REFERENCES bot_catalog(key),
    -- 지금은 Slack 만이다. 다른 transport 가 **실제로** 생길 때 넓힌다(§4.3).
    connector_type           text NOT NULL DEFAULT 'slack_socket'
                             CHECK (connector_type IN ('slack_socket')),
    state                    text NOT NULL DEFAULT 'draft'
                             CHECK (state IN ('draft', 'disabled', 'enabled',
                                              'error', 'retired')),
    -- Slack 이 말해 주는 신원. 사람이 적는 값이 아니라 `auth.test` 결과다.
    -- 토큰을 잘못 붙이면 **다른 워크스페이스에 수집한다.** 그건 오류가 아니라
    -- 유출이고, 붙일 때 확인하지 않으면 한참 뒤에 발견된다.
    team_id                  text NOT NULL DEFAULT '',
    bot_user_id              text NOT NULL DEFAULT '',
    -- NULL 이면 **아직 확인 안 했다** — false(확인했고 틀렸다)와 구분한다.
    identity_ok              boolean,
    identity_error           text NOT NULL DEFAULT '',
    identity_checked_at      timestamptz,
    -- Slack App Manifest 정본 좌표. 표(`slack_app_manifest`)는 Manifest 단계에서
    -- 만든다. 여기서 FK 를 걸면 그 단계까지 이 표를 못 쓴다.
    manifest_id              text NOT NULL DEFAULT '',
    manifest_attested_sha256 text NOT NULL DEFAULT '',
    manifest_attested_at     timestamptz,
    manifest_attested_by     text NOT NULL DEFAULT '',
    -- 런타임이 말하는 것. 신원·Manifest 확인과 **합치지 않는다**(§6.2).
    last_heartbeat_at        timestamptz,
    last_event_at            timestamptz,
    runtime_version          text NOT NULL DEFAULT '',
    runtime_error            text NOT NULL DEFAULT '',
    note                     text NOT NULL DEFAULT '',
    created_at               timestamptz NOT NULL DEFAULT now(),
    updated_at               timestamptz NOT NULL DEFAULT now(),
    updated_by               text NOT NULL DEFAULT '',
    UNIQUE (workspace, bot_key, connector_type)
);

COMMENT ON TABLE bot_connection IS
    'Slack 앱 연결. master_internal 을 여기 넣지 않는다 — 그 정본은 specialist_route 다.';

-- 아래 셋은 `workspace_service` 가 이미 막고 있던 것이다. 새 표가 물려받지 않으면
-- 이관과 동시에 **보호가 사라진다**(1단계 inventory §7.5).

-- 오류 상태가 아닌데 오류 문구가 남아 있으면, 화면이 「정상인데 빨간 글씨」 를
-- 보여 준다. 그 화면을 본 사람은 무엇을 믿어야 할지 모른다.
ALTER TABLE bot_connection DROP CONSTRAINT IF EXISTS bot_connection_error_only_when_error;
ALTER TABLE bot_connection ADD CONSTRAINT bot_connection_error_only_when_error
    CHECK (state = 'error' OR runtime_error = '');

-- **enabled 인데 신원 검사를 통과하지 않은 연결은 없다.**
-- 검사 전에 켤 수 있으면 토큰을 잘못 붙인 채로 수집이 시작된다.
ALTER TABLE bot_connection DROP CONSTRAINT IF EXISTS bot_connection_enabled_needs_identity;
ALTER TABLE bot_connection ADD CONSTRAINT bot_connection_enabled_needs_identity
    CHECK (state <> 'enabled' OR (identity_ok IS TRUE AND team_id <> '' AND bot_user_id <> ''));

-- 같은 워크스페이스에서 두 연결이 **같은 봇 사용자**일 수 없다. 같으면 별도 앱이
-- 아니라 같은 앱을 두 번 등록한 것이고, 그 상태로 Socket Mode 를 두 곳에서 열면
-- 이벤트를 양쪽이 받는다(중복 답변·비용 2배, CLAUDE.md 금지사항).
--
-- `retired` 는 뺀다 — 앱을 갈아 끼운 뒤 옛 행이 새 연결을 막으면, 막는 이유가
-- 화면에 안 보이고 사람은 지우는 쪽으로 간다.
CREATE UNIQUE INDEX IF NOT EXISTS bot_connection_distinct_bot_user
    ON bot_connection (workspace, bot_user_id)
    WHERE bot_user_id <> '' AND state <> 'retired';

CREATE INDEX IF NOT EXISTS bot_connection_by_bot ON bot_connection (bot_key, state);

-- Team ID 일치(§4.3 「해당 workspace 에서 검증된 다른 Slack 연결과 같아야 한다」)는
-- 여기서 CHECK 로 못 막는다 — 다른 행을 봐야 한다. store 가 판정한다
-- (`workspace_service_store.check_identity` 와 같은 자리).


-- ---------------------------------------------------------------------------
-- 3. 연결별 토큰
-- ---------------------------------------------------------------------------
-- `workspace_secret` 과 같은 규칙이다 — 암호문만 담고, **복호화 조회 API 를
-- 만들지 않는다.** 봇이 기동 시 한 번 복호화해 메모리에 들고, 콘솔은 mask 만 읽는다.
CREATE TABLE IF NOT EXISTS bot_connection_secret (
    connection_id bigint NOT NULL REFERENCES bot_connection(id) ON DELETE CASCADE,
    kind          text NOT NULL CHECK (kind IN ('bot', 'app')),
    ciphertext    bytea NOT NULL,
    -- 화면에 보여 줄 가린 값. 예: xoxb-4821…9f0c
    mask          text NOT NULL,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    updated_by    text NOT NULL,
    PRIMARY KEY (connection_id, kind)
);

COMMENT ON TABLE bot_connection_secret IS
    '연결별 봇/앱 토큰. 평문 저장 금지, 복호화 조회 API 금지. 콘솔은 mask 만 읽는다.';

-- Archiver 런타임은 자기 연결 토큰만 기동할 때 읽는다. 표 SELECT 를 주지 않고
-- SECURITY DEFINER 함수로 열을 제한해 master/Hermes 토큰이 같은 계정에 보이지 않게
-- 한다. `archiver_runtime_config` 와 **같은 조건**이다 — 조건이 갈리면 이관 전후로
-- 기동 가능 여부가 달라지고, 그건 이관이 아니라 정책 변경이다.
CREATE OR REPLACE FUNCTION archiver_connection_config(requested_workspace text)
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
      FROM bot_connection a
      JOIN bot_connection_secret bot
        ON bot.connection_id = a.id AND bot.kind = 'bot'
      JOIN bot_connection_secret app
        ON app.connection_id = a.id AND app.kind = 'app'
      LEFT JOIN bot_connection m
        ON m.workspace = a.workspace AND m.bot_key = 'master'
       AND m.connector_type = 'slack_socket'
     WHERE a.workspace = requested_workspace
       AND a.bot_key = 'archiver'
       AND a.connector_type = 'slack_socket'
       AND a.state = 'enabled'
       AND a.identity_ok IS TRUE
       AND m.identity_ok IS TRUE
       AND coalesce(m.bot_user_id, '') <> ''
$$;

REVOKE ALL ON FUNCTION archiver_connection_config(text) FROM PUBLIC;


-- ---------------------------------------------------------------------------
-- 4. Master 내부 호출 라우트
-- ---------------------------------------------------------------------------
-- **`specialist_workspace` 에 열을 더하지 않고 별도 표로 둔다.**
--
-- 그쪽은 배정을 바꿀 때 전량 DELETE 후 재삽입된다
-- (`specialist_store.py` 의 `save`). 같은 행에 운영 상태를 두면 담당자가 배정을
-- 한 번 저장할 때마다 shadow/active 가 조용히 초기화된다. 소속(어느 워크스페이스에
-- 배정됐나)과 운영 상태(지금 어떻게 부르나)는 바뀌는 이유도 주기도 다르다.
--
-- 행이 없는 것은 `disabled` 다(§4.5). 없는 것을 「아직 안 정함」 으로 읽고 기본
-- 동작을 켜면, 배정만 하고 잊은 봇이 사용자 답변 경로에 들어온다.
CREATE TABLE IF NOT EXISTS specialist_route (
    specialist             text NOT NULL REFERENCES specialist_bot(key) ON DELETE CASCADE,
    workspace              text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    route_mode             text NOT NULL DEFAULT 'disabled'
                           CHECK (route_mode IN ('disabled', 'shadow', 'active')),
    -- 그 봇이 답을 못 낼 때 누가 받나. 기본은 Master 자신이다.
    fallback_bot_key       text NOT NULL DEFAULT 'master' REFERENCES bot_catalog(key),
    last_shadow_checked_at timestamptz,
    last_shadow_result     text NOT NULL DEFAULT '',
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now(),
    updated_by             text NOT NULL DEFAULT '',
    PRIMARY KEY (specialist, workspace)
);

COMMENT ON TABLE specialist_route IS
    'Master 내부 호출 운영 상태. 배정 자체는 specialist_workspace 가 정본이다 — '
    '같은 상태를 두 표에 쓰지 않는다.';

CREATE INDEX IF NOT EXISTS specialist_route_active
    ON specialist_route (workspace, route_mode)
    WHERE route_mode <> 'disabled';


-- ---------------------------------------------------------------------------
-- 5. 기존 연결을 새 표로 복사한다
-- ---------------------------------------------------------------------------
-- **복사이고 이동이 아니다.** 이관 때문에 Slack 앱을 재설치하거나 토큰을
-- 재발급하지 않는다(§5).
--
--   workspace_service.master        -> bot_connection(master,   slack_socket)
--   workspace_service.archiver      -> bot_connection(archiver, slack_socket)
--   workspace_service.hermes_direct -> bot_connection(hermes,   slack_socket)
--
-- `hermes_direct` 가 `hermes` 가 되는 것이 이 이관의 요점이다 — 콘솔에서 Hermes 가
-- 하나로 보이기 시작한다.
WITH moved AS (
    INSERT INTO bot_connection
        (workspace, bot_key, connector_type, state, team_id, bot_user_id,
         identity_ok, identity_error, identity_checked_at, runtime_error, note,
         created_at, updated_at, updated_by)
    SELECT s.workspace,
           CASE s.service WHEN 'hermes_direct' THEN 'hermes' ELSE s.service END,
           'slack_socket',
           s.state,
           s.team_id,
           s.bot_user_id,
           s.identity_ok,
           s.identity_error,
           s.identity_checked_at,
           coalesce(s.error, ''),
           s.note,
           s.created_at,
           s.updated_at,
           s.updated_by
      FROM workspace_service s
      JOIN bot_catalog c
        ON c.key = CASE s.service WHEN 'hermes_direct' THEN 'hermes' ELSE s.service END
    ON CONFLICT (workspace, bot_key, connector_type) DO NOTHING
    RETURNING workspace, bot_key
)
-- 이관 감사. `hermes_direct -> hermes/slack_socket` 좌표를 남긴다(§5).
-- 실제로 들어간 행만 남는다 — 재적용은 아무것도 안 적는다.
INSERT INTO archive_config_audit
    (actor, subject, workspace, channel_id, field, old_value, new_value, reason)
SELECT 'migration', 'bot_connection', workspace, '',
       'connection.' || bot_key || '.slack_socket',
       CASE bot_key WHEN 'hermes' THEN 'workspace_service.hermes_direct'
                    ELSE 'workspace_service.' || bot_key END,
       'bot_connection.' || bot_key || '/slack_socket',
       '콘솔 개편 이관(복사). 토큰 재발급 없음'
  FROM moved;

-- 암호문과 mask 를 연결 ID 로 옮긴다. **이미 새 표에 값이 있으면 덮지 않는다**(§5.3).
INSERT INTO bot_connection_secret
    (connection_id, kind, ciphertext, mask, updated_at, updated_by)
SELECT c.id, sec.kind, sec.ciphertext, sec.mask, sec.updated_at, sec.updated_by
  FROM workspace_service_secret sec
  JOIN bot_connection c
    ON c.workspace = sec.workspace
   AND c.bot_key = CASE sec.service WHEN 'hermes_direct' THEN 'hermes' ELSE sec.service END
   AND c.connector_type = 'slack_socket'
ON CONFLICT (connection_id, kind) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 6. 라우트 backfill
-- ---------------------------------------------------------------------------
-- **일괄 `active` 로 바꾸지 않는다**(§4.5). 지금 실제로 라우팅되는 조건은
-- `specialist_router` 의 질의가 정한다 — 배정이 있고, 봇이 `enabled` 이고,
-- health 가 `error` 가 아닐 때다. 그 셋을 만족하는 행만 `active` 로 적는다.
--
-- 나머지는 `disabled` 다. 오늘 안 불리는 것을 `active` 로 적으면, 이관이 라우팅을
-- **바꾸는** 일이 된다.
INSERT INTO specialist_route (specialist, workspace, route_mode, updated_by)
SELECT sw.specialist, sw.workspace,
       CASE WHEN b.state = 'enabled' AND b.health <> 'error' THEN 'active'
            ELSE 'disabled' END,
       'migration'
  FROM specialist_workspace sw
  JOIN specialist_bot b ON b.key = sw.specialist
ON CONFLICT (specialist, workspace) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 7. 권한
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE'
                ' bot_catalog, bot_connection, bot_connection_secret,'
                ' specialist_route TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE bot_connection_id_seq TO tyslackai';
        -- **지우는 권한은 주지 않는다.**
        --
        -- 연결을 그만 쓰는 것은 `state = 'retired'` 이고, 라우트를 끄는 것은
        -- `route_mode = 'disabled'` 다. 행을 지우면 그 연결이 있었다는 사실과
        -- 언제 누가 껐는지가 함께 사라진다 — 사고 뒤에 범위를 정할 수 없다.
        --
        -- 시크릿도 같다. 토큰 행을 지우면 연결은 남고 mask 만 없어져서, 화면은
        -- 「토큰을 넣은 적 없는 연결」 로 보인다.
        --
        -- 선언에서 빼는 것만으로는 이미 준 권한이 사라지지 않는다. 명시적으로
        -- 회수한다(2026-09-14 실측: 초안이 준 권한이 그대로 남아 있었다).
        EXECUTE 'REVOKE DELETE ON TABLE'
                ' bot_catalog, bot_connection, bot_connection_secret,'
                ' specialist_route FROM tyslackai';
    END IF;

    -- archiver 는 시크릿 표를 직접 못 읽는다. 위 함수로 자기 연결 토큰만 받는다.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_archiver') THEN
        REVOKE ALL PRIVILEGES ON TABLE bot_connection_secret FROM tybot_archiver;
        REVOKE ALL PRIVILEGES ON TABLE bot_connection FROM tybot_archiver;
        -- 자기 연결 행은 읽어야 한다 — 어느 워크스페이스에 붙는지, 신원이 무엇이어야
        -- 하는지를 확인한다. 그 표에 토큰은 없다.
        EXECUTE 'GRANT SELECT ON TABLE bot_connection, bot_catalog TO tybot_archiver';
        EXECUTE 'GRANT EXECUTE ON FUNCTION archiver_connection_config(text)'
                ' TO tybot_archiver';
    END IF;
END
$$;

COMMIT;
