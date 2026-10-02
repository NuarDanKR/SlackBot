# 모델 제공사 어댑터 P0-3 (Q&A · 도구 왕복 창구) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 「도구 왕복」 창구(`createToolSession`)를 어댑터에 더하고, 질문 답변(`src/llm/qa.js`)을 그 창구로 이전한다 — **동작 무변화를 기준판 대조로 증명한 뒤에만**. 세 호출 자리 중 마지막이고 가장 크다.

**Architecture:** 앞의 두 창구는 「한 번 부르고 결과를 받는」 모양이라 `generate()` 하나로 끝났다. Q&A 는 다르다 — 모델이 도구를 부르고, 우리가 실행하고, 그 결과를 붙여 다시 부르는 **왕복**이고, 그 왕복 사이에 우리가 끼어들어 캐시 표시를 옮긴다. 그래서 창구가 함수가 아니라 **세션**이다: `createToolSession(spec, {client}).open(req)` 이 세션을 내고, 세션은 ① 회차를 정규화해 흘려주고(`for await`) ② 도구 실행을 대신해 주고(`generateToolResponse`) ③ 다음 회차 대화를 잇는다(`continueWith`). 회사별 어휘 — `beta.messages.toolRunner`·`betas`·`diagnostics.previous_message_id`·`cache_control` 표시와 그 4개 상한·`msg_` 로 시작하는 id 모양·`stop_reason` — 가 전부 세션 안에서 끝난다. `usage` 는 앞 두 창구와 같이 **회사 원문 그대로** 통과시킨다.

**무변화 증명에 먼저 손이 필요하다.** digest·요약 대조에는 `check-llm-summary.js --baseline <sha>` 라는 대조 하네스가 있었지만 **Q&A 에는 없다.** `check-llm-qa.js` 의 `--baseline-stdin` 은 추출 전 `claude.js` 원본을 손으로 넣어야 하는 2026년 초의 장치라 이 이전에는 못 쓴다. 그래서 Task 1 이 sha 로 기준판을 읽는 대조를 먼저 만든다 — **증명 수단 없이 옮기지 않는다.**

**Tech Stack:** Node ≥20 ESM, `@anthropic-ai/sdk ^0.115.0`, 기존 오프라인 검사 하네스. `check-llm-qa.js` 는 **실물 SDK 의 toolRunner 를 그대로 쓰고 `beta.messages.create` 만 가짜로 바꾼다** — 이 성질을 반드시 유지한다. 도구 왕복의 진짜 동작(도구 실행·is_error 감싸기·반복 상한)이 SDK 안에 있어서, 그걸 가짜로 바꾸면 검사가 아무것도 안 지킨다.

## Global Constraints

- **커밋·push 는 각각 WHK 승인 후에만** (저장소 CLAUDE.md). 서브에이전트는 커밋하지 않는다 — 고치고·검사하고·보고만. `git commit`·`git push`·`git add`·`git stash`·`git checkout`·`git restore` 를 쓰지 않는다. 읽기 전용 git 만 쓴다.
- 검사 스크립트는 실제 네트워크·파일·Slack 에 닿지 않는다. `check-llm-qa.js` 는 `globalThis.fetch` 를 막은 채로 돈다 — 그 장치를 없애지 않는다.
- 사업장·비공개 채널 이름을 코드·주석에 쓰지 않는다.
- 수정 후 `npm run check` 전체 통과가 완료 조건.
- 이 계획은 **Q&A 자리만** 이전한다. `src/llm/digest.js`·`src/llm/summary-check.js` 는 건드리지 않는다 (P0-1·P0-2 에서 끝났다).
- 요청 바이트·로그 문구·캐시 표시 위치·게이트 동작·반환 필드가 수정 전과 동일해야 한다. 다른 점이 하나라도 발견되면 이유를 적고 멈춘다 — **검사를 고쳐 맞추지 않는다.**
- **`--baseline-stdin` 을 없애지 않는다.** 이 저장소의 다른 검사 여덟 곳이 같은 이름으로 같은 일을 하는 관례이고, 이 계획은 sha 로 읽는 갈래를 **더한다**.
- `<BASELINE_SHA>` 는 `a8c9a9b11d13930913714d8ec943c3b0fa310937` (P0-2 마지막 커밋). 이 커밋의 `src/llm/qa.js` 가 이전 전 원본이다.

---

### Task 1: Q&A 기준판 대조를 만든다 (검사만 고친다 — `src/` 무변화)

증명 수단을 먼저 세운다. 이 Task 가 끝나면 「지금 `qa.js` 와 sha 의 `qa.js` 가 같은 입력에 같게 움직이나」를 명령 한 줄로 잴 수 있다.

**Files:**
- Modify: `scripts/check-llm-qa.js`

**Interfaces:**
- Consumes: 없음
- Produces: `node scripts/check-llm-qa.js --baseline <40자 sha>` — 시나리오마다 현재 판과 기준판의 trace 를 대조한다. Task 3 이 이것으로 무변화를 증명한다.

- [x] **Step 1: 기준판 로더를 더한다**

`scripts/check-llm-qa.js` 의 import 에 두 줄을 더한다 (`fs`·`createHash`·`compileFunction` 은 이미 있다):

```js
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
```

`const clone = value => ...` 줄 **앞**에 상수를 하나 둔다:

```js
const ROOT = fileURLToPath(new URL('../', import.meta.url));
```

기존 `baselineFactory(source)` 함수 **바로 뒤**에 sha 갈래를 더한다. `--baseline-stdin` 은 그대로 둔다 — 저 갈래는 추출 전 `claude.js` 를 손으로 넣는 옛 장치이고, 이쪽은 `src/llm/qa.js` 를 git 에서 읽는다:

```js
/* sha 의 src/llm/qa.js 를 그때 모양 그대로 세운다. 진입점만 옛 판으로 갈면 옛 배선이
 * 넘기는 의존성 이름과 지금 모듈이 받는 이름이 어긋나 비교 자체가 성립하지 않고, 그 실패가
 * 「계약이 달라졌다」로 잘못 읽힌다 (digest·요약 대조에서 같은 함정을 두 번 겪었다).
 * import 는 떼어 주입한다 — 파일을 쓰지 않는다. */
function baselineQuestionAnswerer(sha) {
  const text = execFileSync('git', ['-C', ROOT, 'show', sha + ':src/llm/qa.js'], {
    encoding: 'utf8', maxBuffer: 1024 * 1024,
  }).replace(/\r\n/g, '\n')
    .replace(/^import [^;]*;$/gm, '')
    .replace(/^export (?=(?:async function|function|const) )/gm, '');
  assert.ok(!/^import\b/m.test(text), '기준판 qa.js 의 import 경계를 확인할 수 없습니다');
  const names = ['createUsageCollector', 'attachUsage'];
  const evaluate = compileFunction(text + '\nreturn createQuestionAnswerer;', names);
  return evaluate(createUsageCollector, attachUsage);
}
```

- [x] **Step 2: `--baseline <sha>` 를 받는다**

`try {` 블록 맨 앞의 `let baseline;` 아래, `if (process.argv.includes('--baseline-stdin')) { … }` 블록 **뒤**에 더한다:

```js
  const shaAt = process.argv.indexOf('--baseline');
  if (shaAt >= 0) {
    const sha = process.argv[shaAt + 1] || '';
    assert.ok(/^[0-9a-f]{40}$/.test(sha), '사용법: node scripts/check-llm-qa.js --baseline <40자 sha>');
    assert.ok(!baseline, '--baseline 과 --baseline-stdin 을 함께 쓰지 않습니다');
    baseline = baselineQuestionAnswerer(sha);
  }
```

- [x] **Step 3: `skipBaseline` 이 sha 갈래에는 안 걸리게 한다**

지금 `skipBaseline: true` 가 붙은 시나리오는 하나(`real wrapper pairs sizes/narrows by tool_use id …`)이고, 그 이유는 주석에 적힌 대로 **추출 전 `claude.js` 기준판이 순서 짝 시절 코드**라서다. sha 기준판(`a8c9a9b`)은 이미 id 짝 코드이므로 그 시나리오도 대조해야 한다 — **가장 촘촘한 시나리오를 대조에서 빼면 대조의 값어치가 절반이 된다.**

`skipBaseline` 을 보는 줄을 갈래별로 나눈다. 기존:

```js
    if (baseline && !scenario.skipBaseline) assert.deepEqual(actual, await replay(baseline, scenario), scenario.name + ': pre-extraction trace');
```

을 다음으로 바꾼다:

```js
    // skipBaseline 은 「추출 전 claude.js」 기준판에만 해당한다 (그 판은 순서 짝 시절 코드다).
    // sha 기준판은 그 이후 코드라 모든 시나리오를 대조한다.
    if (baseline && !(baselineIsStdin && scenario.skipBaseline)) {
      assert.deepEqual(actual, await replay(baseline, scenario),
        scenario.name + (baselineIsStdin ? ': pre-extraction trace' : ': 기준판 trace'));
    }
```

그러려면 `let baseline;` 옆에 갈래를 기억하는 변수가 필요하다. `let baseline;` 을 다음으로 바꾼다:

```js
  let baseline;
  let baselineIsStdin = false;
```

그리고 `--baseline-stdin` 블록 안, `baseline = baselineFactory(source);` 바로 뒤에 한 줄:

```js
    baselineIsStdin = true;
```

- [x] **Step 4: 현재 판이 그대로 통과하는지 확인**

Run: `node scripts/check-llm-qa.js`
Expected: `14 Q&A contracts + factory isolation passed; live model calls 0.`

> 실제: 지금은 `15 Q&A contracts` 다. Task 3 에서 시나리오 하나가 늘었다 — ⑤번 항목 참조.

- [x] **Step 5: sha 기준판 대조가 도는지 확인**

Run: `node scripts/check-llm-qa.js --baseline a8c9a9b11d13930913714d8ec943c3b0fa310937`
Expected: 같은 통과 줄. 지금은 현재 판과 기준판이 **같은 코드**이므로 당연히 맞아야 한다 — 여기서 다르면 로더가 잘못 세운 것이니 멈추고 보고한다.

- [x] **Step 6: 대조가 공허하지 않은지 확인한다 (비공허성)**

**대조를 만들어 놓고 빨개지는 것을 안 본 채 넘어가면 이 Task 는 아무 일도 안 한 것이다.**

**고를 때 조심할 것이 있다.** 시나리오는 자기 `check()` 를 **기준판 대조보다 먼저** 돌린다.
그래서 캐시 표시나 요청 파라미터처럼 `check()` 가 직접 단언하는 값을 망가뜨리면,
**기준판이 현재 코드를 돌고 있어도 똑같이 빨개진다** — 아무것도 구별하지 못한다
(P0-2 계획에서 같은 실수를 한 번 했다). 그러니 **`check()` 는 안 보고 trace 대조만 보는 값**을
골라야 한다. 로그 문구가 그런 값이다 — `trace.logs` 는 어느 시나리오도 단언하지 않는다.

편집 도구로 고쳤다가 편집 도구로 되돌린다 — git 명령을 쓰지 않는다.

1. `src/llm/qa.js` 의 `console.log(\`  -> ${b.name}(...)\`)` 에서 앞머리 `  -> ` 를 `  => ` 로 바꾼다.
2. Run: `node scripts/check-llm-qa.js --baseline a8c9a9b11d13930913714d8ec943c3b0fa310937` → **실패해야 한다** (기준판 trace 의 logs 불일치). 실패 출력을 기록한다.
3. Run: `node scripts/check-llm-qa.js` (기준판 없이) → **통과해야 한다.** 이 둘이 갈리는 것이 「대조가 일을 한다」는 증거다. 갈리지 않으면 고른 값이 잘못된 것이니 다른 값으로 다시 한다.
4. 원래 줄로 되돌린다.
5. Run: `git diff --stat src/` → **아무것도 안 나와야 한다.**

2번이 통과해 버리면 기준판이 현재 코드를 돌고 있다는 뜻이다 — 멈추고 보고한다.

- [x] **Step 7: 전체 검사**

Run: `npm run check`
Expected: 전체 통과

- [x] **Step 8: 커밋 승인 요청 (직접 커밋 금지)**

보고에 Step 5·6 의 출력과 `git diff --stat src/` 가 비었다는 사실을 포함한다.

---

### Task 2: 도구 왕복 창구 `createToolSession`

**Files:**
- Modify: `src/llm/provider.js`
- Modify: `scripts/check-llm-provider.js` (⑩~⑭ 사례 추가)

**Interfaces:**
- Consumes: `src/llm/provider.js` 안의 `normalize`·`anthropicRetryable`·`PROVIDERS`/`createModel` (P0-1·P0-2 산물)
- Produces: `createToolSession(spec, { client }) → { provider, id, retryable, open }`
  - `open({ system, messages, tools, maxIterations, previousMessageId }) → session`
  - `session[Symbol.asyncIterator]()` — 회차를 흘린다. 회차 모양:
    `{ text, stopReason, model, usage, raw, messageId, toolUses: [{id,name,input}], cacheMiss: {type,tokens}|null }`
    - `stopReason` 정규화는 앞 두 창구와 같은 표. `tool_use` 는 표에 없으므로 원문 그대로 지나간다 — Q&A 의 도구 상한 판정이 그 값을 본다
    - `messageId` 는 **다음 요청의 대조 기준으로 쓸 수 있는 값일 때만** 문자열이고 아니면 `null`. 모양 판정(`msg_` 접두)이 회사별이라 창구가 한다
  - `session.generateToolResponse() → Promise<toolMessage|null>` — 도구 실행은 회사 도구 실행기에 맡긴다. 반환은 회사 원문(다음 회차에 그대로 실린다)
  - `session.continueWith(toolMessage, assistantTurn) → void` — 다음 회차 대화를 잇는다. 캐시 표시 옮기기와 대조 기준 넘기기가 **여기 안에서 끝난다**

- [x] **Step 1: 실패하는 검사를 먼저 쓴다** — `scripts/check-llm-provider.js` 확장

import 를 바꾼다:

```js
import { createTextModel, createJsonModel, createToolSession } from '../src/llm/provider.js';
```

파일 맨 끝의 `console.log('[check-llm-provider] OK — …');` **앞**에 통째로 넣는다:

```js
// ── 도구 왕복 창구 (P0-3) ───────────────────────────────────────────
// 앞의 두 창구와 달리 세션이다. 여기서는 ① 요청 조립 ② 회차 정규화 ③ 대화 잇기(캐시 표시·
// 대조 기준)를 못박는다. 도구 실행 자체는 회사 실행기의 일이라 가짜 실행기로 「그대로
// 넘긴다」만 확인한다 — 실제 실행은 scripts/check-llm-qa.js 가 실물 SDK 로 잰다.

const TOOLS = [{ name: 'search', description: '검사용', input_schema: { type: 'object' } }];
const REQ = { system: SYSTEM, messages: MESSAGES, tools: TOOLS, maxIterations: 4, previousMessageId: null };

// toolRunner 를 흉내 내는 가짜. 회차 목록을 주면 순서대로 흘린다.
function fakeRunnerClient(turns, { toolMessage = null } = {}) {
  const runs = [];
  const state = { params: null };
  const client = () => ({ beta: { messages: { toolRunner(params) {
    runs.push(clone(params));
    state.params = params;
    let i = 0;
    return {
      [Symbol.asyncIterator]() {
        return { next: async () => (i < turns.length
          ? { done: false, value: clone(turns[i++]) }
          : { done: true, value: undefined }) };
      },
      generateToolResponse: async () => (toolMessage ? clone(toolMessage) : null),
      setMessagesParams(fn) { state.params = fn(state.params); runs.push(clone(state.params)); },
    };
  } } } });
  return { client, runs, state };
}

const turn = (id, stop = 'end_turn', content = [{ type: 'text', text: '답' }]) => ({
  id, role: 'assistant', stop_reason: stop, content,
  usage: { input_tokens: 2, output_tokens: 1 },
});

// ⑩ 요청 조립 — 회사 어휘로 정확히 이 모양이어야 한다 (Q&A 기준판 대조가 이 모양을 본다).
{
  const { client, runs } = fakeRunnerClient([turn('msg_a')]);
  const s = createToolSession(SPEC, { client }).open(REQ);
  for await (const _ of s) { /* 소비만 한다 */ }
  assert.deepEqual(runs[0], {
    model: 'model-fixture', max_tokens: 1600, output_config: { effort: 'high' },
    system: SYSTEM, messages: MESSAGES, tools: TOOLS, max_iterations: 4,
    betas: ['cache-diagnosis-2026-04-07'], diagnostics: { previous_message_id: null },
  });
}

// ⑪ 회차 정규화 — 글 창구와 같은 본문 추출, 같은 중단 사유 표. tool_use 는 표에 없어 원문 유지.
{
  const { client } = fakeRunnerClient([turn('msg_b', 'tool_use', [
    { type: 'text', text: ' 앞 ' },
    { type: 'tool_use', id: 'toolu_1', name: 'search', input: { q: 1 } },
    { type: 'text', text: '뒤' },
  ])]);
  const got = [];
  for await (const t of createToolSession(SPEC, { client }).open(REQ)) got.push(t);
  assert.equal(got.length, 1);
  assert.equal(got[0].text, '앞 \n뒤');
  assert.equal(got[0].stopReason, 'tool_use');
  assert.equal(got[0].messageId, 'msg_b');
  assert.deepEqual(got[0].toolUses, [{ id: 'toolu_1', name: 'search', input: { q: 1 } }]);
  assert.deepEqual(got[0].usage, { input_tokens: 2, output_tokens: 1 });
  assert.equal(got[0].raw.stop_reason, 'tool_use');
  assert.equal(got[0].cacheMiss, null);
}

// ⑫ (실제로는 이 반대로 갔다 — 아래 「계획과 다르게 간 곳」 ①③ 참조)
// ⑫ 대조 기준으로 쓸 수 있는 id 만 messageId 로 나온다 — 모양이 틀린 값은 지어내지 않는다.
for (const [given, want] of [['msg_ok', 'msg_ok'], ['not-a-message', null], [undefined, null]]) {
  const { client } = fakeRunnerClient([{ ...turn('x'), id: given }]);
  const got = [];
  for await (const t of createToolSession(SPEC, { client }).open(REQ)) got.push(t);
  assert.equal(got[0].messageId, want);
}

// ⑬ 캐시 미스 사유 — 회사가 준 자리에서 꺼내 온다. 없으면 null.
{
  const { client } = fakeRunnerClient([{ ...turn('msg_c'),
    diagnostics: { cache_miss_reason: { type: 'system_changed', cache_missed_input_tokens: 9 } } }]);
  const got = [];
  for await (const t of createToolSession(SPEC, { client }).open(REQ)) got.push(t);
  assert.deepEqual(got[0].cacheMiss, { type: 'system_changed', tokens: 9 });
}

// ⑭ 대화 잇기 — 캐시 표시를 끝으로 옮기고, 앞 표시는 가장 최근 하나만 남기며,
//    다음 회차의 대조 기준이 방금 회차의 id 가 된다. 표시 상한이 4개라 쌓으면 400 이 난다.
{
  const toolMessage = { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'toolu_1', content: '결과' }] };
  const { client, runs } = fakeRunnerClient(
    [turn('msg_d', 'tool_use', [{ type: 'tool_use', id: 'toolu_1', name: 'search', input: {} }])],
    { toolMessage });
  const s = createToolSession(SPEC, { client }).open(REQ);
  for await (const t of s) {
    const tm = await s.generateToolResponse();
    assert.ok(tm, '가짜 실행기가 결과를 준다');
    s.continueWith(tm, t);
  }
  const next = runs.at(-1);
  // 이어 붙인 모양: 원래 대화 + 방금 assistant 회차 + 표시 붙은 도구 결과
  assert.equal(next.messages.length, MESSAGES.length + 2);
  assert.deepEqual(next.messages.at(-1).content.at(-1).cache_control, { type: 'ephemeral' });
  assert.equal(next.messages.at(-1).content.at(-1).content, '결과');
  assert.deepEqual(next.diagnostics, { previous_message_id: 'msg_d' });
  // 표시는 이번 것 하나뿐 (MESSAGES 에는 표시가 없다)
  const marks = next.messages
    .flatMap((m) => (Array.isArray(m.content) ? m.content : []))
    .filter((b) => b.cache_control).length;
  assert.equal(marks, 1);
}

// ⑮ 모르는 provider 는 세션 창구에서도 던진다.
assert.throws(() => createToolSession({ ...SPEC, provider: 'acme' }, { client: () => ({}) }), /acme/);
assert.equal(createToolSession({ ...SPEC, provider: 'anthropic' }, { client: () => ({}) }).provider, 'anthropic');
```

마지막 줄의 문구도 바꾼다:

```js
console.log('[check-llm-provider] OK — 글 창구 + JSON 창구 + 도구 왕복 창구(요청 조립·회차 정규화·id 모양·캐시 미스·대화 잇기)');
```

- [x] **Step 2: 실패 확인**

Run: `node scripts/check-llm-provider.js`
Expected: FAIL — `createToolSession is not a function` (또는 import 관련 `SyntaxError`)

- [x] **Step 3: 구현** — `src/llm/provider.js`

계약 주석 블록의 첫 줄을 바꾸고 세션 줄을 더한다:

```js
 * 계약 (세 창구 — 글 반환 · JSON 반환 · 도구 왕복):
```

그리고 그 블록 끝(`client 는 지연 생성 함수로…` 줄 **앞**)에 더한다:

```js
 *   createToolSession(spec, { client }) → { provider, id, retryable, open }
 *     open(req) → 세션. 세션은 회차를 흘리고(for await), 도구 실행을 대신하고,
 *     다음 회차 대화를 잇는다. 캐시 표시와 대조 기준(previous_message_id)은
 *     **세션 안에서 끝난다** — 둘 다 회사별 어휘이고 상한도 회사별이다.
```

`PROVIDERS` 표 **앞**에 세션 구현을 넣는다:

```js
/* ── 도구 왕복의 캐시 표시 ──────────────────────────────────────────
 *
 * 툴 루프는 회차마다 대화 전체를 다시 보낸다. 표시를 대화 맨 끝으로 옮기면 이미 보낸
 * 부분이 다음 회차에 읽기(1/10 단가)가 되고 새로 늘어난 것만 정가로 나간다. 답변 내용은
 * 바뀌지 않는다 — 같은 것을 같은 순서로 보내고 어디까지 캐시할지만 알려주는 것이다.
 *
 * TTL 을 5분으로 두는 이유: 툴 루프는 몇 초~1분 안에 끝난다. 1시간짜리는 쓰기가 2배라
 * 여기서는 손해다. 시스템 블록의 1시간은 호출 자리가 건다 — 그쪽은 질문과 질문 사이에
 * 재사용돼야 해서 계산이 반대다. */
const TOOL_CACHE_CONTROL = { type: 'ephemeral' }; // ttl 생략 = 5분

function withCacheTail(message) {
  const content = message.content;
  if (!Array.isArray(content) || !content.length) return message;
  return {
    ...message,
    content: content.map((b, i) =>
      i === content.length - 1 ? { ...b, cache_control: TOOL_CACHE_CONTROL } : b,
    ),
  };
}

function hasCacheMark(m) {
  return Array.isArray(m?.content) && m.content.some((b) => b?.cache_control);
}

function withoutCacheMarks(message) {
  return { ...message, content: message.content.map(({ cache_control, ...b }) => b) };
}

/** 앞선 표시는 **가장 최근 것 하나만** 남긴다. 표시는 한 요청에 4개가 상한이라 계속 쌓으면
 * API 가 400 을 낸다. 시스템 1개 + 대화 2개 = 3개로 여유를 둔다. 표시를 떼어도 글자는
 * 그대로라 이미 저장된 앞부분은 계속 맞는다 (캐시는 가장 긴 일치 지점을 알아서 찾는다). */
function rollCacheMarks(messages) {
  const out = [...messages];
  let seen = 0;
  for (let i = out.length - 1; i >= 0; i--) {
    if (!hasCacheMark(out[i])) continue;
    seen += 1;
    if (seen > 1) out[i] = withoutCacheMarks(out[i]);
  }
  return out;
}

/** 다음 요청의 대조 기준으로 쓸 수 있는 값인가. 모양이 틀린 값(빈 문자열 포함)은 400 이고,
 * 모양이 맞는데 없는 id 는 정상 응답에 previous_message_not_found 로 돌아온다(실측).
 * 그래서 낡아서 못 찾는 것은 안전하지만 지어낸 값은 안전하지 않다. */
function anthropicMessageId(id) {
  return typeof id === 'string' && id.startsWith('msg_') ? id : null;
}

function anthropicToolSession(spec, client) {
  return {
    provider: 'anthropic',
    id: spec.id,
    retryable: anthropicRetryable,
    open({ system, messages, tools, maxIterations, previousMessageId }) {
      const runner = client().beta.messages.toolRunner({
        model: spec.id,
        max_tokens: spec.maxTokens,
        output_config: { effort: spec.effort },
        system,
        messages,
        tools,
        max_iterations: maxIterations,
        // 캐시가 **왜** 안 맞았는지를 응답에 적어 달라는 요청이다 (diagnostics.cache_miss_reason).
        // betas 를 빼면 400 "Extra inputs are not permitted" 이 난다 — 베타 기능이라 플래그가 필요하다.
        betas: ['cache-diagnosis-2026-04-07'],
        diagnostics: { previous_message_id: anthropicMessageId(previousMessageId) },
      });
      return {
        [Symbol.asyncIterator]() {
          const inner = runner[Symbol.asyncIterator]();
          return {
            async next() {
              const step = await inner.next();
              if (step.done) return step;
              const raw = step.value;
              const miss = raw.diagnostics?.cache_miss_reason;
              return { done: false, value: {
                ...normalize(raw),
                messageId: anthropicMessageId(raw.id),
                toolUses: (raw.content || [])
                  .filter((b) => b.type === 'tool_use')
                  .map((b) => ({ id: b.id, name: b.name, input: b.input })),
                cacheMiss: miss ? { type: miss.type, tokens: miss.cache_missed_input_tokens || 0 } : null,
              } };
            },
            return: (v) => inner.return?.(v) ?? Promise.resolve({ done: true, value: v }),
          };
        },
        generateToolResponse: () => runner.generateToolResponse(),
        continueWith(toolMessage, assistantTurn) {
          const raw = assistantTurn.raw;
          runner.setMessagesParams((p) => ({
            ...p,
            messages: [
              ...rollCacheMarks(p.messages),
              { role: raw.role, content: raw.content },
              withCacheTail(toolMessage),
            ],
            // 다음 회차가 이 응답과 대조되게 한다.
            diagnostics: { previous_message_id: raw.id ?? null },
          }));
        },
      };
    },
  };
}
```

`PROVIDERS` 표에 세 번째 창구를 더한다:

```js
const PROVIDERS = { anthropic: { text: anthropicTextModel, json: anthropicJsonModel, tool: anthropicToolSession } };
```

파일 끝에 export 를 더한다:

```js
export function createToolSession(spec, { client }) {
  return createModel('tool', spec, client);
}
```

- [x] **Step 4: 통과 확인**

Run: `node scripts/check-llm-provider.js`
Expected: `[check-llm-provider] OK — 글 창구 + JSON 창구 + 도구 왕복 창구…`

앞선 사례 ①~⑨ 가 **그대로 통과해야 한다** — 이 Task 는 기존 두 창구를 건드리지 않는다.

- [x] **Step 5: 앞 두 자리가 안 흔들렸는지 확인**

Run: `node scripts/check-llm-summary.js --baseline 1bcbceb5d52230a8283a573b9eb8cff256287382 --facade`
Expected: 통과 · 기준판 일치

Run: `node scripts/check-llm-qa.js --baseline a8c9a9b11d13930913714d8ec943c3b0fa310937`
Expected: 통과 (아직 `qa.js` 는 창구를 안 쓴다 — 여기서 빨개지면 `provider.js` 를 잘못 건드린 것이다)

- [x] **Step 6: 전체 검사**

Run: `npm run check`
Expected: 전체 통과. 카탈로그는 안 바꾼다 — 새 검사 파일을 만들지 않았다.

- [x] **Step 7: 커밋 승인 요청 (직접 커밋 금지)**

---

### Task 3: Q&A 를 도구 왕복 창구로 이전 + 기준판 무변화 증명

**Files:**
- Modify: `src/llm/qa.js`
- Modify: `src/claude.js`
- Modify: `scripts/check-llm-qa.js` (기준판 조립에 창구 주입)
- Modify: `scripts/check-usage-accounting.js` (세 번째 호출자 — Step 7.5)

**Interfaces:**
- Consumes: Task 2 의 `createToolSession`
- Produces: `createQuestionAnswerer({ config, toolSession, buildTools, systemBlocks, lastSyncedAt, privateQuoteLine, BOT_ANSWER_MARK, logUsage, now, console })` — `anthropic` 의존이 `toolSession: (spec) => 창구` 로 바뀐다. `answerQuestion` 의 인자·반환 모양은 **불변**.

- [x] **Step 1: `src/llm/qa.js` 에서 창구로 옮긴 것을 지운다**

지울 것 — 전부 Task 2 가 창구 안에 같은 코드로 갖고 있다:
- `TOOL_CACHE_CONTROL` 상수와 `withCacheTail`·`hasCacheMark`·`withoutCacheMarks`·`rollCacheMarks` 네 함수, 그리고 그 위의 「툴 루프의 캐시 표시」 주석 블록 전체
- `previousMessageId` 함수 (모양 판정이 창구로 갔다). **`lastMessageId` 변수와 그 위 주석 블록은 남긴다** — 질문과 질문 사이를 잇는 것은 이 팩토리의 일이다

- [x] **Step 2: 팩토리 시그니처와 요청 조립**

시그니처(8-11행)에서 `anthropic` 을 `toolSession` 으로 바꾼다:

```js
export function createQuestionAnswerer({
  config, toolSession, buildTools, systemBlocks, lastSyncedAt, privateQuoteLine,
  BOT_ANSWER_MARK, logUsage, now = () => new Date(), console = globalThis.console,
}) {
```

`const baseline = previousMessageId();` 을 다음으로 바꾼다 (모양 판정은 창구가 한다):

```js
    const baseline = lastMessageId;
```

`const params = { … };` 블록 전체를 요청 객체로 바꾼다. **`system` 조립과 그 위 주석은 그대로 둔다** — 시스템 블록의 1시간 캐시는 이 자리의 판단이다:

```js
    const req = {
      system: [
        {
          type: 'text',
          text: sys.common,
          cache_control: { type: 'ephemeral', ttl: '1h' },
        },
        ...(sys.extra ? [{ type: 'text', text: sys.extra }] : []),
      ],
      messages: [{ role: 'user', content: userText }],
      tools,
      maxIterations: config.limits.maxToolIterations,
      // 대조 기준은 **앞 질문의 마지막 응답**이다 — 색인을 다시 쓰는 것이 첫 회차라, 여기에
      // null 을 주면 정작 알고 싶은 자리에 사유가 안 붙는다 (lastMessageId 주석).
      // 쓸 수 있는 모양인지는 창구가 판정한다.
      previousMessageId: baseline,
    };
```

`const baseline = …` 은 `req` 조립보다 **앞**이어야 한다. 지금 순서(`sys` → `baseline` → 요청)를 그대로 유지한다.

> 실제로는 이 순서를 못 지켰다 — 아래 「계획과 다르게 간 곳」 ②. 모양 판정이 창구 안으로
> 들어가면서 기준값을 **창구에서 되읽어야** 했고, 그래서 `baseline` 이 `req`·`session` 뒤로
> 내려갔다 (`src/llm/qa.js:214`).

`cacheBaseline: Boolean(baseline)` 이 두 군데 있는데 **손대지 않는다.** 겉보기에는 뜻이 바뀌는 것처럼 보이지만(전에는 「모양까지 맞는 값이 있었나」, 이제는 「앞 질문 id 를 들고 있었나」) 관측되는 값은 같다 — **모양 판정이 읽을 때에서 쓸 때로 옮겨 갔을 뿐**이기 때문이다. `lastMessageId` 에는 이제 `turn.messageId`(창구가 모양을 판정해 통과시킨 값)만 들어가므로, 모양이 틀린 id 는 애초에 저장되지 않는다. 기준판 대조의 `invalid message ID is not reused` 시나리오가 이 자리를 정확히 재고, **초록이어야 맞다.** 빨개지면 창구의 모양 판정이 옛 것과 다르다는 뜻이니 멈추고 보고한다.

- [x] **Step 3: 루프를 세션으로 바꾼다**

`const runner = anthropic().beta.messages.toolRunner(params);` 부터 `}` (setMessagesParams 블록 끝)까지를 다음으로 바꾼다. **`observedMessages` 는 그대로 쓴다** — 회차를 못 받은 것만 「관측 못 한 시도」로 세는 판정은 이 자리의 회계다:

```js
    const session = toolSession({ ...config.models.qa }).open(req);
    for await (const turn of observedMessages(session, activeModel)) {
      final = turn;
      addUsage(usage, turn.usage);
      ledger.record(turn.raw, activeModel);
      // 다음 **질문**이 이 응답과 대조되게 한다 (같은 질문 안의 회차 대조는 창구가 한다).
      if (turn.messageId) lastMessageId = turn.messageId;
      for (const u of turn.toolUses) {
        // id 는 크기·좁힘 짝짓기용이다 (attachSizes·attachNarrows) — 마지막 단에서 뗀다.
        toolCalls.push({ id: u.id, name: u.name, input: u.input });
        console.log(`  -> ${u.name}(${JSON.stringify(u.input).slice(0, 120)})`);
      }
      /* 캐시가 **왜** 안 맞았는지를 모아 둔다. 예전에는 찍고 버려서 VM 콘솔에만 남았고,
       * 그래서 「1시간 만료 때문인가 · 권한 조합 때문인가」를 며칠 뒤 로그를 뒤져야 알 수
       * 있었다. 대화 로그에 실으면 그 판정이 그냥 보인다 (convo-log.js). */
      if (turn.cacheMiss) {
        cacheMiss.push(turn.cacheMiss);
        console.log(`  · 캐시 미스: ${turn.cacheMiss.type}${turn.cacheMiss.tokens ? ` (${turn.cacheMiss.tokens} 토큰)` : ''}`);
      }

      /* 대화를 잇는 일을 창구가 가져가면 runner 는 자기가 assistant 메시지를 붙이지 않고
       * 거절 처리도 건너뛴다. 그래서 거절은 그 전에 우리가 끊는다. */
      if (turn.stopReason === 'refusal') break;

      /* 도구 실행은 창구에게 그대로 맡긴다 — 스키마 검증도 에러 감싸기도 그쪽 것이다. */
      const toolMessage = await session.generateToolResponse();
      if (!toolMessage) break; // 도구를 안 불렀다 = 이 답이 최종이다

      session.continueWith(toolMessage, turn);
    }
```

`cacheMiss.push(turn.cacheMiss)` 는 `{type, tokens}` 를 담는다 — `summarizeCacheMiss` 가 그 두 칸을 읽으므로 모양이 같다.

- [x] **Step 4: 거절 재시도(fallback)**

`if (final?.stop_reason === 'refusal') {` 블록을 다음으로 바꾼다. 문구·순서·정산 위치는 그대로다:

```js
    // 안전 분류기가 거절한 경우(내부 재무 질의에서는 드물다) 한 번만 다른 모델로 재시도.
    if (final?.stopReason === 'refusal') {
      // 모델이 바뀌면 단가도 달라지므로 1차 시도분을 먼저 정산해 둔다.
      logUsage('qa', final?.model || config.models.qa.id, usage);
      console.warn('  ! refusal — fallback 모델로 재시도');
      activeModel = config.models.fallback;
      const rerun = toolSession({ ...config.models.qa, id: config.models.fallback }).open(req);
      usage = emptyUsage();
      for await (const turn of observedMessages(rerun, activeModel)) {
        final = turn;
        addUsage(usage, turn.usage);
        ledger.record(turn.raw, activeModel);
        if (turn.messageId) lastMessageId = turn.messageId;
        for (const u of turn.toolUses) toolCalls.push({ id: u.id, name: u.name, input: u.input });
        if (turn.cacheMiss) cacheMiss.push(turn.cacheMiss);
      }
      if (final?.stopReason === 'refusal') {
```

**재시도 세션은 도구를 실행하지 않고 대화를 잇지도 않는다** — 옛 코드도 그랬다(그 `for await` 안에는 `generateToolResponse` 가 없다). 그대로 둔다.

블록 안쪽의 `const model = final?.model || config.models.fallback;` 부터 `return {…}` 까지는 **그대로**다.

- [x] **Step 5: 남은 `stop_reason` 세 곳**

- `const model = final?.model || config.models.qa.id;` — 그대로
- 본문 추출 5줄(`const text = (final?.content || [])…trim();`)을 한 줄로: `const text = final?.text ?? '';`
- `const truncated = final?.stop_reason === 'max_tokens';` → `final?.stopReason === 'max_tokens'`
- `const toolLimit = !truncated && final?.stop_reason === 'tool_use';` → `final?.stopReason === 'tool_use'`

확인: `grep -n "stop_reason\|anthropic\|beta\." src/llm/qa.js` 를 돌리면 **주석 두 줄만** 남아야 한다 — 도구 상한 주석 블록이 SDK 안에서 일어나는 일(`stop_reason === 'tool_use'` 로 빠져나오는 길이 상한 하나뿐이라는 것)을 설명하는 자리다. 그건 여전히 맞는 설명이라 **그대로 둔다.** 실행되는 코드에는 한 글자도 안 남아야 한다.

- [x] **Step 6: `src/claude.js` 배선**

12행 import 를 바꾼다:

```js
import { createTextModel, createJsonModel, createToolSession } from './llm/provider.js';
```

헬퍼를 **`const anthropic = () => (_client ??= new Anthropic());`(36행) 바로 뒤**에 둔다:

```js
// 도구 왕복 창구 — Q&A 가 쓴다. answerQuestion 배선(56행)보다 **위**여야 한다.
const toolSession = (spec) => createToolSession(spec, { client: anthropic });
```

**`textModel`·`jsonModel` 옆(93-94행)에 두면 안 된다.** 그 둘은 배선이 96-99행이라 뒤에 있어도
되지만, `answerQuestion` 배선은 56행이다. 뒤에 두면 `const` 의 TDZ 에 걸려
`ReferenceError: Cannot access 'toolSession' before initialization` 가 **import 하는 순간**
난다 — `check-llm-qa.js --facade`·`check-tool-limit.js`·`npm run ask`·봇 본체가 전부 죽는다
(계획 검증에서 재현됨).

`answerQuestion` 배선에서 `anthropic` → `toolSession`:

```js
export const answerQuestion = createQuestionAnswerer({
  config, toolSession, buildTools, systemBlocks, lastSyncedAt, privateQuoteLine,
  BOT_ANSWER_MARK, logUsage,
});
```

- [x] **Step 7: `scripts/check-llm-qa.js` 하네스 배선**

import 에 더한다:

```js
import { createToolSession } from '../src/llm/provider.js';
```

`replay()` 안의 `const answer = factory({ config, anthropic: () => client, …})` 에서 **두 이름을 다 넘긴다** — 현재 판은 `toolSession` 을, 기준판은 `anthropic` 을 받는다:

```js
  const answer = factory({
    config, anthropic: () => client,
    toolSession: (spec) => createToolSession(spec, { client: () => client }),
    now: () => new Date(fixedTime),
```

기준판 로더(Task 1 의 `baselineQuestionAnswerer`)는 바꿀 것이 없다 — 옛 `qa.js` 는 `anthropic` 을 받고 위에서 계속 넘겨 준다.

- [x] **Step 7.5: 세 번째 호출자 `scripts/check-usage-accounting.js`**

`grep -rn "createQuestionAnswerer(" src/ scripts/` 를 **직접 돌려** 전수 확인한다. 호출 자리는
`claude.js`·`check-llm-qa.js` 둘이 아니라 **셋**이다 — `scripts/check-usage-accounting.js` 의
`qa()`(107행)가 `anthropic: () => client` 만 넘긴다. 안 고치면 아래 Step 10·12 가
`TypeError: toolSession is not a function` 으로 죽는다 (계획 검증에서 재현됨).

상단 import 에 `createToolSession` 을 더하고(그 파일은 이미
`import { createTextModel, createJsonModel } from '../src/llm/provider.js';` 를 갖고 있다 —
같은 줄에 합친다), 108행 근처 의존성에 한 줄 더한다:

```js
    toolSession: (spec) => createToolSession(spec, { client: () => client }),
```

`anthropic: () => client` 는 **지우지 않는다** — 그 파일의 다른 팩토리들이 아직 쓴다.

- [x] **Step 8: 무변화 증명**

Run: `node scripts/check-llm-qa.js --baseline a8c9a9b11d13930913714d8ec943c3b0fa310937`
Expected: 14개 시나리오 전부 통과 + 기준판 trace 일치

**다르면 멈추고 차이를 그대로 보고한다 — 검사를 고쳐 맞추지 않는다.** 시나리오의 기대값을 새 동작에 맞추는 것이 이 Task 에서 할 수 있는 가장 나쁜 일이다. 그렇게 하면 무변화 증명이 「무변화라고 적어 둔 문서」로 바뀐다.

- [x] **Step 9: 증명이 공허하지 않은지 확인**

기준판 배선을 무력화해 본다. 편집 도구로 고쳤다가 편집 도구로 되돌린다.

1. Step 7 에서 넘기는 `anthropic: () => client,` 줄을 지운다 (기준판이 받을 의존성을 뺀다).
2. Run: `node scripts/check-llm-qa.js --baseline a8c9a9b…` → **실패해야 한다.** 옛 `qa.js` 는 `anthropic()` 을 부르므로 그 자리에서 죽는다. 실패 출력을 기록한다.
3. 되돌린다.
4. Run: `git diff --stat scripts/check-llm-qa.js` → Task 1·3 의 변경만 남아 있어야 한다.

**통과해 버리면** 기준판이 옛 모듈을 안 돌고 있다는 뜻이다 — 멈추고 보고한다.

- [x] **Step 10: 주변 검사 회귀**

Run: `node scripts/check-llm-provider.js && node scripts/check-llm-summary.js --baseline 1bcbceb5d52230a8283a573b9eb8cff256287382 --facade && node scripts/check-usage-accounting.js && node scripts/check-tool-limit.js`
Expected: 전부 OK

- [x] **Step 11: facade 갈래까지**

Run: `node scripts/check-llm-qa.js --facade`
Expected: `✓ actual ESM facade with real SDK runner and stubbed create` 포함 통과. 실제 `claude.js` 배선이 창구를 물고 도는지를 여기서 본다.

- [x] **Step 12: 전체 검사**

Run: `npm run check`
Expected: 전체 통과. 실패 시 원인 파악 후 수정 (검사 느슨화 금지).

- [x] **Step 13: 커밋 승인 요청 (직접 커밋 금지)**

보고에 Step 8 의 대조 결과와 Step 9 의 실패 출력을 포함한다.

---

## 끝난 상태 (2026-09-19 확인)

커밋 넷으로 끝났다 — `ca8ca53`(계획서) · `6f4b144`(Task 2) · `df7c45f`(Task 1+3) ·
`441740f`·`6d026de`(아래 「사각 4개」). `npm run check` 전체 통과.

무변화 증명이 공허하지 않다는 것을 2026-09-19 에 다시 쟀다:

- `src/llm/qa.js` 의 도구 호출 로그 앞머리를 `  -> ` → `  => ` 로 한 글자 바꾸면
  `--baseline a8c9a9b…` 는 **빨개지고** 기준판 없는 실행은 **통과한다**. 두 갈래가
  갈리는 것이 대조가 일한다는 증거다
- 기준판이 정말 옛 모듈인지: `replay()` 의 `anthropic: () => client` 를 빼면
  옛 `qa.js` 의 `toolRunner` 호출 줄에서 `TypeError: anthropic is not a function` 으로
  죽는다. 기준판은 sha 의 코드를 돌고 있다
- 가장 촘촘한 시나리오(`real wrapper pairs sizes/narrows by tool_use id …`)가 실제로
  대조되는지도 따로 강제해 확인했다 — Task 1 Step 3 의 `baselineIsStdin` 분기가 의도대로 산다
- P0-3 네 커밋에서 **지워지거나 느슨해진 단언은 없다.** 시나리오도 줄지 않았다

## 계획과 다르게 간 곳

계획서를 쓴 뒤 실제로 돌려 보고 고친 자리들이다 (코드 주석에 `Fix round 1` 로 남아 있다).
동작은 기준판과 같고 증명도 통과하지만, **계획서 본문대로는 아니다.**

① **모양 판정이 「읽을 때」가 아니라 「보낼 때」로 갔다.** 계획 Task 2 Step 3 은 회차가
   `messageId: anthropicMessageId(raw.id)` 로 걸러 내게 했지만, 실제는
   `messageId: raw.id ?? null`(`src/llm/provider.js:182`)이고 판정은 `open()` 들머리
   한 곳(`provider.js:154`)에서만 한다. **이유:** 받을 때 걸러 버리면 모양이 틀린 id 가
   앞서 들고 있던 멀쩡한 기준값을 **안 덮어서**, 다음 질문이 낡은 기준을 보낸다. 질문
   셋짜리 대조에서 실제로 갈렸다.

② **세션이 `previousMessageId` 필드를 내놓는다** (`provider.js:171`) — 계획의 창구 계약에
   없던 것이다. ① 때문에 호출 자리가 「이번에 실제로 보낸 기준」을 알 길이 필요해졌고,
   같은 모양 규칙을 호출 자리에 한 벌 더 두지 않으려고 창구가 되돌려 준다. 그 결과
   `src/llm/qa.js` 의 `const baseline` 이 `req`·`session` **뒤**(214행)로 내려갔다 —
   계획 Task 3 Step 2 가 "앞이어야 한다"고 못박은 자리를 못 지켰다.

③ **검사 ⑫가 계획과 반대를 단언한다.** 계획은 `'not-a-message' → null` 이었지만 실제
   `scripts/check-llm-provider.js:254` 는 회차가 원문을 그대로 통과시키는 것을 재고,
   거르는 것은 **보내는 쪽**을 보는 별도 루프(`:260-267`)가 잰다. ① 의 당연한 결과다.

④ **`check-llm-provider.js` 통과 문구가 계획보다 길다** — 표시 상한·이터레이터 정리·
   이어붙임 모양이 뒤에 더 붙었다.

⑤ **Q&A 시나리오가 14 → 15 로 늘었다.** 새로 넣은
   `invalid message ID overwrites the stored baseline (three questions)` 은 ① 을 잡아낸
   바로 그 검사다. 기존 두 질문짜리 시나리오는 두 동작 모두에서 `null` 이 나와 구별을
   못 한다. 지워진 시나리오는 없다.

## P0-2 가 남긴 사각 4개 — 둘 닫힘, 둘 남음

`441740f`·`6d026de` 가 처리했다. **넷 다 닫힌 것이 아니다.**

| | 사각 | 상태 |
|---|---|---|
| ① | 원장 기록의 `messageId`·`stopReason` 단언 | **닫힘** (`check-usage-accounting.js` 네 자리) |
| ② | `estimateCost` 주입 단언 | **닫힘 (2026-09-19)** — `441740f`·`6d026de` 때는 digest·요약만이었다. Q&A 가 원장을 인자 없이 세워(`createUsageCollector()`) 제 기본값으로 돌았고, **그 기본값이 마침 같은 계산기라 금액은 맞고 배선만 안 재지는** 상태였다. `qa.js` 가 `estimateCost` 를 꽂아 쓰게 바꾸고 표식 계산기로 재는 검사를 더했다 (`check-usage-accounting.js`, 39 → 40건). `src/ingest/summary.js:268` 은 원장을 `merge`·`snapshot` 으로만 써서 단가 계산기를 애초에 안 탄다 — 사각이 아니다 |
| ③ | `ledger.failed` 인자 단언 | **닫힘** (폴백 모델 시도 포함 세 자리) |
| ④ | `STOP_MAP` 덧붙임 탐지 | **2026-09-19 에 좁힘** — 표의 원문과 이름 댄 사유 10개에 더해, `normalize` 가 사유를 만지는 **줄 자체**를 소스에서 못박았다. 넣기 전에 목록 밖 이름(`budget_exceeded → 'ok'`)으로 구멍을 재현해 초록 통과를 보고 넣었다. **남은 길**은 특수 처리를 `normalize` 밖(창구 안쪽·호출 자리)으로 옮기는 것뿐이다 |

## 상시 검사가 못 보는 자리

**`--baseline` 대조는 `npm run check` 에 안 들어간다.** `scripts/check-runner.js:104` 가
검사 스크립트를 `spawn(node, [script])` 로 **인자 없이** 돌리기 때문이다. 위의 로그 한 글자
변조도 `npm run check` 는 초록으로 통과시킨다.

이것은 고쳐야 할 버그가 아니라 **성질**이다. 기준판 sha 는 「그때 그 모양」을 가리키는 고정점
이라, 코드가 정당하게 바뀌면 대조는 **당연히** 갈린다. 상시 관문에 걸면 다음 정당한 변경에서
빨개지고 그때 sha 를 밀어 넣게 되는데, 그 순간 대조는 아무것도 지키지 않는 장식이 된다.
그래서 무변화 증명은 **이전할 때 한 번 손으로 돌리는 것**이고, 계속 지켜야 할 것은
`441740f`·`6d026de` 처럼 **상시 검사의 단언으로 옮겨 놓는다**. 다음 이전(P0-4)도 같은 방식이다.

**2026-09-19 에 그 「옮기기」를 한 번 더 했다.** 기준판에서만 보이던 값 중 가장 값나가는 것이
비용 로그였다 — `qa.js` 가 `logUsage` 를 세 자리에서 찍는데(`251`·`266`·`283`행) **무엇으로
부르는지를 상시 검사 중 아무도 안 봤다.** 특히 251 행은 거절하고 대체 모델로 갈아타기 **직전에
1차 시도분을 먼저 정산하는** 자리다. 그 정산을 빼면 두 모델의 토큰이 한 줄에 뭉쳐 지출이 전부
대체 모델 앞으로 적히는데, 에러는 안 난다. `check-llm-qa.js` 가 `logUsage` 호출에 표를 달아
모으게 하고 시나리오 셋(정상·거절 폴백·두 번 거절)에 단언을 넣었다 — 251 행의 정산을 실제로
빼서 **기준판 없이** 빨개지는 것을 보고 넣었다.

남겨 둔 것은 콘솔 로그 문구다 (`  -> search(...)` 같은 줄). 문구를 못박으면 고칠 때마다
거추장스럽기만 해서, 기준판에서만 보이는 채로 둔다.

## 이 계획이 남기는 것

- ~~**P0-2 가 남긴 사각 4개는 여기서 안 막는다.**~~ 원장 기록의 `messageId`·`stopReason` 단언, `estimateCost` 주입 단언, `ledger.failed` 인자 단언, `STOP_MAP` 덧붙임 탐지 — 넷 다 별개 손질이고, 이 계획의 diff 에 섞으면 무변화 증명이 읽기 어려워진다. 이전이 끝난 뒤 한 번에 한다. → **이전을 끝내고 `441740f`·`6d026de` 로 처리했다. 둘 닫히고 둘 남았다 — 위 표 참조.**
- `effort` 는 Anthropic 어휘다. 타사 매핑이 손실 변환이라는 것은 세 창구가 다 선 뒤 계약 주석에 한 번에 적는다.
- Clio 의 종결 도구(`save_draft`)와 수동 루프는 P0-4 다. 이 세션 계약(`open`/`generateToolResponse`/`continueWith`)이 그때 **도구 실행 주체 선택**을 받게 넓어진다 — Clio 는 도구를 자기가 실행한다.

## 후속 계획 (이 계획 범위 밖)

P0-4 Clio 어댑터(수동 루프·종결 도구) + 두-저장소 어긋남 검사. 설계 원본은 소유자 워크스페이스의 2026-09-18 「모델교체-준비 설계」 기록 문서다 — 이 저장소에는 경로를 적지 않는다.
