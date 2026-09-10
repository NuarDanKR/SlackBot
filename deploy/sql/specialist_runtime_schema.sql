-- 전문 봇 2단계 — 격리 실행 런타임 (specialist-runtime-v2.md 1단계)
--
-- 설계: docs/design/specialist-runtime-v2.md
--
-- **소스 상태와 런타임 상태를 섞지 않는다.** 하나의 `state` 열에 업로드·빌드·승인·
-- 배포를 다 담으면, 「빌드는 됐는데 배포가 안 된 것」 과 「배포했다가 꺼진 것」 이
-- 같은 값으로 보인다. 그 둘은 사람이 할 일이 완전히 다르다.
--
-- 표 넷은 수명주기의 서로 다른 구간을 가진다.
--
--   specialist_source     제출물 — 무엇을 받았나
--   specialist_build      빌드 결과 — 무엇을 만들었나(digest)
--   specialist_deployment 배포 — 무엇이 지금 도는가
--   specialist_runtime_secret  자격 — 무엇으로 인증하나
--
-- **저장하지 않는 것**: 질문, 근거, 응답 본문, HMAC 평문, provider key 평문,
-- 업로드 원본 경로(검역 키만 둔다).

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 제출물 (검역)
-- ---------------------------------------------------------------------------
-- 콘솔은 형식과 크기만 보고 검역 저장소에 넣는다. **이 단계에서 실행하지 않는다.**
-- `quarantine_key` 는 서버가 만든 UUID 다 — 업로드한 파일명을 경로로 쓰면
-- 경로 탈출이 그대로 파일시스템에 닿는다.
CREATE TABLE IF NOT EXISTS specialist_source (
    id             bigserial PRIMARY KEY,
    specialist     text NOT NULL CHECK (specialist ~ '^[a-z][a-z0-9-]{1,31}$'),
    source_type    text NOT NULL CHECK (source_type IN ('git', 'zip')),
    -- git 입력
    repository_url text NOT NULL DEFAULT '',
    release_ref    text NOT NULL DEFAULT '',
    source_commit  text NOT NULL DEFAULT '',
    -- zip 입력. 이름은 표시용이고 경로로 쓰지 않는다.
    source_name    text NOT NULL DEFAULT '',
    bundle_sha256  text NOT NULL DEFAULT '',
    quarantine_key text NOT NULL DEFAULT '',
    status         text NOT NULL DEFAULT 'uploaded'
                   CHECK (status IN ('uploaded', 'source_verified', 'source_rejected')),
    error_code     text NOT NULL DEFAULT '',
    submitted_by   text NOT NULL,
    submitted_at   timestamptz NOT NULL DEFAULT now(),
    -- git 은 저장소·태그가, zip 은 해시가 있어야 한다. 둘 다 비면 무엇을 받았는지
    -- 알 수 없는 행이 남는다.
    CONSTRAINT specialist_source_input CHECK (
        (source_type = 'git' AND btrim(repository_url) <> '' AND btrim(release_ref) <> '')
        OR (source_type = 'zip' AND btrim(bundle_sha256) <> '')
    )
);

CREATE INDEX IF NOT EXISTS specialist_source_recent
    ON specialist_source (specialist, submitted_at DESC);

-- ---------------------------------------------------------------------------
-- 2. 빌드 (무시크릿 · 일회성)
-- ---------------------------------------------------------------------------
-- 운영 산출물은 **태그가 아니라 digest** 로 고정한다. 태그는 움직이고, 움직이는
-- 것을 승인하면 승인한 것과 도는 것이 갈린다.
CREATE TABLE IF NOT EXISTS specialist_build (
    id           bigserial PRIMARY KEY,
    source_id    bigint NOT NULL REFERENCES specialist_source(id) ON DELETE CASCADE,
    runtime      text NOT NULL,
    status       text NOT NULL DEFAULT 'building'
                 CHECK (status IN ('building', 'build_failed', 'image_ready',
                                   'contract_failed')),
    -- `sha256:` + 64 hex. 성공한 빌드만 값을 가진다.
    image_digest text NOT NULL DEFAULT '',
    sbom_sha256  text NOT NULL DEFAULT '',
    -- 검사 결과. 정규화한 코드와 요약만 — 로그 전문은 넣지 않는다.
    checks       jsonb NOT NULL DEFAULT '[]'::jsonb,
    error_code   text NOT NULL DEFAULT '',
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    CONSTRAINT specialist_build_digest_shape CHECK (
        image_digest = '' OR image_digest ~ '^sha256:[0-9a-f]{64}$'
    ),
    -- 성공했다면 digest 가 있어야 한다. 없으면 무엇을 배포할지 모르는 채로
    -- `image_ready` 가 된다.
    CONSTRAINT specialist_build_ready_has_digest CHECK (
        status <> 'image_ready' OR image_digest <> ''
    )
);

CREATE INDEX IF NOT EXISTS specialist_build_by_source
    ON specialist_build (source_id, started_at DESC);

-- ---------------------------------------------------------------------------
-- 3. 배포
-- ---------------------------------------------------------------------------
-- `active` 는 **전문가당 하나**여야 한다. 둘이면 어느 digest 가 답했는지 알 수 없고,
-- 롤백이 무엇으로 되돌리는지도 정해지지 않는다. 부분 유니크 인덱스로 DB 가 막는다.
CREATE TABLE IF NOT EXISTS specialist_deployment (
    id              bigserial PRIMARY KEY,
    specialist      text NOT NULL CHECK (specialist ~ '^[a-z][a-z0-9-]{1,31}$'),
    build_id        bigint NOT NULL REFERENCES specialist_build(id) ON DELETE RESTRICT,
    state           text NOT NULL DEFAULT 'standby'
                    CHECK (state IN ('standby', 'active', 'retired', 'failed')),
    health          text NOT NULL DEFAULT 'unknown'
                    CHECK (health IN ('unknown', 'ok', 'error')),
    error_code      text NOT NULL DEFAULT '',
    deployed_by     text NOT NULL,
    deployed_at     timestamptz NOT NULL DEFAULT now(),
    last_checked_at timestamptz
);

CREATE UNIQUE INDEX IF NOT EXISTS specialist_deployment_one_active
    ON specialist_deployment (specialist) WHERE state = 'active';

CREATE INDEX IF NOT EXISTS specialist_deployment_recent
    ON specialist_deployment (specialist, deployed_at DESC);

-- ---------------------------------------------------------------------------
-- 4. 런타임 자격
-- ---------------------------------------------------------------------------
-- Unix socket 권한이 1차 인증이고, 요청별 HMAC 이 2차다. 평문은 저장하지 않는다 —
-- `llm_secret` 과 같은 방식으로 workspace Fernet key 로 암호화한다.
--
-- **복호화 API 를 만들지 않는다.** 콘솔에는 mask 와 교체일만 보인다. 값을 화면으로
-- 꺼낼 수 있으면 그 순간 브라우저·로그·스크린샷이 전부 보관 장소가 된다.
CREATE TABLE IF NOT EXISTS specialist_runtime_secret (
    specialist text NOT NULL CHECK (specialist ~ '^[a-z][a-z0-9-]{1,31}$'),
    kind       text NOT NULL CHECK (kind IN ('hmac', 'provider')),
    ciphertext text NOT NULL,
    mask       text NOT NULL DEFAULT '',
    enabled    boolean NOT NULL DEFAULT true,
    -- 교체 시 구/신 키를 잠깐 겹친다. 이전 암호문이 없으면 무중단 전환이 안 된다.
    previous_ciphertext text NOT NULL DEFAULT '',
    previous_until      timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL,
    PRIMARY KEY (specialist, kind)
);

-- ---------------------------------------------------------------------------
-- 5. 기존 표에 붙이는 것
-- ---------------------------------------------------------------------------
-- `prompt` 는 지금까지의 어댑터(마스터 프로세스 안), `http` 는 격리 컨테이너다.
-- 기본값이 `prompt` 라 기존 행은 그대로 돈다.
ALTER TABLE specialist_bot
    ADD COLUMN IF NOT EXISTS execution_mode text NOT NULL DEFAULT 'prompt';
ALTER TABLE specialist_bot
    DROP CONSTRAINT IF EXISTS specialist_bot_execution_mode;
ALTER TABLE specialist_bot
    ADD CONSTRAINT specialist_bot_execution_mode
    CHECK (execution_mode IN ('prompt', 'http'));

ALTER TABLE specialist_bot
    ADD COLUMN IF NOT EXISTS active_deployment_id bigint;

COMMENT ON COLUMN specialist_bot.execution_mode IS
    'prompt=마스터 프로세스 안의 어댑터, http=격리 컨테이너. http 인데 active '
    'deployment 가 없거나 health 가 정상이 아니면 라우팅 후보에서 뺀다.';

-- 어느 배포가 답했는지 남긴다. **질문·근거·응답 본문은 넣지 않는다.**
ALTER TABLE specialist_call ADD COLUMN IF NOT EXISTS deployment_id bigint;
ALTER TABLE specialist_call ADD COLUMN IF NOT EXISTS runtime_version text NOT NULL DEFAULT '';
ALTER TABLE specialist_call ADD COLUMN IF NOT EXISTS http_status integer;
ALTER TABLE specialist_call ADD COLUMN IF NOT EXISTS fallback_reason text NOT NULL DEFAULT '';

COMMIT;
