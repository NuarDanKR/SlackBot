-- 봇 근거 채널 — 「이 봇은 어느 채널 자료로 답하나」 를 채널 단위로 적는 표
--
-- 콘솔 > 채널 관리에서 채널을 골라 등록하고, 사용자 · 봇 권한 > VIP 봇 권한자 탭에서 본다.
-- 첫 사용처는 vipbot(경영진용) — 임원은 대개 실무 채널의 멤버가 아니므로, 「어느 채널을 열어
-- 줬는지」 가 사람 멤버십이 아니라 이 표로 정해진다(원칙 3·4: 막는 쪽이 기본, 화이트리스트만).
--
-- ## 규칙
-- - 등록된 채널만 근거가 된다. 비어 있으면 그 봇은 아무 채널도 보지 않는다.
-- - 봇 권한자(`bot_access`)는 누구나 이 목록 전체를 근거로 받는다(사람별 채널은 두지 않는다).
-- - 채널은 ID 로 건다. 이름은 등록 시점의 표시용 사본이다 — 이름이 바뀌어도 권한은 그대로다.
-- - 지우지 않고 해제한다(`removed_*`). 누가 언제 왜 열고 닫았는지가 남아야 한다.
-- - 판정은 `tybot.bot_access.source_channels()` 한 곳에서만 한다.
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

CREATE TABLE IF NOT EXISTS bot_source_channel (
    id             bigserial PRIMARY KEY,
    bot            text NOT NULL CHECK (bot ~ '^[a-z][a-z0-9_-]{1,39}$'),
    workspace      text NOT NULL CHECK (workspace ~ '^[a-z][a-z0-9-]{1,23}$'),
    channel_id     text NOT NULL CHECK (channel_id ~ '^[CG][A-Z0-9]{6,}$'),
    channel_name   text NOT NULL DEFAULT '',
    reason         text NOT NULL CHECK (btrim(reason) <> '' AND length(reason) <= 500),
    added_by       text NOT NULL CHECK (btrim(added_by) <> ''),
    added_at       timestamptz NOT NULL DEFAULT now(),
    removed_at     timestamptz,
    removed_by     text,
    remove_reason  text CHECK (remove_reason IS NULL OR length(remove_reason) <= 500),
    CONSTRAINT bot_source_remove_complete CHECK (
        (removed_at IS NULL AND removed_by IS NULL AND remove_reason IS NULL)
        OR (removed_at IS NOT NULL AND btrim(coalesce(removed_by, '')) <> ''
            AND btrim(coalesce(remove_reason, '')) <> '')
    )
);

COMMENT ON TABLE bot_source_channel IS
    '봇 근거 채널(채널 ID 단위). 등록된 채널만 근거. 지우지 않고 removed_* 로 해제한다.';

CREATE UNIQUE INDEX IF NOT EXISTS bot_source_one_open
    ON bot_source_channel (bot, workspace, channel_id) WHERE removed_at IS NULL;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE bot_source_channel TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE bot_source_channel_id_seq TO tyslackai';
    END IF;
END
$$;

COMMIT;
