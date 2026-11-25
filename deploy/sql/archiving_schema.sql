-- Archiving Bot — 채널 모드 · 수집 상태 · revision · 감사
--
-- 결정: 2026-09-25 오너 확정 (분리 설계 `docs/design/archiving-bot-separation-2026-09-23.md`,
--       구조 2 확정 `docs/verification/2026-09-23-archive-layout-benchmark.md`)
-- 설계: docs/design/archiving-bot-decisions-2026-09-25.md
--
-- ## 이 스키마가 지키는 것
--
-- 1. **두 writer 가 같은 채널에 동시에 쓰지 않는다.** 채널마다 주인이 하나이고,
--    주인이 바뀐 시각(watermark)이 남는다
-- 2. **「검색된다」 를 첨부가 끝나기 전에 말하지 않는다.** 메시지와 첨부의 준비
--    상태를 따로 들고, 둘 다 끝나야 `ready` 다
-- 3. **원문을 고치지 않는다.** 수정·삭제도 새 revision 으로 쌓는다
-- 4. **거부된 것은 본문을 남기지 않는다.** 좌표·hash·사유 코드만 남는다
-- 5. **설정 변경은 지워지지 않는다.** append-only 감사로 남는다
--
-- ## 여기에 없는 것
--
-- 원문 본문·첨부 본문·토큰. 본문은 MD 파일에, 원본은 `objects/` 에 있다. 이 표는
-- **좌표와 상태**만 든다 — DB 가 유출되어도 업무 내용이 따라 나가지 않는다.
--
-- 적용:
--   sudo /opt/tybot/deploy/apply-schema.sh

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 채널 모드와 writer 주인
-- ---------------------------------------------------------------------------
-- **콘솔에서 관리한다**(CLAUDE.md 「운영 기능은 콘솔에 둔다」). ENV 로 두면 채널을
-- 늘릴 때마다 SSH 가 필요하고, 그건 쓸 수 있는 사람이 한 명이라는 뜻이다.
--
-- mode 와 writer_owner 를 **따로** 두는 이유: 「아카이빙 봇이 이 채널을 보고 있다」
-- 와 「이 채널의 원문을 누가 쓴다」 는 다른 사실이다. paused 에서는 주인을 기억하지만
-- 아무도 쓰지 않는다. 한 칸으로 합치면 멈춘 채널이 어느 쪽 몫인지 표현할 수 없다.
CREATE TABLE IF NOT EXISTS archive_channel_mode (
    workspace       text NOT NULL,
    channel_id      text NOT NULL,
    -- off      Archiver 는 수집하지 않고 Master 가 운영 원문을 쓴다
    -- shadow   **과거값**(B-99 2단계 D2). 그림자 수집은 폐기됐다 — 새로 적지 않고,
    --          남은 행이 0 이 되면 아래 가드 블록이 CHECK 에서 뺀다
    -- active   수집해서 운영 아카이브에 쓴다
    -- paused   잠시 멈춘다. 재개하면 watermark 뒤부터 이어 받는다
    mode            text NOT NULL DEFAULT 'off'
                    CHECK (mode IN ('off', 'shadow', 'active', 'paused')),
    -- 이 채널의 운영 원문을 **누가 쓰는가**. 둘이 동시에 쓰면 줄이 섞이고
    -- doc_count 갱신이 유실된다. 값은 하나뿐이라 동시 쓰기가 표현되지 않는다.
    writer_owner    text NOT NULL DEFAULT 'master'
                    CHECK (writer_owner IN ('master', 'archiver')),
    -- 주인이 바뀐 지점. 이 ts **이후**가 새 주인 몫이다.
    --
    -- 왜 필요한가: 인수 순간에 「이미 쓴 것을 또 쓰나」 와 「아무도 안 쓴 구간이
    -- 있나」 가 생긴다. 둘 다 조용한 실패다 — 전자는 중복, 후자는 누락이고
    -- 어느 쪽도 오류를 내지 않는다. 좌표를 박아 두면 나중에 대조할 수 있다.
    cutover_ts      text NOT NULL DEFAULT '',
    cutover_at      timestamptz,
    -- 파일럿 표시. 상한 표시용이었으나 **채널 선택이 초대 기반으로 바뀌면서**
    -- 상한 자체가 사라졌다(2026-09-29). 값은 화면 분류에만 쓴다.
    is_pilot        boolean NOT NULL DEFAULT true,
    note            text NOT NULL DEFAULT '',
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL DEFAULT '',
    PRIMARY KEY (workspace, channel_id)
);

-- ---------------------------------------------------------------------------
-- 초대 기반 채널 발견 (2026-09-29 오너 지시)
-- ---------------------------------------------------------------------------
-- 수집 대상의 정본은 **Archiving Bot 이 초대된 채널**이다. 사람이 채널 ID 를
-- 손으로 적던 방식은 두 가지를 못 막았다.
--
-- 1. 초대는 했는데 등록을 잊어 **수집이 안 되는 채널.** 사람은 초대했으니 된 줄 안다
-- 2. 초대는 풀었는데 목록에 남아 **권한 오류만 쌓는 채널.** 오류는 로그에만 남는다
--
-- 앱 설치만으로 전부 수집하지는 않는다. 초대가 있어야 한다 — 그래서 `membership`
-- 이 `joined` 일 때만 수집한다. 채널 이름 규칙은 보지 않는다(규칙에 안 맞아도
-- 초대됐으면 그 방 사람들은 수집을 기대한다).
ALTER TABLE archive_channel_mode
    ADD COLUMN IF NOT EXISTS membership text NOT NULL DEFAULT 'unknown';
ALTER TABLE archive_channel_mode DROP CONSTRAINT IF EXISTS archive_channel_mode_membership;
ALTER TABLE archive_channel_mode ADD CONSTRAINT archive_channel_mode_membership
    CHECK (membership IN ('joined', 'left', 'unknown'));

-- 언제 확인한 멤버십인가. 오래된 값을 「지금 참여 중」 으로 읽으면, 이미 쫓겨난
-- 채널을 계속 수집 대상으로 본다.
ALTER TABLE archive_channel_mode
    ADD COLUMN IF NOT EXISTS membership_checked_at timestamptz;

-- 사람이 명시적으로 끈 채널. **재초대해도 저절로 켜지지 않는다.**
--
-- 멤버십 상실로 멈춘 것과 사람이 끈 것을 한 칸에 담으면, 다시 초대하는 순간
-- 사람이 끈 채널까지 되살아난다. 끈 사람은 그 사실을 모른다.
ALTER TABLE archive_channel_mode
    ADD COLUMN IF NOT EXISTS operator_hold boolean NOT NULL DEFAULT false;

-- 화면이 「수집되고 있나」 를 답하려면 필요하다. `active` 는 프로세스가 떠 있다는
-- 뜻일 뿐, 이벤트가 들어왔다는 증거가 아니다.
ALTER TABLE archive_channel_mode
    ADD COLUMN IF NOT EXISTS last_event_at timestamptz;

-- 표시용. 이름은 바뀌므로 **키가 아니다**(키는 channel_id).
ALTER TABLE archive_channel_mode
    ADD COLUMN IF NOT EXISTS channel_name text NOT NULL DEFAULT '';
ALTER TABLE archive_channel_mode
    ADD COLUMN IF NOT EXISTS is_private boolean NOT NULL DEFAULT false;

-- 이미 등록돼 수집 중이던 채널은 **참여 중으로 본다.** 손으로 등록했다는 것은
-- 초대가 있었다는 뜻이고, 다음 동기화가 실제 값으로 덮는다. `unknown` 으로 두면
-- 그 사이 수집이 멈춘다.
UPDATE archive_channel_mode
   SET membership = 'joined'
 WHERE membership = 'unknown' AND mode IN ('shadow', 'active');

CREATE INDEX IF NOT EXISTS archive_channel_mode_membership_idx
    ON archive_channel_mode (workspace, membership);


-- active 인데 주인이 archiver 가 아니면 모순이다. 아카이빙 봇이 운영에 쓰는
-- 상태인데 주인은 master 라고 적혀 있으면, 어느 쪽 말을 믿어야 하는지 알 수 없다.
ALTER TABLE archive_channel_mode DROP CONSTRAINT IF EXISTS archive_channel_mode_owner_matches;
ALTER TABLE archive_channel_mode ADD CONSTRAINT archive_channel_mode_owner_matches
    CHECK (
        mode = 'paused'
        OR (mode IN ('off', 'shadow') AND writer_owner = 'master')
        OR (mode = 'active' AND writer_owner = 'archiver')
    );

-- 주인이 archiver 면 인수 좌표가 있어야 한다. 없으면 「언제부터 이 봇 몫인가」 를
-- 나중에 아무도 모른다.
ALTER TABLE archive_channel_mode DROP CONSTRAINT IF EXISTS archive_channel_mode_cutover_present;
ALTER TABLE archive_channel_mode ADD CONSTRAINT archive_channel_mode_cutover_present
    CHECK (writer_owner <> 'archiver' OR cutover_ts <> '');

CREATE INDEX IF NOT EXISTS archive_channel_mode_active
    ON archive_channel_mode (workspace, mode);

-- `shadow` 를 모드 CHECK 에서 뺀다 — **남은 행이 0 일 때만**(B-99 2단계 D5).
--
-- 행이 남아 있는데 CHECK 를 바꾸면 스키마 적용 전체가 실패하고, 그 실패는 다른 표의
-- 변경까지 되돌린다. 그래서 남아 있으면 NOTICE 만 남기고 그대로 둔다. 확인:
-- `deploy/checks/archiver_no_shadow_channels.sql`. 멤버십 동기화가 옛 행을 `active`
-- 로 올린 뒤 다시 적용하면 그때 빠진다.
--
-- 재적용해도 안전하다 — 매번 같은 이름으로 지우고 같은 정의로 만든다. 세는 순간과
-- 바꾸는 순간 사이에 shadow 행이 생기지 않게 표를 잠근다.
DO $$
DECLARE
    shadow_rows bigint;
BEGIN
    LOCK TABLE archive_channel_mode IN SHARE ROW EXCLUSIVE MODE;
    SELECT count(*) INTO shadow_rows FROM archive_channel_mode WHERE mode = 'shadow';
    IF shadow_rows > 0 THEN
        RAISE NOTICE 'archive_channel_mode: shadow 행 %건이 남아 mode CHECK 를 그대로 둡니다'
                     ' (deploy/checks/archiver_no_shadow_channels.sql)', shadow_rows;
        RETURN;
    END IF;
    ALTER TABLE archive_channel_mode
        DROP CONSTRAINT IF EXISTS archive_channel_mode_mode_check;
    ALTER TABLE archive_channel_mode ADD CONSTRAINT archive_channel_mode_mode_check
        CHECK (mode IN ('off', 'active', 'paused'));
END
$$;


-- ---------------------------------------------------------------------------
-- 2. 설정 변경 감사 — **지워지지 않는다**
-- ---------------------------------------------------------------------------
-- 「누가 언제 이 채널을 active 로 바꿨나」 를 답할 수 없으면, 사고가 났을 때
-- 범위를 정할 수 없다. UPDATE·DELETE 를 주지 않는다(§8 GRANT).
CREATE TABLE IF NOT EXISTS archive_config_audit (
    id              bigserial PRIMARY KEY,
    at              timestamptz NOT NULL DEFAULT now(),
    actor           text NOT NULL,
    -- 무엇을 바꿨나. 표 이름이 아니라 **사람이 부르는 이름**이다.
    subject         text NOT NULL,          -- 'channel_mode' · 'feature_flag' · 'retention'
    workspace       text NOT NULL DEFAULT '',
    channel_id      text NOT NULL DEFAULT '',
    field           text NOT NULL,
    old_value       text NOT NULL DEFAULT '',
    new_value       text NOT NULL DEFAULT '',
    reason          text NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS archive_config_audit_recent ON archive_config_audit (at DESC);
CREATE INDEX IF NOT EXISTS archive_config_audit_channel
    ON archive_config_audit (workspace, channel_id, at DESC);

-- ---------------------------------------------------------------------------
-- 초대 = 운영 수집 (2026-11-18 오너 결정: shadow 수집 완전 폐기)
-- ---------------------------------------------------------------------------
-- 전에는 초대된 채널을 `shadow` 로 등록했다. 그림자 기간이 끝났고, 그림자로 들어온
-- 채널은 사람이 다시 인수해야 운영 수집이 됐다 — 그 사이 운영 원문은 Master 몫으로
-- 남아 「초대했는데 Archiver 가 안 쓴다」 가 반복됐다. 이제 초대는 곧 **운영 수집**이다.
--
-- ## 신규 채널의 인수 좌표(cutover_ts) — 「등록 시각 + 정착 60초」
--
-- `cutover_ts` 는 「이 ts **이후**가 Archiver 몫」 이다(위 표 설명). 신규 채널은 사람이
-- 고른 좌표가 없으므로 **DB 가 등록 순간에 정한다**: `clock_timestamp() + 60초` 를 Slack
-- ts 형식(`초.마이크로초`)으로.
--
-- 왜 미래인가. Master 는 채널 주인 판정을 `write_owner.CACHE_SECONDS`(30초) 동안 들고
-- 있다. 등록 직후 Master 는 아직 「행 없음 = 내 것」 으로 쓰고 있을 수 있다. 그 순간
-- Archiver 가 바로 쓰면 **두 writer** 다. 그래서 Archiver 는 `cutover_ts` 가 지나기 전에는
-- 운영 원문에 쓰지 않는다(`write_owner.decide`). 60초는 `handover.DRAIN_SETTLE_SECONDS`
-- (캐시의 두 배)와 같다 — 콘솔 인수와 같은 규칙이다(「겹침 대신 빔」).
--
--   등록 ─(≤30초: Master 캐시 만료)─▶ Master 멈춤 ─(빈 구간)─▶ cutover_ts ─▶ Archiver
--
-- 빈 구간은 `cutover_at`(등록 시각) ~ `cutover_ts` 로 **범위가 기록된다.** 보통 봇
-- 참여 알림뿐이고, 사람 발언이 있으면 그 범위를 소급으로 메운다.
--
-- 이미 Archiver 몫이던 채널(나갔다 재초대)은 **기존 좌표를 보존한다.** 좌표를 새로 박으면
-- 「언제부터 이 봇 몫이었나」 가 지워진다.
--
-- ## 전역 스위치가 꺼져 있으면 옮기지 않는다
--
-- `archiver_writes_live` 가 꺼진 채로 Master 몫을 Archiver 로 넘기면 Master 는 멈추고
-- Archiver 는 쓰지 않는다 — **아무도 안 쓰는** 채널이 된다. 그래서 스위치가 꺼져 있으면
-- 신규 채널은 등록하지 않고(`deferred`, Master 가 계속 씀), Master 몫이던 기존 행은
-- `off`(Master 가 씀)로 둔다. 스위치가 켜진 뒤 다음 동기화가 `active` 로 옮긴다.
CREATE OR REPLACE FUNCTION archive_settled_cutover_ts() RETURNS text
LANGUAGE sql
VOLATILE
SET search_path = public, pg_temp
AS $$
    SELECT to_char(
        extract(epoch FROM clock_timestamp() + interval '60 seconds'),
        'FM0000000000.000000'
    )
$$;

-- Archiver runtime은 설정 표 UPDATE 권한을 받지 않는다. 대신 초대 동기화에 필요한
-- 칸만 이 함수로 쓴다.
--
-- 규칙(이 순서로 판정):
--   1. 잠금 — 콘솔 모드 변경(`archiving_repo.channel_state`)과 **같은 advisory 키**를 잡는다.
--      동기화와 콘솔 인수가 한 채널에서 엇갈려 끼어들지 못한다. 행이 없을 때도 잡히므로
--      두 동기화(주기·이벤트)가 같은 신규 채널을 동시에 INSERT 하지 않는다
--   2. 빠짐(left)·operator_hold·명시적 paused 요청 → `paused`. 주인·좌표는 그대로
--   3. 이미 Archiver 몫 → `active`. 좌표 보존
--   4. Master 몫(off·paused·옛 shadow) + 스위치 켜짐 → `active`·`archiver`·새 좌표(앞으로만)
--   5. Master 몫 + 스위치 꺼짐 → `off`(Master 가 씀)
--   6. 신규 + 스위치 켜짐 → `active`·`archiver`·새 좌표. 꺼짐 → 등록 안 함(`deferred`)
--
-- `shadow` 는 **저장하지도 받지도 않는다**(B-99 2단계). 옛 코드가 보내던 요청값 별칭은
-- 거두었다 — 동기화 코드(`channel_membership.apply_plan`)가 먼저 거절하고, 여기서도
-- 거절한다. 결과 모드를 돌려준다.
--
-- 반환형이 void → text 로 바뀌어 CREATE OR REPLACE 로는 못 바꾼다. 재적용이 안전하도록
-- 매번 지우고 만든다(권한은 이 파일 끝 §8 이 같은 트랜잭션에서 다시 준다).
DROP FUNCTION IF EXISTS archiver_save_membership(
    text, text, text, text, text, boolean, text, boolean, text
);
CREATE FUNCTION archiver_save_membership(
    p_workspace text,
    p_channel_id text,
    p_requested_mode text,
    p_membership text,
    p_channel_name text,
    p_is_private boolean,
    p_actor text,
    p_audit boolean,
    p_reason text
) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    current_row archive_channel_mode%ROWTYPE;
    live_on boolean;
    next_mode text;
    next_owner text;
    next_cutover text;
    next_cutover_at timestamptz;
    fresh_cutover text;
BEGIN
    IF coalesce(p_workspace, '') = '' OR coalesce(p_channel_id, '') = '' THEN
        RAISE EXCEPTION 'workspace and channel_id are required';
    END IF;
    IF p_requested_mode = 'shadow' THEN
        RAISE EXCEPTION 'shadow 수집은 폐기됐습니다 (B-99): membership sync cannot request mode shadow';
    END IF;
    IF p_requested_mode IS NULL OR p_requested_mode NOT IN ('active', 'paused') THEN
        RAISE EXCEPTION 'membership sync cannot request mode %', p_requested_mode;
    END IF;
    IF p_membership NOT IN ('joined', 'left') THEN
        RAISE EXCEPTION 'membership sync cannot write membership %', p_membership;
    END IF;

    PERFORM pg_advisory_xact_lock(
        hashtext('archive-mode:' || p_workspace || '/' || p_channel_id)
    );

    SELECT coalesce(bool_or(enabled), false) INTO live_on
      FROM archive_feature_flag
     WHERE name = 'archiver_writes_live' AND scope = 'global' AND scope_key = '';

    SELECT * INTO current_row
      FROM archive_channel_mode
     WHERE workspace = p_workspace AND channel_id = p_channel_id
     FOR UPDATE;

    IF NOT FOUND THEN
        IF p_membership <> 'joined' OR p_requested_mode = 'paused' THEN
            RAISE EXCEPTION 'a new invited channel must be registered as joined/active';
        END IF;
        IF NOT live_on THEN
            -- 행을 만들지 않으면 Master 가 계속 쓴다(write_owner: 행 없음 = Master).
            RETURN 'deferred';
        END IF;
        fresh_cutover := archive_settled_cutover_ts();
        INSERT INTO archive_channel_mode
            (workspace, channel_id, mode, writer_owner, cutover_ts, cutover_at,
             membership, membership_checked_at, channel_name, is_private, updated_by)
        VALUES
            (p_workspace, p_channel_id, 'active', 'archiver', fresh_cutover, now(),
             'joined', now(), p_channel_name, p_is_private, p_actor);
        IF p_audit THEN
            INSERT INTO archive_config_audit
                (actor, subject, workspace, channel_id, field,
                 old_value, new_value, reason)
            VALUES
                (p_actor, 'channel_membership', p_workspace, p_channel_id,
                 'membership', '', 'joined/active', p_reason),
                (p_actor, 'channel_membership', p_workspace, p_channel_id,
                 'writer_owner', '', 'archiver@' || fresh_cutover,
                 'new invited channel; settle 60s after registration');
        END IF;
        RETURN 'active';
    END IF;

    next_owner := current_row.writer_owner;
    next_cutover := current_row.cutover_ts;
    next_cutover_at := current_row.cutover_at;

    IF p_membership = 'left' OR current_row.operator_hold OR p_requested_mode = 'paused' THEN
        next_mode := 'paused';
    ELSIF current_row.writer_owner = 'archiver' THEN
        next_mode := 'active';
    ELSIF live_on THEN
        next_mode := 'active';
        next_owner := 'archiver';
        fresh_cutover := archive_settled_cutover_ts();
        -- 좌표는 앞으로만 간다(archiving_state.plan_mode_change 와 같은 규칙).
        IF current_row.cutover_ts ~ '^[0-9]+(\.[0-9]+)?$'
           AND current_row.cutover_ts::numeric > fresh_cutover::numeric THEN
            next_cutover := current_row.cutover_ts;
        ELSE
            next_cutover := fresh_cutover;
            next_cutover_at := now();
        END IF;
    ELSE
        next_mode := 'off';
    END IF;

    UPDATE archive_channel_mode
       SET mode = next_mode,
           writer_owner = next_owner,
           cutover_ts = next_cutover,
           cutover_at = next_cutover_at,
           membership = p_membership,
           membership_checked_at = now(),
           channel_name = p_channel_name,
           is_private = p_is_private,
           updated_at = now(),
           updated_by = p_actor
     WHERE workspace = p_workspace AND channel_id = p_channel_id;

    -- 바뀐 것만 남긴다. 스위치가 꺼진 동안 매 동기화가 같은 줄을 쌓으면 진짜 변경이 묻힌다.
    IF p_audit AND (current_row.membership IS DISTINCT FROM p_membership
                    OR current_row.mode IS DISTINCT FROM next_mode) THEN
        INSERT INTO archive_config_audit
            (actor, subject, workspace, channel_id, field,
             old_value, new_value, reason)
        VALUES
            (p_actor, 'channel_membership', p_workspace, p_channel_id,
             'membership', current_row.membership || '/' || current_row.mode,
             p_membership || '/' || next_mode, p_reason);
    END IF;
    IF current_row.writer_owner IS DISTINCT FROM next_owner THEN
        INSERT INTO archive_config_audit
            (actor, subject, workspace, channel_id, field,
             old_value, new_value, reason)
        VALUES
            (p_actor, 'channel_membership', p_workspace, p_channel_id,
             'writer_owner',
             current_row.writer_owner || '@' || coalesce(nullif(current_row.cutover_ts, ''), '-'),
             next_owner || '@' || next_cutover,
             're-invited channel; settle 60s after registration');
    END IF;
    RETURN next_mode;
END
$$;

REVOKE ALL ON FUNCTION archiver_save_membership(
    text, text, text, text, text, boolean, text, boolean, text
) FROM PUBLIC;

CREATE OR REPLACE FUNCTION archiver_mark_channel_event(
    p_workspace text,
    p_channel_id text
) RETURNS boolean
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    affected integer;
BEGIN
    UPDATE archive_channel_mode
       SET last_event_at = now()
     WHERE workspace = p_workspace
       AND channel_id = p_channel_id
       AND membership = 'joined'
       AND operator_hold = false
       -- 옛 shadow 행은 수집하지 않으므로(refused) 이벤트도 적지 않는다(B-99).
       AND mode = 'active';
    GET DIAGNOSTICS affected = ROW_COUNT;
    RETURN affected = 1;
END
$$;

REVOKE ALL ON FUNCTION archiver_mark_channel_event(text, text) FROM PUBLIC;


-- ---------------------------------------------------------------------------
-- 3. 기능 스위치 — 콘솔에서 켠다
-- ---------------------------------------------------------------------------
-- 첨부 분리·edit/delete 보존·ACK 는 ENV 가 아니라 여기서 켠다. ENV 로 두면 켜고
-- 끄는 데 배포가 필요하고, 배포가 필요한 스위치는 사고 중에 못 내린다.
--
-- **기본값은 전부 꺼짐이다.** 읽는 쪽이 붙기 전에 켜면 그 자료가 조용히 답변에서
-- 빠진다(`attachment_writer.separate_attachments` 주석과 같은 이유).
CREATE TABLE IF NOT EXISTS archive_feature_flag (
    name            text NOT NULL,
    enabled         boolean NOT NULL DEFAULT false,
    -- 워크스페이스별로 다르게 켤 수 있어야 한다. 전역만 있으면 파일럿이 불가능하다.
    scope           text NOT NULL DEFAULT 'global',
    scope_key       text NOT NULL DEFAULT '',
    description     text NOT NULL DEFAULT '',
    updated_at      timestamptz NOT NULL DEFAULT now(),
    updated_by      text NOT NULL DEFAULT '',
    PRIMARY KEY (name, scope, scope_key)
);

-- 4ecf634 초안이 `name` 하나만 PK 로 만든 DB도 다시 적용하면 안전하게 확장한다.
ALTER TABLE archive_feature_flag
    ADD COLUMN IF NOT EXISTS scope_key text NOT NULL DEFAULT '';
ALTER TABLE archive_feature_flag
    DROP CONSTRAINT IF EXISTS archive_feature_flag_pkey;
ALTER TABLE archive_feature_flag
    ADD CONSTRAINT archive_feature_flag_pkey PRIMARY KEY (name, scope, scope_key);
ALTER TABLE archive_feature_flag
    DROP CONSTRAINT IF EXISTS archive_feature_flag_scope_valid;
ALTER TABLE archive_feature_flag
    ADD CONSTRAINT archive_feature_flag_scope_valid CHECK (
        scope IN ('global', 'workspace', 'channel')
        AND ((scope = 'global' AND scope_key = '')
             OR (scope <> 'global' AND scope_key <> ''))
    );

INSERT INTO archive_feature_flag (name, scope, scope_key, description) VALUES
    ('attachment_reader_ready',
     'global', '',
     '분리된 첨부 정본을 검색기가 읽고 중복 없이 근거로 사용할 수 있다'),
    ('revision_reader_ready',
     'global', '',
     '검색기가 메시지 revision의 최신 상태를 적용하고 삭제본을 근거에서 제외한다'),
    ('separate_attachments',
     'global', '',
     '첨부 본문을 raw 에서 떼고 별도 정본에만 둔다. 읽는 쪽이 붙은 뒤에 켠다'),
    ('preserve_edit_delete',
     'global', '',
     'message_changed·message_deleted 를 revision 으로 보존한다'),
    ('require_attachment_ack',
     'global', '',
     '첨부가 ready 가 되기 전에는 검색 가능하다고 말하지 않는다'),
    ('archiver_writes_live',
     'global', '',
     '아카이빙 봇이 운영 아카이브에 쓴다. 채널별 writer_owner 보다 상위 차단기다')
ON CONFLICT (name, scope, scope_key) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 4. 보존 정책 — **운영값 없이는 production 으로 못 간다**
-- ---------------------------------------------------------------------------
-- 봇 대화 감사 기록의 보존 기간이다. 값을 코드에 박지 않는 이유는 법무·감사
-- 요구가 바뀌기 때문이고, 값이 **없는 채로 production 에 가는 것을 막는 이유**는
-- 그러면 기본값이 「영구 보관」 이 되기 때문이다. 개인 대화를 영구 보관하는 것은
-- 아무도 결정한 적이 없는데 그냥 그렇게 된다.
CREATE TABLE IF NOT EXISTS archive_retention_policy (
    name            text PRIMARY KEY,
    retention_days  integer,                -- NULL = **아직 안 정했다**. 운영값은 1일 이상
    approved_by     text NOT NULL DEFAULT '',
    approved_at     timestamptz,
    description     text NOT NULL DEFAULT ''
);

ALTER TABLE archive_retention_policy DROP CONSTRAINT IF EXISTS archive_retention_policy_sane;
ALTER TABLE archive_retention_policy ADD CONSTRAINT archive_retention_policy_sane
    CHECK (retention_days IS NULL OR retention_days > 0);

-- 값을 정했다고 말하려면 **누가 정했는지**가 있어야 한다. 날짜만 있고 사람이
-- 없으면 나중에 그 값을 바꿔도 되는지 아무도 모른다.
ALTER TABLE archive_retention_policy DROP CONSTRAINT IF EXISTS archive_retention_policy_approved;
ALTER TABLE archive_retention_policy ADD CONSTRAINT archive_retention_policy_approved
    CHECK (retention_days IS NULL OR (approved_by <> '' AND approved_at IS NOT NULL));

INSERT INTO archive_retention_policy (name, description) VALUES
    ('bot_conversation_audit',
     'TYBot·Hermes 와 사용자의 대화 감사 기록. archive 밖이고 답변 근거가 아니다'),
    ('bot_dm_attachment',
     '봇 DM 첨부. 명시적 아카이브 등록 요청이 없으면 그 요청에서만 쓴다'),
    -- 봇↔사람 1:1 DM 원문(B-57·B-68). 첨부와 **따로** 든다 — 본문과 원본
    -- 바이트는 지우는 자리도 다르고, 기간을 따로 정할 수 있어야 한다.
    -- 사람끼리의 DM 은 수집 대상이 아니므로 여기 없다.
    ('bot_dm_message',
     '봇과 사람의 1:1 DM 원문. 그 사람 본인이 DM 에서 물을 때만 근거가 된다')
ON CONFLICT (name) DO NOTHING;


-- ---------------------------------------------------------------------------
-- 5. 수집 상태 — ACK
-- ---------------------------------------------------------------------------
-- 「방금 올린 파일 검색되나」 에 답하는 표다.
--
-- **메시지와 첨부를 따로 든다.** 본문은 즉시 쓰이고 첨부 변환은 큐를 지나므로,
-- 한 칸으로 합치면 둘 중 느린 쪽에 맞춰 거짓말을 하게 된다 — 본문은 이미
-- 있는데 「아직」 이라고 하거나, 첨부가 아직인데 「됐다」 고 한다.
CREATE TABLE IF NOT EXISTS archive_ingest_state (
    workspace       text NOT NULL,
    channel_id      text NOT NULL,
    message_ts      text NOT NULL,
    -- received            이벤트를 받았다. 아직 아무것도 안 썼다
    -- raw_written         원문 줄이 파일에 들어갔다
    -- attachment_pending  첨부 변환이 남았다. **검색 가능하다고 말하지 않는다**
    -- ready               본문·첨부 모두 끝났다
    -- partial             일부만 됐다. 사람이 볼 것이 있다
    -- refused             검사에서 막혔다(PII 등). 본문은 어디에도 없다
    -- failed              자동으로 더 해 볼 것이 없다
    state           text NOT NULL DEFAULT 'received'
                    CHECK (state IN ('received', 'raw_written', 'attachment_pending',
                                     'ready', 'partial', 'refused', 'failed')),
    attachment_total    integer NOT NULL DEFAULT 0,
    attachment_ready    integer NOT NULL DEFAULT 0,
    -- 어느 배치에 썼나. shadow 와 운영을 구분해야 인수 전후를 대조할 수 있다.
    written_to      text NOT NULL DEFAULT ''
                    CHECK (written_to IN ('', 'shadow', 'live')),
    doc_path        text NOT NULL DEFAULT '',
    error_code      text NOT NULL DEFAULT '',
    first_seen_at   timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id, message_ts)
);

-- 첨부가 다 안 끝났는데 ready 라고 적히는 것을 **표가 막는다.**
-- 코드 규칙으로만 두면 한 경로가 빠지고, 그 경로만 거짓말한다.
ALTER TABLE archive_ingest_state DROP CONSTRAINT IF EXISTS archive_ingest_state_ready_needs_attachments;
ALTER TABLE archive_ingest_state ADD CONSTRAINT archive_ingest_state_ready_needs_attachments
    CHECK (state <> 'ready' OR attachment_ready >= attachment_total);

ALTER TABLE archive_ingest_state DROP CONSTRAINT IF EXISTS archive_ingest_state_counts_sane;
ALTER TABLE archive_ingest_state ADD CONSTRAINT archive_ingest_state_counts_sane
    CHECK (attachment_ready >= 0 AND attachment_total >= 0
           AND attachment_ready <= attachment_total);

CREATE INDEX IF NOT EXISTS archive_ingest_state_unfinished
    ON archive_ingest_state (workspace, channel_id, state)
    WHERE state IN ('received', 'raw_written', 'attachment_pending', 'partial');


-- ---------------------------------------------------------------------------
-- 6. 메시지 revision — 수정·삭제를 **쌓는다**
-- ---------------------------------------------------------------------------
-- 원문은 안 고친다(절대 원칙 1). 그런데 사람이 고친 문장도 원문이다. 둘을 함께
-- 지키는 길은 하나뿐이다 — **고친 것을 새 revision 으로 쌓고, 검색은 최신만 본다.**
--
-- Archiving Bot은 `message_changed`·`message_deleted`를 아래 revision으로
-- 보존한다. 검색 reader가 이 표의 최신 상태를 적용하기 전에는 운영 전환하지 않는다.
--
-- **본문은 여기 없다.** 본문은 MD 파일에 있고 이 표는 좌표와 hash 만 든다.
CREATE TABLE IF NOT EXISTS archive_message_revision (
    workspace       text NOT NULL,
    channel_id      text NOT NULL,
    -- 원본 메시지의 좌표. 수정본도 **같은 message_ts** 를 갖는다 — 그래서 이게
    -- revision 들을 묶는 열쇠다.
    message_ts      text NOT NULL,
    revision_no     integer NOT NULL,
    -- create    처음 들어온 줄
    -- change    사람이 고쳤다
    -- delete    사람이 지웠다. 앞 revision 의 본문은 **남는다**
    -- redact    PII·법적 삭제. 본문을 남기지 않는다
    kind            text NOT NULL
                    CHECK (kind IN ('create', 'change', 'delete', 'redact')),
    -- Slack 이 준 수정 시각. 없으면 빈 문자열이다 — 지어내지 않는다.
    edited_ts       text NOT NULL DEFAULT '',
    author_id       text NOT NULL DEFAULT '',
    -- 본문 동일성. 본문 자체는 안 든다.
    body_sha256     text NOT NULL DEFAULT '',
    doc_path        text NOT NULL DEFAULT '',
    line_no         integer,
    -- redact 일 때만 채운다. 왜 지웠는지 없으면 나중에 되돌릴 수도 없고
    -- 되돌리면 안 되는지도 모른다.
    reason_code     text NOT NULL DEFAULT '',
    recorded_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id, message_ts, revision_no)
);

-- 첫 revision 은 반드시 create 다. change 로 시작하면 원본이 없다는 뜻이고,
-- 그때 「무엇에서 무엇으로 바뀌었나」 를 말할 수 없다.
ALTER TABLE archive_message_revision DROP CONSTRAINT IF EXISTS archive_message_revision_first_is_create;
ALTER TABLE archive_message_revision ADD CONSTRAINT archive_message_revision_first_is_create
    CHECK (revision_no > 1 OR kind = 'create');

ALTER TABLE archive_message_revision DROP CONSTRAINT IF EXISTS archive_message_revision_no_positive;
ALTER TABLE archive_message_revision ADD CONSTRAINT archive_message_revision_no_positive
    CHECK (revision_no >= 1);

-- redact 는 본문 해시를 남기지 않는다. 해시가 남으면 사전 대입으로 짧은 본문을
-- 되찾을 수 있고, 그건 「본문을 남기지 않는다」 를 지킨 것이 아니다.
ALTER TABLE archive_message_revision DROP CONSTRAINT IF EXISTS archive_message_revision_redact_is_bare;
ALTER TABLE archive_message_revision ADD CONSTRAINT archive_message_revision_redact_is_bare
    CHECK (kind <> 'redact' OR (body_sha256 = '' AND reason_code <> ''));

-- CHECK 만으로는 revision 2를 첫 행으로 넣거나 번호를 건너뛰는 것을 막을 수 없다.
-- 좌표별 advisory lock으로 첫 INSERT 경쟁도 직렬화하고, 직전 revision이 정확히
-- 하나 앞인지 확인한다. 같은 PK의 재시도는 본래 PK/ON CONFLICT가 처리하게 둔다.
CREATE OR REPLACE FUNCTION enforce_archive_message_revision_order()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    latest_no integer;
    latest_kind text;
BEGIN
    PERFORM pg_advisory_xact_lock(
        hashtext(NEW.workspace || chr(31) || NEW.channel_id),
        hashtext(NEW.message_ts)
    );

    IF EXISTS (
        SELECT 1
          FROM archive_message_revision
         WHERE workspace = NEW.workspace
           AND channel_id = NEW.channel_id
           AND message_ts = NEW.message_ts
           AND revision_no = NEW.revision_no
    ) THEN
        RETURN NEW;
    END IF;

    SELECT revision_no, kind
      INTO latest_no, latest_kind
      FROM archive_message_revision
     WHERE workspace = NEW.workspace
       AND channel_id = NEW.channel_id
       AND message_ts = NEW.message_ts
     ORDER BY revision_no DESC
     LIMIT 1;

    IF latest_no IS NULL THEN
        IF NEW.revision_no <> 1 OR NEW.kind <> 'create' THEN
            RAISE EXCEPTION 'first archive revision must be create revision 1';
        END IF;
    ELSE
        IF NEW.revision_no <> latest_no + 1 THEN
            RAISE EXCEPTION 'archive revision must follow %, got %', latest_no, NEW.revision_no;
        END IF;
        IF latest_kind = 'redact' THEN
            RAISE EXCEPTION 'redacted archive message cannot receive another revision';
        END IF;
        IF NEW.kind = 'create' THEN
            RAISE EXCEPTION 'create is only valid for the first archive revision';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS archive_message_revision_order ON archive_message_revision;
CREATE TRIGGER archive_message_revision_order
BEFORE INSERT ON archive_message_revision
FOR EACH ROW EXECUTE FUNCTION enforce_archive_message_revision_order();

CREATE INDEX IF NOT EXISTS archive_message_revision_latest
    ON archive_message_revision (workspace, channel_id, message_ts, revision_no DESC);

-- 일반 검색이 볼 것 — **최신 비삭제 revision 만.**
-- 뷰로 두는 이유: 조회하는 쪽마다 「최신을 고르는 SQL」 을 쓰면 한 군데가 어긋나
-- 그 경로만 지워진 문장을 보여 준다.
CREATE OR REPLACE VIEW archive_message_current AS
SELECT workspace, channel_id, message_ts, revision_no, kind,
       author_id, body_sha256, doc_path, line_no, recorded_at
  FROM (
        SELECT DISTINCT ON (workspace, channel_id, message_ts)
               workspace, channel_id, message_ts, revision_no, kind,
               author_id, body_sha256, doc_path, line_no, recorded_at
          FROM archive_message_revision
         ORDER BY workspace, channel_id, message_ts, revision_no DESC
       ) AS latest
 WHERE kind NOT IN ('delete', 'redact');

COMMENT ON VIEW archive_message_current IS
    '메시지별 최신 비삭제 revision. 삭제·redact는 이 뷰에서 이미 제외한다.';


-- ---------------------------------------------------------------------------
-- 7. 첨부 revision — 재변환이 **덮어쓰지 않는다**
-- ---------------------------------------------------------------------------
-- revision 은 결정적이다: source SHA + 변환기 이름/버전 + 설정/스키마 hash.
-- 그래서 같은 입력에 같은 변환기면 같은 revision 이 나오고(멱등), 변환기를
-- 고치면 자동으로 다른 revision 이 된다.
--
-- **덮어쓰지 않는 이유**: 옛 변환본을 근거로 인용한 답변이 이미 나가 있을 수
-- 있다. 덮으면 사람이 출처를 눌렀을 때 인용된 문장이 없다.
CREATE TABLE IF NOT EXISTS archive_attachment_revision (
    workspace           text NOT NULL,
    channel_id          text NOT NULL,
    file_id             text NOT NULL,
    -- 위 넷의 결정적 hash. 코드가 만든다(`archiving.revision.attachment_revision`).
    revision            text NOT NULL,
    source_sha256       text NOT NULL,
    converter_name      text NOT NULL,
    converter_version   text NOT NULL,
    -- 변환 설정·스키마의 결정적 hash. 설정이 바뀌면 결과가 바뀌므로 revision 에 든다.
    config_sha256       text NOT NULL,
    -- 이 첨부가 딸린 메시지. 원문에는 file_id·message_ts·링크만 남는다.
    message_ts          text NOT NULL DEFAULT '',
    doc_path            text NOT NULL DEFAULT '',
    -- pending    변환 대기
    -- ready      변환본이 있고 근거로 쓸 수 있다
    -- failed     자동으로 더 해 볼 것이 없다
    -- superseded 더 새 revision 이 나왔다. **지우지 않는다**
    state               text NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'ready', 'failed', 'superseded')),
    -- 과거 raw 에 본문이 복제돼 있던 것을 소급 생성한 경우. legacy 중복 제외에 쓴다.
    is_backfill         boolean NOT NULL DEFAULT false,
    legacy_doc_path     text NOT NULL DEFAULT '',
    error_code          text NOT NULL DEFAULT '',
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id, file_id, revision)
);

CREATE INDEX IF NOT EXISTS archive_attachment_revision_pending
    ON archive_attachment_revision (workspace, channel_id, state)
    WHERE state IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS archive_attachment_revision_message
    ON archive_attachment_revision (workspace, channel_id, message_ts);


-- ---------------------------------------------------------------------------
-- 8. 거부 기록 — **본문 없이**
-- ---------------------------------------------------------------------------
-- `writer.screen` 이 막으면 그 메시지는 지금 어디에도 안 남는다. 그래서 사람이
-- 「그 메시지 왜 없냐」 물었을 때 답할 근거가 없다 — 수집이 안 된 것인지, 막힌
-- 것인지, 버그인지 구분이 안 된다.
--
-- 좌표와 사유만 남긴다. 본문을 남기면 PII 를 막으려고 만든 표가 PII 저장소가 된다.
CREATE TABLE IF NOT EXISTS archive_refusal (
    id              bigserial PRIMARY KEY,
    workspace       text NOT NULL,
    channel_id      text NOT NULL,
    message_ts      text NOT NULL DEFAULT '',
    -- 'rrn' · 'account_no' · 'phone' · 'secret' 같은 코드. **일치한 문자열이 아니다.**
    reason_code     text NOT NULL,
    -- 본문 해시조차 남기지 않는다. 짧은 본문은 해시에서 되찾을 수 있다.
    speaker_hash    text NOT NULL DEFAULT '',
    at              timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS archive_refusal_recent
    ON archive_refusal (workspace, channel_id, at DESC);

COMMENT ON TABLE archive_refusal IS
    '수집 거부 기록. 본문·일치 문자열 금지 — 좌표와 사유 코드만.';


-- ---------------------------------------------------------------------------
-- 9. 봇 대화 감사 — **archive 밖이고 답변 근거가 아니다**
-- ---------------------------------------------------------------------------
-- TYBot·Hermes 와 사용자의 대화는 여기 남는다. `archive/workspaces` 밖이므로
-- `ArchiveStore._files()` 글롭에 걸리지 않고, 검색·요약·답변 근거에서 빠진다.
--
-- 표 이름에 `audit` 을 넣은 이유: 다음 사람이 이 표를 보고 「여기 대화가 있네,
-- 검색에 넣자」 고 하지 않게 하기 위해서다. 이름이 용도를 말해야 한다.
CREATE TABLE IF NOT EXISTS bot_conversation_audit (
    id              bigserial PRIMARY KEY,
    workspace       text NOT NULL,
    bot             text NOT NULL,          -- 'tybot' · 'hermes'
    user_id         text NOT NULL,
    channel_id      text NOT NULL DEFAULT '',
    message_ts      text NOT NULL DEFAULT '',
    role            text NOT NULL CHECK (role IN ('user', 'bot')),
    -- 본문을 둘지 말지는 보존 정책이 정한다. 정책값이 없으면 production 에
    -- 못 가므로(§4), 값이 없는 채로 본문이 쌓이는 일은 없다.
    body            text NOT NULL DEFAULT '',
    at              timestamptz NOT NULL DEFAULT now(),
    -- 보존 만료 예정 시각. 정책값에서 계산해 넣는다.
    expires_at      timestamptz
);

CREATE INDEX IF NOT EXISTS bot_conversation_audit_user
    ON bot_conversation_audit (workspace, user_id, at DESC);
CREATE INDEX IF NOT EXISTS bot_conversation_audit_expiry
    ON bot_conversation_audit (expires_at) WHERE expires_at IS NOT NULL;

COMMENT ON TABLE bot_conversation_audit IS
    '봇↔사용자 대화 감사. archive 밖이며 검색·요약·답변 근거에서 제외한다.';


-- ---------------------------------------------------------------------------
-- 10. 권한 — archiver 는 **설정을 읽고 상태를 쓴다. 그것뿐이다**
-- ---------------------------------------------------------------------------
-- 표만 만들고 GRANT 를 안 주면 봇에게는 그 표가 없는 것과 같다(2026-09-14 실측).
--
-- `tybot_archiver` 에게 주지 않는 것:
-- - DELETE (어디에도). 아카이빙 봇은 지우는 일을 하지 않는다
-- - 설정 표의 UPDATE. 모드를 바꾸는 것은 콘솔의 일이다 — 봇이 자기 모드를
--   바꿀 수 있으면 채널 모드·writer 주인이 안전장치가 아니게 된다
-- - 감사 표의 UPDATE·DELETE. append-only 가 아니면 감사가 아니다
-- - 시크릿 표 일체
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tybot_archiver') THEN
        -- 이전 초안이 부여한 권한도 재적용 시 실제로 회수한다. GRANT 목록에서
        -- 빼는 것만으로는 PostgreSQL의 기존 권한이 사라지지 않는다.
        REVOKE ALL PRIVILEGES ON TABLE archive_config_audit FROM tybot_archiver;
        REVOKE ALL PRIVILEGES ON SEQUENCE archive_config_audit_id_seq FROM tybot_archiver;
        -- 설정: 읽기만
        EXECUTE 'GRANT SELECT ON TABLE'
                ' archive_channel_mode, archive_feature_flag, archive_retention_policy'
                ' TO tybot_archiver';
        -- 상태: 쓰기. 자기가 한 일을 기록한다
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE'
                ' archive_ingest_state, archive_attachment_revision'
                ' TO tybot_archiver';
        -- append-only: INSERT 만. UPDATE 도 DELETE 도 없다
        EXECUTE 'GRANT SELECT, INSERT ON TABLE'
                ' archive_message_revision, archive_refusal'
                ' TO tybot_archiver';
        EXECUTE 'GRANT SELECT ON archive_message_current TO tybot_archiver';
        EXECUTE 'GRANT EXECUTE ON FUNCTION archiver_save_membership('
                'text, text, text, text, text, boolean, text, boolean, text)'
                ' TO tybot_archiver';
        EXECUTE 'GRANT EXECUTE ON FUNCTION archiver_mark_channel_event('
                'text, text) TO tybot_archiver';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE'
                ' archive_refusal_id_seq'
                ' TO tybot_archiver';
    END IF;

    -- 콘솔·봇 본체는 기존 역할을 쓴다.
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        REVOKE UPDATE, DELETE ON TABLE
            archive_message_revision, archive_refusal, archive_config_audit
            FROM tyslackai;
        REVOKE UPDATE ON TABLE bot_conversation_audit FROM tyslackai;
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE'
                ' archive_channel_mode, archive_feature_flag, archive_retention_policy,'
                ' archive_ingest_state, archive_attachment_revision'
                ' TO tyslackai';
        EXECUTE 'GRANT SELECT, INSERT ON TABLE'
                ' archive_message_revision, archive_refusal, archive_config_audit'
                ' TO tyslackai';
        -- 대화 감사는 보존기간 만료 작업만 DELETE 한다. 본문 수정은 허용하지 않는다.
        EXECUTE 'GRANT SELECT, INSERT, DELETE ON TABLE'
                ' bot_conversation_audit TO tyslackai';
        EXECUTE 'GRANT SELECT ON archive_message_current TO tyslackai';
        EXECUTE 'GRANT EXECUTE ON FUNCTION archiver_save_membership('
                'text, text, text, text, text, boolean, text, boolean, text)'
                ' TO tyslackai';
        EXECUTE 'GRANT EXECUTE ON FUNCTION archiver_mark_channel_event('
                'text, text) TO tyslackai';
        EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE'
                ' archive_refusal_id_seq, archive_config_audit_id_seq,'
                ' bot_conversation_audit_id_seq'
                ' TO tyslackai';
    END IF;
END
$$;

COMMIT;
