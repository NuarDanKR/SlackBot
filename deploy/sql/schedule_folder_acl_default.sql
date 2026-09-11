-- 일정 폴더↔조직을 ACL 기준 기본 허용으로 바꾼다 (2026-09-11 오너 결정)
--
-- ## 왜
-- 이 매핑은 우리 추정이 아니라 **그룹웨어의 폴더 ACL** 이다. Oracle 뷰
-- `V_TYSLACK_SCHEDULE_FOLDER` 의 `org_code` 는 `SYS_OBJECT_ACL` 에서
-- `SUBJECTTYPE='GR'` · `READ='R'` 인 행, 즉 "이 부서는 이 폴더를 읽을 수 있다" 를
-- 그룹웨어가 직접 선언한 값이다.
--
-- 그룹웨어가 이미 열어 준 것을 우리가 한 번 더 승인받는 것은 같은 판단을 두 번 하는
-- 것이고, 그 사이에는 **아무에게도 일정 DM 이 가지 않는다.** 실제로 그렇게 됐다 —
-- 승인 표가 빈 채로 남아 DM 이 0건이었고, 켠 사람은 오지 않는 알림을 기다렸다.
--
-- ## 노출이 늘지 않는다
-- 받는 사람은 그 폴더를 그룹웨어에서 이미 열어 볼 수 있다. 게다가 `/일정 알림` 을
-- 스스로 켠 사람만 받는다. 우리가 더하는 것은 **밀어 주기**뿐이다.
--
-- ## 사람이 판단한 행은 건드리지 않는다
-- `approved_by` 가 아직 `자동수집(미승인)` 인 행만 켠다. 사람이 끈 행은 `approved_by`
-- 가 그 사람으로 바뀌어 있으므로 이 UPDATE 에 걸리지 않는다 — **끈 것을 되살리지
-- 않는다.** 시끄러운 폴더를 끄는 것은 여전히 사람 몫이고, 그 결정을 우리가 덮으면
-- 끌 방법이 없어진다.
--
-- 여러 번 실행해도 안전하다(두 번째 실행은 0행).
--
-- 실행:
--   sudo cat /opt/tybot/deploy/sql/schedule_folder_acl_default.sql \
--     | sudo -u postgres psql -p 55432 -d tyslackai -f -

BEGIN;

-- 1. 폴더 — 발송 대상으로 켠다.
UPDATE schedule_folder
   SET enabled = true,
       approved_by = 'ACL 자동',
       updated_at = now()
 WHERE NOT enabled
   AND approved_by = '자동수집(미승인)';

-- 2. 폴더↔조직 — ACL 행은 곧 허용이다.
UPDATE schedule_folder_org
   SET enabled = true,
       approved_by = 'ACL 자동',
       updated_at = now()
 WHERE NOT enabled
   AND approved_by = '자동수집(미승인)';

-- 3. 이미 켜진 폴더인데 조직 행이 아예 없는 경우를 대표 조직으로 메운다.
--
-- `schedule_folder.org_code` 는 대표 조직 하나이고, 판정은 `schedule_folder_org` 가
-- 한다. 둘이 갈라져 있어서 폴더는 켜졌는데 DM 은 0건인 상태가 실제로 있었다.
-- 다음 동기화가 ACL 전체를 채우지만, 그때까지 기다리지 않는다.
INSERT INTO schedule_folder_org (source_folder_id, org_code, enabled, approved_by)
SELECT f.source_folder_id, f.org_code, true, 'ACL 자동(대표조직 보정)'
  FROM schedule_folder f
 WHERE f.enabled
   AND f.org_code IS NOT NULL
   AND EXISTS (SELECT 1 FROM org_unit o WHERE o.code = f.org_code)
ON CONFLICT (source_folder_id, org_code) DO NOTHING;

COMMIT;

-- 확인 — 켜진 폴더 중 승인 조직이 없는 것이 0이어야 한다:
--   SELECT count(*) FROM schedule_folder f
--    WHERE f.enabled AND NOT EXISTS (
--      SELECT 1 FROM schedule_folder_org fo
--       WHERE fo.source_folder_id = f.source_folder_id AND fo.enabled);
