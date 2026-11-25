-- 운영 확인: shadow 수집 폐기(2026-11-18) 뒤 채널 모드 상태 점검 — **읽기 전용**
--
-- 스키마가 아니다(apply-schema.sh 가 적용하지 않는다). 아무것도 고치지 않는다.
-- 마지막 DO 블록이 위반이 있으면 오류로 끝나므로 psql 종료 코드로 통과/실패를 본다.
--
-- 실행(서버, psql 은 postgres 계정이 파일을 읽으므로 cat 으로 넘긴다):
--   sudo cat /opt/tybot/deploy/checks/archiver_no_shadow_channels.sql \
--     | sudo -u postgres psql -p 55432 -d tyslackai -v ON_ERROR_STOP=1 -f -
--
-- 특정 워크스페이스만 보려면 아래 \set 값을 바꾼다('' 이면 전체).
\set ws ''

\echo '== 1. 모드별 채널 수 (shadow 는 0 이어야 한다)'
SELECT workspace, mode, writer_owner, membership, operator_hold, count(*) AS channels
  FROM archive_channel_mode
 WHERE (:'ws' = '' OR workspace = :'ws')
 GROUP BY 1, 2, 3, 4, 5
 ORDER BY 1, 2, 3, 4, 5;

\echo '== 2. 위반: shadow 행 (기대 0행)'
SELECT workspace, channel_id, channel_name, writer_owner, membership, updated_at, updated_by
  FROM archive_channel_mode
 WHERE mode = 'shadow' AND (:'ws' = '' OR workspace = :'ws')
 ORDER BY workspace, channel_id;

\echo '== 3. 위반: 참여 중·hold 아님인데 active 가 아닌 채널 (다음 동기화 뒤 기대 0행)'
\echo '   스위치가 꺼져 있으면 off 로 남는 것이 정상이다 — 4번 결과와 함께 본다'
SELECT workspace, channel_id, channel_name, mode, writer_owner, cutover_ts,
       membership_checked_at
  FROM archive_channel_mode
 WHERE membership = 'joined' AND NOT operator_hold AND mode <> 'active'
   AND (:'ws' = '' OR workspace = :'ws')
 ORDER BY workspace, channel_id;

\echo '== 4. 전역 스위치 archiver_writes_live (꺼져 있으면 신규 초대는 등록 보류)'
SELECT name, scope, scope_key, enabled
  FROM archive_feature_flag
 WHERE name = 'archiver_writes_live';

\echo '== 5. 위반: operator_hold 인데 paused 가 아닌 채널 (기대 0행)'
SELECT workspace, channel_id, channel_name, mode, writer_owner
  FROM archive_channel_mode
 WHERE operator_hold AND mode <> 'paused'
   AND (:'ws' = '' OR workspace = :'ws');

\echo '== 6. 위반: 주인·좌표 모순 (CHECK 가 막지만 재확인, 기대 0행)'
SELECT workspace, channel_id, mode, writer_owner, cutover_ts
  FROM archive_channel_mode
 WHERE (mode = 'active' AND (writer_owner <> 'archiver' OR cutover_ts = ''))
    OR (mode IN ('off', 'shadow') AND writer_owner <> 'master');

\echo '== 7. 정착 대기 중인 채널 (cutover_ts 가 아직 미래 — 최대 60초간 정상)'
SELECT workspace, channel_id, channel_name, cutover_at, cutover_ts
  FROM archive_channel_mode
 WHERE mode = 'active' AND cutover_ts ~ '^[0-9]+(\.[0-9]+)?$'
   AND cutover_ts::numeric > extract(epoch FROM now())
   AND (:'ws' = '' OR workspace = :'ws');

\echo '== 8. 최근 24시간 자동 인수 기록 (빈 구간 = cutover_at ~ cutover_ts, 소급 판단용)'
SELECT a.at, a.workspace, a.channel_id, a.old_value, a.new_value, m.cutover_at
  FROM archive_config_audit a
  LEFT JOIN archive_channel_mode m USING (workspace, channel_id)
 WHERE a.subject = 'channel_membership' AND a.field = 'writer_owner'
   AND a.at > now() - interval '24 hours'
   AND (:'ws' = '' OR a.workspace = :'ws')
 ORDER BY a.at DESC;

\echo '== 9. 위반: supervisor 희망 상태 shadow (B-99 2단계, 기대 0행 — 콘솔에서 live/off 로 바꾼다)'
SELECT workspace, desired_mode, observed_state, generation, updated_at, updated_by
  FROM archiver_workspace_runtime
 WHERE desired_mode = 'shadow' AND (:'ws' = '' OR workspace = :'ws')
 ORDER BY workspace;

\echo '== 10. CHECK 에 shadow 가 아직 남아 있나 (남은 행 0 뒤 apply-schema 재적용으로 빠진다)'
SELECT conrelid::regclass AS table_name, conname,
       pg_get_constraintdef(oid) LIKE '%shadow%' AS still_allows_shadow
  FROM pg_constraint
 WHERE conname IN ('archive_channel_mode_mode_check',
                   'archiver_workspace_runtime_desired_mode_check')
 ORDER BY conname;

\echo '== 판정'
SELECT set_config('tybot.check_ws', :'ws', false);
DO $$
DECLARE
    ws text := current_setting('tybot.check_ws');
    shadow_rows integer;
    held_running integer;
    contradictions integer;
    desired_shadow integer;
BEGIN
    SELECT count(*) INTO shadow_rows FROM archive_channel_mode
     WHERE mode = 'shadow' AND (ws = '' OR workspace = ws);
    SELECT count(*) INTO held_running FROM archive_channel_mode
     WHERE operator_hold AND mode <> 'paused' AND (ws = '' OR workspace = ws);
    SELECT count(*) INTO contradictions FROM archive_channel_mode
     WHERE (mode = 'active' AND (writer_owner <> 'archiver' OR cutover_ts = ''))
        OR (mode IN ('off', 'shadow') AND writer_owner <> 'master');
    SELECT count(*) INTO desired_shadow FROM archiver_workspace_runtime
     WHERE desired_mode = 'shadow' AND (ws = '' OR workspace = ws);
    IF shadow_rows > 0 OR held_running > 0 OR contradictions > 0 OR desired_shadow > 0 THEN
        RAISE EXCEPTION 'FAIL: shadow=% held_not_paused=% owner_contradictions=% desired_shadow=%',
            shadow_rows, held_running, contradictions, desired_shadow;
    END IF;
    RAISE NOTICE 'PASS: shadow 행 0, hold 채널 전부 paused, 주인·좌표 모순 0, 희망 상태 shadow 0';
END
$$;
