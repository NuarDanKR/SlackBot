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
-- 1.1 규칙 변경 전후 시험 결과 (B-17)
-- ---------------------------------------------------------------------------
-- 답변 원문 아카이브가 아니다. 승인자가 변경 효과를 확인하는 30일짜리 파생 기록이다.
-- 질문·답변이 들어 있으므로 같은 워크스페이스 개발자와 관리자에게만 API로 보인다.
CREATE TABLE IF NOT EXISTS specialist_rule_test (
    id                  uuid PRIMARY KEY,
    specialist          text NOT NULL REFERENCES specialist_bot(key) ON DELETE CASCADE,
    workspace           text NOT NULL REFERENCES workspace(key) ON DELETE CASCADE,
    requester           text NOT NULL,
    question            text NOT NULL CHECK (length(question) BETWEEN 1 AND 2000),
    current_rules_hash  text NOT NULL CHECK (current_rules_hash ~ '^[0-9a-f]{64}$'),
    draft_rules_hash    text NOT NULL CHECK (draft_rules_hash ~ '^[0-9a-f]{64}$'),
    current_result      jsonb NOT NULL,
    draft_result        jsonb NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    expires_at          timestamptz NOT NULL,
    CHECK (expires_at > created_at)
);

CREATE INDEX IF NOT EXISTS specialist_rule_test_recent
    ON specialist_rule_test (specialist, created_at DESC);

COMMENT ON TABLE specialist_rule_test IS
    '규칙 변경 전후 시험 결과. raw/와 답변 검색 색인에는 들어가지 않는 30일 파생 기록.';

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

-- 권한. 소유자가 다르면 GRANT 는 **자동으로 따라오지 않는다.**
--
-- 표를 `postgres` 로 만들면 소유자도 postgres 가 되고, 봇 역할은 그 표를 못 읽는다.
-- 그런데 `information_schema` 는 권한 필터가 걸린 뷰라서 **표가 아예 안 보이고**,
-- 화면에는 「표가 없다」 는 오류가 나간다. 스키마를 다시 적용해도 달라지지 않는다.
-- `review_digest_sent`(2026-09-11)·`specialist_source`(2026-09-14) 에서 두 번 겪었다.
--
-- 역할이 없는 개발 DB 에서도 적용이 통째로 실패하지 않도록 존재를 확인하고 준다.
DO $$
DECLARE
    seq_name text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tyslackai') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE specialist_secret TO tyslackai';
        EXECUTE 'GRANT SELECT, INSERT, DELETE ON TABLE specialist_rule_test TO tyslackai';
        -- bigserial 시퀀스도 함께. 없으면 INSERT 만 권한 오류로 죽는다.
        --
        -- `ALL SEQUENCES IN SCHEMA public` 은 쓰지 않는다 — 다른 파일이 만든 남의
        -- 시퀀스까지 건드려서, 적용하는 역할이 그것들의 소유자가 아니면 **파일 전체가
        -- 실패한다.** 이 파일이 만든 표에 딸린 것만 정확히 준다.
        FOR seq_name IN
            SELECT quote_ident(n.nspname) || '.' || quote_ident(s.relname)
              FROM pg_class s
              JOIN pg_namespace n ON n.oid = s.relnamespace
              JOIN pg_depend d ON d.objid = s.oid AND d.deptype = 'a'
              JOIN pg_class t ON t.oid = d.refobjid
             WHERE s.relkind = 'S' AND t.relname = ANY(ARRAY['specialist_secret'])
        LOOP
            EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE ' || seq_name || ' TO tyslackai';
        END LOOP;
    END IF;
END
$$;

COMMIT;
