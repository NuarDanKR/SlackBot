-- 일정 알림 분을 10분 하나로 맞춘다 (2026-09-11 오너 결정)
--
-- 고를 수 있게 두면 사람마다 다른 값이 되고, 그 선택이 쓸모 있었던 적이 없었다.
-- 알림은 오거나 안 오거나다. 그래서 화면에서 분 선택 버튼을 없앴다.
--
-- 컬럼과 CHECK 제약은 **그대로 둔다.** 다시 여러 값을 쓰게 될 때 스키마를 또 바꾸지
-- 않아도 되고, 이미 저장된 값도 제약을 통과한다.
--
-- 이 UPDATE 가 없으면 예전에 30분·둘 다를 고른 사람은 계속 그 값으로 받는데, 화면에는
-- 「10분 전」 이라고 적혀 있다 — 화면과 실제가 다른 상태가 된다.
--
-- `enabled` 은 건드리지 않는다. 끈 사람은 계속 꺼져 있어야 한다.
--
-- 여러 번 실행해도 안전하다(두 번째 실행은 0행).

BEGIN;

UPDATE schedule_dm_preference
   SET reminder_minutes = ARRAY[10]::smallint[],
       updated_at = now()
 WHERE reminder_minutes <> ARRAY[10]::smallint[];

-- 예전 분으로 잡혀 있는 미발송 건도 정리한다. 다음 플래너 회차가 10분으로 다시 넣는다.
UPDATE schedule_dm_delivery
   SET status = 'cancelled', cancelled_at = now(), updated_at = now()
 WHERE status IN ('pending', 'retry')
   AND reminder_minutes <> 10;

COMMIT;
