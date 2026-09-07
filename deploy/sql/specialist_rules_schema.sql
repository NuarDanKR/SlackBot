-- 전문 봇 답변 규칙 · 전용 API 키를 콘솔에서 다룬다 (B-38)
--
-- 팀마다 개발자가 다르고, 그 팀이 **자기 규칙과 자기 키**로 답하고 싶어 한다.
-- 코드 저장소를 나누는 대신 이 둘을 콘솔에서 받는다.
--
-- 처음에는 「프롬프트를 콘솔에서 편집하지 않는다」 로 설계했다. 이유는 답변 규칙이
-- **코드 리뷰 없이 바뀌는 길**이 생긴다는 것이었다. 그 걱정은 이미 해소돼 있다 —
-- 변경이 `specialist_change_request` 를 지나 승인을 받고, `console_audit_event` 에
-- 남는다. 리뷰의 자리가 git 에서 콘솔로 옮겨간 것이지 없어진 것이 아니다.
--
-- 설계: docs/design/specialist-deployment.md

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 답변 규칙
-- ---------------------------------------------------------------------------
-- 비어 있으면 저장소의 `specialist_prompts/<key>.md` 를 쓴다. 그래서 콘솔을 안 쓰는
-- 전문가도 그대로 돌고, 콘솔에서 규칙을 지우면 파일로 되돌아간다.
ALTER TABLE specialist_bot
    ADD COLUMN IF NOT EXISTS rules text NOT NULL DEFAULT '';

-- 길이를 묶는다. 규칙은 **질문마다** 프롬프트에 실린다 — 길어지면 비용이 질문 수에
-- 비례해 오르고, 그 비용은 근거를 읽는 데가 아니라 지시문에 쓰인다.
ALTER TABLE specialist_bot
    DROP CONSTRAINT IF EXISTS specialist_bot_rules_len;
ALTER TABLE specialist_bot
    ADD CONSTRAINT specialist_bot_rules_len CHECK (length(rules) <= 8000);

-- 규칙이 바뀔 때마다 오른다. 콘솔에서 「지금 무엇이 돌고 있나」 를 보이는 값이고,
-- 답변 품질이 달라졌을 때 되짚는 기준이다.
ALTER TABLE specialist_bot
    ADD COLUMN IF NOT EXISTS rules_version integer NOT NULL DEFAULT 0;

COMMENT ON COLUMN specialist_bot.rules IS
    '콘솔에서 편집하는 답변 규칙. 비면 저장소의 프롬프트 파일을 쓴다.';

-- ---------------------------------------------------------------------------
-- 2. 전문가 전용 API 키
-- ---------------------------------------------------------------------------
-- `llm_secret` 은 프로바이더 단위(우리 키)다. 팀이 **자기 키**로 답하고 싶으면
-- 전문가 단위가 필요하다. 비용과 한도가 그 팀 계정에 걸린다.
--
-- 규칙은 `llm_secret` 과 같다: 평문 저장 금지, 복호화 조회 API 금지,
-- 콘솔은 mask 만 읽는다. 암호화 키는 DB 밖 파일에 있다.
CREATE TABLE IF NOT EXISTS specialist_secret (
    specialist text PRIMARY KEY REFERENCES specialist_bot(key) ON DELETE CASCADE,
    -- 어느 프로바이더의 키인가. 모델과 짝이 맞아야 한다 —
    -- anthropic 키로 gpt 를 부를 수 없다.
    provider   text NOT NULL CHECK (provider IN ('anthropic', 'openai')),
    ciphertext bytea NOT NULL,
    mask       text NOT NULL,
    enabled    boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL
);

COMMENT ON TABLE specialist_secret IS
    '전문가 전용 LLM 키. 비용이 그 팀 계정에 걸린다. 평문 저장·복호화 조회 금지.';

COMMIT;
