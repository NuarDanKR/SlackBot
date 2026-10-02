#!/usr/bin/env node
/**
 * 모델 제공사 창구(provider.js)의 오프라인 계약 검사.
 * 가짜 클라이언트만 쓰고 네트워크·파일·Slack 에 닿지 않는다.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { APIConnectionError } from '@anthropic-ai/sdk';
import { createTextModel, createJsonModel, createToolSession, createToolLoop } from '../src/llm/provider.js';

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

/* ③-2 중단 사유 표의 **전체 계약**.
 *
 * 위 ③·⑦ 은 표에 **있는** 세 사유와 없는 사유 하나(`tool_use`)를 잰다. 그것만으로는
 * 표에 새 줄이 느는 것을 아무도 못 본다 — `pause_turn: 'ok'` 를 더해도 `tool_use` 는
 * 여전히 원문 그대로 나가서 전부 초록이다. 그런데 새 사유를 `ok` 로 옮기는 것이 바로
 * 잘린 답이 완성본으로 나가는 길이고, 이제 세 창구가 같은 표를 쓰므로 한 줄이 세 자리로
 * 번진다. 그래서 「표에 무엇이 있나」를 통째로 못박는다 — 늘어도 빠져도 바뀌어도 빨개진다.
 *
 * 표를 **소스에서 읽는다** — 창구를 통해 재는 것만으로는 「없는 사유는 원문 유지」라는
 * 성질 때문에 새 줄을 밖에서 셀 방법이 없다(모든 문자열을 다 넣어 볼 수는 없다). */
const STOP_MAP_CONTRACT = { end_turn: 'ok', max_tokens: 'max_tokens', refusal: 'refusal' };
{
  const src = fs.readFileSync(new URL('../src/llm/provider.js', import.meta.url), 'utf8');
  const literal = src.match(/const STOP_MAP = (\{[^}]*\})\s*;/);
  assert.ok(literal, 'provider.js 에서 STOP_MAP 리터럴을 찾지 못했습니다');
  const actual = new Function('return ' + literal[1])();
  assert.deepEqual(actual, STOP_MAP_CONTRACT,
    '중단 사유 표가 바뀌었습니다 — 새 사유를 정규화하려면 이 계약을 함께 고치고, '
    + "특히 'ok' 로 옮기는 것은 잘린 응답이 완성본으로 나갈 수 있는지 먼저 따져야 합니다");
  // 그리고 그 표가 **실제로 세 창구의 동작을 지배하는지**도 같은 자리에서 본다 —
  // 리터럴만 보면 normalize 가 표를 안 쓰게 바뀌어도 초록이다.
  for (const [given, want] of Object.entries(STOP_MAP_CONTRACT)) {
    const { client } = fakeClient({ model: 'm', stop_reason: given, content: [], usage: {} });
    const res = await createTextModel(SPEC, { client }).generate({ system: SYSTEM, messages: MESSAGES });
    assert.equal(res.stopReason, want, `표에 있는 사유 ${given} 이 ${want} 로 정규화돼야 합니다`);
  }
  /* 표에 **없는** 사유는 원문 그대로 나가야 한다 — 이름을 대 놓고 하나씩 잰다.
   *
   * 리터럴만 못박으면 표를 그대로 두고 `normalize` 안에서 특정 사유만 따로 'ok' 로
   * 돌리는 수정이 통과한다 (2026-09-19 대조 검증에서 `pause_turn` 으로 실제로 통과했다).
   * 아래 목록은 회사가 실제로 더할 법한 사유들이고, `tool_use` 하나만 재던 자리를 넓힌
   * 것이다. */
  for (const given of ['tool_use', 'pause_turn', 'stop_sequence', 'model_context_window_exceeded',
    'content_filter', 'length', 'timeout', 'error', 'incomplete', 'unknown_future_reason']) {
    assert.ok(!Object.hasOwn(STOP_MAP_CONTRACT, given), `${given} 은 표에 없는 사유여야 합니다`);
    const { client } = fakeClient({ model: 'm', stop_reason: given, content: [], usage: {} });
    const res = await createTextModel(SPEC, { client }).generate({ system: SYSTEM, messages: MESSAGES });
    assert.equal(res.stopReason, given,
      `표에 없는 사유 ${given} 은 원문 그대로 나가야 합니다 — 'ok' 로 옮기면 잘린 답이 완성본으로 나갑니다`);
  }
  /* 위 목록은 **이름을 아는 사유**만 막는다. 목록에 없는 이름으로 특수 처리를 넣으면
   * 그대로 통과한다 (2026-09-19 에 `budget_exceeded → 'ok'` 로 실제로 재현했다).
   * 밖에서 문자열을 넣어 보는 방식으로는 닫을 수 없다 — 모든 문자열을 넣어 볼 수 없다.
   *
   * 그래서 **normalize 가 사유를 만지는 줄 자체**를 못박는다. 이 한 줄 말고 다른 길이
   * 없으면 이름 모를 사유를 특수 처리할 자리가 normalize 안에 없다. */
  const normalizeBody = src.match(/function normalize\(raw\) \{([\s\S]*?)\n\}/);
  assert.ok(normalizeBody, 'provider.js 에서 normalize 본문을 찾지 못했습니다');
  const stopLines = normalizeBody[1].split('\n').filter((l) => l.includes('stop_reason'));
  assert.deepEqual(stopLines.map((l) => l.trim()),
    ['stopReason: STOP_MAP[raw.stop_reason] ?? raw.stop_reason,'],
    'normalize 가 중단 사유를 만지는 길은 표 하나뿐이어야 합니다 — 여기에 조건을 더하면 '
    + '표는 그대로인 채 특정 사유만 몰래 다른 값이 되고, 위의 이름 목록으로는 못 잡습니다');
}
/* ── 이 자리가 **못 잡는 것**을 적어 둔다 (2026-09-19).
 *
 * 위 셋이 닫는 것은 ① 표 리터럴이 늘거나 줄거나 바뀌는 것 ② **이름을 댄** 사유가
 * `normalize` 어디에서든 'ok' 로 옮겨지는 것 ③ **이름 모를** 사유를 `normalize` 안에서
 * 따로 돌리는 것 — ③ 은 밖에서 문자열을 넣어 보는 대신 그 함수가 사유를 만지는 줄을
 * 통째로 못박아 닫았다.
 *
 * 닫지 **못하는** 것: 특수 처리를 `normalize` **밖**으로 — 창구 안쪽이나 호출 자리로 —
 * 옮기면 여전히 초록으로 지나간다. 사유를 만지는 자리가 앞으로 늘어나면 그 자리도 같은
 * 방식으로 못박아야 한다. 그러니 새 중단 사유를 다루게 될 때는 이 검사가 통과했다는 것을
 * 근거로 삼지 말고, 무엇을 새로 만졌는지부터 보고 표에 먼저 더한다. */

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

// ── 도구 왕복 창구 (P0-3) ───────────────────────────────────────────
// 앞의 두 창구와 달리 세션이다. 여기서는 ① 요청 조립 ② 회차 정규화 ③ 대화 잇기(캐시 표시·
// 대조 기준)를 못박는다. 도구 실행 자체는 회사 실행기의 일이라 가짜 실행기로 「그대로
// 넘긴다」만 확인한다 — 실제 실행은 scripts/check-llm-qa.js 가 실물 SDK 로 잰다.

const TOOLS = [{ name: 'search', description: '검사용', input_schema: { type: 'object' } }];
const REQ = { system: SYSTEM, messages: MESSAGES, tools: TOOLS, maxIterations: 4, previousMessageId: null };

// toolRunner 를 흉내 내는 가짜. 회차 목록을 주면 순서대로 흘린다.
// trackReturn 을 주면 안쪽 이터레이터에 return() 을 심고, 불렸는지를 그 객체에 남긴다
// (Fix round 1 — 이터레이터 정리 위임을 잡는 자리가 필요해 추가).
function fakeRunnerClient(turns, { toolMessage = null, trackReturn = null } = {}) {
  const runs = [];
  const state = { params: null };
  const client = () => ({ beta: { messages: { toolRunner(params) {
    runs.push(clone(params));
    state.params = params;
    let i = 0;
    return {
      [Symbol.asyncIterator]() {
        return {
          next: async () => (i < turns.length
            ? { done: false, value: clone(turns[i++]) }
            : { done: true, value: undefined }),
          ...(trackReturn ? { return: async (v) => { trackReturn.called = true; return { done: true, value: v }; } } : {}),
        };
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

/* ⑫ 모양 판정은 **보내는 쪽 한 곳뿐**이다 (Fix round 1 로 바로잡은 자리).
 *
 * 회차가 내는 messageId 는 회사가 준 id **그대로**다. 받을 때 걸러 버리면 호출 자리가 들고
 * 있던 멀쩡한 기준을 모양 틀린 id 가 안 덮게 되고, 그러면 다음 질문이 낡은 기준을 보낸다 —
 * 요청 바이트와 반환 칸(cacheBaseline)이 둘 다 갈린다. 거르는 일은 open() 이 한다:
 * 요청의 diagnostics 와 세션이 내놓는 previousMessageId 두 곳에 그 걸러진 값이 나온다. */
for (const [given, want] of [['msg_ok', 'msg_ok'], ['not-a-message', 'not-a-message'], ['', ''], [undefined, null]]) {
  const { client } = fakeRunnerClient([{ ...turn('x'), id: given }]);
  const got = [];
  for await (const t of createToolSession(SPEC, { client }).open(REQ)) got.push(t);
  assert.equal(got[0].messageId, want, '회차의 messageId 는 회사가 준 id 를 거르지 않고 그대로 내야 한다');
}
for (const [given, sent] of [['msg_ok', 'msg_ok'], ['not-a-message', null], ['', null], [null, null], [undefined, null]]) {
  const { client, runs } = fakeRunnerClient([turn('msg_z')]);
  const s = createToolSession(SPEC, { client }).open({ ...REQ, previousMessageId: given });
  assert.deepEqual(runs[0].diagnostics, { previous_message_id: sent },
    '모양이 틀린 기준은 요청에 null 로 나가야 한다: ' + JSON.stringify(given));
  assert.equal(s.previousMessageId, sent,
    '세션은 자기가 실제로 보낸 기준을 되읽게 해야 한다: ' + JSON.stringify(given));
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

// ── Fix round 1 ─────────────────────────────────────────────────────
// 리뷰가 짚은 세 자리: ⑭ 는 rollCacheMarks 를 실제로 못 조인다(회차가 하나뿐이라 표시가
// 쌓일 틈이 없다), 이터레이터 return 위임은 안 잡힌다, 이어 붙는 assistant 메시지의 모양
// (role·content 두 칸뿐)도 안 잡힌다. 세 자리를 각각 못박는다.

// ⑯ 표시 상한 — 회차를 셋 몰아 앞선 표시가 두 개 이상 쌓이게 한 뒤, 세 번째 이어 붙이기
//    뒤의 표시 총합을 잰다. 회차가 하나뿐이면 rollCacheMarks 는 늘 무연산이라(표시가 최대
//    1개뿐이라 「가장 최근 것 하나만 남기기」가 그저 통과) ⑭ 만으로는 못 잡는다.
{
  const toolMessage = { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'toolu_x', content: '결과' }] };
  const turns3 = [
    turn('msg_r1', 'tool_use', [{ type: 'tool_use', id: 'toolu_x', name: 'search', input: {} }]),
    turn('msg_r2', 'tool_use', [{ type: 'tool_use', id: 'toolu_x', name: 'search', input: {} }]),
    turn('msg_r3', 'tool_use', [{ type: 'tool_use', id: 'toolu_x', name: 'search', input: {} }]),
  ];
  const { client, runs } = fakeRunnerClient(turns3, { toolMessage });
  const s = createToolSession(SPEC, { client }).open(REQ);
  for await (const t of s) {
    const tm = await s.generateToolResponse();
    s.continueWith(tm, t);
  }
  const last = runs.at(-1);
  const marks = last.messages
    .flatMap((m) => (Array.isArray(m.content) ? m.content : []))
    .filter((b) => b.cache_control).length;
  // 앞선 표시(직전 회차 하나) + 이번 새 표시 = 2. rollCacheMarks 를 빼면(=...p.messages) 세
  // 번째 이어 붙이기 시점에 앞선 표시가 이미 2개 쌓여 있어 안 깎이고 3개로 는다.
  assert.equal(marks, 2, '앞선 표시는 가장 최근 것 하나로 눌리고 새 표시 하나만 더해야 한다');
}

// ⑰ 이터레이터 정리 위임 — for-await 를 일찍 끝내면(break) 세션이 안쪽 이터레이터의
//    return() 을 불러야 한다. qa.js 의 observedMessages 가 finally 에서 iterator.return?.()
//    를 부르므로, 위임이 없으면 안쪽 toolRunner 가 자원을 정리할 기회를 못 받는다.
{
  const trackReturn = { called: false };
  const { client } = fakeRunnerClient([turn('msg_g'), turn('msg_h')], { trackReturn });
  const s = createToolSession(SPEC, { client }).open(REQ);
  for await (const t of s) {
    assert.equal(t.messageId, 'msg_g');
    break;
  }
  assert.equal(trackReturn.called, true, 'for-await 를 일찍 끝내면 안쪽 이터레이터의 return 이 위임돼야 한다');
}

// ⑱ 대화 잇기 모양 — 이어 붙는 assistant 메시지는 raw 전체가 아니라 role·content 두 칸뿐이어야
//    한다. raw 를 통째로 실으면 id·usage·stop_reason 이 섞여 나가는데, qa.js 는 오늘 그 두
//    칸만 보낸다 — ⑭ 는 메시지 개수만 세서 이 모양은 못 잡았다.
{
  const toolMessage = { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'toolu_2', content: '결과' }] };
  const assistantContent = [{ type: 'tool_use', id: 'toolu_2', name: 'search', input: {} }];
  const { client, runs } = fakeRunnerClient(
    [turn('msg_i', 'tool_use', assistantContent)],
    { toolMessage });
  const s = createToolSession(SPEC, { client }).open(REQ);
  for await (const t of s) {
    const tm = await s.generateToolResponse();
    s.continueWith(tm, t);
  }
  const next = runs.at(-1);
  assert.deepEqual(next.messages.at(-2), { role: 'assistant', content: assistantContent });
}

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

/* ㉕ **빈 provider 값은 생략이 아니다.**
 *
 * 오타(`anthorpic`)·대문자(`Anthropic`)·공백 낀 값은 ⑤⑨⑮㉔ 가 잡는데, 빈 문자열과 null 은
 * `spec.provider || 'anthropic'` 의 `||` 를 타고 **조용히 기본값으로 떨어졌다** (2026-09-19
 * 감사에서 8개 값 실측). 설정 파일에 `"provider": ""` 를 자리표시자로 남겨 두는 흔한 실수가
 * 약속된 「던지기」 대신 통과한다. 칸이 **아예 없는 것**만 생략으로 친다.
 *
 * 네 창구를 다 건다 — 갈래는 createModel 한 곳이지만, 창구가 늘 때 한쪽만 새는 것을 막는다. */
for (const [label, make] of [
  ['글', createTextModel], ['JSON', createJsonModel], ['도구 왕복', createToolSession], ['수동 루프', createToolLoop],
]) {
  for (const bad of ['', '   ', null, 0, false]) {
    assert.throws(() => make({ ...SPEC, provider: bad }, { client: () => ({}) }), /모르는 모델 제공사/,
      `${label} 창구: 빈 provider 값(${JSON.stringify(bad)})이 조용히 기본값으로 떨어졌습니다`);
  }
  assert.equal(make(SPEC, { client: () => ({}) }).provider, 'anthropic',
    `${label} 창구: provider 칸이 없는 것은 생략이라 기본값이어야 합니다`);
}

console.log('[check-llm-provider] OK — 글 창구 + JSON 창구 + 도구 왕복 창구(요청 조립·회차 정규화·id 모양·캐시 미스·대화 잇기·표시 상한·이터레이터 정리·이어붙임 모양) + 수동 루프 창구(요청 조립·회차 정규화·중단 사유 표·표시 옮기기)');
