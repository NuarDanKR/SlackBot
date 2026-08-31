-- TYBot 팀 일정 공지 스키마 (PostgreSQL 16+)
--
-- 적용 순서:
--   psql -U <사용자> -d tyslackai -f deploy/sql/console_schema.sql
--   psql -U <사용자> -d tyslackai -f deploy/sql/schedule_schema.sql
--
-- 여러 번 실행해도 안전하다(IF NOT EXISTS). `workspace` 외래키 때문에
-- console_schema.sql 을 먼저 적용해야 한다.
--
-- | 성격 | 테이블 | DROP 되면 |
-- |---|---|---|
-- | Oracle 에서 재수집 | schedule_occurrence | 다음 동기화에서 복구 |
-- | 백업 필수 | schedule_folder, schedule_channel, schedule_delivery | 승인·발송 이력 유실 |
-- | 운영 이력 | schedule_sync_run | 과거 동기화 이력 유실, 다음 동기화는 가능 |
--
-- 일정 제목·장소는 MD 아카이브와 애플리케이션 로그에 쓰지 않는다.
--
-- ## 스냅샷은 어디서 오나 (2026-08-31 방식 A로 확정)
-- 봇 서버가 그룹웨어 Oracle 을 **읽기 전용으로 직접 조회**한다(1523/tcp 단방향).
-- SFTP 전송·내부망 배치서버는 쓰지 않는다.
--
--   scripts/schedule_export.py --mode live      # 향후 48시간, 1분 주기
--   scripts/schedule_export.py --mode reconcile # 향후 30일, 매시간
--
-- 두 모드 모두 `<모드>-<시각>/schedule.jsonl` + `manifest.json` 을 만든다.
-- **파일을 거치는 이유**: 조회가 반쯤 실패한 결과를 검사 없이 반영하지 않기 위해서다.
-- manifest 의 `snapshot_id`·`mode`·`generated_at`·`horizon_start`·`horizon_end`·
-- `source_folders` 는 아래 `schedule_sync_run` 컬럼과 이름이 같다.
-- `schedule.jsonl` 의 필드도 `schedule_occurrence` 컬럼과 이름이 같다.

BEGIN;

-- ===========================================================================
-- 1. Oracle 팀 일정 폴더 승인 목록
-- ===========================================================================

CREATE TABLE IF NOT EXISTS schedule_folder (
    source_folder_id bigint PRIMARY KEY,
    label            text NOT NULL CHECK (btrim(label) <> ''),
    enabled          boolean NOT NULL DEFAULT true,
    approved_by      text NOT NULL CHECK (btrim(approved_by) <> ''),
    approved_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- 이 폴더를 볼 수 있는 부서. Oracle 의 폴더 ACL(`SYS_OBJECT_ACL`)에서 나온다
-- (`V_TYSLACK_SCHEDULE_FOLDER`, 스냅샷 manifest 의 `folders`).
-- 폴더 ID 를 사람이 찾아 적는 대신, 부서 코드로 어느 워크스페이스에 붙일지 판단한다.
-- 한 폴더가 여러 부서에 열려 있으면 manifest 에 여러 행으로 오므로 여기서는
-- 대표 부서 하나만 둔다(연결은 schedule_channel 이 워크스페이스별로 맡는다).
ALTER TABLE schedule_folder ADD COLUMN IF NOT EXISTS org_code text;

COMMENT ON TABLE schedule_folder IS
    'Oracle 팀 일정 폴더 허용 목록. 개인 일정 폴더는 등록하지 않는다.';
COMMENT ON COLUMN schedule_folder.org_code IS
    'Oracle 폴더 ACL 에서 온 부서 코드. org_unit.code 와 같은 체계다.';

-- 한 Oracle 일정 폴더는 한 워크스페이스 안에서 공지 채널 하나에만 연결한다.
-- 한 공지 채널이 여러 팀 일정 폴더를 받는 것은 허용한다.
CREATE TABLE IF NOT EXISTS schedule_channel (
    workspace         text NOT NULL REFERENCES workspace(key) ON DELETE RESTRICT,
    source_folder_id  bigint NOT NULL REFERENCES schedule_folder(source_folder_id)
                      ON DELETE RESTRICT,
    slack_channel_id  text NOT NULL
                      CHECK (slack_channel_id ~ '^[CG][A-Z0-9]{8,}$'),
    channel_label     text NOT NULL CHECK (btrim(channel_label) <> ''),
    -- 허용값은 30분, 10분, 또는 둘 다다. 기본은 공지 피로가 낮은 30분 1회다.
    reminder_minutes  smallint[] NOT NULL DEFAULT ARRAY[30]::smallint[],
    enabled           boolean NOT NULL DEFAULT true,
    updated_by        text NOT NULL CHECK (btrim(updated_by) <> ''),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, source_folder_id),
    CONSTRAINT schedule_channel_reminders CHECK (
        reminder_minutes = ARRAY[30]::smallint[]
        OR reminder_minutes = ARRAY[10]::smallint[]
        OR reminder_minutes = ARRAY[30, 10]::smallint[]
    )
);

CREATE INDEX IF NOT EXISTS schedule_channel_target
    ON schedule_channel (workspace, slack_channel_id) WHERE enabled;

COMMENT ON TABLE schedule_channel IS
    '팀 일정 폴더를 Slack 공지 채널 하나에 연결한다. 사용자별 DM에는 사용하지 않는다.';

-- ===========================================================================
-- 2. 수신 스냅샷 이력
-- ===========================================================================

CREATE TABLE IF NOT EXISTS schedule_sync_run (
    snapshot_id     text PRIMARY KEY
                    CHECK (snapshot_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{7,127}$'),
    mode            text NOT NULL CHECK (mode IN ('live', 'reconcile')),
    generated_at    timestamptz NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    applied_at      timestamptz,
    horizon_start   timestamptz NOT NULL,
    horizon_end     timestamptz NOT NULL,
    source_folders  bigint[] NOT NULL CHECK (
                        cardinality(source_folders) > 0
                        AND array_position(source_folders, NULL) IS NULL
                    ),
    row_count       integer NOT NULL CHECK (row_count >= 0),
    manifest_sha256 text NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    status          text NOT NULL DEFAULT 'received'
                    CHECK (status IN ('received', 'applied', 'rejected')),
    error           text,
    CONSTRAINT schedule_sync_horizon CHECK (horizon_end > horizon_start),
    CONSTRAINT schedule_sync_state CHECK (
        (status = 'received' AND applied_at IS NULL AND error IS NULL)
        OR (status = 'applied' AND applied_at IS NOT NULL AND error IS NULL)
        OR (status = 'rejected' AND applied_at IS NULL AND error IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS schedule_sync_recent
    ON schedule_sync_run (generated_at DESC);
CREATE INDEX IF NOT EXISTS schedule_sync_live_health
    ON schedule_sync_run (applied_at DESC) WHERE mode = 'live' AND status = 'applied';

COMMENT ON TABLE schedule_sync_run IS
    '일정 스냅샷 검증·반영 이력. 제목과 장소를 error에 기록하지 않는다.';

-- ===========================================================================
-- 3. 일정 발생 건 (Oracle 재수집 가능)
-- ===========================================================================

CREATE TABLE IF NOT EXISTS schedule_occurrence (
    source_folder_id  bigint NOT NULL REFERENCES schedule_folder(source_folder_id)
                      ON DELETE RESTRICT,
    date_id           bigint NOT NULL,
    event_id          bigint NOT NULL,
    link_event_id     bigint,
    subject           text,
    place             text,
    starts_at         timestamptz NOT NULL,
    ends_at           timestamptz NOT NULL,
    is_all_day        boolean NOT NULL DEFAULT false,
    is_repeat         boolean NOT NULL DEFAULT false,
    source_modified_at timestamptz,
    source_deleted_at  timestamptz,
    content_sha256    text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    last_snapshot_id  text NOT NULL REFERENCES schedule_sync_run(snapshot_id)
                      ON DELETE RESTRICT,
    first_seen_at     timestamptz NOT NULL DEFAULT now(),
    last_seen_at      timestamptz NOT NULL DEFAULT now(),
    details_purged_at timestamptz,
    PRIMARY KEY (source_folder_id, date_id),
    CONSTRAINT schedule_occurrence_time CHECK (ends_at >= starts_at),
    CONSTRAINT schedule_occurrence_purge CHECK (
        details_purged_at IS NULL OR (subject IS NULL AND place IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS schedule_occurrence_upcoming
    ON schedule_occurrence (starts_at)
    WHERE source_deleted_at IS NULL AND NOT is_all_day;
CREATE INDEX IF NOT EXISTS schedule_occurrence_event
    ON schedule_occurrence (source_folder_id, event_id);
CREATE INDEX IF NOT EXISTS schedule_occurrence_retention
    ON schedule_occurrence (ends_at) WHERE details_purged_at IS NULL;

COMMENT ON TABLE schedule_occurrence IS
    'Oracle 일정 발생 건 캐시. 제목·장소는 종료 7일 후 NULL 처리하고 MD에 넣지 않는다.';

-- ===========================================================================
-- 4. Slack 공지 발송 큐·이력
-- ===========================================================================

CREATE TABLE IF NOT EXISTS schedule_delivery (
    id                bigserial PRIMARY KEY,
    source_folder_id  bigint NOT NULL,
    date_id           bigint NOT NULL,
    workspace         text NOT NULL,
    slack_channel_id  text NOT NULL
                      CHECK (slack_channel_id ~ '^[CG][A-Z0-9]{8,}$'),
    reminder_minutes  smallint NOT NULL CHECK (reminder_minutes IN (10, 30)),
    scheduled_for     timestamptz NOT NULL,
    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'sending', 'retry', 'sent',
                                        'cancelled', 'failed')),
    attempts          smallint NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at   timestamptz,
    locked_at         timestamptz,
    locked_by         text,
    slack_message_ts  text,
    sent_at           timestamptz,
    cancelled_at      timestamptz,
    last_error        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (source_folder_id, date_id)
        REFERENCES schedule_occurrence(source_folder_id, date_id) ON DELETE RESTRICT,
    FOREIGN KEY (workspace, source_folder_id)
        REFERENCES schedule_channel(workspace, source_folder_id) ON DELETE RESTRICT,
    -- 채널 매핑이 나중에 바뀌어도 이미 발송한 대상은 이 행에 그대로 남긴다.
    UNIQUE (source_folder_id, date_id, workspace, reminder_minutes),
    CONSTRAINT schedule_delivery_state CHECK (
        (status = 'sent' AND sent_at IS NOT NULL AND cancelled_at IS NULL)
        OR (status = 'cancelled' AND cancelled_at IS NOT NULL AND sent_at IS NULL)
        OR (status NOT IN ('sent', 'cancelled') AND sent_at IS NULL AND cancelled_at IS NULL)
    )
);

-- 여러 워커가 FOR UPDATE SKIP LOCKED 로 이 인덱스를 훑는다.
CREATE INDEX IF NOT EXISTS schedule_delivery_due
    ON schedule_delivery ((COALESCE(next_attempt_at, scheduled_for)), id)
    WHERE status IN ('pending', 'retry');
CREATE INDEX IF NOT EXISTS schedule_delivery_stale_lock
    ON schedule_delivery (locked_at)
    WHERE status = 'sending';
CREATE INDEX IF NOT EXISTS schedule_delivery_retention
    ON schedule_delivery ((COALESCE(sent_at, cancelled_at, updated_at)));

COMMENT ON TABLE schedule_delivery IS
    'Slack 팀 공지 채널 발송 큐와 멱등 이력. 동일 발생 건·워크스페이스·알림 시점은 한 번만 생성한다.';
COMMENT ON COLUMN schedule_delivery.last_error IS
    '제목·장소·Slack 메시지 본문을 넣지 않는다. 오류 코드와 비민감 요약만 저장한다.';

COMMIT;

-- 운영 역할 권한 예시:
--   GRANT SELECT, INSERT, UPDATE, DELETE
--     ON schedule_folder, schedule_channel TO tybot_console;
--   GRANT SELECT, INSERT, UPDATE, DELETE
--     ON schedule_sync_run, schedule_occurrence, schedule_delivery TO tybot_bot;
--   GRANT SELECT ON schedule_folder, schedule_channel TO tybot_bot;
--   GRANT USAGE, SELECT ON SEQUENCE schedule_delivery_id_seq TO tybot_bot;
