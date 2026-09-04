-- 전문 봇 라우팅 · 모델 지정 · MCP 연결 (B-36 라우터, B-39 MCP)
--
-- `specialist_bot` 은 이미 있고(콘솔 수명주기), 여기서 **세 가지를 더한다.**
-- 전부 `ADD COLUMN IF NOT EXISTS` 라 여러 번 돌려도 안전하고, 지우는 것이 없다.
--
--   1. routing_hint — 저가 모델이 「누구에게 물을지」 판단할 때 읽는 설명
--   2. model        — 이 전문가가 쓸 모델(비면 마스터 기본값)
--   3. specialist_mcp — 이 전문가가 붙을 수 있는 외부 MCP 서버 허용 목록
--
-- 설계: docs/design/bot-hierarchy.md

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. 라우팅 설명
-- ---------------------------------------------------------------------------
-- `domain` 은 화면에 보이는 분류다("법률"). 라우터 프롬프트에 그것만 실으면
-- 「하자보수 책임기간」 을 법률로 보낼지 판단하기 어렵다. 무엇을 물어야 하는지
-- 예시까지 담는 자리를 따로 둔다.
--
-- **길이를 묶는다.** 이 값은 질문마다 프롬프트에 실리므로, 길어지면 라우팅 비용이
-- 전문가 수에 비례해 오른다. 그 비용은 답변 품질이 아니라 분류에 쓰인다.
ALTER TABLE specialist_bot
    ADD COLUMN IF NOT EXISTS routing_hint text NOT NULL DEFAULT '';

ALTER TABLE specialist_bot
    DROP CONSTRAINT IF EXISTS specialist_bot_routing_hint_len;
ALTER TABLE specialist_bot
    ADD CONSTRAINT specialist_bot_routing_hint_len
    CHECK (length(routing_hint) <= 300);

-- ---------------------------------------------------------------------------
-- 2. 모델 지정
-- ---------------------------------------------------------------------------
-- 비어 있으면 마스터의 기본 모델을 쓴다. **여기서 검증하지 않는다** — 모델 목록은
-- 게이트웨이 레지스트리가 알고, DB 가 알 수 없다. 없는 모델이 들어오면 호출 시점에
-- UnknownModel 로 떨어지고 마스터 답변으로 폴백한다.
ALTER TABLE specialist_bot
    ADD COLUMN IF NOT EXISTS model text NOT NULL DEFAULT '';

-- 이 전문가를 부를 최소 신뢰도. 라우터가 이보다 낮게 판단하면 마스터가 직접 답한다.
-- 전문가별로 다르게 두는 이유: 오답의 값이 다르다. 법률·회계는 틀리면 사람이 오판하고,
-- 내부 기록은 틀려도 원문을 다시 보면 된다.
ALTER TABLE specialist_bot
    ADD COLUMN IF NOT EXISTS min_confidence numeric(3, 2) NOT NULL DEFAULT 0.60;

ALTER TABLE specialist_bot
    DROP CONSTRAINT IF EXISTS specialist_bot_min_confidence_range;
ALTER TABLE specialist_bot
    ADD CONSTRAINT specialist_bot_min_confidence_range
    CHECK (min_confidence >= 0 AND min_confidence <= 1);

COMMENT ON COLUMN specialist_bot.routing_hint IS
    '라우터가 읽는 설명. 질문마다 프롬프트에 실리므로 300자로 묶는다.';
COMMENT ON COLUMN specialist_bot.model IS
    '이 전문가가 쓸 모델. 비면 마스터 기본값. 유효성은 게이트웨이가 판정한다.';

-- ---------------------------------------------------------------------------
-- 3. MCP 연결 허용 목록
-- ---------------------------------------------------------------------------
-- 법률 봇은 외부 법령을 MCP 로 읽는다. 그런데 MCP 서버는 **우리 밖**이고,
-- 어댑터가 우리 근거를 그 서버로 보내면 권한 경계가 그 서버 운영자에게 넘어간다.
--
-- 기술로 막을 수 없는 자리다. 그래서 **허용 목록으로** 막는다 — 어댑터는 여기
-- 선언된 서버만 쓸 수 있고, 추가는 다른 관리자의 승인을 받는다.
-- 무엇이 붙어 있는지 한 화면에서 보이는 것 자체가 통제다.
CREATE TABLE IF NOT EXISTS specialist_mcp (
    specialist  text NOT NULL REFERENCES specialist_bot(key) ON DELETE CASCADE,
    -- Messages API 의 `mcp_servers[].name`. `tools[].mcp_server_name` 과 같아야 한다 —
    -- 한쪽만 주면 검증 오류다.
    name        text NOT NULL CHECK (name ~ '^[a-z][a-z0-9_-]{1,31}$'),
    url         text NOT NULL,
    -- 무엇을 읽는 서버인지. 승인하는 사람이 판단할 근거다.
    purpose     text NOT NULL DEFAULT '',
    enabled     boolean NOT NULL DEFAULT false,
    approved_by text NOT NULL DEFAULT '',
    approved_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    created_by  text NOT NULL,
    PRIMARY KEY (specialist, name)
);

-- **평문 http 를 막는다.** 사내 질문이 그 URL 로 나가는데 도중에 읽히면 안 된다.
-- localhost 도 막는다 — 서버 안에서 돌리는 것은 MCP 가 아니라 우리 코드로 한다.
ALTER TABLE specialist_mcp
    DROP CONSTRAINT IF EXISTS specialist_mcp_url_https;
ALTER TABLE specialist_mcp
    ADD CONSTRAINT specialist_mcp_url_https
    CHECK (url ~ '^https://[a-zA-Z0-9]' AND url NOT ILIKE '%localhost%');

-- 승인 없이 켤 수 없다. 스키마에서 막아 두면 코드가 한 곳을 빼먹어도 새지 않는다.
ALTER TABLE specialist_mcp
    DROP CONSTRAINT IF EXISTS specialist_mcp_enabled_needs_approval;
ALTER TABLE specialist_mcp
    ADD CONSTRAINT specialist_mcp_enabled_needs_approval
    CHECK (NOT enabled OR (btrim(approved_by) <> '' AND approved_at IS NOT NULL));

CREATE INDEX IF NOT EXISTS specialist_mcp_live
    ON specialist_mcp (specialist) WHERE enabled;

COMMENT ON TABLE specialist_mcp IS
    '전문 봇이 붙을 수 있는 외부 MCP 서버 허용 목록. 승인된 것만 켜진다.';

-- ---------------------------------------------------------------------------
-- 4. 라우팅 판정 기록
-- ---------------------------------------------------------------------------
-- `specialist_call` 에는 어느 전문가가 답했는지가 남는다. 그런데 **마스터가 직접
-- 답한 경우**는 남지 않아, "왜 전문가에게 안 갔나" 를 되짚을 수 없다.
-- 라우터가 `none` 을 골랐거나 실패한 것도 판정이므로 함께 남긴다.
ALTER TABLE specialist_call
    ADD COLUMN IF NOT EXISTS router_model text NOT NULL DEFAULT '';

COMMIT;
