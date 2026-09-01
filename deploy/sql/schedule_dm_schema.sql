-- 일정 알림 개인 DM — 스키마
--
-- 설계: docs/design/schedule-dm-reminders.md
-- 적용:  psql -U tybot -d tybot -f deploy/sql/schedule_dm_schema.sql
--
-- 여러 번 실행해도 안전하다(전부 IF NOT EXISTS 또는 존재 확인 후 추가).
--
-- ## 기존 채널 공지를 건드리지 않는다
-- `schedule_channel` 과 `schedule_delivery` 는 그대로 둔다. DM 은 **별도 경로**로
-- 추가한다 — 한 테이블에 두 전달 수단을 섞으면 멱등 키와 외래키가 서로를 깨뜨린다.
--
-- ## 되돌리기
--   DROP TABLE IF EXISTS schedule_dm_delivery;
--   DROP TABLE IF EXISTS schedule_dm_preference;
--   DROP TABLE IF EXISTS schedule_folder_org;
--   ALTER TABLE user_identity DROP CONSTRAINT IF EXISTS user_identity_workspace_user_emp_key;
-- 발송 이력을 지우면 "이미 보냈다"는 근거가 사라져 재적용 시 중복 발송이 난다.
-- 되돌리기 전에 schedule_dm_delivery 를 따로 백업할 것.

BEGIN;

-- ===========================================================================
-- 1. 폴더 ↔ 조직 다대다 허용 목록
-- ===========================================================================
--
-- `schedule_folder.org_code` 는 대표 조직 하나만 담는다. 실제 Oracle 폴더 ACL 은
-- 한 폴더를 여러 조직에 열 수 있다. 대표 하나로 수신자를 계산하면 **누락 또는 오발송**이
-- 생긴다. manifest 의 `folders` 배열은 후보 자료일 뿐이며 자동 승인하지 않는다.
CREATE TABLE IF NOT EXISTS schedule_folder_org (
    source_folder_id bigint NOT NULL
        REFERENCES schedule_folder(source_folder_id) ON DELETE RESTRICT,
    org_code         text NOT NULL REFERENCES org_unit(code) ON DELETE RESTRICT,
    enabled          boolean NOT NULL DEFAULT true,
    approved_by      text NOT NULL CHECK (btrim(approved_by) <> ''),
    approved_at      timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_folder_id, org_code)
);

CREATE INDEX IF NOT EXISTS schedule_folder_org_lookup
    ON schedule_folder_org (org_code) WHERE enabled;

COMMENT ON TABLE schedule_folder_org IS
    'Oracle 폴더 ACL 의 승인된 조직 목록. 관리자 승인 없이는 DM 대상이 되지 않는다.';

-- ===========================================================================
-- 2. 신원 무결성 — 선호 행이 실제 같은 사람인지 보장한다
-- ===========================================================================
--
-- 이 유니크가 없으면 schedule_dm_preference 가 (workspace, slack_user, emp_no) 로
-- 외래키를 걸 수 없다. 걸지 않으면 남의 사번으로 DM 수신을 켜는 것을 막을 수 없다.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'user_identity_workspace_user_emp_key'
    ) THEN
        ALTER TABLE user_identity
            ADD CONSTRAINT user_identity_workspace_user_emp_key
            UNIQUE (workspace, slack_user, emp_no);
    END IF;
END
$$;

-- ===========================================================================
-- 3. 사용자 수신 설정
-- ===========================================================================
--
-- 기본값은 **미수신**이다. 사람이 Slack 에서 켜야 한다.
-- `emp_no` 가 기본키인 이유: 한 사람은 여러 워크스페이스에 있어도 **대표 수신 위치가
-- 하나**여야 한다. 다른 워크스페이스에서 켜면 이 행이 그쪽으로 옮겨간다.
CREATE TABLE IF NOT EXISTS schedule_dm_preference (
    emp_no            text PRIMARY KEY REFERENCES employee(emp_no) ON DELETE RESTRICT,
    workspace         text NOT NULL REFERENCES workspace(key) ON DELETE RESTRICT,
    slack_user        text NOT NULL,
    reminder_minutes  smallint[] NOT NULL DEFAULT ARRAY[30]::smallint[],
    enabled           boolean NOT NULL DEFAULT true,
    updated_by        text NOT NULL CHECK (btrim(updated_by) <> ''),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (workspace, slack_user, emp_no)
        REFERENCES user_identity(workspace, slack_user, emp_no) ON DELETE RESTRICT,
    CONSTRAINT schedule_dm_preference_reminders CHECK (
        reminder_minutes = ARRAY[30]::smallint[]
        OR reminder_minutes = ARRAY[10]::smallint[]
        OR reminder_minutes = ARRAY[30, 10]::smallint[]
    )
);

CREATE INDEX IF NOT EXISTS schedule_dm_preference_on
    ON schedule_dm_preference (workspace) WHERE enabled;

COMMENT ON TABLE schedule_dm_preference IS
    '개인 DM 수신 설정. emp_no 당 한 행 = 대표 수신 워크스페이스 하나.';

-- ===========================================================================
-- 4. DM 발송 큐·이력
-- ===========================================================================
--
-- 중복 방지 키가 Slack 사용자 ID 가 **아니라** 사번인 이유: 대표 워크스페이스가 바뀌면
-- Slack ID 도 바뀌지만 같은 사람에게 두 번 보내면 안 된다.
--
-- 본문을 저장하지 않는다. 제목·장소·사용자 이름은 이 테이블 어디에도 넣지 않는다
-- (`last_error` 포함). 남길 것은 식별자·상태·시각·비민감 오류 코드다.
CREATE TABLE IF NOT EXISTS schedule_dm_delivery (
    id                bigserial PRIMARY KEY,
    source_folder_id  bigint NOT NULL,
    date_id           bigint NOT NULL,
    emp_no            text NOT NULL REFERENCES employee(emp_no) ON DELETE RESTRICT,
    workspace         text NOT NULL REFERENCES workspace(key) ON DELETE RESTRICT,
    slack_user        text NOT NULL,
    reminder_minutes  smallint NOT NULL CHECK (reminder_minutes IN (10, 30)),
    scheduled_for     timestamptz NOT NULL,
    status            text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending', 'sending', 'retry', 'sent',
                                        'cancelled', 'expired', 'no_identity', 'failed')),
    attempts          integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
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
    UNIQUE (source_folder_id, date_id, emp_no, reminder_minutes)
);

-- 발송 워커가 매분 훑는 경로. 보낼 것만 빠르게 집는다.
CREATE INDEX IF NOT EXISTS schedule_dm_delivery_due
    ON schedule_dm_delivery (scheduled_for)
    WHERE status IN ('pending', 'retry');
-- 일정이 바뀌거나 취소될 때 미발송 행을 찾는 경로.
CREATE INDEX IF NOT EXISTS schedule_dm_delivery_open
    ON schedule_dm_delivery (source_folder_id, date_id)
    WHERE status IN ('pending', 'retry', 'sending');

COMMENT ON TABLE schedule_dm_delivery IS
    '개인 DM 발송 큐·이력. 제목·장소·사용자 이름을 저장하지 않는다.';
COMMENT ON COLUMN schedule_dm_delivery.last_error IS
    'Slack 오류 코드와 비민감 요약만. 메시지 본문 금지.';

COMMIT;
