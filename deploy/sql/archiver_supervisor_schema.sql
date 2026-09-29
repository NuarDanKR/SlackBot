-- Archiver supervisor 운영 상태와 소급 수집
--
-- 설계: docs/design/archiver-supervisor-backfill-console-2026-09-29.md §3.2·§4.2
-- 순수 상태 전이: src/tybot/archive/supervisor_state.py
--
-- ## 왜 연결 표에 안 붙이나
--
-- `bot_connection` 은 **자격증명의 정본**이다. 거기에 「지금 돌고 있나」 를 같이
-- 담으면 두 가지가 한 행에서 싸운다 — 토큰을 바꾼 것과 worker 가 죽은 것이 같은
-- `state` 칸을 두고 다투고, 그때 화면은 어느 쪽을 보여 줄지 정할 수 없다.
--
-- 그래서 **희망 상태(desired)와 관측 상태(observed)를 따로** 둔다. 사람이 원하는
-- 것과 프로세스가 실제로 하는 것은 다를 수 있고, 그 차이가 곧 「무엇이 고장났나」 다.
--
-- ## 파일럿 범위
--
-- `desired_mode` 는 `off`·`shadow`·`live` 를 표현할 수 있지만, 파일럿에서는
-- `live` 를 **코드가 거절한다**(`supervisor_state.plan_desired_change`). 스키마에서
-- 아예 빼지 않는 이유는, 나중에 열 때 표를 고치면 그 시점에 지문이 또 바뀌기 때문이다.
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. workspace 별 수집 희망·관측 상태
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS archiver_workspace_runtime (
    workspace       text PRIMARY KEY REFERENCES workspace(key) ON DELETE CASCADE,
    -- 사람이 원하는 것. 콘솔이 바꾼다.
    desired_mode    text NOT NULL DEFAULT 'off'
                    CHECK (desired_mode IN ('off', 'shadow', 'live')),
    -- 프로세스가 실제로 하는 것. supervisor 가 쓴다.
    --
    -- `degraded` 는 **돌지만 온전치 않다** 는 뜻이다(예: 일부 채널 권한 없음).
    -- `error` 와 합치면 「멈췄다」 와 「절반만 된다」 를 구분할 수 없다.
    observed_state  text NOT NULL DEFAULT 'stopped'
                    CHECK (observed_state IN ('stopped', 'starting', 'running',
                                              'degraded', 'error')),
    -- 설정 세대. supervisor 는 이 값이 바뀐 것을 보고 다시 읽는다.
    --
    -- 시각으로 비교하지 않는다 — 서버와 DB 시계가 다르면 갱신을 놓치고, 그때
    -- worker 는 옛 설정으로 계속 돈다.
    generation      bigint NOT NULL DEFAULT 1,
    -- worker 가 살아 있다고 말한 마지막 시각. 없으면 **한 번도 안 떴다.**
    heartbeat_at    timestamptz,
    -- 마지막으로 Slack 이벤트를 받은 시각과 마지막으로 원문을 디스크에 쓴 시각.
    --
    -- 둘을 나누는 이유: 이벤트는 오는데 안 써지는 상태가 제일 나쁘다. 하나로
    -- 합치면 그 상태가 「정상」 으로 보인다.
    last_event_at   timestamptz,
    last_write_at   timestamptz,
    -- 사람이 읽을 사유와 기계가 분류할 코드. **본문·토큰은 넣지 않는다.**
    error_code      text NOT NULL DEFAULT '',
    error_note      text NOT NULL DEFAULT '',
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL DEFAULT ''
);

COMMENT ON TABLE archiver_workspace_runtime IS
    '워크스페이스별 수집 희망·관측 상태. 자격증명은 bot_connection 이 정본이다.';

-- 예전 초안은 `off + running`을 금지했다. 하지만 shadow worker가 도는 중에 사람이
-- off를 누르면 supervisor가 generation을 읽고 멈출 때까지 그 조합이 반드시 생긴다.
-- desired/observed의 차이는 저장하고, 오래 지속될 때 운영 화면에서 장애로 알린다.
ALTER TABLE archiver_workspace_runtime
    DROP CONSTRAINT IF EXISTS archiver_runtime_off_is_not_running;

ALTER TABLE archiver_workspace_runtime
    DROP CONSTRAINT IF EXISTS archiver_runtime_generation_positive;
ALTER TABLE archiver_workspace_runtime ADD CONSTRAINT archiver_runtime_generation_positive
    CHECK (generation >= 1);

-- 오류 상태가 아닌데 코드가 남아 있으면 화면이 「정상인데 빨간 글씨」 를 보여 준다.
ALTER TABLE archiver_workspace_runtime
    DROP CONSTRAINT IF EXISTS archiver_runtime_error_only_when_error;
ALTER TABLE archiver_workspace_runtime ADD CONSTRAINT archiver_runtime_error_only_when_error
    CHECK (observed_state IN ('error', 'degraded') OR error_code = '');


-- ---------------------------------------------------------------------------
-- 2. 채널별 cursor
-- ---------------------------------------------------------------------------
-- 실시간이 어디까지 왔고 소급이 어디까지 읽었나. **둘을 따로 둔다** — 소급이
-- 실시간보다 뒤처진 구간이 곧 「아직 안 메운 곳」 이다.
CREATE TABLE IF NOT EXISTS archive_channel_cursor (
    workspace        text NOT NULL,
    channel_id       text NOT NULL,
    -- 실시간으로 마지막으로 받은 메시지의 Slack ts.
    last_realtime_ts text NOT NULL DEFAULT '',
    -- 소급으로 마지막까지 읽어 **디스크에 쓴** 지점. 읽기만 한 곳은 포함하지
    -- 않는다 — 쓰기 전에 전진시키면 그 구간은 영영 안 메워진다.
    last_history_ts  text NOT NULL DEFAULT '',
    last_success_at  timestamptz,
    -- Slack 이 기다리라고 한 시각. 그전에는 다시 부르지 않는다.
    retry_after      timestamptz,
    status           text NOT NULL DEFAULT 'idle'
                     CHECK (status IN ('idle', 'running', 'blocked', 'error')),
    error_code       text NOT NULL DEFAULT '',
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id)
);

COMMENT ON TABLE archive_channel_cursor IS
    '채널별 실시간·소급 진행 좌표. 쓰기가 끝난 뒤에만 전진한다.';


-- ---------------------------------------------------------------------------
-- 3. 소급 수집 작업
-- ---------------------------------------------------------------------------
-- **0건과 실패를 같은 결과로 만들지 않는다**(§4.2). 「아무것도 없었다」 와
-- 「못 읽었다」 는 사람이 할 일이 다르다.
CREATE TABLE IF NOT EXISTS archive_backfill_job (
    id            bigserial PRIMARY KEY,
    workspace     text NOT NULL,
    -- 비우면 그 워크스페이스의 대상 채널 전부.
    channel_id    text NOT NULL DEFAULT '',
    from_ts       text NOT NULL DEFAULT '',
    to_ts         text NOT NULL DEFAULT '',
    dry_run       boolean NOT NULL DEFAULT true,
    state         text NOT NULL DEFAULT 'queued'
                  CHECK (state IN ('queued', 'running', 'partial', 'succeeded',
                                   'failed', 'cancelled')),
    requested_by  text NOT NULL,
    reason        text NOT NULL,
    -- 무엇을 몇 건 했나. 발견과 기록을 나눠 센다 — 같은 수가 아니고, 다르면
    -- 그 차이가 곧 중복·거부·실패다.
    found_count     integer NOT NULL DEFAULT 0,
    written_count   integer NOT NULL DEFAULT 0,
    duplicate_count integer NOT NULL DEFAULT 0,
    refused_count   integer NOT NULL DEFAULT 0,
    failed_count    integer NOT NULL DEFAULT 0,
    -- 본문·토큰 없는 코드만. 사람이 읽을 문장은 화면이 코드로 만든다.
    error_code    text NOT NULL DEFAULT '',
    started_at    timestamptz,
    finished_at   timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE archive_backfill_job IS
    '소급 수집 작업. dry-run 은 본문을 남기지 않고 건수만 센다.';

CREATE INDEX IF NOT EXISTS archive_backfill_job_recent
    ON archive_backfill_job (workspace, created_at DESC);

-- 끝난 작업에는 끝난 시각이 있어야 한다. 없으면 화면이 「도는 중」 으로 보여 주고,
-- 사람은 멈춘 작업을 계속 기다린다.
ALTER TABLE archive_backfill_job DROP CONSTRAINT IF EXISTS archive_backfill_job_finished;
ALTER TABLE archive_backfill_job ADD CONSTRAINT archive_backfill_job_finished
    CHECK (
        (state IN ('queued', 'running') AND finished_at IS NULL)
        OR (state NOT IN ('queued', 'running') AND finished_at IS NOT NULL)
    );

ALTER TABLE archive_backfill_job DROP CONSTRAINT IF EXISTS archive_backfill_job_counts_nonnegative;
ALTER TABLE archive_backfill_job ADD CONSTRAINT archive_backfill_job_counts_nonnegative
    CHECK (
        found_count >= 0 AND written_count >= 0 AND duplicate_count >= 0
        AND refused_count >= 0 AND failed_count >= 0
    );

ALTER TABLE archive_backfill_job DROP CONSTRAINT IF EXISTS archive_backfill_job_request_nonempty;
ALTER TABLE archive_backfill_job ADD CONSTRAINT archive_backfill_job_request_nonempty
    CHECK (btrim(requested_by) <> '' AND btrim(reason) <> '');


-- ---------------------------------------------------------------------------
-- 4. 권한
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE'
                ' archiver_workspace_runtime, archive_channel_cursor,'
                ' archive_backfill_job TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE archive_backfill_job_id_seq'
                ' TO tyslackai';
        -- 지우는 권한은 주지 않는다. 작업 기록을 지우면 「그때 무엇을 돌렸나」 가
        -- 사라지고, 소급은 되돌릴 수 없는 종류의 일이다.
        EXECUTE 'REVOKE DELETE ON TABLE'
                ' archiver_workspace_runtime, archive_channel_cursor,'
                ' archive_backfill_job FROM tyslackai';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_archiver') THEN
        -- supervisor 는 관측 상태·cursor·작업 진행을 쓴다. 희망 상태(desired_mode)는
        -- 사람이 정하는 값이라 **읽기만** 한다 — 프로세스가 자기 희망을 적으면
        -- 콘솔에서 끈 것이 되살아난다.
        -- **열 단위로 준다.** 표 전체에 UPDATE 를 주면 주석이 말하는 경계를
        -- 코드가 지키지 않는다 — 권한은 문장이 아니라 GRANT 가 정한다.
        EXECUTE 'REVOKE UPDATE ON TABLE archiver_workspace_runtime,'
                ' archive_channel_cursor, archive_backfill_job FROM tybot_archiver';
        EXECUTE 'GRANT SELECT ON TABLE archiver_workspace_runtime TO tybot_archiver';
        EXECUTE 'GRANT UPDATE (observed_state, heartbeat_at, last_event_at,'
                ' last_write_at, error_code, error_note, updated_at, updated_by)'
                ' ON TABLE archiver_workspace_runtime TO tybot_archiver';
        EXECUTE 'GRANT SELECT, INSERT ON TABLE archive_channel_cursor'
                ' TO tybot_archiver';
        EXECUTE 'GRANT UPDATE (last_realtime_ts, last_history_ts, last_success_at,'
                ' retry_after, status, error_code, updated_at)'
                ' ON TABLE archive_channel_cursor TO tybot_archiver';
        EXECUTE 'GRANT SELECT ON TABLE archive_backfill_job TO tybot_archiver';
        EXECUTE 'GRANT UPDATE (state, found_count, written_count, duplicate_count,'
                ' refused_count, failed_count, error_code, started_at, finished_at)'
                ' ON TABLE archive_backfill_job TO tybot_archiver';
        EXECUTE 'REVOKE DELETE ON TABLE'
                ' archiver_workspace_runtime, archive_channel_cursor,'
                ' archive_backfill_job FROM tybot_archiver';
    END IF;
END
$$;

COMMIT;
