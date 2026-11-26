-- B-100 이미지 처리 상태 댓글 (2026-11-24 오너 결정 · 2026-11-26 구현)
--
-- 코드: src/tybot/archive/image_status_notice.py
--
-- 이미지 하나 = 행 하나 = Slack 스레드 댓글 하나. 상태가 **실제로 앞으로 갈 때만**
-- version 이 오르고, Archiver 는 version > delivered_version 인 행만 Slack 에 반영한다
-- (`chat.update` 로 같은 댓글을 고친다). 같은 단계를 다시 적는 것은 아무것도 바꾸지 않는다.
--
-- ## Slack 대상은 실시간 접수뿐이다
-- slack_eligible 은 Archiver 가 **실시간 수집에서 접수한** 이미지에만 켠다. 소급·과거
-- 아카이브 이미지는 worker 가 처리해도 DB·콘솔에만 남고 댓글이 생기지 않는다.
--
-- ## 칸마다 주인이 있다
--   단계 칸(worker·Archiver)  phase · job_id · job_created_at · payload · version · updated_at
--   전달 칸(Archiver 만)      slack_eligible · reply_ts · delivered_version · delivery_state ·
--                             attempt_version · attempts · last_attempt_at · next_attempt_at ·
--                             error_code
-- worker(tybot_vision)는 단계 칸만 INSERT·UPDATE 한다 — 열 권한으로 강제한다. 새 판이
-- 「보낼 차례」 인지는 전달 칸을 지우는 대신 attempt_version < version 으로 읽는다.
-- 콘솔(같은 Vision 계정)의 재전송은 좁은 함수 image_notice_resend() 로만 한다.
--
-- 누가 쓰나
--   tybot_archiver — 접수, 댓글 게시·수정 결과
--   tybot_vision   — 처리 중·완료·확인 필요·보류·실패(Slack 토큰 없음), 콘솔 조회·재전송 함수
--   tyslackai      — Archiver 가 앱 계정(DATABASE_URL)으로 도는 서버의 같은 역할
--
-- 본문 칸(payload)에는 **가린 뒤** 자른 값만 들어간다 — 파일 이름(가림), 기본 OCR 상태,
-- 가드를 지난 설명·태그·전사 12줄. 예외 본문·provider 응답·키는 넣지 않는다.
--
-- archiver_image_description_schema.sql 뒤에 적용한다(정책 표를 읽는 함수가 있다).

BEGIN;

CREATE TABLE IF NOT EXISTS image_status_notice (
    workspace text NOT NULL CHECK (workspace ~ '^[a-z0-9][a-z0-9_-]{0,63}$'),
    channel_id text NOT NULL CHECK (channel_id ~ '^[CG][A-Z0-9]{8,31}$'),
    message_ts text NOT NULL CHECK (message_ts ~ '^[0-9]{1,20}\.[0-9]{1,9}$'),
    file_id text NOT NULL CHECK (file_id ~ '^F[A-Z0-9]{8,31}$'),
    phase text NOT NULL CHECK (phase IN (
        'accepted','processing','completed','uncertain','deferred','failed'
    )),
    job_id text NOT NULL DEFAULT '' CHECK (job_id = '' OR job_id ~ '^[a-f0-9]{32}$'),
    job_created_at timestamptz,
    payload jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(payload) = 'object'),
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    slack_eligible boolean NOT NULL DEFAULT false,
    delivered_version integer NOT NULL DEFAULT 0 CHECK (delivered_version >= 0),
    reply_ts text NOT NULL DEFAULT ''
        CHECK (reply_ts = '' OR reply_ts ~ '^[0-9]{1,20}\.[0-9]{1,9}$'),
    delivery_state text NOT NULL DEFAULT 'pending' CHECK (delivery_state IN (
        'pending','retrying','delivered','failed_delivery'
    )),
    attempt_version integer NOT NULL DEFAULT 0 CHECK (attempt_version >= 0),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_attempt_at timestamptz,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    error_code text NOT NULL DEFAULT '' CHECK (error_code ~ '^[a-z0-9_]{0,64}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id, message_ts, file_id),
    CHECK (delivered_version <= version),
    CHECK (delivery_state <> 'delivered' OR reply_ts <> ''),
    -- 대상이 아닌 행은 Slack 에 나간 적이 없다.
    CHECK (slack_eligible OR (reply_ts = '' AND delivered_version = 0))
);
CREATE INDEX IF NOT EXISTS image_status_notice_due
    ON image_status_notice(workspace, slack_eligible, next_attempt_at);
CREATE INDEX IF NOT EXISTS image_status_notice_recent
    ON image_status_notice(workspace, updated_at DESC);

-- Archiver 는 Vision 표를 열지 못한다(check_schema_drift 가 막는다). 접수 댓글에
-- 「Vision 처리 대기」 와 「기본 OCR 만 씀」 을 가르려면 정책이 켜졌는지만 알면 된다.
-- 참·거짓 하나만 돌려주는 함수로 연다. 판정은 `ImageDescriptionStore._effective` 와 같다
-- (전역 행 AND 워크스페이스 행).
CREATE OR REPLACE FUNCTION image_notice_vision_enabled(p_workspace text)
RETURNS boolean
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
    SELECT COALESCE(
               (SELECT enabled FROM archiver_image_description_policy WHERE workspace = ''),
               false)
       AND COALESCE(
               (SELECT enabled FROM archiver_image_description_policy
                 WHERE workspace = p_workspace AND p_workspace <> ''),
               false)
$$;

-- 콘솔 「다시 보내기」. 전달 실패한 **현재 판**을 다시 대기로 돌린다. 이것 말고는 콘솔(Vision
-- 계정)이 전달 칸을 바꿀 길이 없다. 대상이 아닌 행(소급 이미지)은 되돌리지 않는다.
CREATE OR REPLACE FUNCTION image_notice_resend(
    p_workspace text, p_channel_id text, p_message_ts text, p_file_id text
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    v_count integer;
BEGIN
    UPDATE image_status_notice
       SET delivery_state = 'pending', attempts = 0, error_code = '',
           next_attempt_at = now(), updated_at = now()
     WHERE workspace = p_workspace AND channel_id = p_channel_id
       AND message_ts = p_message_ts AND file_id = p_file_id
       AND slack_eligible
       AND version > delivered_version
       AND attempt_version = version
       AND delivery_state IN ('failed_delivery', 'retrying');
    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count = 1;
END
$$;

REVOKE ALL ON TABLE image_status_notice FROM PUBLIC;
REVOKE ALL ON FUNCTION image_notice_vision_enabled(text) FROM PUBLIC;
REVOKE ALL ON FUNCTION image_notice_resend(text, text, text, text) FROM PUBLIC;

DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_archiver') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE image_status_notice TO tybot_archiver;
        GRANT EXECUTE ON FUNCTION image_notice_vision_enabled(text) TO tybot_archiver;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_vision') THEN
        -- 표 전체 권한은 좁은 GRANT 로 사라지지 않는다 — 먼저 회수한다.
        REVOKE ALL ON TABLE image_status_notice FROM tybot_vision;
        GRANT SELECT ON TABLE image_status_notice TO tybot_vision;
        GRANT INSERT (workspace, channel_id, message_ts, file_id, phase, job_id,
                      job_created_at, payload)
            ON TABLE image_status_notice TO tybot_vision;
        GRANT UPDATE (phase, job_id, job_created_at, payload, version, updated_at)
            ON TABLE image_status_notice TO tybot_vision;
        GRANT EXECUTE ON FUNCTION image_notice_resend(text, text, text, text) TO tybot_vision;
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        GRANT SELECT, INSERT, UPDATE ON TABLE image_status_notice TO tyslackai;
        GRANT EXECUTE ON FUNCTION image_notice_vision_enabled(text) TO tyslackai;
    END IF;
END $$;

COMMIT;
