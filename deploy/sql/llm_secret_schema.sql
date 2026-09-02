-- LLM API 키 저장 (`.env` 편집을 대체한다)
--
-- `.env` 는 **평문**이다. 서버에 들어갈 수 있는 사람은 누구나 읽고, 백업·복사본에
-- 그대로 따라다니며, 누가 언제 바꿨는지 남지 않는다.
--
-- 여기서는 Fernet 으로 암호화해 넣는다. 암호화 키는 DB 밖의 파일
-- (`/etc/tybot/workspace-secret.key`, 0400)에 둔다 — **DB 백업만으로는 풀 수 없어야**
-- 이 저장이 평문보다 나아진다. 두 곳이 동시에 새지 않는 한 키는 안전하다.
--
-- workspace_secret 과 같은 규칙이다: 평문 저장 금지, 복호화 조회 API 금지,
-- 콘솔은 mask 만 읽는다.

BEGIN;

CREATE TABLE IF NOT EXISTS llm_secret (
    provider   text PRIMARY KEY
               CHECK (provider IN ('anthropic', 'openai')),
    ciphertext bytea NOT NULL,
    -- 화면에 보여 줄 가린 값. 예: sk-ant-…9f0c
    mask       text NOT NULL,
    -- 사용 중지. 삭제 대신 끈다 — 지우면 언제 무엇을 쓰고 있었는지가 사라진다.
    enabled    boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL
);

COMMENT ON TABLE llm_secret IS
    'LLM API 키. 평문 저장 금지, 복호화 조회 API 금지. 콘솔은 mask 만 읽는다.';

COMMIT;
