# 모델 제공사 어댑터 P0-1 (digest 자리) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 모델 호출을 감싸는 제공사 어댑터(`src/llm/provider.js`)를 신설하고, 일일·주간 요약(digest) 자리를 첫 소비자로 이전한다 — **동작 무변화를 기준판 대조로 증명한 뒤에만**.

**Architecture:** `createTextModel(spec, {client})` 가 「글 반환」 창구를 제공한다. 중단 사유를 정규화하고(`ok`·`max_tokens`·`refusal`, 모르는 것은 원문 유지), 재시도 가능 판정(`retryable`)을 제공사 안으로 옮기며, usage 는 **회사 원문 그대로** 통과시킨다(format.js 의 5분/1시간 캐시 단가 구분이 원문 필드를 본다). digest.js 는 Anthropic SDK 어휘(`APIConnectionError`, `stop_reason` 원문)를 더 이상 모른다. 무변화 증명은 기존 `check-llm-summary.js --baseline` 하네스(수정 전 커밋의 요청·로그·대기·반환을 trace 로 대조)를 확장해 수행한다.

**Tech Stack:** Node ≥20 ESM, `@anthropic-ai/sdk ^0.115.0`, 기존 오프라인 검사 하네스 (`scripts/check-*.js`, 가짜 클라이언트 + 네트워크 차단).

## Global Constraints

- **커밋·push 는 각각 WHK 승인 후에만** (저장소 CLAUDE.md). 서브에이전트는 커밋하지 않는다 — 고치고·검사하고·보고만.
- 검사 스크립트는 실제 네트워크·파일·Slack 에 닿지 않는다 (기존 check 계열과 동일).
- 사업장·비공개 채널 이름을 코드·주석에 쓰지 않는다.
- 수정 후 `npm run check` 전체 통과가 완료 조건.
- 이 계획은 digest 자리만 이전한다. **qa.js·summary-check.js 는 건드리지 않는다** (P0-2·P0-3).
- 요청 바이트·로그 문구·재시도 사다리·게이트 동작이 수정 전과 동일해야 한다 (D2). 다른 점이 하나라도 발견되면 이유를 적고 멈춘다 — 조용히 맞추지 않는다.

---

### Task 1: 제공사 어댑터 `src/llm/provider.js`

**Files:**
- Create: `src/llm/provider.js`
- Test: `scripts/check-llm-provider.js`

**Interfaces:**
- Produces: `createTextModel(spec, { client }) → { provider, id, generate, retryable }`
  - `spec: { provider?: 'anthropic', id: string, effort: string, maxTokens: number }` (여분 키 무시)
  - `client: () => SDK클라이언트` — 지연 생성 함수. 검사가 가짜를 꽂는 자리라 주입받는다
  - `generate({ system, messages }) → Promise<{ text, stopReason, model, usage, raw }>`
  - `retryable(err) → boolean`
- Consumes: 없음 (신규 최하층)

- [ ] **Step 1: 실패하는 검사를 먼저 쓴다** — `scripts/check-llm-provider.js`

```js
#!/usr/bin/env node
/**
 * 모델 제공사 창구(provider.js)의 오프라인 계약 검사.
 * 가짜 클라이언트만 쓰고 네트워크·파일·Slack 에 닿지 않는다.
 */
import assert from 'node:assert/strict';
import { APIConnectionError } from '@anthropic-ai/sdk';
import { createTextModel } from '../src/llm/provider.js';

const clone = (v) => JSON.parse(JSON.stringify(v));
const SPEC = { id: 'model-fixture', effort: 'high', maxTokens: 1600 };
const SYSTEM = [{ type: 'text', text: '지시문' }];
const MESSAGES = [{ role: 'user', content: '원문' }];

function fakeClient(reply) {
  const calls = [];
  const client = () => ({ messages: { stream: (params) => {
    calls.push(clone(params));
    return { finalMessage: async () => clone(reply) };
  } } });
  return { client, calls };
}

// ① 요청 파라미터 — Anthropic 어휘로 정확히 이 모양이어야 한다 (digest 기준판 대조가 이 모양을 본다).
{
  const reply = { model: 'served-fixture', stop_reason: 'end_turn',
    content: [{ type: 'text', text: ' 본문 ' }, { type: 'tool_use' }, { type: 'text', text: '둘째' }],
    usage: { input_tokens: 3, output_tokens: 2 } };
  const { client, calls } = fakeClient(reply);
  const res = await createTextModel(SPEC, { client }).generate({ system: SYSTEM, messages: MESSAGES });
  assert.deepEqual(calls, [{ model: 'model-fixture', max_tokens: 1600,
    output_config: { effort: 'high' }, system: SYSTEM, messages: MESSAGES }]);
  // ② 본문 추출 — digest 의 기존 규칙 그대로: text 블록만, '\n' 연결, **전체** 양끝만 trim.
  // 블록별 trim 이 아니다 — '본문' 뒤 공백이 살아남는 것이 기존 동작이다
  // (check-llm-summary.js 의 기존 기대값 '앞 \n뒤' 와 같은 규칙). 블록별로 깎아 "고치면"
  // Task 2 의 기준판 대조가 깨진다.
  assert.equal(res.text, '본문 \n둘째');
  assert.equal(res.stopReason, 'ok');
  assert.equal(res.model, 'served-fixture');
  assert.deepEqual(res.usage, reply.usage);
  assert.equal(res.raw.stop_reason, 'end_turn');
}

// ③ 중단 사유 정규화 — 아는 것만 옮기고 모르는 것은 원문 유지 (새 사유가 'ok' 로 조용히 묻히면 안 된다).
for (const [given, want] of [['max_tokens', 'max_tokens'], ['refusal', 'refusal'], ['tool_use', 'tool_use']]) {
  const { client } = fakeClient({ model: 'm', stop_reason: given, content: [], usage: {} });
  const res = await createTextModel(SPEC, { client }).generate({ system: SYSTEM, messages: MESSAGES });
  assert.equal(res.stopReason, want);
}

// ④ 재시도 판정 — digest.js 의 기존 isRetryableApiError 진리표 그대로.
{
  const m = createTextModel(SPEC, { client: () => { throw new Error('호출 불필요'); } });
  const conn = Object.create(APIConnectionError.prototype); // instanceof 만 필요하다
  assert.equal(m.retryable(conn), true);
  for (const type of ['overloaded_error', 'api_error', 'rate_limit_error'])
    assert.equal(m.retryable(Object.assign(new Error('x'), { type })), true);
  for (const status of [408, 409, 429, 500, 503])
    assert.equal(m.retryable(Object.assign(new Error('x'), { status })), true);
  assert.equal(m.retryable(Object.assign(new Error('x'), { status: 400 })), false);
  assert.equal(m.retryable(new Error('맨 에러')), false);
}

// ⑤ 모르는 provider 는 던진다 — 조용히 Anthropic 으로 가면 오타가 엉뚱한 단가로 계산된다.
assert.throws(() => createTextModel({ ...SPEC, provider: 'acme' }, { client: () => ({}) }), /acme/);
assert.equal(createTextModel({ ...SPEC, provider: 'anthropic' }, { client: () => ({}) }).provider, 'anthropic');

console.log('[check-llm-provider] OK — 요청 모양·본문 추출·중단 사유 정규화·재시도 진리표·provider 검증');
```

- [ ] **Step 2: 실패 확인**

Run: `node scripts/check-llm-provider.js`
Expected: FAIL — `Cannot find module '../src/llm/provider.js'`

- [ ] **Step 3: 구현** — `src/llm/provider.js`

```js
/**
 * 모델 제공사 어댑터 — 호출 자리는 이 창구만 보고, 회사별 SDK 어휘는 이 안에서 끝낸다.
 *
 * 계약 (P0-1 은 「글 반환」 창구만. JSON 창구·도구 왕복 창구는 P0-2·P0-3):
 *   createTextModel(spec, { client }) → { provider, id, generate, retryable }
 *   generate({ system, messages }) → { text, stopReason, model, usage, raw }
 *     - stopReason 정규화: 'ok' | 'max_tokens' | 'refusal'. 모르는 사유는 원문 그대로 —
 *       'ok' 로 뭉개면 새 사유가 조용히 완성본으로 통과한다.
 *     - usage 는 회사 원문 그대로다. format.js 의 5분/1시간 캐시 단가 구분이 원문 필드를
 *       보므로, 여기서 정규화하면 회계가 후퇴한다 (2026-09-18 설계 검증 3번 발견).
 *     - raw 는 회사 원문 응답 — 비용 원장(usage.js record)이 message.id 중복 제거에 쓴다.
 *   retryable(err) → 다시 걸어 볼 만한 실패인가. 에러 어휘가 회사마다 달라 판정을 여기 둔다.
 *
 * client 는 지연 생성 함수로 주입받는다 — 검사(check-llm-provider.js 등)가 가짜를 꽂는 자리다.
 */
import { APIConnectionError } from '@anthropic-ai/sdk';

const STOP_MAP = { end_turn: 'ok', max_tokens: 'max_tokens', refusal: 'refusal' };

/** 다시 걸어 볼 만한 에러인가 — 과부하·레이트리밋·5xx·연결 끊김. 잘못된 요청(4xx)은 다시 걸어도 같다.
 * (digest.js 의 isRetryableApiError 를 그대로 옮겼다 — 판정은 이제 여기 한 곳뿐이다) */
function anthropicRetryable(err) {
  if (err instanceof APIConnectionError) return true;
  // APIError.type 은 응답 본문의 error.type 이다. 스트림 안에서 온 에러는 status 가 없어 이쪽으로만 잡힌다.
  if (['overloaded_error', 'api_error', 'rate_limit_error'].includes(err?.type)) return true;
  const status = err?.status;
  return typeof status === 'number' && (status === 408 || status === 409 || status === 429 || status >= 500);
}

function anthropicTextModel(spec, client) {
  return {
    provider: 'anthropic',
    id: spec.id,
    retryable: anthropicRetryable,
    async generate({ system, messages }) {
      // 출력이 길어질 수 있어 스트리밍으로 받는다 (비스트리밍은 HTTP 타임아웃 위험).
      const raw = await client().messages.stream({
        model: spec.id,
        max_tokens: spec.maxTokens,
        output_config: { effort: spec.effort },
        system,
        messages,
      }).finalMessage();
      return {
        text: (raw.content || []).filter((b) => b.type === 'text').map((b) => b.text).join('\n').trim(),
        stopReason: STOP_MAP[raw.stop_reason] ?? raw.stop_reason,
        model: raw.model,
        usage: raw.usage,
        raw,
      };
    },
  };
}

export function createTextModel(spec, { client }) {
  const provider = spec.provider || 'anthropic';
  if (provider === 'anthropic') return anthropicTextModel(spec, client);
  throw new Error(`모르는 모델 제공사입니다: ${provider} — config.json 의 models.*.provider 를 확인하세요.`);
}
```

- [ ] **Step 4: 통과 확인**

Run: `node scripts/check-llm-provider.js`
Expected: `[check-llm-provider] OK — …`

- [ ] **Step 5: 검사 카탈로그에 등록** — 반드시 이 Task 안에서 한다. `check-check-modes.js` 가 「카탈로그가 발견된 node 검사를 전부 덮나」를 fail-closed 로 검증하므로, 새 `check-*.js` 파일만 있고 등록이 없으면 `npm run check` 가 깨진다.

`scripts/check-catalog.js` 에서 `"file": "scripts/check-llm-summary.js"` 항목을 찾아 그 객체 **앞**에:

```json
  {
    "file": "scripts/check-llm-provider.js",
    "what": "모델 제공사 창구의 요청 모양·중단 사유 정규화·재시도 진리표가 계약을 지키나",
    "runtime": "node",
    "mode": "offline",
    "reason": "Synthetic inputs or repository source inspection; default arguments only."
  },
```

- [ ] **Step 6: 전체 검사**

Run: `npm run check`
Expected: 전체 통과 (신규 검사 줄 포함)

- [ ] **Step 7: 커밋 승인 요청 (직접 커밋 금지)**

WHK 에게 보고: "provider.js + 검사 신설·등록, digest 미접속 상태 (기존 동작 무변화)". 승인 시 본 세션이 경로 지정으로 커밋: `git add src/llm/provider.js scripts/check-llm-provider.js scripts/check-catalog.js`

---

### Task 2: digest 를 어댑터로 이전 + 기준판 무변화 증명

**Files:**
- Modify: `src/llm/digest.js` (import·팩토리 시그니처·시도 루프·게이트)
- Modify: `src/claude.js:92-94` (generateDigest 배선) + import 추가
- Modify: `scripts/check-llm-summary.js:48-51` (DEP_NAMES)·`:85` (baseline 판 감지)·import
- Modify: `createDigestGenerator` 를 부르는 나머지 검사 — `scripts/check-usage-accounting.js`, `scripts/check-live-llm.js` (같은 치환 패턴)

**Interfaces:**
- Consumes: Task 1 의 `createTextModel(spec, { client })`
- Produces: `createDigestGenerator({ config, textModel, promptFile, logUsage, estimateCost, errLabel, sleep })` — `anthropic` 의존이 `textModel: (spec) => 창구` 로 바뀐다. `generateDigest` 의 인자·반환 모양은 **불변**.

- [ ] **Step 1: 기준판 커밋을 먼저 기록한다 (수정 전!)**

Run: `git -C . rev-parse HEAD`
기록해 둔 40자 sha 를 이후 `<BASELINE_SHA>` 로 쓴다. 작업 트리가 Task 1 커밋 뒤 깨끗한지 `git status` 로 확인.

- [ ] **Step 2: `src/llm/digest.js` 수정**

지울 것: 6행 `import { APIConnectionError } from '@anthropic-ai/sdk';` 와 56-63행 `isRetryableApiError` 전체 (판정은 provider 로 이사).

팩토리 시그니처 (65-67행):

```js
export function createDigestGenerator({
  config, textModel, promptFile, logUsage, estimateCost, errLabel, sleep = defaultSleep,
}) {
```

params 조립 (88-94행) — params 객체 대신 두 상수로:

```js
    const system = [{ type: 'text', text: promptFile(kind) }];
    const messages = [{ role: 'user', content: userText }];
```

시도 루프 (111-137행) — 대기·경고 문구·tried 기록·던지기 조건은 글자 그대로 유지:

```js
    for (let i = 0; i < attempts.length; i++) {
      const [model, wait] = attempts[i];
      const m = textModel({ ...cfg, id: model });
      if (wait) {
        console.warn(`[${kind}] ${Math.round(wait / 1000)}초 뒤 다시 시도합니다 (${i + 1}/${attempts.length})`);
        await sleep(wait);
      } else if (i) {
        console.warn(`[${kind}] 대체 모델(${model})로 시도합니다 (${i + 1}/${attempts.length})`);
      }
      try {
        final = await m.generate({ system, messages });
        ledger.record(final.raw, model);
        tried.push({ model, waitMs: wait, errorType: null });
        break;
      } catch (err) {
        ledger.failed(model);
        attachUsage(err, ledger.snapshot());
        tried.push({ model, waitMs: wait, errorType: errLabel(err) });
        // 마지막 시도이거나 다시 걸어도 같은 종류면 그대로 던진다 — scheduler 가 DM 으로 알린다.
        if (i === attempts.length - 1 || !m.retryable(err)) {
          // 던지면 반환값이 없다 — 이력을 에러에 실어야 로그가 본다.
          err.hermesAttempts = tried;
          throw err;
        }
        console.warn(`[${kind}] 요약 호출 실패 (${errLabel(err)})`);
      }
    }
```

게이트·반환 — `final` 이 정규화 결과가 됐으므로 세 곳만 바뀐다 (주석·문구는 그대로):
- `logUsage(\`digest:${kind}\`, final.model, final.usage);` — 불변 (필드명 동일)
- 141행 `final.stop_reason === 'refusal'` → `final.stopReason === 'refusal'`
- 156행 `final.stop_reason === 'max_tokens'` → `final.stopReason === 'max_tokens'`
- 170-175행 본문 추출 → `text: final.text,` 한 줄 (추출 규칙은 provider 가 동일하게 수행 — Task 1 검사 ②가 보증)

- [ ] **Step 3: `src/claude.js` 배선**

10행 근처 import 에 추가: `import { createTextModel } from './llm/provider.js';`

`errLabel` 함수 뒤, 91행 `compareSummary` export 앞에 (facade 검사가 이 위치의 코드만 뽑아 평가하므로 **errLabel 주석 블록보다 뒤**여야 한다):

```js
// 모델 제공사 창구 — Q&A 와 같은 지연 클라이언트를 물려 만든다.
// 검사(check-llm-summary.js)가 createTextModel 을 같은 이름으로 주입한다.
const textModel = (spec) => createTextModel(spec, { client: anthropic });
```

92-94행 배선에서 `anthropic` → `textModel`:

```js
export const generateDigest = createDigestGenerator({
  config, textModel, promptFile, logUsage, estimateCost, errLabel,
});
```

`compareSummary`(91행)는 그대로 둔다 — P0-2 범위.

- [ ] **Step 4: `scripts/check-llm-summary.js` 하네스 수정**

import 추가: `import { createTextModel } from '../src/llm/provider.js';`

DEP_NAMES(48-51행)에 `'createTextModel'` 추가, facadeFactory 의 주입 map(67-71행)에 `createTextModel,` 추가.

baseline 판 감지(85행) — 지금은 무조건 legacy(true)라 ERR_MARK 판 기준판이 안 읽힌다:

```js
    baselineFactory = facadeFactory(original, !original.includes(ERR_MARK));
```

- [ ] **Step 5: 나머지 호출자 치환** — `grep -rl "createDigestGenerator(" src/ scripts/` 로 전수 확인 (검증 시점 실측: `check-usage-accounting.js:144` 는 `anthropic: () => 가짜클라이언트`, `check-live-llm.js:85` 는 `anthropic: () => capture(kind)` — 둘 다 아래 패턴에 맞음). 각각에서 `createDigestGenerator({ …, anthropic: X, … })` 를 다음 패턴으로:

```js
createDigestGenerator({ …, textModel: (spec) => createTextModel(spec, { client: X }), … })
```

파일 상단에 `import { createTextModel } from '../src/llm/provider.js';` 추가. X 는 그 파일이 원래 넘기던 클라이언트 함수 그대로.

- [ ] **Step 6: 무변화 증명 (D2)**

Run: `node scripts/check-llm-summary.js --baseline <BASELINE_SHA> --facade`
Expected: OK — 수정 전 커밋과 같은 입력에 대해 요청 파라미터·로그·대기·반환/오류 trace 동일. **다르면 멈추고 차이를 보고한다 — 검사를 고쳐 맞추지 않는다.**

- [ ] **Step 7: 주변 검사 회귀**

Run: `node scripts/check-llm-provider.js && node scripts/check-usage-accounting.js && node scripts/check-llm-qa.js`
Expected: 전부 OK (qa 는 미변경 확인용)

- [ ] **Step 8: 커밋 승인 요청 (직접 커밋 금지)**

보고에 Step 6 의 baseline 대조 결과를 포함한다.

---

### Task 3: config 문서화 + 전체 통과

**Files:**
- Modify: `config.example.json:81-89` (models 블록)

**Interfaces:**
- Consumes: Task 1·2 결과물
- Produces: `npm run check` 가 신규 검사를 포함해 전체 통과

- [ ] **Step 1: `config.example.json` models 블록에 provider 설명 추가** (83행 `"qa"` 앞):

```json
    "_provider": "각 모델에 provider 를 적을 수 있다. 생략하면 anthropic. 다른 값은 아직 구현이 없어서, 적으면 그 자리를 쓰는 순간 던진다 — 오타가 조용히 Anthropic 요금으로 계산되는 것을 막는 동작이다.",
```

- [ ] **Step 2: 전체 검사**

Run: `npm run check`
Expected: 전체 통과, 신규 검사 줄 포함. 실패 시 원인 파악 후 수정 (검사 느슨화 금지).

- [ ] **Step 3: 커밋 승인 요청 (직접 커밋 금지)**

---

## 후속 계획 (이 계획 범위 밖)

P0-2 요약 대조(JSON 창구) → P0-3 QA 도구 왕복 창구(회차 훅·거부 개입·시도 단위 회계) → P0-4 Clio(수동 루프·종결 도구 + 두-저장소 어긋남 검사). 설계 원본은 소유자 워크스페이스의 같은 날짜(2026-09-18) 「모델교체-준비 설계」 기록 문서다 — 이 저장소에는 경로를 적지 않는다.
