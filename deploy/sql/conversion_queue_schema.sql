-- 첨부 변환 재처리 큐 (B-45 §5)
--
-- 설계: docs/design/operational-warning-recovery-and-answer-progress.md
--
-- **payload 에 원문도 토큰도 없다.** 여기 있는 것은 좌표(workspace/channel_id/
-- file_id/sha256)와 코드뿐이고, 워커는 그 좌표로 고정된 경로를 연다. 큐가
-- 유출되어도 업무 내용이 따라 나가지 않는다.
--
-- 적용:
--   sudo cat deploy/sql/conversion_queue_schema.sql \
--     | sudo -u postgres psql -p 55432 -d tyslackai -f -

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 작업
-- ---------------------------------------------------------------------------
-- 키는 `(workspace, channel_id, file_id, original_sha256, pipeline_version)` 이다.
--
-- `original_sha256` 이 키에 있는 이유: 같은 이름으로 **다른 내용**이 다시 올라오면
-- 그것은 다른 작업이다. 없으면 새 파일의 실패가 옛 파일의 성공에 가려진다.
--
-- `pipeline_version` 이 키에 있는 이유: 변환기를 고치면 과거에 실패한 파일을 다시
-- 시도해야 한다. 버전을 올리는 것만으로 새 작업이 되게 한다 — 표를 손으로
-- 비우는 절차를 만들면 아무도 안 한다.
CREATE TABLE IF NOT EXISTS conversion_job (
    id                bigserial PRIMARY KEY,
    workspace         text NOT NULL,
    channel_id        text NOT NULL,
    file_id           text NOT NULL,
    original_sha256   text NOT NULL DEFAULT '',
    pipeline_version  text NOT NULL DEFAULT '1',
    -- queued  다음 시각이 되면 실행한다
    -- leased  누군가 잡고 있다. 임대가 끝나면 회수된다
    -- succeeded 끝났다
    -- failed  자동으로 더 해 볼 것이 없다(파일 문제)
    -- held    환경이 복구될 때까지 멈춘다. `failed` 와 **사람이 할 일이 다르다**
    state             text NOT NULL DEFAULT 'queued'
                      CHECK (state IN ('queued', 'leased', 'succeeded', 'failed', 'held')),
    error_code        text NOT NULL DEFAULT '',
    attempt_count     integer NOT NULL DEFAULT 0,
    max_attempts      integer NOT NULL DEFAULT 4,
    next_attempt_at   timestamptz NOT NULL DEFAULT now(),
    -- 누가 잡고 있나. 회수할 때 누구 것이었는지 알아야 원인을 되짚는다.
    lease_owner       text NOT NULL DEFAULT '',
    lease_expires_at  timestamptz,
    converter         text NOT NULL DEFAULT '',
    converter_version text NOT NULL DEFAULT '',
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    -- 잡고 있는 작업에는 임대 만료가 있어야 한다. 없으면 그 행은 **영원히**
    -- 회수되지 않고, 그게 큐의 가장 흔한 고장이다.
    CONSTRAINT conversion_job_lease_has_expiry CHECK (
        state <> 'leased' OR lease_expires_at IS NOT NULL
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS conversion_job_identity
    ON conversion_job (workspace, channel_id, file_id, original_sha256, pipeline_version);

-- 워커가 매번 여는 질의다. 정렬까지 인덱스로 덮는다.
CREATE INDEX IF NOT EXISTS conversion_job_ready
    ON conversion_job (next_attempt_at) WHERE state = 'queued';

CREATE INDEX IF NOT EXISTS conversion_job_leased
    ON conversion_job (lease_expires_at) WHERE state = 'leased';

-- 콘솔 첨부 진단이 파일 하나의 최신 상태를 읽는다.
CREATE INDEX IF NOT EXISTS conversion_job_by_file
    ON conversion_job (workspace, channel_id, file_id, updated_at DESC);

-- ---------------------------------------------------------------------------
-- 2. 회로 차단기
-- ---------------------------------------------------------------------------
-- 파일이 아니라 **환경**이 고장났을 때, 파일마다 되풀이하면 같은 오류가 수천 줄
-- 쌓이고 정작 고쳐야 할 한 줄이 묻힌다.
--
-- 열린 회로가 스스로 닫히지는 않는다. 다만 일정 시간 뒤 한 건만 흘려 보내
-- 확인한다 — 안 그러면 사람이 고쳐도 큐가 회복되지 않는다.
CREATE TABLE IF NOT EXISTS conversion_breaker (
    converter         text NOT NULL,
    converter_version text NOT NULL DEFAULT '',
    state             text NOT NULL DEFAULT 'closed'
                      CHECK (state IN ('closed', 'open')),
    failure_count     integer NOT NULL DEFAULT 0,
    reason_code       text NOT NULL DEFAULT '',
    opened_at         timestamptz,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (converter, converter_version),
    -- 열린 회로에는 언제 열렸는지가 있어야 한다. 없으면 half-open 판정을 할 수
    -- 없어 영원히 열린 채로 남는다.
    CONSTRAINT conversion_breaker_open_has_time CHECK (
        state <> 'open' OR opened_at IS NOT NULL
    )
);

-- ---------------------------------------------------------------------------
-- 3. 권한
-- ---------------------------------------------------------------------------
-- **표만 만들고 GRANT 를 안 주면 봇에게는 그 표가 없는 것과 같다**(2026-09-14 실측).
-- `information_schema` 는 권한 필터가 걸린 뷰라, GRANT 가 없으면 표가 아예 안 보인다.
-- 그래서 「표가 없다」 는 오류가 나고, 스키마를 다시 적용해도 아무것도 달라지지 않는다.
--
-- 표를 `postgres` 로 만들면 소유자도 postgres 다. 소유자가 다르면 GRANT 는 자동으로
-- 따라오지 않으므로 여기서 명시한다.
DO $$
DECLARE
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE'
                ' conversion_job, conversion_breaker TO tyslackai';
        -- bigserial 시퀀스도 함께. 없으면 INSERT 만 권한 오류로 죽는다.
        --
        -- `ALL SEQUENCES IN SCHEMA public` 은 쓰지 않는다 — 남의 시퀀스까지 건드려서
        -- 적용하는 역할이 소유자가 아니면 **파일 전체가 실패한다.**
        FOR seq_name IN
            SELECT quote_ident(n.nspname) || '.' || quote_ident(s.relname)
              FROM pg_class s
              JOIN pg_namespace n ON n.oid = s.relnamespace
              JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
              JOIN pg_class t ON t.oid = d.refobjid
             WHERE s.relkind = 'S' AND t.relname = ANY(ARRAY['conversion_job'])
        LOOP
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE ' || seq_name || ' TO tyslackai';
        END LOOP;
    END IF;
END
$$;

COMMENT ON TABLE conversion_job IS
    '첨부 변환 재처리 큐. 좌표와 코드만 담는다 — 원문·토큰 금지.';
COMMENT ON COLUMN conversion_job.state IS
    'held 는 환경 복구 대기이고 failed 는 그 파일을 포기한 것이다. 사람이 할 일이 다르다.';

COMMIT;
