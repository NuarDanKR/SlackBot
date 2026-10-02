# 모델 제공사 어댑터 P0-2 (요약 대조 · JSON 창구) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 「JSON 반환」 창구(`createJsonModel`)를 어댑터에 더하고, 상단 요약 대조(`src/llm/summary-check.js`)를 그 창구로 이전한다 — **동작 무변화를 기준판 대조로 증명한 뒤에만**. 더불어 P0-1 실행 중 반박 검토가 뚫어 본 하네스 사각(`logUsage` 인자를 아무도 안 본다)을 **이전 전에** 막는다.

**Architecture:** P0-1 의 `createTextModel` 은 스트리밍으로 글을 받는 창구였다. 요약 대조는 **비스트리밍 `messages.create` + `output_config.format.json_schema`** 로 응답 형식을 API 수준에서 못박는 자리라 창구가 하나 더 필요하다. `createJsonModel(spec, {client}).generate({system, messages, schema})` 가 그 자리이고, 반환 모양(`text`·`stopReason`·`model`·`usage`·`raw`)과 `retryable` 은 글 창구와 **같은 정규화 함수**를 쓴다. JSON **파싱은 창구가 하지 않는다** — 파싱 실패 문구가 도메인 문구(`요약 대조 응답이 JSON 이 아닙니다`)라 옮기면 무변화 증명이 깨지고, 형식 보장은 요청 쪽(schema)에서 이미 끝난다. 무변화 증명은 P0-1 과 같은 `check-llm-summary.js --baseline <sha> --facade` 하네스를 쓰되, 기준판 `summary-check.js` 도 git 에서 읽어 **옛 SDK 경로 대 새 창구 경로**를 대조하게 넓힌다.

**Tech Stack:** Node ≥20 ESM, `@anthropic-ai/sdk ^0.115.0`, 기존 오프라인 검사 하네스 (`scripts/check-*.js`, 가짜 클라이언트 + 네트워크 차단).

## Global Constraints

- **커밋·push 는 각각 WHK 승인 후에만** (저장소 CLAUDE.md). 서브에이전트는 커밋하지 않는다 — 고치고·검사하고·보고만. `git commit`·`git push`·`git add`·`git stash`·`git checkout`·`git restore` 를 쓰지 않는다. 읽기 전용 git(`git log`·`git show`·`git diff`·`git status`·`git rev-parse`)만 쓴다.
- 검사 스크립트는 실제 네트워크·파일·Slack 에 닿지 않는다 (기존 check 계열과 동일).
- 사업장·비공개 채널 이름을 코드·주석에 쓰지 않는다.
- 수정 후 `npm run check` 전체 통과가 완료 조건.
- 이 계획은 **요약 대조 자리만** 이전한다. **`src/llm/qa.js` 는 건드리지 않는다** (P0-3). `src/llm/digest.js` 의 동작도 바꾸지 않는다 (P0-1 에서 끝났다).
- 요청 바이트·로그 문구·게이트 동작·던지는 객체가 수정 전과 동일해야 한다. 다른 점이 하나라도 발견되면 이유를 적고 멈춘다 — **검사를 고쳐 맞추지 않는다.**
- `<BASELINE_SHA>` 는 `1bcbceb5d52230a8283a573b9eb8cff256287382` (P0-1 마지막 커밋). 이 커밋의 `src/claude.js`·`src/llm/summary-check.js` 가 이전 전 짝이다.

---

### Task 1: 하네스 사각을 먼저 막는다 (검사만 고친다 — `src/` 무변화)

P0-1 반박 검토가 실증한 것: **`digest.js`·`summary-check.js` 가 `logUsage` 에 넘기는 usage 를 전부 0 으로 바꿔도 `npm run check` 가 통과한다.** 두 겹으로 비어 있다 — 기준판 대조의 `contract()` 가 `usage`·`cost` 이벤트를 걸러 내고(회계는 R1b 에서 의도적으로 바뀌어 기준판과 못 댄다), 받아 줄 `check-usage-accounting.js` 는 digest 픽스처에 빈 함수를 꽂는다. 원장은 정확하고 틀리는 것은 **대화 로그에 찍히는 비용 줄**이라, 증상이 「`약 $0.000` 이 조용히 나간다」다.

기준판 대조로는 못 막으니 **현재 실행 단언**으로 막는다. Task 3 이 바로 이 층(usage 통과)을 만지므로 **이전 전에** 깔아 둔다.

**Files:**
- Modify: `scripts/check-llm-summary.js` (단언 추가 · legacy 분기 제거)

**Interfaces:**
- Consumes: 없음
- Produces: 없음 (검사 강화만). `facadeFactory` 의 시그니처가 `(text, makeDigest)` 로 줄어든다 — Task 3 이 여기에 인자를 하나 더 붙인다.

- [ ] **Step 1: `logUsage` 인자 단언을 다섯 자리에 넣는다**

지금은 전부 `events(r, 'usage').length` 로 **개수만** 센다. 개수는 그대로 두고 그 아래 줄을 더한다.

`digest` 정상(202-221행 루프 안, `assert.equal(events(r, 'usage').length, 1);` 바로 뒤):

```js
      assert.deepEqual(events(r, 'usage')[0], ['usage', `digest:${kind}`, 'result-fixture', USAGE]);
```

`요약 거절: 반환 보존·비용 누락 수정`(243-250행, `assert.equal(events(r, 'usage').length, 1);` 바로 뒤):

```js
  assert.deepEqual(events(r, 'usage')[0], ['usage', 'digest:daily', 'result-fixture', USAGE]);
  assert.equal(r.outcome.result.model, 'result-fixture');
  assert.deepEqual(r.outcome.result.usage, USAGE);
```

뒤의 두 줄은 별개 사각이다 — `contract()` 가 거부 결과에서 `model`·`usage` 를 지우므로(190-192행) 기준판 대조가 그 값을 안 본다.

`대조 정상·스키마`(320-339행, `assert.equal(events(r, 'usage').length, 1);` 바로 뒤):

```js
      assert.deepEqual(events(r, 'usage')[0], ['usage', 'summary-check', 'result-fixture', USAGE]);
```

`대조 거절: 빈 목록 보존·비용 누락 수정`(342-345행, 같은 자리):

```js
  assert.deepEqual(events(r, 'usage')[0], ['usage', 'summary-check', 'result-fixture', USAGE]);
```

`대조 상한은 JSON 파싱보다 먼저 실패`(346-349행, 같은 자리):

```js
  assert.deepEqual(events(r, 'usage')[0], ['usage', 'summary-check', 'result-fixture', USAGE]);
```

- [ ] **Step 2: 도달 불가가 된 legacy 분기를 걷어낸다**

`facadeFactory` 의 `legacy` 갈래는 `ERR_MARK` 가 없던 옛 `claude.js` 용인데, 그만큼 옛 sha 에는 `src/llm/digest.js` 자체가 없어 P0-1 의 `baselineDigestGenerator` 가 먼저 죽는다 — 즉 지금은 **아무 sha 로도 못 탄다**. 남겨 두면 다음 사람이 살아 있는 갈래로 읽는다.

48행 `const SUMMARY_MARK = '/* ── 상단 요약 대조';` 을 지우고, 75-84행을 다음으로 바꾼다:

```js
function facadeFactory(text, makeDigest = createDigestGenerator) {
  const start = text.indexOf(ERR_MARK);
  assert.ok(start >= 0 && text.indexOf(ERR_MARK, start + ERR_MARK.length) < 0, '유일한 함수 경계가 필요합니다');
  let body = text.slice(start).replace(/\r\n/g, '\n');
```

106행 기준판 조립에서 두 번째 인자(판 감지)를 뺀다:

```js
    baselineFactory = facadeFactory(original, baselineDigestGenerator(baselineSha));
```

- [ ] **Step 3: 현재 판이 통과하는지 확인**

Run: `node scripts/check-llm-summary.js`
Expected: 33개 사례 전부 통과 (단언을 더했을 뿐 소스는 안 바꿨으므로 통과해야 한다)

- [ ] **Step 4: 기준판 대조가 여전히 도는지 확인**

Run: `node scripts/check-llm-summary.js --baseline 1bcbceb5d52230a8283a573b9eb8cff256287382 --facade`
Expected: 33개 통과 · 기준판 일치 · 실제 모델 API 호출 0회

- [ ] **Step 5: 새 단언이 실제로 빨개지는지 확인한다 (비공허성)**

**단언을 더해 놓고 빨개지는 것을 안 본 채 넘어가면 이 Task 는 아무 일도 안 한 것이다.** 아래를 편집 도구로 고쳤다가 편집 도구로 되돌린다 — git 명령을 쓰지 않는다.

1. `src/llm/digest.js` 의 `logUsage(\`digest:${kind}\`, final.model, final.usage);` 를 `logUsage(\`digest:${kind}\`, final.model, { input_tokens: 0, output_tokens: 0 });` 로 바꾼다.
2. Run: `node scripts/check-llm-summary.js` → **실패해야 한다** (`daily 정상 요청·반환·사용량` 에서 `usage` 인자 불일치). 실패 출력을 기록한다.
3. 원래 줄로 되돌린다.
4. `src/llm/summary-check.js` 의 `logUsage('summary-check', final.model, final.usage);` 를 같은 식으로 0 으로 바꾼다.
5. Run: `node scripts/check-llm-summary.js` → **실패해야 한다** (`대조 정상·스키마` 에서). 실패 출력을 기록한다.
6. 원래 줄로 되돌린다.
7. Run: `git diff --stat src/` → **아무것도 안 나와야 한다.** 나오면 되돌리기가 덜 된 것이니 그 자리를 고친다.

둘 중 하나라도 **통과해 버리면** 단언이 잘못 들어간 것이다 — 멈추고 보고한다.

- [ ] **Step 6: 전체 검사**

Run: `npm run check`
Expected: 전체 통과

- [ ] **Step 7: 커밋 승인 요청 (직접 커밋 금지)**

보고에 Step 5 의 두 실패 출력과 `git diff --stat src/` 가 비었다는 사실을 포함한다. 승인 시 본 세션이 경로 지정으로 커밋: `git add scripts/check-llm-summary.js`

---

### Task 2: JSON 창구 `createJsonModel`

**Files:**
- Modify: `src/llm/provider.js` (정규화 함수 추출 + JSON 창구 추가 + 제공사 표)
- Modify: `scripts/check-llm-provider.js` (⑥~⑨ 사례 추가)

**Interfaces:**
- Consumes: P0-1 의 `createTextModel` (같은 파일 · 같은 정규화)
- Produces: `createJsonModel(spec, { client }) → { provider, id, generate, retryable }`
  - `spec`·`client` 는 `createTextModel` 과 같다
  - `generate({ system, messages, schema }) → Promise<{ text, stopReason, model, usage, raw }>`
  - **파싱하지 않는다** — `text` 는 글 창구와 같은 규칙으로 뽑은 원문 문자열이다
  - `retryable(err) → boolean` (글 창구와 같은 함수)

- [ ] **Step 1: 실패하는 검사를 먼저 쓴다** — `scripts/check-llm-provider.js` 확장

8행의 import 를 바꾼다:

```js
import { createTextModel, createJsonModel } from '../src/llm/provider.js';
```

파일 맨 끝의 `console.log('[check-llm-provider] OK — …');` **앞**에 다음을 통째로 넣는다:

```js
// ── JSON 창구 (P0-2) ────────────────────────────────────────────────
// 글 창구와 다른 점은 둘뿐이다: 비스트리밍 create 로 가고, output_config 에 format 이 붙는다.
// 반환 모양·중단 사유·재시도 판정은 같은 함수를 쓰므로 여기서는 「같다」를 못박는다.

const SCHEMA = {
  type: 'object',
  properties: { findings: { type: 'array', items: { type: 'object' } } },
  required: ['findings'],
  additionalProperties: false,
};

// create 만 있는 가짜다 — 창구가 stream 을 부르면 TypeError 로 죽는다. 그게 「비스트리밍」 단언이다.
function fakeJsonClient(reply) {
  const calls = [];
  const client = () => ({ messages: { create: async (params) => {
    calls.push(clone(params));
    return clone(reply);
  } } });
  return { client, calls };
}

// ⑥ 요청 파라미터 — schema 가 output_config.format 안에 들어가고 effort 와 나란히 있어야 한다.
{
  const reply = { model: 'served-json', stop_reason: 'end_turn',
    content: [{ type: 'text', text: ' {"findings":[]} ' }], usage: { input_tokens: 5, output_tokens: 1 } };
  const { client, calls } = fakeJsonClient(reply);
  const res = await createJsonModel(SPEC, { client })
    .generate({ system: SYSTEM, messages: MESSAGES, schema: SCHEMA });
  assert.deepEqual(calls, [{ model: 'model-fixture', max_tokens: 1600,
    output_config: { effort: 'high', format: { type: 'json_schema', schema: SCHEMA } },
    system: SYSTEM, messages: MESSAGES }]);
  // 본문 추출 규칙은 글 창구와 같다 — 파싱하지 않고 문자열로 낸다.
  assert.equal(res.text, '{"findings":[]}');
  assert.equal(res.stopReason, 'ok');
  assert.equal(res.model, 'served-json');
  assert.deepEqual(res.usage, reply.usage);
  assert.equal(res.raw.stop_reason, 'end_turn');
}

// ⑦ 중단 사유 정규화 — 글 창구와 같은 표. 모르는 사유는 원문 유지.
for (const [given, want] of [['max_tokens', 'max_tokens'], ['refusal', 'refusal'], ['tool_use', 'tool_use']]) {
  const { client } = fakeJsonClient({ model: 'm', stop_reason: given, content: [], usage: {} });
  const res = await createJsonModel(SPEC, { client })
    .generate({ system: SYSTEM, messages: MESSAGES, schema: SCHEMA });
  assert.equal(res.stopReason, want);
}

// ⑧ 재시도 판정은 글 창구와 같은 함수여야 한다 — 두 창구가 갈리면 같은 실패에 다르게 굴게 된다.
{
  const dead = () => { throw new Error('호출 불필요'); };
  assert.equal(createJsonModel(SPEC, { client: dead }).retryable,
    createTextModel(SPEC, { client: dead }).retryable);
}

// ⑨ 모르는 provider 는 JSON 창구에서도 던진다.
assert.throws(() => createJsonModel({ ...SPEC, provider: 'acme' }, { client: () => ({}) }), /acme/);
assert.equal(createJsonModel({ ...SPEC, provider: 'anthropic' }, { client: () => ({}) }).provider, 'anthropic');
```

마지막 줄의 문구도 바꾼다:

```js
console.log('[check-llm-provider] OK — 글 창구(요청 모양·본문 추출·중단 사유·재시도 진리표·provider) + JSON 창구(스키마 요청 모양·비스트리밍·같은 정규화)');
```

- [ ] **Step 2: 실패 확인**

Run: `node scripts/check-llm-provider.js`
Expected: FAIL — `createJsonModel is not a function` (또는 import 관련 `SyntaxError`)

- [ ] **Step 3: 구현** — `src/llm/provider.js`

4-12행의 계약 주석 블록에서 첫 줄을 바꾸고 JSON 창구 줄을 더한다:

```js
 * 계약 (P0-1·P0-2 는 「글 반환」·「JSON 반환」 창구. 도구 왕복 창구는 P0-3):
 *   createTextModel(spec, { client }) → { provider, id, generate, retryable }
 *   generate({ system, messages }) → { text, stopReason, model, usage, raw }
 *   createJsonModel(spec, { client }) → 같은 모양. generate 가 schema 를 더 받는다.
 *   generate({ system, messages, schema }) → 같은 반환
 *     - **파싱하지 않는다.** text 는 글 창구와 같은 규칙으로 뽑은 원문이고, 파싱과 그
 *       실패 문구는 호출 자리의 도메인이다 (요약 대조는 잘림을 파싱보다 먼저 판정한다).
```

30-59행(`anthropicTextModel` 부터 끝까지)을 다음으로 바꾼다:

```js
/** 회사 원문 응답 → 창구 공통 반환. 두 창구가 같은 규칙을 쓰게 한 자리다 —
 * 갈라 두면 한쪽만 고쳐지고 그 차이는 에러 없이 흐른다. */
function normalize(raw) {
  return {
    text: (raw.content || []).filter((b) => b.type === 'text').map((b) => b.text).join('\n').trim(),
    stopReason: STOP_MAP[raw.stop_reason] ?? raw.stop_reason,
    model: raw.model,
    usage: raw.usage,
    raw,
  };
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
      return normalize(raw);
    },
  };
}

function anthropicJsonModel(spec, client) {
  return {
    provider: 'anthropic',
    id: spec.id,
    retryable: anthropicRetryable,
    async generate({ system, messages, schema }) {
      // 스트리밍을 쓰지 않는다 — 응답이 스키마로 묶인 짧은 JSON 이고, 옛 호출도 create 였다.
      // format 으로 형식을 API 가 보장한다. 프롬프트로 부탁하던 시절에는 그 부탁이 깨지면
      // 그 채널 회차가 통째로 실패했다 (summary-check.js 주석 참조).
      const raw = await client().messages.create({
        model: spec.id,
        max_tokens: spec.maxTokens,
        output_config: { effort: spec.effort, format: { type: 'json_schema', schema } },
        system,
        messages,
      });
      return normalize(raw);
    },
  };
}

const PROVIDERS = { anthropic: { text: anthropicTextModel, json: anthropicJsonModel } };

function createModel(kind, spec, client) {
  const provider = spec.provider || 'anthropic';
  const make = PROVIDERS[provider]?.[kind];
  if (!make) throw new Error(`모르는 모델 제공사입니다: ${provider} — config.json 의 models.*.provider 를 확인하세요.`);
  return make(spec, client);
}

export function createTextModel(spec, { client }) {
  return createModel('text', spec, client);
}

export function createJsonModel(spec, { client }) {
  return createModel('json', spec, client);
}
```

- [ ] **Step 4: 통과 확인**

Run: `node scripts/check-llm-provider.js`
Expected: `[check-llm-provider] OK — 글 창구… + JSON 창구…`

P0-1 의 ①~⑤ 사례가 **그대로 통과해야 한다** — `normalize` 추출은 순수 리팩터링이라 글 창구 동작이 바뀌면 안 된다.

- [ ] **Step 5: digest 가 안 흔들렸는지 확인**

Run: `node scripts/check-llm-summary.js --baseline 1bcbceb5d52230a8283a573b9eb8cff256287382 --facade`
Expected: 통과 · 기준판 일치. `provider.js` 를 만졌으므로 글 창구가 그대로인지 여기서 한 번 더 본다.

- [ ] **Step 6: 전체 검사**

Run: `npm run check`
Expected: 전체 통과

카탈로그(`scripts/check-catalog.js`)는 **안 바꾼다** — 새 검사 파일을 만들지 않고 기존 `check-llm-provider.js` 를 넓혔다.

- [ ] **Step 7: 커밋 승인 요청 (직접 커밋 금지)**

승인 시: `git add src/llm/provider.js scripts/check-llm-provider.js`

---

### Task 3: 요약 대조를 JSON 창구로 이전 + 기준판 무변화 증명

**Files:**
- Modify: `src/llm/summary-check.js` (팩토리 시그니처 · 요청 조립 · 게이트 · 본문)
- Modify: `src/claude.js` (import · `jsonModel` 헬퍼 · `compareSummary` 배선)
- Modify: `scripts/check-llm-summary.js` (import · `DEP_NAMES` · 주입 map · `baselineSummaryChecker` · 기준판 조립)
- Modify: `scripts/check-usage-accounting.js:171-172`, `scripts/check-live-llm.js:93-96` (같은 치환)

**Interfaces:**
- Consumes: Task 2 의 `createJsonModel(spec, { client })`
- Produces: `createSummaryChecker({ config, jsonModel, promptFile, logUsage, estimateCost })` — `anthropic` 의존이 `jsonModel: (spec) => 창구` 로 바뀐다. `compareSummary` 의 인자·반환 모양은 **불변** (`{channel, summary, transcript, onUsage}` → `Promise<Array<finding>>`).

- [ ] **Step 1: `src/llm/summary-check.js` 수정**

41행 팩토리 시그니처:

```js
export function createSummaryChecker({ config, jsonModel, promptFile, logUsage, estimateCost }) {
```

67-82행(`const params = {…}` 부터 **`} catch (err) {` 줄까지 포함**)을 다음으로 바꾼다. 아래 조각의 마지막 줄이 그 `} catch (err) {` 이므로 범위와 조각의 끝이 맞는다 — 82행을 범위에서 빼면 `} catch (err) {` 가 두 줄이 되어 SyntaxError 가 난다. **catch 블록의 본문(83-88행)은 한 글자도 바꾸지 않는다** — `ledger.failed(cfg.id)`·`attachUsage`·`onUsage?.`·`throw err` 순서가 회계 계약이다.

```js
    const system = [{ type: 'text', text: promptFile('summary-check') }];
    const messages = [{ role: 'user', content: userText }];

    const ledger = createUsageCollector({ estimate: estimateCost });
    // 창구 만들기는 try 밖이다 (digest.js 와 같은 자리). config 의 provider 오타는 여기서
    // 던지므로 ledger.failed·onUsage 를 안 거치고 나간다 — 옛 코드에 없던 유일한 갈래이고,
    // 시도마다 같은 cfg 라 첫 호출 전에만 날 수 있는 설정 오류다.
    const m = jsonModel(cfg);
    let final;
    try {
      final = await m.generate({ system, messages, schema: SUMMARY_CHECK_SCHEMA });
    } catch (err) {
```

89행 — 원장에 넘기는 것이 회사 원문 응답이 된다:

```js
    ledger.record(final.raw, cfg.id);
```

92행 `logUsage('summary-check', final.model, final.usage);` 는 **그대로**다 (필드명이 같다).

93행·100행 게이트 두 곳만 이름이 바뀐다. **주석·문구·던지는 메시지는 그대로**:

- `if (final.stop_reason === 'refusal') return [];` → `if (final.stopReason === 'refusal') return [];`
- `if (final.stop_reason === 'max_tokens') {` → `if (final.stopReason === 'max_tokens') {`

107-111행 본문 추출을 한 줄로 줄인다 (추출 규칙은 창구가 같게 수행 — Task 2 검사 ⑥이 보증):

```js
    const text = final.text;
```

- [ ] **Step 2: `src/claude.js` 배선**

`createTextModel` import 줄을 바꾼다:

```js
import { createTextModel, createJsonModel } from './llm/provider.js';
```

93행 `textModel` 헬퍼 **바로 뒤**에 한 줄 더한다 (facade 검사가 `ERR_MARK` 이후 코드만 뽑아 평가하므로 이 위치여야 한다):

```js
const jsonModel = (spec) => createJsonModel(spec, { client: anthropic });
```

96행 배선에서 `anthropic` → `jsonModel`:

```js
export const compareSummary = createSummaryChecker({ config, jsonModel, promptFile, logUsage, estimateCost });
```

- [ ] **Step 3: `scripts/check-llm-summary.js` 하네스 확장**

import 를 바꾼다 (23행):

```js
import { createTextModel, createJsonModel } from '../src/llm/provider.js';
```

`DEP_NAMES`(50-54행)에 `'createJsonModel'` 을 더한다:

```js
const DEP_NAMES = [
  'config', 'anthropic', 'promptFile', 'logUsage', 'estimateCost', 'sleep',
  'APIConnectionError', 'console', 'createDigestGenerator', 'createSummaryChecker',
  'createTextModel', 'createJsonModel',
];
```

`baselineDigestGenerator` 함수 **바로 뒤**에 짝을 더한다. 이유는 P0-1 과 같다 — 진입점만 옛 판으로 갈면 옛 배선이 넘기는 `anthropic` 과 지금 모듈이 받는 `jsonModel` 이 어긋나 비교가 성립하지 않는다:

```js
/* 기준판은 그때의 summary-check.js 도 함께 읽어 평가한다 (baselineDigestGenerator 와 같은 이유).
 * 이렇게 해야 「옛 SDK 직통 경로 대 새 창구 경로」의 대조가 되고, 둘을 이어 붙여 돌리면
 * 기준판까지 새 창구를 통과해 증명이 무력해진다. 파일을 쓰지 않는다. */
function baselineSummaryChecker(sha) {
  const text = execFileSync('git', ['-C', ROOT, 'show', sha + ':src/llm/summary-check.js'], {
    encoding: 'utf8', maxBuffer: 1024 * 1024,
  }).replace(/\r\n/g, '\n')
    .replace(/^import [^;]*;$/gm, '')
    .replace(/^export (?=(?:async function|function|const) )/gm, '');
  assert.ok(!/^import\b/m.test(text), '기준판 summary-check.js 의 import 경계를 확인할 수 없습니다');
  const names = ['createUsageCollector', 'attachUsage'];
  const evaluate = compileFunction(text + '\nreturn createSummaryChecker;', names);
  return evaluate(createUsageCollector, attachUsage);
}
```

`facadeFactory`(Task 1 에서 `(text, makeDigest)` 로 줄어든 것)에 인자를 하나 더 받게 한다:

```js
function facadeFactory(text, makeDigest = createDigestGenerator, makeChecker = createSummaryChecker) {
```

주입 map(87-92행)에서 `createSummaryChecker` 를 인자로 바꾸고 `createJsonModel` 을 더한다:

```js
  return (deps) => evaluate(...DEP_NAMES.map((key) => ({
    ...deps, APIConnectionError,
    createDigestGenerator: (options) => makeDigest({ ...options, sleep: deps.sleep }),
    createSummaryChecker: makeChecker,
    createTextModel,
    createJsonModel,
  })[key]));
```

기준판 조립(106행)에 셋째 인자를 더한다:

```js
    baselineFactory = facadeFactory(original, baselineDigestGenerator(baselineSha), baselineSummaryChecker(baselineSha));
```

- [ ] **Step 4: 나머지 호출자 치환**

`grep -rn "createSummaryChecker(" src/ scripts/` 로 전수 확인한다. **계획의 목록을 믿지 말고 직접 센다.** 검증 시점 실측으로는 아래 둘이다.

`scripts/check-usage-accounting.js:171-172` — 18행의 `import { createTextModel } from '../src/llm/provider.js';` 를 `import { createTextModel, createJsonModel } from '../src/llm/provider.js';` 로 바꾸고, 배선을 바꾼다:

```js
    const run = createSummaryChecker({ config, promptFile: () => 'fixture', logUsage() {}, estimateCost,
      jsonModel: (spec) => createJsonModel(spec, { client: () => ({ messages: { create: async () => {
        if (response instanceof Error) throw response; return response;
      } } }) }) });
```

`scripts/check-live-llm.js:93-96` — 12행의 `import { createTextModel } from '../src/llm/provider.js';` 를 `import { createTextModel, createJsonModel } from '../src/llm/provider.js';` 로 바꾸고, 배선을 바꾼다:

```js
  await createSummaryChecker({
    config: cfg, jsonModel: (spec) => createJsonModel(spec, { client: () => capture('summaryCheck') }),
    estimateCost, logUsage() {},
    promptFile: () => 'Compare synthetic summary and transcript. If consistent return {"findings":[]}.',
  })({ channel: 'synthetic', summary: 'The sample task is complete.', transcript: 'The sample task is complete.' });
```

- [ ] **Step 5: 무변화 증명**

Run: `node scripts/check-llm-summary.js --baseline 1bcbceb5d52230a8283a573b9eb8cff256287382 --facade`
Expected: 33개 사례 통과 · 기준판 일치 · 실제 모델 API 호출 0회

**다르면 멈추고 차이를 그대로 보고한다 — 검사를 고쳐 맞추지 않는다.** 특히 `대조 정상·스키마` 사례가 `output_config.format.schema` 를 세세히 대므로(328-334행), 스키마가 요청에 실리는 모양이 달라지면 여기서 걸린다.

- [ ] **Step 6: 기준판이 정말 옛 경로를 도는지 확인한다 (증명의 비공허성)**

**소스를 망가뜨리는 방식으로는 이걸 못 잰다.** 요청 파라미터를 틀리면 `대조 정상·스키마` 의 직접 단언(`scripts/check-llm-summary.js:326` 의 `assert.equal(p.max_tokens, …)`)이 기준판 대조보다 **먼저** 빨개진다 — 즉 기준판이 새 코드를 돌고 있어도 똑같이 빨개지므로 아무것도 증명하지 못한다.

대신 **기준판 배선 자체를 무력화해 본다.** 편집 도구로 고쳤다가 편집 도구로 되돌린다.

1. 기준판 조립 줄(106행 근처)의 셋째 인자를 **지금 모듈**로 바꾼다:

```js
    baselineFactory = facadeFactory(original, baselineDigestGenerator(baselineSha), createSummaryChecker);
```

2. Run: `node scripts/check-llm-summary.js --baseline 1bcbceb5d52230a8283a573b9eb8cff256287382 --facade` → **실패해야 한다.** 기준판 `claude.js` 는 `{ config, anthropic, … }` 로 배선하는데 지금 모듈은 `jsonModel` 을 기대하므로 `jsonModel is not a function` 이 난다. 실패 출력을 기록한다.
3. 원래 줄(`baselineSummaryChecker(baselineSha)`)로 되돌린다.
4. Run: `git diff --stat scripts/check-llm-summary.js` → Step 3 의 변경만 남아 있어야 한다.

**여기서 통과해 버리면** 기준판이 옛 모듈을 안 읽고 있다는 뜻이다 (대조가 새 코드 대 새 코드가 된다) — 멈추고 보고한다.

- [ ] **Step 7: 주변 검사 회귀**

Run: `node scripts/check-llm-provider.js && node scripts/check-usage-accounting.js && node scripts/check-llm-qa.js`
Expected: 전부 OK (`check-llm-qa` 는 P0-3 범위가 안 흔들렸는지 확인용)

- [ ] **Step 8: 전체 검사**

Run: `npm run check`
Expected: 전체 통과. 실패 시 원인 파악 후 수정 (검사 느슨화 금지).

- [ ] **Step 9: 커밋 승인 요청 (직접 커밋 금지)**

보고에 Step 5 의 기준판 대조 결과와 Step 6 의 실패 출력을 포함한다. 승인 시:
`git add src/llm/summary-check.js src/claude.js scripts/check-llm-summary.js scripts/check-usage-accounting.js scripts/check-live-llm.js`

---

## 이 계획이 남기는 것

- `src/llm/qa.js` 는 그대로다 — 도구 왕복 창구는 P0-3. 그때 `claude.js` 에 세 번째 헬퍼가 붙고, `answerQuestion` 배선이 바뀐다.
- `contract()` 가 `usage`·`cost` 이벤트를 거르는 것 자체는 **안 고친다** — 회계는 R1b 에서 의도적으로 바뀌어 기준판과 댈 수 없다. Task 1 이 현재 실행 단언으로 그 자리를 덮으므로, 거르는 설계는 그대로 두는 것이 맞다.
- `effort` 는 Anthropic 어휘다. 타사 매핑이 손실 변환이라는 것은 P0-3 이후 창구 계약 주석에 명시한다 (설계 확정판의 「어댑터 계약의 세부」).

## 후속 계획 (이 계획 범위 밖)

P0-3 QA 도구 왕복 창구(회차 훅·거부 개입·시도 단위 회계) → P0-4 Clio(수동 루프·종결 도구 + 두-저장소 어긋남 검사). 설계 원본은 소유자 워크스페이스의 2026-09-18 「모델교체-준비 설계」 기록 문서다 — 이 저장소에는 경로를 적지 않는다.
