-- 채널 요약 검토자 (B-37)
--
-- 요약은 봇이 **후보만** 만들고 사람이 확정한다. 그 사람을 여기에 둔다.
-- 설계: docs/design/summary-review.md
--
-- **왜 파일이 아니라 DB 인가** — 검토 대상 목록과 결정 이력이 이 값을 참조한다.
-- 채널 소유자(`ChannelOwnerStore`)는 JSON 파일에 있는데, 그건 "누가 만들었나" 하나뿐이라
-- 참조하는 것이 없다. 검토자는 다르다: 후보 목록·발송 이력·헬스 체크가 함께 읽고,
-- 여러 프로세스(봇·타이머·콘솔)가 동시에 본다. 파일 잠금으로 버티는 자리가 아니다.

BEGIN;

CREATE TABLE IF NOT EXISTS channel_reviewer (
    workspace     text NOT NULL,
    -- Slack 채널 ID. **이름이 아니다** — 채널명은 바뀌고, 바뀌면 검토자가 조용히
    -- 사라진다(`/채널 이름변경` 이 실제로 그렇게 만든다).
    channel_id    text NOT NULL,
    -- 표시용. 이름이 바뀌면 갱신되지만, 판정에 쓰지 않는다.
    channel_name  text NOT NULL DEFAULT '',
    reviewer_user text NOT NULL,
    -- 보낼 시각(KST). 채널 개설자가 정한다.
    send_at       time NOT NULL DEFAULT '08:00',
    -- 사용 중지. 삭제 대신 끈다 — 지우면 언제부터 검토가 멈췄는지 알 수 없다.
    enabled       boolean NOT NULL DEFAULT true,
    set_by        text NOT NULL,
    set_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (workspace, channel_id, reviewer_user)
);

COMMENT ON TABLE channel_reviewer IS
    '채널별 요약 검토자. 검토자가 없으면 요약을 반영하지 않는다(자동 반영으로 물러서지 않는다).';

-- 발송 시각 훑기. 1분마다 도는 타이머가 "지금 보낼 것" 을 찾는 경로다.
CREATE INDEX IF NOT EXISTS channel_reviewer_due
    ON channel_reviewer (send_at) WHERE enabled;

-- 검토자별 담당 채널. 사람이 그만두면 무엇이 비는지 바로 나온다.
CREATE INDEX IF NOT EXISTS channel_reviewer_person
    ON channel_reviewer (workspace, reviewer_user) WHERE enabled;

COMMIT;
