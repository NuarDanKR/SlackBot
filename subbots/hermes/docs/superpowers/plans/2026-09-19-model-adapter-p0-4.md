# 모델 제공사 어댑터 P0-4 (Clio 수동 루프 · 두-저장소 어긋남 검사) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 네 번째이자 마지막 호출 자리인 Clio(`~/clio/src/agent.js`)를 어댑터 창구로 옮기고, 어댑터가 두 저장소에 생기므로 **두 판이 어긋나면 빨개지는 검사**를 세운다 — 앞 셋과 같이 **동작 무변화를 기준판 대조로 증명한 뒤에만.**

**Architecture:** Clio 루프는 Hermes Q&A 와 **모양이 다르다.** Q&A 는 회사 도구 실행기(`beta.messages.toolRunner`)에 왕복을 통째로 맡겼지만, Clio 는 `messages.stream()` 을 **자기 `for` 루프**로 24번까지 돌리고 도구도 자기가 실행한다(`dispatch`). 끝나는 길도 다르다 — `save_draft` 라는 **종결 도구**를 모델이 부르면 그 자리에서 반환한다. 그래서 P0-3 의 세션 계약(`open`/`generateToolResponse`/`continueWith`)에 실행 주체 선택을 **끼워 넣지 않고**, 네 번째 창구 `createToolLoop` 를 따로 낸다 — SDK 진입점도(`stream` vs `toolRunner`), 대화 잇기 모양도(`tool_result` 블록 배열 vs SDK 가 만든 메시지), 캐시 표시 정책도(1개 유지 vs 굴리기) 다르기 때문이다. 한 `open()` 안에 두 갈래를 두면 양쪽 다 읽기 어려워지고 무변화 증명이 섞인다.

**어댑터를 두 저장소가 나눠 쓴다.** Clio 는 이미 Hermes 의 읽기 계층 11개 파일을 **바이트 단위로 같게** 들고 있고(`clio/tests/shared-read-layer.test.js`), `src/llm/provider.js` 도 같은 방식으로 더한다. 그래서 Hermes 쪽에 `createToolLoop` 를 먼저 세우고(아무도 안 쓴다), 그 파일을 그대로 복사한 뒤, Clio 가 그것을 쓰게 옮긴다. Hermes 는 `anthropicToolLoop` 를, Clio 는 `anthropicToolSession` 을 각각 안 쓰는 채로 들고 있게 된다 — 지금 `format.js`·`documents/*.js` 가 그런 것과 같다.

**설계 문서와 다르게 가는 곳 하나 — 승인 때 함께 알린다.** 설계 원본(소유자 워크스페이스 2026-09-18 기록)은 「이 세션 계약이 **도구 실행 주체 선택을 받게 넓어진다**」고 적었다. 두 루프를 실제로 다 읽고 나서 그 길을 안 간다 — `anthropicToolSession` 은 `toolRunner` 객체를 쥐고 `setMessagesParams` 로 대화를 잇고 `betas`·`diagnostics.previous_message_id` 를 **항상 보낸다.** Clio 요청에 그 둘이 실리면 요청 바이트가 달라져 **무변화 증명 자체가 깨진다.** 한 `open()` 에 합치면 공유되는 것은 요청 조립과 `normalize` 뿐이고 나머지가 전부 갈래가 된다. 이 편차는 계획 승인 때 보이고, 설계 문서를 고칠지 묻는다.

**Tech Stack:** Node ≥20 ESM, `@anthropic-ai/sdk ^0.115.0` (양쪽 같은 버전). Hermes 는 `scripts/check-*.js` + `npm run check`, Clio 는 `node --test tests/*.test.js` (`npm test`). 기준판 대조는 양쪽 다 **git 에서 옛 소스를 읽어 `compileFunction` 으로 세우는** 방식이다 — Clio 에는 그 장치가 없어서 Task 4 가 먼저 만든다.

**배포:** Clio 는 Hermes 와 같은 GCP VM 의 `/opt/clio/code` 에서 `clio.service` 로 돈다. 이 계획은 `src/` 를 고치므로 **`git pull` 뒤 서비스 재시작이 있어야 새 코드가 돈다** — 자동 pull 만으로는 옛 코드가 계속 돌고 에러도 경고도 안 난다. 배포는 승인 후 별도로 한다.

## Global Constraints

- **커밋·push 는 저장소마다 각각 WHK 승인 후에만.** 서브에이전트는 커밋하지 않는다 — 고치고·검사하고·보고만. `git commit`·`git push`·`git add`·`git stash`·`git checkout`·`git restore` 를 쓰지 않는다. 읽기 전용 git 만 쓴다.
- **Clio 에는 `.githooks` 가 없다.** Hermes 는 `pre-push` 관문이 승인 목록과 대조하지만 **Clio 는 아무것도 안 막는다** — 실수로 나가면 그대로 나간다. 이 계획은 두 저장소를 오가므로 특히 주의한다.
- 검사는 실제 네트워크·Slack·모델 API 에 닿지 않는다. Clio 시험은 `client` 를 주입받아 가짜를 꽂는다 (`tests/agent.test.js` 의 `fakeClient`).
- 사업장·비공개 채널 이름을 코드·주석에 쓰지 않는다 (Hermes `scripts/check-business-names.js` 가 잡는다).
- Hermes 는 `npm run check` 전체 통과, Clio 는 `npm test` 전체 통과가 완료 조건.
- **요청 바이트·로그 문구·캐시 표시 위치·반환 필드가 수정 전과 동일해야 한다.** 다른 점이 하나라도 나오면 이유를 적고 멈춘다 — **검사를 고쳐 맞추지 않는다.**
- `<HERMES_BASELINE>` = `1f64be6688f1c0ef6c58358fef9409fa116e0aa3` (P0-3 마무리 커밋, origin/main).
- `<CLIO_BASELINE>` = `43a3e07` 의 전체 sha. Task 4 Step 1 에서 `git -C ~/clio rev-parse 43a3e07` 로 확정해 적어 넣는다. 이 커밋의 `src/agent.js` 가 이전 전 원본이다.
- **`shared-read-layer.test.js` 의 11개 파일 목록을 줄이지 않는다.** 이 계획은 12번째를 **더한다.**

---

### Task 1: Hermes 에 수동 루프 창구 `createToolLoop` (아직 아무도 안 쓴다)

P0-1·P0-2·P0-3 과 같은 순서다 — 창구를 먼저 세우고, 쓰는 것은 나중 Task 가 한다. 이 Task 가 끝나도 Hermes 의 동작은 **한 글자도 안 바뀐다.**

**Files:**
- Modify: `~/hermes/code/src/llm/provider.js`
- Modify: `~/hermes/code/scripts/check-llm-provider.js`

**Interfaces:**
- Consumes: `provider.js` 안의 `normalize`·`anthropicRetryable`·`PROVIDERS`/`createModel` (P0-1~P0-3 산물)
- Produces: `createToolLoop(spec, { client }) → { provider, id, retryable, open }`
  - `open({ system, messages, tools }) → loop`
  - `loop.next() → Promise<turn>` — 한 회차를 부르고 정규화해 돌려준다. 회차 모양:
    `{ text, stopReason, model, usage, raw, toolUses: [{id,name,input}] }`
    - `stopReason` 정규화는 앞 세 창구와 같은 표(`STOP_MAP`). `tool_use` 는 표에 없어 원문 그대로 지나간다
    - `messageId`·`cacheMiss` 는 **없다** — Clio 는 `previous_message_id` 도 `cache-diagnosis` 베타도 안 쓴다. 안 쓰는 필드를 지어내면 그것이 계약인 줄 읽힌다
  - `loop.continueWith(assistantTurn, toolResults) → void` — 대화를 잇는다. `toolResults` 는 **호출 자리가 만든** `{ type:'tool_result', tool_use_id, content }` 배열이다. 캐시 표시를 맨 끝으로 옮기고 **직전 표시를 지우는 것**이 이 안에서 끝난다

- [ ] **Step 1: 실패하는 검사를 먼저 쓴다** — `scripts/check-llm-provider.js`

import 줄을 바꾼다:

```js
import { createTextModel, createJsonModel, createToolSession, createToolLoop } from '../src/llm/provider.js';
```

파일 맨 끝의 `console.log('[check-llm-provider] OK — …');` **앞**에 통째로 넣는다:

```js
// ── 수동 루프 창구 (P0-4) ──────────────────────────────────────────
// 도구 왕복 창구와 다른 점: 회사 실행기를 안 쓴다. 호출 자리가 자기 루프를 돌리고 도구도
// 자기가 실행한다 (Clio 가 그렇다 — 종결 도구 save_draft 로 끝나고, 도구 실행에 자기
// 권한 판정이 끼어든다). 그래서 창구가 맡는 것은 ① 요청 조립 ② 회차 정규화
// ③ 대화 잇기(캐시 표시 옮기기) 셋뿐이다.

// stream().finalMessage() 를 흉내 내는 가짜. 회차 목록을 순서대로 흘린다.
function fakeStreamClient(turns) {
  const runs = [];
  let i = 0;
  const client = () => ({ messages: { stream(params) {
    runs.push(clone(params));
    const value = turns[i++];
    assert.ok(value, '예상보다 많이 불렀습니다');
    return { finalMessage: async () => clone(value) };
  } } });
  return { client, runs };
}

const loopTurn = (stop = 'end_turn', content = [{ type: 'text', text: '답' }]) => ({
  id: 'msg_x', model: 'model-fixture', role: 'assistant', stop_reason: stop, content,
  usage: { input_tokens: 2, output_tokens: 1 },
});

// ⑲ 요청 조립 — 회사 어휘로 정확히 이 모양이어야 한다. betas·diagnostics 는 **없다.**
{
  const { client, runs } = fakeStreamClient([loopTurn()]);
  const loop = createToolLoop(SPEC, { client }).open({ system: SYSTEM, messages: MESSAGES, tools: TOOLS });
  await loop.next();
  assert.deepEqual(runs[0], {
    model: 'model-fixture', max_tokens: 1600, output_config: { effort: 'high' },
    system: SYSTEM, messages: MESSAGES, tools: TOOLS,
  });
}

// ⑳ 회차 정규화 — 다른 창구와 같은 본문 추출·같은 중단 사유 표. tool_use 는 원문 유지.
{
  const { client } = fakeStreamClient([loopTurn('tool_use', [
    { type: 'text', text: ' 앞 ' },
    { type: 'tool_use', id: 'toolu_1', name: 'search', input: { q: 1 } },
    { type: 'text', text: '뒤' },
  ])]);
  const t = await createToolLoop(SPEC, { client })
    .open({ system: SYSTEM, messages: MESSAGES, tools: TOOLS }).next();
  assert.equal(t.text, '앞 \n뒤');
  assert.equal(t.stopReason, 'tool_use');
  assert.deepEqual(t.toolUses, [{ id: 'toolu_1', name: 'search', input: { q: 1 } }]);
  assert.deepEqual(t.usage, { input_tokens: 2, output_tokens: 1 });
  assert.equal(t.raw.stop_reason, 'tool_use');
  assert.equal(t.model, 'model-fixture');
  // 안 쓰는 필드를 지어내지 않는다 — 있으면 그것이 계약인 줄 읽힌다.
  assert.equal('messageId' in t, false);
  assert.equal('cacheMiss' in t, false);
}

// ㉑ 표에 있는 사유는 정규화되고, 없는 사유는 원문 그대로 — 수동 루프 창구도 같은 표를 탄다.
for (const [given, want] of [['end_turn', 'ok'], ['max_tokens', 'max_tokens'],
  ['refusal', 'refusal'], ['tool_use', 'tool_use'], ['pause_turn', 'pause_turn']]) {
  const { client } = fakeStreamClient([loopTurn(given, [])]);
  const t = await createToolLoop(SPEC, { client })
    .open({ system: SYSTEM, messages: MESSAGES, tools: TOOLS }).next();
  assert.equal(t.stopReason, want, `수동 루프 창구도 같은 중단 사유 표를 써야 합니다: ${given}`);
}

// ㉒ 대화 잇기 — assistant 회차와 도구 결과가 순서대로 붙고, 캐시 표시는 **맨 끝 하나뿐**이다.
{
  const first = loopTurn('tool_use', [{ type: 'tool_use', id: 'toolu_1', name: 'search', input: {} }]);
  const second = loopTurn('tool_use', [{ type: 'tool_use', id: 'toolu_2', name: 'search', input: {} }]);
  const { client, runs } = fakeStreamClient([first, second, loopTurn()]);
  const loop = createToolLoop(SPEC, { client }).open({ system: SYSTEM, messages: MESSAGES, tools: TOOLS });

  const t1 = await loop.next();
  loop.continueWith(t1, [{ type: 'tool_result', tool_use_id: 'toolu_1', content: '결과1' }]);
  const t2 = await loop.next();
  loop.continueWith(t2, [{ type: 'tool_result', tool_use_id: 'toolu_2', content: '결과2' }]);
  await loop.next();

  const last = runs.at(-1);
  // 원래 대화 + (assistant, tool_result) 두 쌍
  assert.equal(last.messages.length, MESSAGES.length + 4);
  // at(-2) 는 **두 번째** assistant 회차다 (-1 은 그 뒤의 도구 결과). 첫 회차는 at(-4) 다.
  assert.deepEqual(last.messages.at(-2).content, second.content);
  assert.deepEqual(last.messages.at(-4).content, first.content);
  assert.equal(last.messages.at(-1).content.at(-1).content, '결과2');
  // 표시는 **가장 최근 것 하나뿐** — 직전 회차의 표시는 지워져야 한다.
  const marks = last.messages
    .flatMap((m) => (Array.isArray(m.content) ? m.content : []))
    .filter((b) => b.cache_control).length;
  assert.equal(marks, 1, '표시가 쌓이면 한 요청 4개 상한에 걸려 400 이 납니다');
  assert.deepEqual(last.messages.at(-1).content.at(-1).cache_control, { type: 'ephemeral' });
}

// ㉓ 빈 도구 결과는 **옛 Clio 와 똑같이 던진다.** 여기서 조용히 넘어가게 고치면 죽던 것이
//    기형 대화로 바뀐다 — 무변화가 아니다. 이 단언이 그 손을 막는 자리다.
{
  const { client } = fakeStreamClient([loopTurn('tool_use', [])]);
  const loop = createToolLoop(SPEC, { client }).open({ system: SYSTEM, messages: MESSAGES, tools: TOOLS });
  const t = await loop.next();
  assert.throws(() => loop.continueWith(t, []), TypeError,
    '빈 도구 결과는 옛 Clio 와 같이 던져야 합니다 — 가드를 넣으면 무변화가 깨집니다');
}

// ㉔ 모르는 provider 는 수동 루프 창구에서도 던진다.
assert.throws(() => createToolLoop({ ...SPEC, provider: 'acme' }, { client: () => ({}) }), /acme/);
assert.equal(createToolLoop({ ...SPEC, provider: 'anthropic' }, { client: () => ({}) }).provider, 'anthropic');
```

마지막 줄 문구도 바꾼다:

```js
console.log('[check-llm-provider] OK — 글 창구 + JSON 창구 + 도구 왕복 창구(요청 조립·회차 정규화·id 모양·캐시 미스·대화 잇기·표시 상한·이터레이터 정리·이어붙임 모양) + 수동 루프 창구(요청 조립·회차 정규화·중단 사유 표·표시 옮기기)');
```

- [ ] **Step 2: 실패 확인**

Run: `cd ~/hermes/code && node scripts/check-llm-provider.js`
Expected: FAIL — `createToolLoop is not a function` (또는 import 관련 `SyntaxError`)

- [ ] **Step 3: 구현** — `src/llm/provider.js`

계약 주석 블록의 첫 줄을 바꾼다:

```js
 * 계약 (네 창구 — 글 반환 · JSON 반환 · 도구 왕복 · 수동 루프):
```

그리고 그 블록 끝(`client 는 지연 생성 함수로…` 줄 **앞**)에 더한다:

```js
 *   createToolLoop(spec, { client }) → { provider, id, retryable, open }
 *     open(req) → 루프. next() 로 한 회차씩 부르고, continueWith(회차, 도구결과들) 로
 *     대화를 잇는다. **도구는 호출 자리가 실행한다** — 종결 도구로 끝나거나 도구 실행에
 *     자기 권한 판정이 끼어드는 자리(Clio)가 쓴다. 캐시 표시 옮기기만 창구가 맡는다.
 *     회차에 messageId·cacheMiss 는 **없다** — 이 창구를 쓰는 자리가 안 쓰는 것을
 *     지어내면 그것이 계약인 줄 읽힌다.
 *
 *     **주의 — 도구 왕복 창구의 continueWith 와 이름은 같고 인자는 반대다.**
 *       세션: continueWith(toolMessage, assistantTurn)   ← 회사가 만든 메시지가 먼저
 *       루프: continueWith(assistantTurn, toolResults)   ← 회차가 먼저
 *     세션은 회사 실행기가 만들어 준 것을 받고, 루프는 호출 자리가 만든 것을 받는다.
 *     타사 구현을 쓸 때 이 둘을 바꿔 넣으면 **에러 없이 대화가 뒤집힌다.**
 *
 * effort 는 **Anthropic 어휘다.** 타사에 옮길 때 손실 변환이 된다 — 저쪽은 등급이 없거나
 * 개수가 다르거나(예: 추론 토큰 예산 같은 연속값) 아예 다른 축이다. 그래서 네 창구 모두
 * spec.effort 를 **그대로 얹기만 하고 해석하지 않는다.** 타사 구현을 더하는 날 그 회사
 * 어휘로 옮기는 표를 그 provider 안에 두고, 옮기면서 무엇이 뭉개지는지를 거기 적는다 —
 * 이 자리에서 공통 등급으로 미리 뭉개면 Anthropic 쪽 정보까지 함께 잃는다.
```

`PROVIDERS` 표 **앞**, `anthropicToolSession` 함수 **뒤**에 구현을 넣는다:

```js
/* ── 수동 루프의 캐시 표시 ──────────────────────────────────────────
 *
 * 도구 왕복 창구(rollCacheMarks)는 표시를 「가장 최근 하나만」 남기려고 대화를 훑는다.
 * 수동 루프는 그럴 필요가 없다 — 표시를 다는 자리가 이 함수 하나뿐이라, 직전에 단 블록을
 * 손에 들고 있다가 지우면 된다. 훑지 않으므로 호출 자리가 직접 넣은 표시(시스템 블록 등)를
 * 건드리지 않는다. 상한 4개 중 시스템 1개 + 이력 1개만 쓴다. */
function anthropicToolLoop(spec, client) {
  return {
    provider: 'anthropic',
    id: spec.id,
    retryable: anthropicRetryable,
    open({ system, messages, tools }) {
      const convo = [...messages];
      let marked = null; // 직전 회차에 표시를 단 블록
      return {
        async next() {
          // 스트리밍으로 받는다 — 출력 상한이 크면 비스트리밍은 HTTP 타임아웃이 난다.
          const raw = await client().messages.stream({
            model: spec.id,
            max_tokens: spec.maxTokens,
            output_config: { effort: spec.effort },
            system,
            tools,
            messages: convo,
          }).finalMessage();
          return {
            ...normalize(raw),
            toolUses: (raw.content || [])
              .filter((b) => b.type === 'tool_use')
              .map((b) => ({ id: b.id, name: b.name, input: b.input })),
          };
        },
        /* **빈 toolResults 를 막지 않는다 — 일부러 그렇다.** 막고 싶은 손이 여기서 가장
         * 많이 움직인다: `if (!toolResults.length) return;` 한 줄이면 되니까. 그런데 옛
         * Clio 코드(agent.js:554-556)는 그 경우 `undefined.cache_control` 에서 TypeError 로
         * **죽는다.** 가드를 넣으면 죽던 것이 조용한 진행으로 바뀌고, 도구 결과 없이
         * assistant 로 끝난 기형 대화가 다음 요청으로 나간다 — 크래시가 400 으로 바뀔 뿐
         * 나아지는 것이 없고, 무엇보다 **이 계획이 증명하겠다는 무변화가 깨진다.**
         * 고칠 값어치가 있는 자리지만 그것은 이전이 끝난 뒤 눈 뜨고 하는 별건이다. */
        continueWith(assistantTurn, toolResults) {
          const raw = assistantTurn.raw;
          convo.push({ role: raw.role, content: raw.content });
          if (marked) delete marked.cache_control;
          marked = toolResults[toolResults.length - 1];
          marked.cache_control = TOOL_CACHE_CONTROL;
          convo.push({ role: 'user', content: toolResults });
        },
      };
    },
  };
}
```

`PROVIDERS` 표에 네 번째 창구를 더한다:

```js
const PROVIDERS = { anthropic: {
  text: anthropicTextModel, json: anthropicJsonModel,
  tool: anthropicToolSession, loop: anthropicToolLoop,
} };
```

파일 끝에 export 를 더한다:

```js
export function createToolLoop(spec, { client }) {
  return createModel('loop', spec, client);
}
```

- [ ] **Step 4: 통과 확인**

Run: `cd ~/hermes/code && node scripts/check-llm-provider.js`
Expected: 새 통과 문구. 앞선 사례 ①~⑮ 가 **그대로 통과해야 한다** — 이 Task 는 기존 세 창구를 안 건드린다.

- [ ] **Step 5: 앞 세 자리가 안 흔들렸는지 확인**

Run: `cd ~/hermes/code && node scripts/check-llm-qa.js --baseline 1f64be6688f1c0ef6c58358fef9409fa116e0aa3`
Expected: 15개 시나리오 통과 + 기준판 일치

Run: `cd ~/hermes/code && node scripts/check-llm-summary.js --baseline 1bcbceb5d52230a8283a573b9eb8cff256287382 --facade`
Expected: 통과 · 기준판 일치

- [ ] **Step 6: 전체 검사**

Run: `cd ~/hermes/code && npm run check`
Expected: 전체 통과. 카탈로그는 안 바꾼다 — 새 검사 파일을 만들지 않았다.

- [ ] **Step 7: 커밋 승인 요청 (직접 커밋 금지)**

보고에 Step 4·5 의 출력을 포함한다. 커밋 제목 제안:
`feat(모델): 수동 루프 창구를 더한다 (아직 아무도 안 쓴다)`

---

### Task 2: `provider.js` 를 Clio 로 복사하고 어긋남 검사에 12번째 파일로 넣는다

이전(Task 4)은 아직 안 한다. 여기서는 **파일이 두 저장소에 같게 있다**는 상태만 만들고, 그것이 어긋나면 빨개지는 자리를 세운다.

**Files:**
- Create: `~/clio/src/llm/provider.js` (Hermes 판 그대로)
- Modify: `~/clio/tests/shared-read-layer.test.js`

**Interfaces:**
- Consumes: Task 1 의 `~/hermes/code/src/llm/provider.js`
- Produces: `~/clio/src/llm/provider.js` — Hermes 판과 **바이트 단위로 같다.** Task 4 가 여기서 `createToolLoop` 를 import 한다.

- [ ] **Step 1: 실패하는 검사를 먼저 쓴다**

`~/clio/tests/shared-read-layer.test.js` 의 첫 시험에서 파일 목록에 한 줄을 더한다. 기존:

```js
  for (const file of ['config.js', 'search-terms.js', 'archive.js', 'documents.js', 'documents/parse.js', 'documents/store.js', 'documents/access.js', 'documents/brief.js', 'documents/search.js', 'documents/read.js', 'format.js']) {
```

을 다음으로 바꾼다 (`llm/provider.js` 를 맨 뒤에 더한 것뿐이다):

```js
  for (const file of ['config.js', 'search-terms.js', 'archive.js', 'documents.js', 'documents/parse.js', 'documents/store.js', 'documents/access.js', 'documents/brief.js', 'documents/search.js', 'documents/read.js', 'format.js', 'llm/provider.js']) {
```

- [ ] **Step 2: 실패 확인**

Run: `cd ~/clio && node --test tests/shared-read-layer.test.js`
Expected: FAIL — `ENOENT ... clio/src/llm/provider.js` (파일이 아직 없다)

- [ ] **Step 3: 복사한다**

Run:
```
mkdir -p ~/clio/src/llm
cp ~/hermes/code/src/llm/provider.js ~/clio/src/llm/provider.js
```

**한 글자도 고치지 않는다.** Clio 가 안 쓰는 `anthropicToolSession`·`createToolSession` 도 그대로 둔다 — 바이트가 같아야 어긋남 검사가 일한다. 지금 `format.js`·`documents/*.js` 가 같은 사정이다.

- [ ] **Step 4: 통과 확인**

Run: `cd ~/clio && node --test tests/shared-read-layer.test.js`
Expected: PASS

- [ ] **Step 5: 어긋남 검사가 공허하지 않은지 확인한다 (비공허성)**

**검사를 만들어 놓고 빨개지는 것을 안 본 채 넘어가면 이 Step 은 아무 일도 안 한 것이다.**

편집 도구로 고쳤다가 편집 도구로 되돌린다 — git 명령을 쓰지 않는다.

1. `~/clio/src/llm/provider.js` 의 `const STOP_MAP = …` 줄 끝에 공백 하나를 더한다.
2. Run: `cd ~/clio && node --test tests/shared-read-layer.test.js` → **실패해야 한다** (`llm/provider.js` 불일치). 실패 출력을 기록한다.
3. 공백을 되돌린다.
4. Run: `cd ~/clio && git status --short` → **두 줄**이 나와야 한다 — `?? src/llm/provider.js`(새로 복사한 것, 아직 untracked)와 ` M tests/shared-read-layer.test.js`(Step 1 에서 목록에 한 줄 더한 것). 셋째 줄이 있으면 되돌리기가 덜 된 것이니 멈추고 보고한다.

2번이 통과해 버리면 목록에 이름을 잘못 적은 것이다 — 멈추고 보고한다.

- [ ] **Step 6: Hermes 저장소가 없을 때를 정한다**

이 시험은 `path.resolve(ROOT, '../hermes/code')` 를 읽는다. **Hermes 를 나란히 두지 않은 컴퓨터에서는 `ENOENT` 로 빨개진다** — 어긋난 것이 아니라 잴 수 없는 것인데, 지금은 그 둘이 똑같이 실패로 보인다. 새 팀이 Clio 만 clone 하면 그날부터 빨간 시험 하나를 안고 시작한다.

**잴 수 없는 것은 「통과」가 아니라 「못 잼」으로 낸다.** 시험 본문 맨 앞에 넣는다:

```js
test('shared readers match Hermes byte-for-byte', (t) => {
  /* 나란히 둔 Hermes 저장소를 못 찾으면 **어긋남이 없다는 뜻이 아니라 잴 수 없다는 뜻**이다.
   * 조용히 통과시키면 그때부터 이 시험은 아무것도 안 지키면서 초록으로 남는다. */
  if (!fs.existsSync(HERMES)) {
    t.skip(`Hermes 저장소를 못 찾아 어긋남을 재지 못했습니다 — ${HERMES}`);
    return;
  }
```

(닫는 `});` 는 그대로 둔다. `t` 를 쓰려면 시험 콜백이 인자를 받아야 하므로 `() =>` 를 `(t) =>` 로 바꾼 것이다.)

- [ ] **Step 7: 건너뛰기가 실제로 도는지 확인**

Run: `cd ~/clio && node --test tests/shared-read-layer.test.js`
Expected: PASS (Hermes 가 나란히 있으므로 건너뛰지 않는다)

임시로 못 찾게 만들어 건너뛰기 갈래를 한 번 본다 — 편집 도구로 `HERMES` 상수를 `path.resolve(ROOT, '../hermes-없음/code')` 로 바꾸고 돌린 뒤 되돌린다.
Expected: 건너뜀 표시와 위 문구가 보이고, **종료 코드는 0 이 아니어야 한다면 그대로 보고한다** — `node --test` 의 skip 은 통과로 센다. 그 성질을 확인만 하고 넘어간다.

- [ ] **Step 8: Clio 전체 시험**

Run: `cd ~/clio && npm test`
Expected: 전체 통과

- [ ] **Step 9: 커밋 승인 요청 (Clio 저장소 — 직접 커밋 금지)**

**Clio 에는 push 관문이 없다.** 승인받은 것만 들어갔는지 `git -C ~/clio status` 로 직접 확인해 보고에 넣는다. 커밋 제목 제안:
`feat(모델): 제공사 어댑터를 Hermes 에서 들여온다 (아직 아무도 안 쓴다)`

---

### Task 3: 어긋남이 **양쪽에서** 보이게 한다

Task 2 의 검사는 **Clio 쪽에만** 있다. Hermes 에서 `provider.js` 를 고치고 `npm run check` 를 돌리면 초록이고, 어긋남은 Clio 시험을 돌릴 때까지 안 보인다. Hermes 를 고치는 사람이 Clio 시험을 돌리는 습관은 없다 — **고친 쪽에서 보여야 한다.**

**이름이 `check-` 가 아니라 `verify-` 인 것은 일부러다.** `check-runner.js:55` 의 발견 규칙이 `scripts/check-*.js` 를 전부 집어 오고 `validateNodeChecks`(:64-67)가 **카탈로그에 없으면 `npm run check` 를 죽인다.** 상시 검사에서 빼려면 면제 목록에 넣거나 이름을 비켜야 하는데, **면제 목록을 건드리는 것은 관문을 느슨하게 하는 것**이라 이름을 비킨다.

**Files:**
- Create: `~/hermes/code/scripts/verify-shared-with-clio.js`

**Interfaces:**
- Consumes: `~/clio/src/**` (나란히 있을 때만)
- Produces: `node scripts/verify-shared-with-clio.js` — 두 저장소가 나눠 쓰는 파일이 어긋나면 빨개진다. **손으로 돌리는 검사다** — `npm run check` 에 안 들어간다 (WHK 결정 2026-09-19, 아래 Step 1 주석에 이유).

- [ ] **Step 1: 검사를 쓴다**

`~/hermes/code/scripts/verify-shared-with-clio.js` 를 새로 만든다:

```js
/**
 * 두 저장소가 나눠 쓰는 파일이 어긋났나 — **고치는 쪽에서** 보이게 한다.
 *
 * 같은 판정이 Clio 의 tests/shared-read-layer.test.js 에도 있다. 거기만 있으면 Hermes 를
 * 고친 사람은 어긋난 줄 모르고, Clio 시험을 누가 돌릴 때까지 그 차이는 에러 없이 흐른다.
 * 목록은 **한 벌뿐이어야 하므로** 저쪽 파일에서 읽어 온다 — 두 곳에 적으면 목록이 어긋난다.
 *
 * **npm run check 에는 안 들어간다 (WHK 결정 2026-09-19).** Hermes 는 팀이 나눠 쓰는
 * 저장소고 Clio 는 소유자 개인 것이라, 대부분의 Hermes 설치에는 Clio 가 아예 없다. 거기서
 * 「못 잼」(종료 2)을 내면 npm run check 가 영원히 0 이 아닌 값으로 끝나고, 그러면 사람이
 * 그 숫자를 무시하게 된다 — 관문 하나를 통째로 죽이는 대가다. 그래서 이것은 **공유 파일을
 * 고칠 때 손으로 돌리는 검사**이고, 그 사실을 src/llm/provider.js 머리 주석에 적어 둔다.
 * 자동으로 지키는 자리는 Clio 쪽 npm test 다.
 */
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const CLIO = path.resolve(ROOT, '../../clio');
const LIST_FILE = path.join(CLIO, 'tests/shared-read-layer.test.js');

if (!fs.existsSync(LIST_FILE)) {
  console.log(`Clio 저장소가 없어 잴 것이 없습니다 — ${CLIO}`);
  process.exit(0);
}

/* 목록의 원본은 Clio 시험 파일이다. 여기에 베껴 적으면 파일이 하나 늘거나 줄 때
 * 두 목록이 갈리고, 갈린 쪽은 조용히 덜 잰다. */
const source = fs.readFileSync(LIST_FILE, 'utf8');
const literal = source.match(/for \(const file of (\[[^\]]*\])\)/);
assert.ok(literal, 'Clio 시험에서 공유 파일 목록을 찾지 못했습니다 — 저쪽 모양이 바뀌었습니다');
const files = new Function('return ' + literal[1])();
assert.ok(files.length >= 12, `공유 파일이 ${files.length}개뿐입니다 — 목록이 줄었는지 확인하세요`);

const drifted = [];
for (const file of files) {
  const mine = path.join(ROOT, 'src', file);
  const theirs = path.join(CLIO, 'src', file);
  if (!fs.existsSync(theirs)) { drifted.push(`${file} — Clio 에 없습니다`); continue; }
  if (!fs.readFileSync(mine).equals(fs.readFileSync(theirs))) drifted.push(`${file} — 내용이 다릅니다`);
}

assert.deepEqual(drifted, [],
  '두 저장소가 나눠 쓰는 파일이 어긋났습니다 — 고친 쪽을 상대 저장소에도 그대로 옮기세요:\n  '
  + drifted.join('\n  '));

console.log(`[verify-shared-with-clio] OK — 공유 파일 ${files.length}개가 Clio 와 같습니다`);
```

- [ ] **Step 2: 통과 확인**

Run: `cd ~/hermes/code && node scripts/verify-shared-with-clio.js`
Expected: `[verify-shared-with-clio] OK — 공유 파일 12개가 Clio 와 같습니다`

- [ ] **Step 3: 비공허성 — 실제로 어긋나게 해 본다**

편집 도구로 고쳤다가 편집 도구로 되돌린다.

1. `~/clio/src/llm/provider.js` 의 `const STOP_MAP = …` 줄 끝에 공백 하나를 더한다.
2. Run: `cd ~/hermes/code && node scripts/verify-shared-with-clio.js` → **실패해야 한다** (`llm/provider.js — 내용이 다릅니다`). 실패 출력을 기록한다.
3. 되돌린다.
4. Run: `git -C ~/clio status --short` → 비어야 한다.

- [ ] **Step 4: Clio 가 없는 갈래도 실제로 본다**

편집 도구로 `CLIO` 상수를 `path.resolve(ROOT, '../../clio-없음')` 으로 바꾸고 돌린 뒤 되돌린다.
Expected: `Clio 저장소가 없어 잴 것이 없습니다 — …` 가 찍히고 **종료 코드 0**.

- [ ] **Step 5: 카탈로그에 올리지 **않는다** — 대신 손으로 돌릴 자리를 남긴다**

**이 검사는 `npm run check` 에 넣지 않는다** (WHK 결정 2026-09-19). `check-catalog.js` 를 건드리지 않는다. 이름이 `verify-` 라 발견 규칙(`/^check-.*\.js$/`)에 안 걸리므로 등록 없이도 `npm run check` 가 죽지 않는다 — 그것을 이 Step 에서 확인한다.

Run: `cd ~/hermes/code && npm run check`
Expected: 전체 통과. **`Unregistered node check` 로 죽지 않아야 한다** — 죽으면 파일 이름이 `check-` 로 시작하는 것이니 이름을 고친다.

그리고 **손으로 돌릴 자리를 아는 사람만 아는 상태로 두지 않는다.** `~/hermes/code/src/llm/provider.js` 의 머리 주석(`client 는 지연 생성 함수로…` 줄 **뒤**)에 두 줄 더한다:

```js
 *
 * **이 파일은 Clio 저장소와 바이트가 같아야 한다** (clio/src/llm/provider.js). 고쳤으면
 * 양쪽에 같이 옮기고 `node scripts/verify-shared-with-clio.js` 로 확인한다 — 상시 검사에는
 * 안 들어간다(Clio 가 없는 설치가 대부분이라). 자동으로 지키는 자리는 Clio 의 npm test 다.
```

**이 두 줄을 더하면 Clio 판과 바이트가 달라진다.** 그러니 이 Step 뒤에 곧바로 다시 복사한다:

```
cp ~/hermes/code/src/llm/provider.js ~/clio/src/llm/provider.js
```

- [ ] **Step 6: 전체 검사**

Run: `cd ~/hermes/code && node scripts/verify-shared-with-clio.js`
Expected: `OK — 공유 파일 12개가 Clio 와 같습니다` (Step 5 의 주석 추가 뒤 다시 복사했으므로 같아야 한다)

Run: `cd ~/hermes/code && npm run check`
Expected: 전체 통과

Run: `cd ~/clio && npm test`
Expected: 전체 통과

- [ ] **Step 7: 커밋 승인 요청 (두 저장소 — 직접 커밋 금지)**

**이 Task 는 두 저장소를 다 건드린다** — Hermes 에 검사와 주석, Clio 에 주석이 옮겨 간 `provider.js`. 각각 따로 승인받는다. 보고에 Step 3·4 의 출력을 포함한다. 커밋 제목 제안:
- Hermes: `test(공유): 두 저장소가 나눠 쓰는 파일의 어긋남을 고치는 쪽에서 본다`
- Clio: `chore(모델): 공유 파일 주석을 Hermes 와 맞춘다`

---

### Task 4: Clio 기준판 대조를 만든다 (시험만 고친다 — `src/` 무변화)

증명 수단을 먼저 세운다. Hermes 에는 `--baseline <sha>` 가 있지만 **Clio 에는 없다.** 이 Task 가 끝나면 「지금 `agent.js` 와 sha 의 `agent.js` 가 같은 입력에 같게 움직이나」를 명령 한 줄로 잴 수 있다.

**Files:**
- Create: `~/clio/tests/agent-baseline.test.js`

**Interfaces:**
- Consumes: 없음
- Produces: `node --test tests/agent-baseline.test.js` — 시나리오마다 현재 판과 기준판의 trace 를 대조한다. Task 4 가 이것으로 무변화를 증명한다.

- [ ] **Step 1: 기준판 sha 를 확정한다**

Run: `git -C ~/clio rev-parse 43a3e07`
40자 sha 를 받아 적는다. 아래 코드의 `BASELINE` 에 그 값을 넣는다 — **`43a3e07` 같은 짧은 형태로 두지 않는다** (짧은 sha 는 저장소가 커지면 모호해질 수 있다).

- [ ] **Step 2: 대조 시험을 쓴다**

`~/clio/tests/agent-baseline.test.js` 를 새로 만든다:

```js
/**
 * 기준판 대조 — 지금 agent.js 와 sha 의 agent.js 가 같은 입력에 같게 움직이나.
 *
 * 모델 어댑터 이전(P0-4)의 무변화를 재는 자리다. 진입점만 옛 판으로 갈면 옛 배선이
 * 넘기는 의존성 이름과 지금 모듈이 받는 이름이 어긋나 비교 자체가 성립하지 않고, 그 실패가
 * 「계약이 달라졌다」로 잘못 읽힌다. 그래서 파일 전체를 세우고 import 만 떼어 주입한다.
 *
 * **모델 API·Slack 에는 닿지 않지만 실물 아카이브는 읽는다** — systemPrompt(access) 가
 * 색인을 만들려고 디스크를 본다 (기존 agent.test.js 가 system 총량 1만 자를 단언하는 것이
 * 그 증거다). 두 판이 같은 순간 같은 파일을 읽으므로 대조는 성립한다. 아카이브가 없는
 * 컴퓨터에서는 색인이 비어 두 판 다 같게 비고, 그래도 대조는 성립한다 — 다만 그때는
 * 「색인이 실리는 자리」를 재지 못한다.
 *
 * 이전이 끝나면 이 파일은 **지운다** — 고정 sha 대조를 상시로 두면 agent.js 의 다음 정당한
 * 변경에서 빨개지고, 그때 sha 를 밀어 넣는 순간 아무것도 안 지키는 장식이 된다.
 * 계속 지켜야 할 것은 tests/agent.test.js 의 단언으로 옮긴다 (Task 6).
 */
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { compileFunction } from 'node:vm';
import { fileURLToPath } from 'node:url';

import * as archive from '../src/archive.js';
import * as documents from '../src/documents.js';
import { clio } from '../src/clio-config.js';
import { config, canSee } from '../src/config.js';
import { estimateCost, logUsage } from '../src/format.js';
import { detectPlace } from '../src/search-terms.js';
import { namesFromToolResult } from '../src/sources.js';
import { OWNER_ACCESS } from '../src/access.js';
import { draftDocument } from '../src/agent.js';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const BASELINE = '<CLIO_BASELINE 40자 sha 를 여기 적는다>';

/* sha 의 src/agent.js 를 그때 모양 그대로 세운다. import 는 떼어 주입한다 — 파일을 쓰지 않는다.
 * Anthropic 기본 클라이언트는 절대 안 만들어진다 (시나리오가 client 를 항상 꽂는다). */
function baselineDraftDocument() {
  const text = execFileSync('git', ['-C', ROOT, 'show', BASELINE + ':src/agent.js'], {
    encoding: 'utf8', maxBuffer: 1024 * 1024,
  }).replace(/\r\n/g, '\n')
    .replace(/^import [^;]*;$/gm, '')
    .replace(/^import \{[^}]*\} from [^;]*;$/gm, '')
    .replace(/^export (?=(?:async function|function|const) )/gm, '');
  assert.ok(!/^import\b/m.test(text), '기준판 agent.js 의 import 경계를 확인할 수 없습니다');
  const bindings = {
    Anthropic: function () { throw new Error('기준판이 실제 클라이언트를 만들려 했습니다'); },
    clio, config, canSee, estimateCost, logUsage, detectPlace, namesFromToolResult,
    buildArchiveBriefSplit: archive.buildArchiveBriefSplit,
    searchArchive: archive.searchArchive,
    readChannel: archive.readChannel,
    resolveChannel: archive.resolveChannel,
    listReadableChannels: archive.listReadableChannels,
    buildDocumentsBriefSplit: documents.buildDocumentsBriefSplit,
    searchDocuments: documents.searchDocuments,
    readDocument: documents.readDocument,
    hasDocuments: documents.hasDocuments,
    markArchivedAttachments: documents.markArchivedAttachments,
    resolveProject: documents.resolveProject,
    narrowableProjects: documents.narrowableProjects,
  };
  const evaluate = compileFunction(
    text + '\nreturn draftDocument;', Object.keys(bindings),
  );
  return evaluate(...Object.values(bindings));
}

/* 비용 로그 줄은 맨 앞이 시각이다 — `[2026-09-19T01:02:03.456Z] clio · …` (format.js 의
 * logUsage). **그 시각만 지우고 나머지 문구는 그대로 비교한다.** 현재판과 기준판을 몇 ms
 * 차이로 돌리므로 시각까지 비교하면 코드가 같아도 늘 빨개진다.
 *
 * **로그를 통째로 대조에서 빼지 않는다.** 빼는 것이 가장 쉬운 수리인데, 그러면 모델 이름과
 * 토큰 수가 조용히 틀려도 초록이 된다 — Hermes 에서 2026-09-19 에 막 닫은 구멍이 그것이고,
 * 여기서 빼면 그대로 재현된다. */
const unstamp = (s) => s.replace(/^\[\d{4}-\d\d-\d\dT[\d:.]+Z\]/, '[시각]');

/** 한 시나리오를 돌리고 **밖에서 보이는 것 전부**를 담는다 — 요청 바이트·반환·로그. */
async function replay(draft, scenario) {
  const trace = { runs: [], logs: [], result: null };
  const turns = [...scenario.turns];
  const client = { messages: { stream(params) {
    trace.runs.push(structuredClone(params));
    const value = turns.shift();
    assert.ok(value, '예상보다 많이 불렀습니다');
    return { finalMessage: async () => structuredClone(value) };
  } } };
  const realLog = console.log;
  console.log = (...args) => trace.logs.push(args.map((a) => unstamp(String(a))));
  try {
    trace.result = await draft({ request: '합성 요청', client, access: OWNER_ACCESS, ...scenario.opts });
  } finally {
    console.log = realLog;
  }
  assert.equal(turns.length, 0, '준비한 회차를 다 쓰지 않았습니다');
  return trace;
}

const turn = (stop, content) => ({
  id: 'msg_fixture', model: clio.model, role: 'assistant', stop_reason: stop, content,
  usage: { input_tokens: 100, output_tokens: 10 },
});
const save = (input) => turn('tool_use', [{ type: 'tool_use', id: 'toolu_s', name: 'save_draft', input }]);

const scenarios = [
  {
    name: 'save_draft 한 번에 끝난다',
    turns: [save({ title: '시험', markdown: '# 시험', formats: ['docx'], note: '' })],
  },
  {
    name: '도구 없이 글로 끝난다',
    turns: [turn('end_turn', [{ type: 'text', text: '어느 건인가요?' }])],
  },
  {
    name: '잘린 회차는 save_draft 가 있어도 잘림 안내다',
    turns: [{ ...save({ title: 'x', markdown: 'y' }), stop_reason: 'max_tokens' }],
  },
  /* 도구는 **일부러 없는 이름**으로 부른다. dispatch 가 `모르는 도구: …` 로 던지고
   * 루프가 그것을 `도구 오류: …` 문자열로 받아 다음 회차에 싣는다 (agent.js:530-533).
   * 진짜 도구를 부르면 실제 아카이브를 디스크에서 읽어 결과가 그날 자료에 따라 달라지고,
   * 대조가 「코드가 같나」가 아니라 「자료가 그대로인가」를 재게 된다. */
  {
    name: '도구를 한 번 돌고 저장한다 — 대화 잇기와 캐시 표시가 걸린 자리',
    turns: [
      turn('tool_use', [{ type: 'tool_use', id: 'toolu_1', name: '합성도구', input: {} }]),
      save({ title: '두 회차', markdown: '# 두 회차', formats: [], note: '' }),
    ],
  },
  {
    name: '도구를 두 번 돌면 직전 캐시 표시가 지워진다',
    turns: [
      turn('tool_use', [{ type: 'tool_use', id: 'toolu_1', name: '합성도구', input: {} }]),
      turn('tool_use', [{ type: 'tool_use', id: 'toolu_2', name: '합성도구', input: {} }]),
      save({ title: '세 회차', markdown: '# 세 회차', formats: [], note: '' }),
    ],
  },
  {
    name: '한 회차가 도구를 둘 부르면 표시는 마지막 결과에만 붙는다',
    turns: [
      turn('tool_use', [
        { type: 'tool_use', id: 'toolu_a', name: '합성도구', input: {} },
        { type: 'tool_use', id: 'toolu_b', name: '합성도구', input: {} },
      ]),
      save({ title: '둘', markdown: '# 둘', formats: [], note: '' }),
    ],
  },
  /* 본문 앞뒤 공백 — 창구의 normalize 는 trim 하고 옛 Clio 는 안 했다. Task 4 가 옛 추출을
   * 유지하므로 **이 둘이 초록이어야 맞다.** res.text 로 갈아 끼우면 여기가 빨개진다. */
  {
    name: '앞뒤 공백이 있는 답은 공백까지 그대로 나간다',
    turns: [turn('end_turn', [{ type: 'text', text: '  앞뒤 공백  ' }])],
  },
  {
    name: '공백만 있는 답은 빈 응답으로 바뀌지 않는다',
    turns: [turn('end_turn', [{ type: 'text', text: '   ' }])],
  },
  /* 도구 왕복 상한(MAX_TURNS=24)까지 다 쓰고 나오는 출구. 24회차 배열을 손으로 적지 않는다 —
   * 상한이 바뀌면 이 시나리오가 조용히 다른 것을 재게 된다. */
  {
    name: '도구 왕복 상한을 다 쓰면 안내로 끝난다',
    turns: Array.from({ length: 24 }, () =>
      turn('tool_use', [{ type: 'tool_use', id: 'toolu_n', name: '합성도구', input: {} }])),
  },
];

test('현재 판이 기준판과 같게 움직인다', async () => {
  const baseline = baselineDraftDocument();
  for (const scenario of scenarios) {
    const now = await replay(draftDocument, scenario);
    const was = await replay(baseline, scenario);
    assert.deepEqual(now, was, scenario.name + ': 기준판 trace');
  }
});
```

- [ ] **Step 3: 현재 판이 그대로 통과하는지 확인**

Run: `cd ~/clio && node --test tests/agent-baseline.test.js`
Expected: PASS. 지금은 현재 판과 기준판이 **같은 코드**이므로 당연히 맞아야 한다 — 여기서 다르면 로더가 잘못 세운 것이니 멈추고 보고한다.

**`합성도구` 라는 이름이 실제 `TOOLS` 에 없어야 한다** (지금 있는 것은 `search_archive`·`read_channel`·`search_documents`·`read_document`·`save_draft` 다섯뿐이다). 확인: `grep -n "name: '" ~/clio/src/agent.js`. 만약 이름이 겹치면 겹치지 않는 다른 이름으로 바꾼다 — 겹치면 진짜 도구가 돌아 디스크를 읽는다.

- [ ] **Step 4: 대조가 공허하지 않은지 확인한다 (비공허성)**

**고를 때 조심할 것이 있다.** `trace` 는 요청 바이트·반환·로그를 다 담으므로, **아무 값이나 망가뜨려도 빨개진다.** 여기서 확인할 것은 「기준판이 정말 옛 코드를 돌고 있나」이므로, **기준판에만 있고 현재 판에는 없을 수 없는 값**이 아니라 **둘 다 도는지**를 본다.

편집 도구로 고쳤다가 편집 도구로 되돌린다 — git 명령을 쓰지 않는다.

1. `~/clio/src/agent.js` 의 `MAX_TURNS` 를 `24` 에서 `2` 로 바꾼다.
2. Run: `cd ~/clio && node --test tests/agent-baseline.test.js` → **실패해야 한다** (다섯째 시나리오가 세 회차를 못 돈다). 실패 출력을 기록한다.
3. 되돌린다.
4. Run: `cd ~/clio && git diff --stat src/` → **아무것도 안 나와야 한다.**

2번이 통과해 버리면 기준판이 현재 코드를 돌고 있다는 뜻이다 — 멈추고 보고한다.

- [ ] **Step 5: Clio 전체 시험**

Run: `cd ~/clio && npm test`
Expected: 전체 통과

- [ ] **Step 6: 커밋 승인 요청 (Clio 저장소 — 직접 커밋 금지)**

보고에 Step 3·4 의 출력과 `git -C ~/clio diff --stat src/` 가 비었다는 사실을 포함한다. 커밋 제목 제안:
`test(회차): 기준판 대조를 세운다 — 어댑터 이전 전에 잴 수단을 먼저 만든다`

---

### Task 5: Clio 루프를 수동 루프 창구로 이전 + 기준판 무변화 증명

**Files:**
- Modify: `~/clio/src/agent.js`
- Modify: `~/clio/clio.example.json` (설정 예시에 `provider` 한 줄)

**Interfaces:**
- Consumes: Task 2 의 `~/clio/src/llm/provider.js` 의 `createToolLoop`, Task 4 의 대조 시험
- Produces: `draftDocument({ request, form, priorDraft, client, access })` — 인자·반환 모양 **불변**. 안에서만 창구를 탄다.

- [ ] **Step 1: import 를 바꾼다**

`~/clio/src/agent.js` 9행의 `import Anthropic from '@anthropic-ai/sdk';` 는 **그대로 둔다** — `defaultClient()` 가 아직 쓴다. 그 아래 import 무리 맨 끝(21행 `namesFromToolResult` 줄 뒤)에 더한다:

```js
import { createToolLoop } from './llm/provider.js';
```

- [ ] **Step 2: 루프를 창구로 바꾼다**

`~/clio/src/agent.js` 의 `for (let turn = 0; turn < MAX_TURNS; turn++) {` 부터 그 블록 끝(`messages.push({ role: 'user', content: toolResults });` 다음 `}`)까지를 바꾼다.

**바꾸는 것은 세 곳뿐이다** — ① 모델을 부르는 자리 ② 도구 결과를 붙이는 자리 ③ `res` 를 읽는 이름. 그 사이의 판정(잘림·종결 도구·빈 답·도구 실행·`meta` 집계)은 **한 글자도 안 건드린다.**

`const messages = [...]` (453행) **뒤**, `const meta = {...}` **앞**에 창구를 연다:

```js
  const loop = createToolLoop(
    { id: clio.model, maxTokens: 64000, effort: clio.effort, provider: clio.provider },
    { client: () => api },
  ).open({ system: systemPrompt(access), tools: TOOLS, messages });
```

**`systemPrompt(access)` 를 부르는 횟수가 달라진다 — 그리고 대조는 이 변화를 못 잰다.** 옛 코드는 `for` 안에서 회차마다 다시 불렀고, 위 코드는 `open()` 때 한 번만 부른다.

`systemPrompt` 는 순수 함수가 아니라 **디스크 상태의 함수**다 — `buildArchiveBriefSplit` 이 `config.js` 의 `readCached`(mtime 기반 파일 캐시)를 타고 채널 이름 캐시도 60초짜리다. 시험 프로세스 안에서는 파일이 안 바뀌므로 **회차마다 불러도 한 번만 불러도 같은 바이트가 나오고, 대조는 원리적으로 초록이다.** 그러니 **초록을 「한 번만 불러도 된다」의 증명으로 읽지 말 것** — 그것은 「시험 도는 동안 디스크가 안 바뀌었다」는 뜻뿐이다.

실제로 갈리는 자리는 운영이다: VM 이 15분마다 자료 저장소를 자동으로 받아오므로, 긴 초안 작업 도중 색인이 갱신되면 **옛 코드는 그 회차부터 새 색인을 보내고 새 코드는 `open()` 시점 것으로 고정**한다. 한 요청 안에서 색인이 고정되는 쪽이 캐시에도 유리하고 답도 일관되므로 **개선으로 보이지만, 이 계획이 스스로 증명할 수 없는 변화다.** 그래서 이것은 **승인받을 항목**으로 올린다 — Task 5 Step 9 보고에 「한 요청 안에서 색인이 고정된다」를 적어 확인받는다. 무변화가 꼭 필요하다는 판단이 나오면 `open()` 밖에서 회차마다 `systemPrompt(access)` 를 다시 불러 `req.system` 을 갱신하는 길이 있다 — 그때는 창구에 `system` 을 회차마다 받는 자리가 필요해진다.

`let cacheMarked = null;` 과 그 위 주석 두 줄을 **지운다** — 표시 옮기기가 창구로 갔다.

`for` 루프 안에서 `const stream = api.messages.stream({...});` 부터 `const res = await stream.finalMessage();` 까지(그 위 주석 두 줄 포함)를 다음으로 바꾼다:

```js
    const res = await loop.next();
```

`res.usage`·`res.stop_reason`·`res.content` 를 읽는 자리를 창구 이름으로 바꾼다:

- `if (res.usage) {` → `if (res.usage) {` — **그대로** (usage 는 회사 원문 그대로 통과한다)
- `if (res.stop_reason === 'max_tokens')` → `if (res.stopReason === 'max_tokens')`
- `const toolUses = res.content.filter((b) => b.type === 'tool_use');` → `const toolUses = res.toolUses;`
- `if (res.stop_reason !== 'tool_use')` → `if (res.stopReason !== 'tool_use')`
- 그 안의 본문 추출 한 줄은 **`res.text` 로 바꾸지 않는다.** `res.content` 만 `res.raw.content` 로 갈고 나머지는 그대로 둔다:

```js
      const text = res.raw.content.filter((b) => b.type === 'text').map((b) => b.text).join('\n');
```

**`res.text` 를 쓰면 동작이 달라진다.** 창구의 `normalize`(`provider.js:45`)는 `.trim()` 을 하는데 옛 Clio 는 안 했다. 앞뒤 공백이 있는 답은 바이트가 달라지고, **공백만 있는 답은 옛 코드에서 그대로 나가지만 새 코드에서는 `''` 가 되어 `'(빈 응답)'` 으로 갈래가 바뀐다.** 이 계획의 하드 룰이 「반환 필드가 수정 전과 동일」이므로 여기서는 옛 추출을 유지한다. `res.text` 로 가는 것(=답변 앞뒤 공백을 떼는 것)은 그 자체로는 나아 보이지만 **눈 뜨고 따로 결정할 한 줄**이고, 이 계획의 diff 에 섞으면 무변화 증명이 읽기 어려워진다. 「이 계획이 남기는 것」에 적어 뒀다.

**`toolUses` 의 모양이 달라진다 — 여기가 이 Task 에서 가장 조용히 틀릴 수 있는 자리다.** 옛 코드의 `toolUses` 는 회사 원문 블록(`{type,id,name,input}`)이고, 창구가 주는 것은 `{id,name,input}` 이다. `type` 이 빠진다. 지금 `.type` 을 읽는 자리는 502·512행 둘인데, **502행은 `res.toolUses` 로 바뀌어 없어지고 512행은 `res.raw.content` 로 남는다** — 원문 블록을 읽으므로 `type` 이 그대로 있다. 나머지 — `toolUses.find((b) => b.name === 'save_draft')` 와 `tu.id`·`tu.name`·`tu.input` — 은 **그대로 돈다.**

확인: 이 Step 을 끝낸 뒤 `grep -n "\.type" ~/clio/src/agent.js` 를 돌리면 **512행 하나만** 나와야 하고 그 줄은 `res.raw.content` 를 읽고 있어야 한다. 다른 줄이 남아 있으면 `toolUses` 원소에서 없어진 칸을 읽는 자리이니 멈추고 보고한다.

`messages.push({ role: 'assistant', content: res.content });` (516행)를 **지운다** — 창구가 한다.

루프 맨 끝의 네 줄

```js
    if (cacheMarked) delete cacheMarked.cache_control;
    cacheMarked = toolResults[toolResults.length - 1];
    cacheMarked.cache_control = { type: 'ephemeral' };
    messages.push({ role: 'user', content: toolResults });
```

을 한 줄로 바꾼다:

```js
    loop.continueWith(res, toolResults);
```

- [ ] **Step 3: `meta` 집계는 그대로 둔다**

`meta.turns`·`meta.usage`·`meta.costUsd`·`logUsage`·`meta.toolCalls`·`meta.seenNames` 는 **한 글자도 안 건드린다.** 회차 지출 집계는 이 자리의 회계이고, `res.usage` 가 회사 원문 그대로 오므로 계산이 같다.

확인: `grep -n "stop_reason\|api\.messages\|cacheMarked" ~/clio/src/agent.js` 를 돌리면 **아무것도 안 나와야 한다.** 지금 걸리는 자리는 469·474·495·511·554-556행이고 이 Task 가 그 전부를 없앤다. (`defaultClient`(24행)는 이 셋 중 무엇에도 안 걸리고 `api` 변수는 계속 쓰이므로 **지우지 않는다** — 창구에 `client: () => api` 로 넘어간다.)

- [ ] **Step 4: 설정에 `provider` 를 적을 수 있다고 알린다**

`~/clio/clio.example.json` 의 `"model"` 줄 **바로 아래**에 한 줄 더한다 (Hermes 의 `1bcbceb` 과 같은 성격이다):

```json
  "provider": "anthropic",
```

`~/clio/src/clio-config.js` 의 기본값에도 같은 줄을 더한다 — `model: 'claude-opus-5',` 줄 바로 아래:

```js
  provider: 'anthropic',
```

**`provider` 를 안 적어도 돌아야 한다.** `createModel` 이 `spec.provider || 'anthropic'` 이라 `undefined` 도 안전하지만, 설정 기본값에 적어 두면 「여기를 바꾸면 되는구나」가 보인다.

- [ ] **Step 5: 무변화 증명**

Run: `cd ~/clio && node --test tests/agent-baseline.test.js`
Expected: 시나리오 여섯 개 전부 통과 + 기준판 trace 일치

**다르면 멈추고 차이를 그대로 보고한다 — 시험을 고쳐 맞추지 않는다.** 시나리오의 기대값을 새 동작에 맞추는 것이 이 Task 에서 할 수 있는 가장 나쁜 일이다. 그렇게 하면 무변화 증명이 「무변화라고 적어 둔 문서」로 바뀐다.

**특히 볼 것:** `trace.runs` 의 `messages` 가 회차마다 **바이트 단위로** 같아야 한다. 캐시 표시(`cache_control`)가 붙는 블록이 옛 코드와 같은 자리인지가 여기서 갈린다.

- [ ] **Step 6: 증명이 공허하지 않은지 확인**

기준판 배선을 무력화해 본다. 편집 도구로 고쳤다가 편집 도구로 되돌린다.

1. Task 4 의 `bindings` 에서 `clio,` 를 지운다 (기준판이 받을 의존성을 뺀다).
2. Run: `cd ~/clio && node --test tests/agent-baseline.test.js` → **실패해야 한다.** 옛 `agent.js` 는 `clio.model` 을 읽으므로 그 자리에서 죽는다. 실패 출력을 기록한다.
3. 되돌린다.

**통과해 버리면** 기준판이 옛 모듈을 안 돌고 있다는 뜻이다 — 멈추고 보고한다.

- [ ] **Step 7: 주변 시험 회귀**

Run: `cd ~/clio && npm test`
Expected: 전체 통과. 특히 `tests/agent.test.js` 의 출구 넷(`save_draft` → draft · 도구 없음 → reply · `max_tokens` → 잘림 안내 · 상한 → 안내)과 `tests/shared-read-layer.test.js` 가 그대로 통과해야 한다.

`tests/agent.test.js` 의 `fakeClient` 는 `messages.stream().finalMessage()` 를 흉내 내므로 **그대로 돈다** — 창구도 같은 SDK 진입점을 쓴다. 여기서 빨개지면 창구가 다른 것을 부르고 있다는 뜻이다.

- [ ] **Step 8: Hermes 쪽이 안 흔들렸는지 확인**

Run: `cd ~/hermes/code && npm run check`
Expected: 전체 통과. 이 Task 는 Hermes 를 안 건드렸지만, `shared-read-layer` 가 두 저장소를 잇고 있으므로 한 번 본다.

- [ ] **Step 9: 커밋 승인 요청 (Clio 저장소 — 직접 커밋 금지)**

보고에 Step 5 의 대조 결과와 Step 6 의 실패 출력을 포함한다. 그리고 **대조가 증명하지 못한 것 하나를 따로 적어 확인받는다:**

> 「`systemPrompt(access)` 를 한 요청에 한 번만 부르게 됐습니다. 옛 코드는 회차마다 다시 불렀습니다. 시험은 이 차이를 못 잽니다(디스크가 안 바뀌어서). 운영에서는 긴 초안 작업 도중 자료가 갱신되면 갈립니다 — 한 요청 안에서 색인이 고정되는 쪽이라 캐시에 유리하고 답도 일관됩니다. 이대로 가도 될까요?」

커밋 제목 제안:
`refactor(회차): 도구 루프를 제공사 창구로 옮기고 무변화를 기준판으로 증명한다`

**이 Task 를 커밋했으면 Task 6 을 같은 세션에서 이어서 한다** — 기준판 시험을 `npm test` 에 남겨 둔 채 끝내지 않는다.

---

### Task 6: 기준판 시험을 걷어내고, 지킬 것은 상시 시험으로 옮긴다

**이 Task 를 「나중에」로 미루지 않는다.** `tests/agent-baseline.test.js` 는 고정 sha 를 보므로 `npm test` 에 그대로 두면 **`agent.js` 의 다음 정당한 변경에서 빨개진다.** 그때 사람이 하는 일은 sha 를 그날 것으로 밀어 넣는 것이고, 그 순간 대조는 아무것도 안 지키는 장식이 된다 — P0-3 이 같은 자리에서 적어 둔 원칙이다.

**Files:**
- Delete: `~/clio/tests/agent-baseline.test.js`
- Modify: `~/clio/tests/agent.test.js`

**Interfaces:**
- Consumes: Task 5 가 끝나 무변화가 증명된 상태
- Produces: 없음 — 상시 시험만 남는다

- [ ] **Step 1: 기준판 시험에만 있던 단언 셋을 상시 시험으로 옮긴다**

기준판 대조가 재던 것 중 **이전이 끝나도 계속 지켜야 하는 것**은 셋이다. `~/clio/tests/agent.test.js` 에 더한다 (기존 `fakeClient` 옆, 파일 맨 끝):

```js
/* 아래 셋은 기준판 대조(agent-baseline.test.js, P0-4 에서 쓰고 지웠다)가 재던 것을 옮겨 온
 * 것이다. 어댑터 이전이 끝나도 계속 지켜야 하는 성질이라, 고정 sha 에 매달지 않고 여기 둔다. */

/** 회차 목록을 순서대로 흘리고 보낸 요청을 담는 가짜. */
function recordingClient(turns) {
  const runs = [];
  const rest = [...turns];
  return { runs, client: { messages: { stream(params) {
    runs.push(structuredClone(params));
    const value = rest.shift();
    assert.ok(value, '예상보다 많이 불렀습니다');
    return { finalMessage: async () => structuredClone(value) };
  } } } };
}
const rec = (stop, content) => ({
  id: 'msg_fixture', model: clio.model, role: 'assistant', stop_reason: stop, content,
  usage: { input_tokens: 100, output_tokens: 10 },
});

test('캐시 표시는 마지막 도구 결과 하나에만 붙고 직전 것은 지워진다', async () => {
  const toolTurn = (id) => rec('tool_use', [{ type: 'tool_use', id, name: '합성도구', input: {} }]);
  const { runs, client } = recordingClient([toolTurn('t1'), toolTurn('t2'),
    rec('tool_use', [{ type: 'tool_use', id: 's', name: 'save_draft', input: { title: 'x', markdown: 'y' } }])]);
  await draftDocument({ request: '합성', client, access: OWNER_ACCESS });
  const marks = runs.at(-1).messages
    .flatMap((m) => (Array.isArray(m.content) ? m.content : []))
    .filter((b) => b.cache_control).length;
  assert.equal(marks, 1, '표시가 쌓이면 한 요청 4개 상한에 걸려 400 이 납니다');
  assert.deepEqual(runs.at(-1).messages.at(-1).content.at(-1).cache_control, { type: 'ephemeral' });
});

test('요청에는 betas·diagnostics 가 실리지 않는다', async () => {
  // Q&A 창구(도구 왕복)는 저 둘을 늘 보낸다. 수동 루프 창구가 같은 파일에 있으므로
  // 실수로 섞이면 요청 바이트가 달라지는데 답은 멀쩡히 나와 눈에 안 띈다.
  const { runs, client } = recordingClient([rec('end_turn', [{ type: 'text', text: 'x' }])]);
  await draftDocument({ request: '합성', client, access: OWNER_ACCESS });
  assert.equal('betas' in runs[0], false);
  assert.equal('diagnostics' in runs[0], false);
  assert.equal(runs[0].max_tokens, 64000);
  assert.deepEqual(runs[0].output_config, { effort: clio.effort });
});

test('답변 본문은 앞뒤 공백을 그대로 둔다', async () => {
  // 창구의 normalize 는 trim 하지만 이 자리는 원문 블록에서 직접 뽑는다 (P0-4 에서 정한 것).
  // res.text 로 갈아 끼우면 여기가 빨개진다 — 그때는 눈 뜨고 결정할 일이다.
  const { client } = recordingClient([rec('end_turn', [{ type: 'text', text: '  앞뒤  ' }])]);
  const r = await draftDocument({ request: '합성', client, access: OWNER_ACCESS });
  assert.equal(r.text, '  앞뒤  ');
});
```

`clio` 와 `structuredClone` 이 그 파일에 이미 있는지 확인한다 — `clio` 가 없으면 `import { clio } from '../src/clio-config.js';` 를 상단에 더한다. `assert`·`draftDocument`·`OWNER_ACCESS` 는 이미 있다.

- [ ] **Step 2: 새 단언이 비공허한지 확인한다**

편집 도구로 고쳤다가 되돌린다. 세 시험이 각각 다른 것을 재는지 하나씩 본다.

1. `~/clio/src/llm/provider.js` 의 `anthropicToolLoop` 안 `if (marked) delete marked.cache_control;` 를 지운다 → 첫 시험이 **빨개져야** 한다 (`marks` 가 2). 되돌린다.
2. 같은 함수의 `messages: convo,` 위에 `betas: ['x'],` 를 더한다 → 둘째 시험이 **빨개져야** 한다. 되돌린다.
3. `~/clio/src/agent.js` 의 본문 추출을 `res.text` 로 바꾼다 → 셋째 시험이 **빨개져야** 한다. 되돌린다.

셋 중 하나라도 초록이면 그 단언은 아무것도 안 지키는 것이니 멈추고 보고한다.

- [ ] **Step 3: 기준판 시험을 지운다**

Run: `rm ~/clio/tests/agent-baseline.test.js`

**지우는 것이 맞다.** 남겨 두면 다음 사람이 sha 를 밀어 넣어 살리고, 그때부터 그 파일은 「대조하고 있다」고 말하면서 아무것도 대조하지 않는다. 다시 필요해지면 이 계획서의 Task 4 를 보고 그때의 sha 로 새로 세운다 — 그것이 원래 쓰임이다.

- [ ] **Step 4: 전체 시험**

Run: `cd ~/clio && npm test`
Expected: 전체 통과. `agent-baseline.test.js` 가 목록에서 사라지고 `agent.test.js` 가 세 건 늘어야 한다.

- [ ] **Step 5: 커밋 승인 요청 (Clio 저장소 — 직접 커밋 금지)**

보고에 Step 2 의 실패 출력 셋을 포함한다. 커밋 제목 제안:
`test(회차): 기준판 대조를 걷고 지킬 것은 상시 시험으로 옮긴다`

---

## 이 계획이 남기는 것

- **Clio 의 지출 집계는 원장(`createUsageCollector`)을 안 쓴다.** `meta.usage` 에 손으로 더하고 `estimateCost` 를 직접 부른다 — Hermes 는 P0-1~P0-3 에서 원장으로 갔지만 Clio 는 그대로다. 같은 병(중복 id 를 두 번 세기·실패 시도를 안 세기)이 남아 있을 수 있는데, **이 계획의 diff 에 섞으면 무변화 증명이 읽기 어려워진다.** 이전이 끝난 뒤 별건으로 본다.
- **`clio.json` 의 `provider` 는 적을 수만 있고 갈아 끼울 타사 구현이 아직 없다** — `anthropic` 말고 다른 값을 넣으면 던진다. 타사 구현은 P3 이다.
- **답변 본문의 앞뒤 공백을 떼는 한 줄** — Clio 는 `res.raw.content` 에서 직접 뽑아 옛 동작을 지킨다. 창구의 `res.text`(=`.trim()` 한 것)로 가는 것이 아마 낫지만, **공백만 있는 답이 `'(빈 응답)'` 으로 갈래가 바뀌는 변화**라 무변화 증명과 같은 diff 에 넣지 않았다. 눈 뜨고 따로 결정할 한 줄이다.
- **빈 도구 결과에서 죽는 것도 그대로 뒀다** — `stop_reason` 이 `tool_use` 인데 `tool_use` 블록이 0개면 옛 Clio 는 TypeError 로 죽었고, 창구도 똑같이 죽게 뒀다(검사 ㉓ 이 그것을 못박는다). 실전에서 닿기 어려운 코너지만 **고칠 값어치는 있다** — 죽는 대신 「도구를 안 불렀다」로 읽고 최종 답으로 처리하는 쪽이 맞아 보인다. 별건이다.
- **`meta` 의 `seenNames`·`narrowedTo`·`widenedFrom` 은 이번 대조가 사실상 안 쟀다** — 시나리오가 일부러 없는 도구를 불러 `dispatch` 가 던지므로 그 칸들이 양쪽 다 빈 채로 비교된다. 이전이 그 코드를 안 건드리므로 위험은 낮지만, **「meta 무변화를 증명했다」가 아니라 「빈 meta 가 같음을 확인했다」**가 정확한 말이다. 진짜로 재려면 `dispatch` 를 주입 가능하게 해야 하고 그건 별개 손질이다.
- **`--baseline` 대조는 Hermes 쪽도 상시 검사 밖이다** (`check-runner.js:104` 가 인자 없이 돌린다). Clio 쪽은 Task 6 이 걷어낸다. 양쪽 다 **이전할 때 손으로 한 번 돌리는 것**이고, 계속 지킬 것은 상시 시험의 단언으로 옮긴다.

## 후속 계획 (이 계획 범위 밖)

P1(B2 Q&A 비상 모드 · B1' 가짜 발견 거르기). 설계 원본은 소유자 워크스페이스의 2026-09-18 「모델교체-준비 설계」 기록 문서다 — 이 저장소에는 경로를 적지 않는다.

**P2(섀도 대조)와 C1'(백필)은 WHK 가 켜는 시점을 정한다** — 둘 다 요금이 실제로 는다. B3(digest 비상 모드)도 WHK 결정 대기다.
