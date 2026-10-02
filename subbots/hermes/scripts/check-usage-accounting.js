#!/usr/bin/env node
import { createSchedulerFailure } from '../src/scheduler/failure.js';
import { createSlackQuestion } from '../src/slack/question.js';
/**
 * Codex R1b regression tests. Synthetic inputs only; no live config, API, Slack,
 * archive writes, Git operations or disk fixtures. Actual production bodies are
 * evaluated with fake boundaries; independent assertions check accounting.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { syntheticPlan, validateLivePlan } from './check-live-llm.js';
import { createUsageCollector, summarizeUsage, usageFields, attachUsage } from '../src/llm/usage.js';
import { estimateCost, logUsage } from '../src/format.js';
import { createQuestionAnswerer } from '../src/llm/qa.js';
import { createDigestGenerator } from '../src/llm/digest.js';
import { createTextModel, createJsonModel, createToolSession } from '../src/llm/provider.js';
import { createSummaryChecker } from '../src/llm/summary-check.js';
import { createLogCodec } from '../src/convo-log/codec.js';
import { createLogStore } from '../src/convo-log/store.js';
import { createLogRenderer } from '../src/convo-log/render.js';
import { createLogStats } from '../src/convo-log/stats.js';

const silent = { log() {}, warn() {}, error() {} };
const source = (p) => fs.readFileSync(new URL('../src/' + p, import.meta.url), 'utf8');
function evaluate(body, exports, deps) {
  return compileFunction(body.replace(/^export /gm, '') + '\nreturn {' + exports + '};', Object.keys(deps))(...Object.values(deps));
}
function slice(p, start, end) {
  const s = source(p), a = s.indexOf(start), b = end ? s.indexOf(end, a + start.length) : s.length;
  assert.ok(a >= 0 && b > a, 'production boundary: ' + p);
  return s.slice(a, b);
}
let passed = 0;
async function check(name, fn) { await fn(); passed++; console.log('  ✓ ' + name); }
const u = { input_tokens: 100, output_tokens: 10, cache_creation_input_tokens: 40, cache_read_input_tokens: 50 };
/* 합성 응답에는 **회차마다 다른** `msg_…` id 를 단다.
 *
 * id 가 없으면 원장의 `messageId` 가 원문에서도 null 이라, 「원문 대신 회차 객체를 넘겼다」를
 * 재는 단언이 아예 물지 않는다(양쪽 다 null). 반대로 같은 id 를 두 응답에 주면 원장이
 * 중복으로 걸러 아래의 `records.length` 계수(3·2)가 조용히 줄어든다 — 그래서 일련번호다. */
let msgSeq = 0;
const msg = (model = 'claude-opus-5', stop = 'end_turn', content = [{ type: 'text', text: 'synthetic answer' }]) =>
  ({ id: 'msg_fixture' + String(++msgSeq).padStart(3, '0'), model, stop_reason: stop, usage: structuredClone(u), content, role: 'assistant' });
/* `model` 이 **없는** 응답. 원장이 「요청한 모델」로 되메꾸는 갈래를 재는 자리다.
 *
 * `msg()` 의 기본값은 건드리지 않는다 — 기존 단언들이 전부 그 `model` 로 금액·byModel 을
 * 재고 있어서, 기본값에서 빼면 여기 있는 계산이 통째로 뜻을 잃는다. 새 자리에서만 뺀다. */
const msgNoModel = (stop, content) => { const m = msg(undefined, stop, content); delete m.model; return m; };
const expected = estimateCost('claude-opus-5', u).usd;
/* ── 꽂아 준 단가 계산기를 원장이 **실제로 쓰는지** 재는 표식.
 *
 * 호출 자리는 `createUsageCollector({ estimate: estimateCost })` 로 계산기를 넘기는데,
 * `usage.js` 가 같은 함수를 기본값으로도 들고 있어서 그 한 줄을 통째로 빼도 지금은 아무
 * 검사도 안 빨개진다 — 오늘 둘이 같은 함수라서다. 어댑터 작업이 하는 일이 바로 꽂는 것을
 * 갈아 끼우는 일이라, 갈아 끼운 것이 무시되면 옛 단가로 계산된 금액이 조용히 나간다.
 *
 * 그래서 **진짜일 수 없는 금액**을 내는 계산기를 꽂고 그 숫자가 그대로 나오는지 본다.
 * 주입이 빠지면 진짜 계산기가 돌아 `expected` 가 나오고 여기서 갈린다. */
const SENTINEL_USD = 0.00424242;
const sentinelEstimate = () => ({ usd: SENTINEL_USD, rateKnown: true, rateBasis: 'sentinel-injected' });
assert.notEqual(SENTINEL_USD, expected, '표식 금액이 진짜 금액과 같으면 이 단언은 아무것도 재지 않습니다');
const oneLedger = createUsageCollector(); oneLedger.record(msg());
const one = oneLedger.snapshot();

await check('console cost retains currency marker and marks unknown rates', () => {
  const lines = [], saved = console.log;
  try {
    console.log = (s) => lines.push(s);
    logUsage('fixture', 'claude-opus-5', u);
    logUsage('fixture', 'unknown', u);
  } finally { console.log = saved; }
  assert.match(lines[0], /약 \$/);
  assert.match(lines[1], /단가 미상/);
});
await check('per-model pricing, refusal and cache TTL subtotal', () => {
  const l = createUsageCollector();
  l.record(msg('claude-opus-5', 'refusal'));
  l.record(msg('claude-sonnet-5'));
  const r = l.snapshot();
  assert.equal(r.costUsd, expected + estimateCost('claude-sonnet-5', u).usd);
  assert.equal(r.byModel.length, 2);
  assert.equal(r.usage.cache_creation.ephemeral_1h_input_tokens, 80);
  assert.equal(r.complete, true);
});
/* 합계의 **네 칸을 전부** 고정 숫자로 잰다.
 *
 * 2026-09-19 감사 전까지 여기서 보던 것은 `cache_creation.ephemeral_1h` 한 칸뿐이었다.
 * 그래서 `summarizeUsage` 가 입력/출력을 뒤바꿔 더하거나 캐시 읽기를 아예 안 더해도
 * 상시 검사가 전부 초록이었다. 금액(`costUsd`)은 회차별 기록에서 따로 계산돼 맞으니
 * **돈은 맞는데 토큰 내역만 조용히 틀리는** 모양이라 더 오래 안 들킨다 — 대화 로그의
 * in/cache-w/cache-r/out 칸과 월 분해표가 그 숫자를 읽고, 「캐시가 얼마나 먹히나」를
 * 그것으로 판단한다.
 *
 * 고정물 `u` 가 네 칸을 100·10·40·50 으로 **서로 다르게** 두는 것이 이 단언의 전제다.
 * 값을 같게 만들면 뒤바뀜이 안 보인다. */
await check('ledger totals are asserted on every token column, not just one', () => {
  const l = createUsageCollector();
  l.record(msg('claude-opus-5'));
  l.record(msg('claude-sonnet-5'));
  const r = l.snapshot();
  assert.deepEqual(r.usage, {
    input_tokens: 200, output_tokens: 20,
    cache_creation_input_tokens: 80, cache_read_input_tokens: 100,
    cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 80 },
  }, '합계의 토큰 칸이 원문 합과 다릅니다');
  // 모델별 줄도 같은 합산기를 탄다 — 한쪽만 고쳐지는 것을 막는다.
  for (const row of r.byModel) {
    assert.deepEqual(row.usage, {
      input_tokens: 100, output_tokens: 10,
      cache_creation_input_tokens: 40, cache_read_input_tokens: 50,
      cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 40 },
    }, `모델별 합계가 원문과 다릅니다: ${row.model}`);
  }
});
/* 5분/1시간 쓰기 갈래를 따로 누른다.
 *
 * 위 고정물 `u` 는 `cache_creation` 객체가 없어 쓰기가 **전부 1시간으로 접힌다** — 그래서
 * 두 칸을 맞바꾸는 잘못이 저기서는 안 보인다. 두 칸은 단가가 1.25배와 2배로 달라서
 * 뭉개지면 금액이 그대로 틀린다. 7 과 11 처럼 서로 다른 수를 쓰는 것이 전제다. */
await check('cache write TTL split is kept apart in the totals', () => {
  const l = createUsageCollector();
  const split = msg();
  split.usage = {
    input_tokens: 1, output_tokens: 1, cache_read_input_tokens: 0,
    // 30 중 7+11 만 갈래가 밝혀진 경우. 남는 12 는 안전하게 1시간으로 접는다(usage.js 규칙).
    cache_creation_input_tokens: 30,
    cache_creation: { ephemeral_5m_input_tokens: 7, ephemeral_1h_input_tokens: 11 },
  };
  l.record(split);
  assert.deepEqual(l.snapshot().usage.cache_creation,
    { ephemeral_5m_input_tokens: 7, ephemeral_1h_input_tokens: 23 },
    '5분/1시간 쓰기 갈래가 뭉개졌습니다');
});
await check('unknown rate / missing usage are not zero', () => {
  for (const response of [{ model: 'unpriced', usage: u }, { model: 'claude-opus-5' }, { model: 'claude-opus-5', usage: { input_tokens: -1, output_tokens: 1 } }]) {
    const l = createUsageCollector(); l.record(response);
    assert.equal(l.snapshot().costUsd, null);
    assert.equal(l.snapshot().unknownAttempts, 1);
  }
});
await check('known subtotal survives connection failure; snapshots are detached', () => {
  const l = createUsageCollector(); const response = msg();
  l.record(response); response.usage.input_tokens = 999999;
  l.failed('claude-opus-5');
  const a = l.snapshot(); a.records.length = 0;
  assert.equal(l.snapshot().knownCostUsd, expected);
  assert.equal(l.snapshot().costUsd, null);
  assert.equal(l.snapshot().records.length, 2);
});
await check('same message ID counted once, zero usage is valid', () => {
  const l = createUsageCollector(); l.record({ ...msg(), id: 'msg_test' }); l.record({ ...msg(), id: 'msg_test' });
  assert.equal(l.snapshot().records.length, 1);
  const z = createUsageCollector(); z.record({ model: 'claude-opus-5', usage: { input_tokens: 0, output_tokens: 0 } });
  assert.equal(z.snapshot().costUsd, 0);
});
await check('error attachment retains identity and totals', () => {
  const e = new Error('synthetic');
  assert.equal(attachUsage(e, one), e);
  assert.equal(usageFields(e.hermesAccounting).costUsd, expected);
});

const config = { timezone: 'UTC', models: {
  qa: { id: 'claude-opus-5', maxTokens: 100, effort: 'low' },
  fallback: 'claude-sonnet-5',
}, limits: { maxToolIterations: 3 } };
function qa(sequence, toolError, estimate = estimateCost) {
  let call = 0;
  const client = { beta: { messages: { toolRunner: () => {
    assert.ok(call < sequence.length, 'unexpected model run');
    const steps = sequence[call++];
    return {
      async *[Symbol.asyncIterator]() { for (const s of steps) { if (s instanceof Error) throw s; yield s; } },
      generateToolResponse: async () => { if (toolError) throw toolError; return { role: 'user', content: [{ type: 'tool_result', content: 'synthetic' }] }; },
      setMessagesParams() {},
    };
  } } } };
  return createQuestionAnswerer({
    config, anthropic: () => client,
    toolSession: (spec) => createToolSession(spec, { client: () => client }),
    buildTools: () => [], systemBlocks: () => ({ common: 'synthetic' }),
    lastSyncedAt: () => 'synthetic', privateQuoteLine: () => 'none', BOT_ANSWER_MARK: 'bot',
    logUsage() {}, estimateCost: estimate, createUsageCollector, attachUsage, console: silent,
  });
}
const ask = { question: 'synthetic', access: {}, origin: 'fixture', asker: 'fixture' };
await check('Q&A fallback retains first refusal cost and final diagnostics/tools', async () => {
  const fallback = { ...msg('claude-sonnet-5', 'tool_use', [{ type: 'tool_use', name: 'search', input: {} }]),
    id: 'msg_fallback', diagnostics: { cache_miss_reason: { type: 'fixture', cache_missed_input_tokens: 2 } } };
  const first = { ...msg('claude-opus-5', 'refusal'), id: 'msg_qa_first' };
  const last = { ...msg('claude-sonnet-5'), id: 'msg_qa_last' };
  const result = await qa([[first], [fallback, last]])(ask);
  assert.equal(result.accounting.records.length, 3);
  assert.equal(result.costUsd, expected + 2 * estimateCost('claude-sonnet-5', u).usd);
  assert.equal(result.toolCalls.length, 1);
  assert.equal(result.cacheMiss[0].type, 'fixture');
  /* ── 원장에 넣는 것은 **회사 원문 응답**이지 창구가 만든 회차 객체가 아니다.
   *
   * 회차 객체에는 `id` 도 `stop_reason` 도 없다(`messageId`·`stopReason` 이라는 다른 이름으로
   * 있다). 그래서 `ledger.record(turn, …)` 으로 바꿔도 모델·사용량·금액이 전부 그대로라
   * 기존 단언은 하나도 안 빨개지는데, 원장의 id 중복 제거가 죽고 대화 로그에서 응답 id 가
   * 사라진다 — 에러 없이 조용히 틀리는 자리다. 앞의 둘은 첫 모델 루프(qa.js 의 첫
   * `ledger.record`), 뒤의 둘은 거절 뒤 재시도 루프(두 번째 `ledger.record`)를 잰다. */
  assert.deepEqual(result.accounting.records.map((r) => r.messageId),
    ['msg_qa_first', 'msg_fallback', 'msg_qa_last'],
    '원장은 원문 응답의 id 를 그대로 실어야 합니다 (회차 객체에는 id 가 없습니다)');
  assert.deepEqual(result.accounting.records.map((r) => r.stopReason),
    ['refusal', 'tool_use', 'end_turn'],
    '원장은 원문 응답의 stop_reason 을 그대로 실어야 합니다');
});
/* ── 응답에 `model` 이 없으면 원장은 **그 회차에 요청한 모델**로 적는다.
 *
 * `ledger.record(turn.raw, activeModel)` 의 둘째 인자는 이 경우에만 쓰인다. 고정물이 전부
 * `model` 을 달고 오니 지금까지 그 인자를 통째로 빼도 32개가 전부 초록이었다 — 빠지면
 * 회사가 모양이 깨진 응답을 한 번 줄 때 그 지출이 `(unknown)` 칸으로 들어가 어느 모델에
 * 쓴 돈인지가 사라지고, 단가를 못 찾아 금액까지 미상이 된다. 에러는 안 난다.
 *
 * 두 루프의 요청 모델이 서로 다르므로(본 루프 qa.id · 재시도 fallback) 한 줄씩 따로 잰다 —
 * 한 단언이 양쪽을 우연히 덮으면 그건 두 자리를 본 것이 아니다. */
await check('Q&A ledger falls back to each loop\'s requested model when the response omits it', async () => {
  const r = await qa([[msgNoModel('refusal')], [msgNoModel()]])(ask);
  assert.equal(r.accounting.records.length, 2);
  assert.equal(r.accounting.records[0].model, config.models.qa.id,
    '본 루프: 응답에 model 이 없으면 원장은 요청한 qa 모델로 적어야 합니다');
  assert.equal(r.accounting.records[1].model, config.models.fallback,
    '거부 뒤 재시도: 응답에 model 이 없으면 원장은 요청한 fallback 모델로 적어야 합니다');
});
await check('Q&A both models refuse but both costs remain', async () => {
  const r = await qa([[msg('claude-opus-5', 'refusal')], [msg('claude-sonnet-5', 'refusal')]])(ask);
  assert.equal(r.refused, true); assert.equal(r.accounting.records.length, 2);
});
await check('Q&A partial success then failure retains known cost on same error', async () => {
  const err = new Error('connection');
  await assert.rejects(qa([[msg(), err]])(ask), (e) => {
    assert.equal(e, err); assert.equal(e.hermesAccounting.knownCostUsd, expected);
    assert.equal(e.hermesAccounting.unknownAttempts, 1);
    /* 못 본 시도도 **어느 모델에 걸었던 것인지** 적혀야 한다 — `ledger.failed(model)` 의
     * 인자가 빠지면 `(unknown)` 으로 들어가 실패 집계표에서 다른 모델의 실패와 한 줄로
     * 뭉개진다 (2026-08-28 에 같은 종류를 한 번 고쳤다). 금액·건수는 그대로라 조용하다. */
    const failed = e.hermesAccounting.records.at(-1);
    assert.equal(failed.unknownReason, 'unobserved-attempt');
    assert.equal(failed.model, config.models.qa.id,
      'Q&A: 실패한 시도는 그때 걸었던 모델로 적혀야 합니다 ((unknown) 이면 집계에서 뭉개집니다)');
    return true;
  });
});

/* ── 실패가 **대체 모델 차례에** 났을 때. 위의 단언들은 전부 첫 시도에서 실패하는
 * 고정물이라, `ledger.failed(config.models.qa.id)` 로 **박아** 버려도 전부 초록이었다
 * (2026-09-19 대조 검증). 실패 집계표에서 「대체 모델도 실패했다」가 첫 모델 실패로
 * 둔갑하면, 대체 모델이 못 쓰게 됐다는 사실 자체가 표에서 사라진다. */
await check('Q&A failure during the fallback rerun is attributed to the fallback model', async () => {
  const err = new Error('fallback connection');
  await assert.rejects(qa([[msg('claude-opus-5', 'refusal')], [err]])(ask), (e) => {
    assert.equal(e, err);
    assert.equal(e.hermesAccounting.records.length, 2);
    assert.equal(e.hermesAccounting.knownCostUsd, expected);
    assert.equal(e.hermesAccounting.unknownAttempts, 1);
    const failed = e.hermesAccounting.records.at(-1);
    assert.equal(failed.unknownReason, 'unobserved-attempt');
    assert.equal(failed.model, config.models.fallback,
      'Q&A: 대체 모델 차례에 난 실패는 대체 모델로 적혀야 합니다 (첫 모델로 박으면 여기서 갈립니다)');
    return true;
  });
});
await check('local Q&A tool failure does not invent an API attempt', async () => {
  const err = new Error('local tool');
  await assert.rejects(qa([[msg()]], err)(ask), (e) => {
    assert.equal(e, err); assert.equal(e.hermesAccounting.costUsd, expected);
    assert.equal(e.hermesAccounting.unknownAttempts, 0); return true;
  });
});
const digestConfig = { models: { daily: config.models.qa, fallback: config.models.fallback } };
function digest(steps, estimate = estimateCost) {
  let i = 0;
  return createDigestGenerator({
    config: digestConfig, textModel: (spec) => createTextModel(spec, { client: () => ({ messages: { stream: () => ({ finalMessage: async () => {
      const r = steps[i++]; if (r instanceof Error) throw r; return r;
    } }) } }) }), promptFile: () => 'synthetic', logUsage() {}, estimateCost: estimate,
    errLabel: () => 'fixture', sleep: async () => {},
  });
}
const opts = { kind: 'daily', transcript: 'synthetic', windowLabel: 'fixture', stats: '1' };
await check('digest refusal and max_tokens both retain usage', async () => {
  const refused = { ...msg('claude-opus-5', 'refusal'), id: 'msg_digest_refusal' };
  const r = await digest([refused])(opts);
  assert.equal(r.costUsd, expected);
  // digest.js 도 원장에 원문 응답을 넣는다 — 회차 객체를 넣으면 여기서 둘 다 null 이 된다.
  assert.equal(r.accounting.records[0].messageId, 'msg_digest_refusal');
  assert.equal(r.accounting.records[0].stopReason, 'refusal');
  const cut = { ...msg('claude-opus-5', 'max_tokens'), id: 'msg_digest_cut' };
  await assert.rejects(digest([cut])(opts), (e) => {
    assert.equal(e.hermesAccounting.costUsd, expected);
    assert.equal(e.hermesAccounting.records[0].messageId, 'msg_digest_cut');
    assert.equal(e.hermesAccounting.records[0].stopReason, 'max_tokens');
    return true;
  });
});
await check('digest retry failure + success preserves unknown and subtotal', async () => {
  const warn = console.warn; console.warn = () => {};
  try {
    const r = await digest([Object.assign(new Error('retry'), { status: 503 }), msg()])(opts);
    assert.equal(r.accounting.knownCostUsd, expected);
    assert.equal(r.costUsd, null); assert.equal(r.accounting.unknownAttempts, 1);
    /* 실패한 시도도 **어느 모델에 걸었던 것인지** 적혀야 한다. `ledger.failed(model)` 의
     * 인자가 빠지면 `(unknown)` 이 되고, 실패 집계표에서 모델이 다른 실패들이 한 줄로
     * 뭉개진다 — 2026-08-28 에 한 번 고친 것과 같은 종류다. 에러는 안 난다. */
    assert.equal(r.accounting.records[0].unknownReason, 'unobserved-attempt');
    assert.equal(r.accounting.records[0].model, digestConfig.models.daily.id,
      '요약: 실패한 시도는 그때 걸었던 모델로 적혀야 합니다 ((unknown) 이면 집계에서 뭉개집니다)');
  } finally { console.warn = warn; }
});
/* 요약도 같다 — 사다리를 **끝까지** 태워 대체 모델 차례에 실패시킨다. 기본 모델로 세 번
 * 실패한 뒤 네 번째가 대체 모델이고, 거기서 또 실패하면 던진다. 위의 실패 단언은 첫 시도만
 * 보고 있어서 `ledger.failed(cfg.id)` 로 박아도 초록이었다 (2026-09-19 대조 검증). */
await check('digest failure on the fallback attempt is attributed to the fallback model', async () => {
  const warn = console.warn; console.warn = () => {};
  try {
    const overloaded = () => Object.assign(new Error('retry'), { status: 503 });
    await assert.rejects(digest([overloaded(), overloaded(), overloaded(), overloaded()])(opts), (e) => {
      assert.equal(e.hermesAccounting.records.length, 4);
      assert.equal(e.hermesAccounting.unknownAttempts, 4);
      assert.equal(e.hermesAccounting.knownCostUsd, 0);
      assert.deepEqual(e.hermesAccounting.records.map((r) => r.model),
        [digestConfig.models.daily.id, digestConfig.models.daily.id, digestConfig.models.daily.id, digestConfig.models.fallback],
        '요약: 실패는 **그 시도의** 모델로 적혀야 합니다 — 마지막은 대체 모델입니다');
      return true;
    });
  } finally { console.warn = warn; }
});
/* ── 꽂아 준 단가 계산기가 실제로 금액을 낸다 (요약 창구).
 * `createUsageCollector({ estimate: estimateCost })` 에서 `estimate` 를 빼면 원장이 제
 * 기본값(진짜 계산기)으로 돌아가 여기서 `expected` 가 나온다. */
await check('digest ledger prices with the injected estimator, not its own default', async () => {
  const r = await digest([msg()], sentinelEstimate)(opts);
  assert.equal(r.accounting.records.length, 1);
  assert.equal(r.costUsd, SENTINEL_USD,
    '요약: 원장은 꽂아 준 단가 계산기로 금액을 내야 합니다 (주입이 빠지면 제 기본값이 돕니다)');
  assert.equal(r.accounting.records[0].rateBasis, 'sentinel-injected');
});
/* Q&A 도 같다. 이 자리가 오래 비어 있었다 — `qa.js` 가 원장을 인자 없이 세워
 * (`createUsageCollector()`) 제 기본값으로 돌았고, 기본값이 마침 같은 계산기라 **금액은
 * 맞는데 아무도 그 배선을 안 재는** 상태였다. 꽂아 준 계산기가 무시돼도 검사가 안 빨개졌다. */
await check('Q&A ledger prices with the injected estimator, not its own default', async () => {
  const result = await qa([[{ ...msg(), id: 'msg_sentinel' }]], undefined, sentinelEstimate)(ask);
  assert.equal(result.accounting.records.length, 1);
  assert.equal(result.costUsd, SENTINEL_USD,
    'Q&A: 원장은 꽂아 준 단가 계산기로 금액을 내야 합니다 (주입이 빠지면 제 기본값이 돕니다)');
  assert.equal(result.accounting.records[0].rateBasis, 'sentinel-injected');
});
await check('summary comparison ledger prices with the injected estimator, not its own default', async () => {
  const snapshots = [];
  const run = createSummaryChecker({ config, promptFile: () => 'fixture', logUsage() {}, estimateCost: sentinelEstimate,
    // 거절로 끝내 파싱 단계를 거치지 않게 한다 — 여기서 재는 것은 금액뿐이다.
    jsonModel: (spec) => createJsonModel(spec, { client: () => ({ messages: {
      create: async () => msg('claude-opus-5', 'refusal'),
    } }) }) });
  await run({ channel: 'fixture', summary: 'fixture', transcript: 'fixture', onUsage: (a) => snapshots.push(a) });
  assert.equal(snapshots.length, 1);
  assert.equal(snapshots[0].costUsd, SENTINEL_USD,
    '요약 대조: 원장은 꽂아 준 단가 계산기로 금액을 내야 합니다');
  assert.equal(snapshots[0].records[0].rateBasis, 'sentinel-injected');
});
/* 요약도 같다 — 다만 여기서 적어야 하는 것은 `cfg.id` 가 아니라 **그 시도의 모델**이다.
 * 기본 모델로 세 번 실패한 뒤 대체 모델로 나간 회차를 쓴다: `ledger.record(final.raw, model)`
 * 에서 인자가 빠지면 `(unknown)`, `model` 대신 `cfg.id` 를 넣어도 여기서 갈린다. */
await check('digest ledger falls back to that attempt\'s model when the response omits it', async () => {
  const warn = console.warn; console.warn = () => {};
  try {
    const overloaded = () => Object.assign(new Error('retry'), { status: 503 });
    const r = await digest([overloaded(), overloaded(), overloaded(), msgNoModel()])(opts);
    assert.equal(r.accounting.records.length, 4);
    assert.equal(r.accounting.records[0].model, digestConfig.models.daily.id);
    assert.equal(r.accounting.records.at(-1).model, digestConfig.models.fallback,
      '요약: 응답에 model 이 없으면 원장은 그 시도에 요청한 모델로 적어야 합니다');
  } finally { console.warn = warn; }
});
await check('summary comparison ledger falls back to the requested model when the response omits it', async () => {
  const snapshots = [];
  const run = createSummaryChecker({ config, promptFile: () => 'fixture', logUsage() {}, estimateCost,
    jsonModel: (spec) => createJsonModel(spec, { client: () => ({ messages: {
      // 거절로 끝내 파싱 단계를 거치지 않게 한다 — 여기서 재는 것은 원장에 적힌 모델뿐이다.
      create: async () => msgNoModel('refusal'),
    } }) }) });
  await run({ channel: 'fixture', summary: 'fixture', transcript: 'fixture', onUsage: (a) => snapshots.push(a) });
  assert.equal(snapshots.length, 1);
  assert.equal(snapshots[0].records[0].model, config.models.qa.id,
    '요약 대조: 응답에 model 이 없으면 원장은 요청한 cfg.id 로 적어야 합니다');
});
for (const response of [msg('claude-opus-5', 'refusal'), msg('claude-opus-5', 'max_tokens'), msg(), new Error('connection')]) {
  await check('summary comparison emits accounting exactly once: ' + (response.stop_reason || 'error'), async () => {
    const snapshots = [];
    const run = createSummaryChecker({ config, promptFile: () => 'fixture', logUsage() {}, estimateCost,
      jsonModel: (spec) => createJsonModel(spec, { client: () => ({ messages: { create: async () => {
        if (response instanceof Error) throw response; return response;
      } } }) }) });
    try { await run({ channel: 'fixture', summary: 'fixture', transcript: 'fixture', onUsage: (a) => snapshots.push(a) }); }
    catch (e) { assert.ok(e.hermesAccounting); }
    assert.equal(snapshots.length, 1);
    assert.equal(snapshots[0].costUsd, response instanceof Error ? null : expected);
    if (!(response instanceof Error)) {
      // summary-check.js 도 원장에 원문 응답을 넣는다 — 회차 객체에는 id·stop_reason 이 없다.
      assert.equal(snapshots[0].records[0].messageId, response.id);
      assert.equal(snapshots[0].records[0].stopReason, response.stop_reason);
      assert.ok(response.id?.startsWith('msg_'), '고정물에 실제 msg_ id 가 있어야 단언이 뭅니다');
    } else {
      /* 호출이 통째로 실패한 갈래 — `ledger.failed(cfg.id)` 의 인자가 빠지면 `(unknown)`
       * 이 되고, 실패 집계표에서 어느 모델로 대조하다 실패했는지가 사라진다. */
      assert.equal(snapshots[0].records.length, 1);
      assert.equal(snapshots[0].records[0].unknownReason, 'unobserved-attempt');
      assert.equal(snapshots[0].records[0].model, config.models.qa.id,
        '요약 대조: 실패한 시도는 그때 걸었던 모델(cfg.id)로 적혀야 합니다');
    }
  });
}

await check('Slack Q&A posting failure logs generated cost exactly once', async () => {
  const logs = [];
  const { handleQuestion: handle } = createSlackQuestion({
    answerQuestion: async () => ({ text: 'answer', channels: [], toolCalls: [], accounting: one }),
    resolveAccess: async () => ({ access: {}, origin: 'fixture' }), accessLabel: () => 'public',
    permalink: () => null, logConversation: (r) => logs.push(r), usageFields,
    chunkForSlack: (s) => [s], toSlackMrkdwn: (s) => s, console: silent,
    // 채널 멘션 풀기는 이 시험의 관심사(비용 집계) 밖이라 항등함수로 둔다.
    // 규칙 자체는 `check-channel-ids.js`, 배선은 `check-slack-question.js` 가 잰다.
    resolveChannelMentions: (s) => s,
  });
  await handle({ text: 'question', event: { user: 'fixture', channel: 'fixture' }, client: {
    reactions: { add: async () => {}, remove: async () => {} },
    users: { info: async () => ({}) }, chat: { postMessage: async () => { throw new Error('post failed'); } },
  } });
  assert.equal(logs.length, 1); assert.equal(logs[0].ok, false); assert.equal(logs[0].costUsd, expected);
});

const digestBody = slice('digest.js', 'function summarize(entries)');
function delivery(generate, postFails) {
  return evaluate(digestBody, 'runDigest', {
    config: { digest: { deliverTo: 'channel', channelId: 'fixture', minMessages: 1 }, owner: { name: 'fixture' } },
    PUBLIC_ACCESS: {}, FULL_ACCESS: {}, accessLabel: () => 'public',
    dailyWindow: () => ({ label: 'fixture' }), weeklyWindow: () => ({ label: 'fixture' }),
    fetchWindow: async () => [{ channel: 'fixture', messages: [{ replies: [], files: [] }] }],
    formatTranscript: () => 'fixture', documentsUploadedIn: () => ({ docs: [], hidden: 0, gistOnly: 0 }), unconvertedAmong: () => 0,
    generateDigest: generate, toSlackMrkdwn: (s) => s, chunkForSlack: (s) => [s],
    markDailyDigestSent() {}, logConversation() {}, usageFields, attachUsage, console: silent,
  }).runDigest({ chat: { postMessage: async () => { if (postFails) throw new Error('post'); } },
    conversations: { info: async () => ({}) } }, 'daily');
}
await check('digest refusal return and post exception carry accounting', async () => {
  const r = await delivery(async () => ({ text: '', refused: true, accounting: one }), false);
  assert.equal(r.accounting.costUsd, expected);
  await assert.rejects(delivery(async () => ({ text: 'answer', accounting: one }), true),
    (e) => e.hermesAccounting.costUsd === expected);
});
await check('scheduler logs usage before failing notification, without duplicate log', async () => {
  const logs = [];
  let notifiedAfterLog = false;
  const { onFailure } = createSchedulerFailure({
    config: { owner: { slackUserId: 'fixture' }, timezone: 'UTC' },
    LOG_KIND: {}, usageFields, logConversation: (r) => logs.push(r), console: silent,
  });
  await onFailure({ conversations: { open: async () => {
    notifiedAfterLog = logs.length === 1;
    assert.equal(logs.length, 1);
    throw new Error('notification');
  } } }, 'daily', 'fixture', 'failure', { accounting: one });
  assert.equal(notifiedAfterLog, true);
  assert.equal(logs.length, 1); assert.equal(logs[0].costUsd, expected);
});
await check('ingest channel comparisons merge once, including failed channel', async () => {
  let call = 0;
  const snapshots = [];
  const run = evaluate(slice('ingest/summary.js', 'export async function checkSummaries'), 'checkSummaries', {
    path, CHANNELS_DIR: '/fixture', fs: { existsSync: () => true }, extractSummary: () => ({ text: 'fixture' }),
    createUsageCollector, verifyEvidence: () => ({ kept: [], dropped: [] }),
    compareSummary: async ({ onUsage }) => { onUsage(one); if (++call === 2) throw new Error('parse'); return []; },
  }).checkSummaries;
  const r = await run([{ channel: 'a' }, { channel: 'b' }], { onUsage: (a) => snapshots.push(a) });
  assert.equal(r.accounting.costUsd, 2 * expected); assert.equal(r.failed.length, 1);
  assert.equal(snapshots.at(-1).records.length, 2);
  /* 여기 원장은 `createUsageCollector()` 를 **인자 없이** 부른다 — 단가 계산기를 안 꽂는다.
   * 지금은 무해하다. 이 원장은 이미 금액이 실려 온 기록을 `merge` 로 받아 `snapshot` 만
   * 하고 `record` 를 한 번도 안 부르기 때문이다. 그래서 기본 계산기가 돌 일이 없다.
   *
   * 그런데 모양은 a430181 이 고친 결함(꽂아 준 계산기를 안 쓰던 자리)과 똑같다. 누가 여기에
   * `record` 를 한 줄 더하는 날 주입 규약이 조용히 안 지켜지고, 그때도 아무 검사가 안
   * 빨개진다 (2026-09-19 감사). **전제를 원문에 못박아 둔다** — 전제가 깨지면 여기서 멈춘다. */
  const summaryBody = slice('ingest/summary.js', 'export async function checkSummaries');
  assert.ok(/createUsageCollector\(\)/.test(summaryBody), 'checkSummaries 의 원장 생성 자리를 찾지 못했습니다');
  assert.ok(!/\bledger\.record\(/.test(summaryBody),
    'checkSummaries 의 원장이 record 를 쓰기 시작했습니다 — 단가 계산기를 주입하도록 고쳐야 합니다');
});
for (const dry of [true, false]) {
  for (const fail of ['none', 'compare', 'derive', 'report', 'cleanup']) {
    await check('ingest ' + (dry ? 'dry: zero writes' : 'live: one cost record') + ' / ' + fail, async () => {
      const logs = [], consoleLines = [];
      let mkdirCalls = 0, appendCalls = 0, gitBoundaryCalls = 0;
      // Use the actual persistent log writer with an in-memory filesystem.
      // A log spy alone previously let the incorrect dry-run contract pass.
      const logBody = source('convo-log.js').replace(/^import[\s\S]*?;\r?\n/gm, '');
      const { append } = evaluate(logBody, 'append', {
        createLogCodec, createLogStats, createLogRenderer, createLogStore,
        fs: {
          mkdirSync() { mkdirCalls++; },
          appendFileSync(_, line) { appendCalls++; logs.push(JSON.parse(line)); },
        },
        path, config: { timezone: 'UTC' }, LOG_ENABLED: true, LOG_DIR: '/fixture/rendered', LOG_RAW_DIR: '/fixture/raw',
        console: silent,
      });
      const run = evaluate(slice('ingest/index.js', 'export async function runIngest'), 'runIngest', {
        config, targetPaths: () => [],
        head: async () => { gitBoundaryCalls++; return 'fixture-sha'; },
        syncBeforeWork: async () => { gitBoundaryCalls++; return 'fixture-sync'; },
        rollback: async () => { gitBoundaryCalls++; },
        runGate: async () => ({ passed: true }), hasChanges: async () => false,
        ingestConversations: async (_, opts) => {
          assert.equal(opts.dry, dry);
          return { totalAdded: 1, newChannels: [], errors: [], channels: [{ added: 1, transcript: 'fixture' }] };
        },
        renderConversationLog: ({ write }) => { assert.equal(write, !dry); return null; },
        checkSummaries: async (_, { onUsage }) => {
          onUsage(one);
          if (fail === 'compare') throw new Error('compare');
          return { findings: [], failed: [], accounting: one };
        },
        runDerive: async () => { if (fail === 'derive') throw new Error('derive'); return { changed: [], unresolved: [] }; },
        runPendingWork: () => ({ total: 0 }),
        clearTemp() { if (fail === 'cleanup') throw new Error('cleanup'); },
        classifyFailure: (r) => r.fatal ? { reason: 'fatal' } : null, stepsReached: () => 'fixture',
        compose: () => 'fixture',
        send: async (_, __, opts) => {
          assert.equal(opts.dry, dry);
          if (fail === 'report') throw new Error('report');
          return { sent: !dry };
        },
        logConversation: append, usageFields,
        console: { ...silent, log: (...args) => consoleLines.push(args.join(' ')) },
      }).runIngest;
      const result = await run({}, { dry });
      if (['report', 'cleanup'].includes(fail)) {
        assert.ok(result.secondaryErrors.some(e => e.stage === (fail === 'report' ? 'send' : 'cleanup')));
      }
      assert.equal(result.summaryAccounting.costUsd, expected);
      assert.equal(result.accountingLog, dry ? 'not-persisted' : 'attempted');
      assert.equal(mkdirCalls, dry ? 0 : 1);
      assert.equal(appendCalls, dry ? 0 : 1);
      if (dry) {
        assert.equal(gitBoundaryCalls, 0);
        assert.ok(consoleLines.some((line) => line.includes('--dry') && line.includes(String(expected))));
        if (result) assert.equal(result.summaryAccounting.costUsd, expected);
      } else {
        assert.equal(logs.length, 1);
        assert.equal(logs[0].costUsd, expected);
        assert.equal(logs[0].accountingOnly, true);
        assert.ok(!result?.accounting, 'scheduler must not count the same cost again');
      }
    });
  }
}
await check('rendered unknown subtotal is not parsed as a complete cost', () => {
  const body = source('convo-log.js').replace(/^import[\s\S]*?;\r?\n/gm, '');
  const {
    renderEntry, parseCostLine, failureStats, parseEntryHeader, parseToolLine, TOOL_LINE_PREFIX,
  } = evaluate(body, 'renderEntry, parseCostLine, failureStats, parseEntryHeader, parseToolLine, TOOL_LINE_PREFIX', {
    createLogCodec, createLogStats, createLogRenderer, createLogStore,
    fs: {}, path, config: { timezone: 'UTC' }, LOG_ENABLED: false, LOG_DIR: null, LOG_RAW_DIR: null,
  });
  const incomplete = summarizeUsage([...one.records, { model: 'unknown', costUsd: null }]);
  const text = renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'qa', ...usageFields(incomplete) });
  assert.match(text, /총액 미상/);
  const line = text.split('\n').find((s) => s.startsWith('> **소요**'));
  assert.equal(parseCostLine(line).costUsd, null);
  assert.equal(parseCostLine('> **비용** old-model · 약 $0.123').costUsd, 0.123);
  const accountingOnly = { at: '2026-01-01T00:00:00Z', kind: 'ingest', accountingOnly: true, ok: false, accounting: one };
  assert.equal(failureStats([accountingOnly, { kind: 'ingest', ok: false }]).failedTotal, 1);
  assert.ok(!/^### /m.test(renderEntry(accountingOnly)));
  /* 분해 스크립트의 본문을 그대로 쓴다 — 파일 읽기 없이.
   *
   * **떼어 오는 것은 그 스크립트의 「판정」이다.** 전에는 부품(`parseTokens`)만 떼어다 봐서,
   * 훑는 루프를 2026-09-15 의 버그로 통째로 되돌려도 여기 32개가 전부 초록이었다 —
   * 확인된 지출이 조용히 사라지는데 아무 신호가 없었다(2026-09-16 변이 시험). 그래서
   * 그쪽이 갈래를 `classifyCostLine` 한 함수로 빼고, 여기서 그것을 본다.
   *
   * 주입하는 `parseCostLine` 은 **위에서 convo-log.js 본문으로 만든 그 함수**다 —
   * 따로 만든 것을 넣으면 렌더와 파서를 맞대 본다는 말이 여기서부터 거짓이 된다. */
  const parserSource = fs.readFileSync(new URL('cost-breakdown.mjs', import.meta.url), 'utf8');
  const start = parserSource.indexOf('const TOKENS');
  const end = parserSource.indexOf('\nconst entries =', start);
  assert.ok(start >= 0 && end > start);
  const { classifyCostLine, scanLines } = evaluate(
    parserSource.slice(start, end), 'classifyCostLine, scanLines',
    { parseCostLine, parseEntryHeader, parseToolLine, TOOL_LINE_PREFIX },
  );
  const parseTokens = (line) => {
    const got = classifyCostLine(line);
    return got?.kind === 'count' ? got.tok : null;
  };
  const full = renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'qa', elapsedMs: 1000, ...usageFields(one) });
  const fullLine = full.split('\n').find((s) => s.startsWith('> **비용**'));
  assert.ok(parseTokens(fullLine));
  /* 미상 시도가 하나라도 섞이면 같은 회차가 `소요` 로 나가고 `약 $` 대신 `확인분 USD` 가
   * 붙는다. 앞의 모양만 읽던 때는 **확인된 지출까지 통째로 빠지고** 합계가 아무 표시 없이
   * 적게 찍혔다 — 렌더와 분해 스크립트를 여기서 한 줄로 묶어 둔다 (2026-09-15). */
  const partialLine = renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'qa', elapsedMs: 1000, ...usageFields(incomplete) })
    .split('\n').find((s) => s.startsWith('> **소요**'));
  const partial = parseTokens(partialLine);
  assert.ok(partial, partialLine);
  assert.equal(partial.usd, incomplete.knownCostUsd);
  assert.equal(partial.unknown, incomplete.unknownAttempts);
  /* **머리말이 `소요` 여도 모델은 실려 있다.** 머리말로 모델 유무를 재면 이 줄이
   * 「모델 없음」이 되어 통째로 건너뛰어진다 — 확인된 지출이 아무 표시 없이 사라진다. */
  assert.equal(parseCostLine(partialLine).model, incomplete.byModel.map((r) => r.model).join('+'));
  assert.equal(parseCostLine(partialLine).knownCostUsd, incomplete.knownCostUsd);
  assert.equal(parseCostLine(partialLine).unknownAttempts, incomplete.unknownAttempts);
  for (const line of [fullLine, partialLine]) assert.ok(parseCostLine(line)?.model, line);
  // 모델이 안 실린 회차 — 셀 것이 없으니 건너뛴다(못 읽은 줄로 세면 안 된다).
  const bare = renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'qa', elapsedMs: 11500 })
    .split('\n').find((s) => s.startsWith('> **소요**'));
  assert.ok(bare, String(bare));
  assert.equal(parseCostLine(bare).model, null);
  /* 사용량 없이 금액만 실린 회차 — **조용히 빠지면 안 된다.** 건너뛰기를 「토큰 내역이
   * 있나」로 가르면 이 줄의 $ 가 소리 없이 합계에서 빠진다. 모델이 실렸으니 읽어야 하고,
   * 못 읽으면 못 읽었다고 말해야 한다(스크립트가 숫자를 안 내고 멈춘다). */
  for (const extra of [{ elapsedMs: 1000 }, {}, { ...usageFields(one), costUsd: 9.999 }]) {
    const priceOnly = renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'qa', model: 'claude-opus-5', costUsd: 9.999, ...extra })
      .split('\n').find((s) => s.startsWith('> **비용**'));
    assert.equal(parseCostLine(priceOnly)?.model, 'claude-opus-5', String(priceOnly));
  }
  /* 모델이 없는데 금액만 실린 줄 — 분해는 못 하지만 **그 돈을 세어서 말해야 한다.**
   * `knownCostUsd` 가 null 로 오면 스크립트가 그 회차를 조용히 버린다. */
  const bareMoney = renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'qa', elapsedMs: 4000, costUsd: 0.25 })
    .split('\n').find((s) => s.startsWith('> **소요**'));
  assert.equal(parseCostLine(bareMoney).model, null);
  assert.equal(parseCostLine(bareMoney).knownCostUsd, 0.25);

  /* ── 첫 칸이 될 수 있는 **네 모양 전부** 를 렌더로 만들어 음성 쪽을 잰다.
   *
   * 전에는 소요 시간으로 시작하는 줄만 재고 있었다. 나머지 셋(`in …`·`확인분 USD …`·
   * `약 $…`)은 판정에서 빼도 **아무 검사도 안 빨개졌다**(2026-09-16 변이 시험 M5·M6).
   * 그 셋은 렌더가 실제로 첫 칸에 낼 수 있는 모양이라, 빠지면 그 줄의 첫 칸이 모델로
   * 읽히고 없는 단가로 쪼갠 숫자가 합계에 들어간다. */
  for (const [why, entry] of [
    ['소요 시간이 첫 칸', { elapsedMs: 11500 }],
    // 모델을 일부러 안 싣는다 — `usageFields` 를 그대로 쓰면 모델이 함께 실려 첫 칸이 아니게 된다.
    ['사용량이 첫 칸', { usage: one.usage }],
    ['확인분이 첫 칸', { accounting: incomplete }],
    ['금액이 첫 칸', { costUsd: 0.25 }],
  ]) {
    const line = renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'qa', ...entry })
      .split('\n').find((s) => /^> \*\*(비용|소요)\*\*/.test(s));
    assert.ok(line, why);
    const c = parseCostLine(line);
    assert.equal(c.model, null, `${why}: 첫 칸을 모델로 읽으면 안 됩니다 — ${line}`);
    assert.equal(c.unreadable, false, `${why}: 못 읽음이 아닙니다 — ${line}`);
    assert.equal(classifyCostLine(line).kind, 'skip', `${why} — ${line}`);
  }

  /* ── 사용량 기록(`> **계측 금액**`)도 **돈 줄이다.**
   *
   * 이 줄을 못 읽던 동안 그 돈이 월 머리말 합계에는 들어가고 분해 도구에는 안 들어가,
   * 같은 저장소의 두 숫자가 아무 표시 없이 어긋났다. 「못 쪼갠 돈」으로 세어져야 한다. */
  const acct = renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'ingest', accountingOnly: true, accounting: incomplete })
    .split('\n').find((s) => s.startsWith('> **계측 금액**'));
  assert.ok(acct, '사용량 기록에 금액 줄이 없습니다');
  const acctParsed = parseCostLine(acct);
  assert.equal(acctParsed.accounting, true, acct);
  assert.equal(acctParsed.model, null, acct);
  assert.equal(acctParsed.knownCostUsd, incomplete.knownCostUsd, acct);
  assert.equal(acctParsed.unknownAttempts, incomplete.unknownAttempts, acct);
  assert.deepEqual(classifyCostLine(acct), { kind: 'skip', usd: incomplete.knownCostUsd }, acct);

  /* ── 못 읽은 줄은 **조용히 사라지면 안 된다.** 「돈 줄이 아니다(null)」와 갈라야 한다.
   *
   * 아래 셋은 렌더가 낼 수 없는 모양이다. 손상된 로그·찢어진 쓰기에서 온다. 전에는 이런
   * 첫 칸이 **모델로 읽혀** 없는 단가로 쪼갠 숫자가 합계에 들어갔다 — 경고는 났지만
   * 지어낸 숫자가 함께 나갔다. 숫자를 아예 안 내고 멈추는 것이 맞다 (2026-09-16). */
  for (const line of [
    '> **소요** 1.5초(재시도) · in 1 / cache-w 1 / cache-r 1 / out 1 · 약 $0.500',
    '> **비용** claude opus 5 · 1.0초 · in 1 / cache-w 1 / cache-r 1 / out 1 · 약 $0.500',
    '> **비용**',
    '> **계측 금액** 2026-01-01T00:00:00Z · 확인분 USD 0.000000 / 미상 ?회',
  ]) {
    const c = parseCostLine(line);
    assert.ok(c, `돈 줄로 받아야 합니다(null 이면 조용히 사라집니다) — ${line}`);
    assert.equal(c.unreadable, true, `못 읽음으로 올려야 합니다 — ${line}`);
    assert.equal(classifyCostLine(line).kind, 'bad', line);
  }
  // 돈 줄이 아닌 것은 그대로 null 이다 — 위 「못 읽음」과 섞이면 둘 다 뜻을 잃는다.
  for (const line of ['**비용** 모델', '> **근거** #어디', '', '> **비용x** 모델']) {
    assert.equal(parseCostLine(line), null, line);
    assert.equal(classifyCostLine(line), null, line);
  }

  /* ── 훑는 루프를 **실제로 몰아 본다.**
   *
   * 여기까지는 전부 부품 시험이었다. 루프 자체는 저장소의 어떤 검사도 안 봐서, 사용량
   * 기록 블록을 앞 회차에 갖다 붙이는 실수를 넣어도 전부 초록이었다 (2026-09-16 변이
   * 시험 M13). 합성 md 줄로 루프를 돌려 그 갈래를 못 박는다. */
  const acctBlock = renderEntry({ at: '2026-01-01T00:05:00Z', kind: 'ingest', accountingOnly: true, accounting: incomplete });
  const md = [
    '## 2026-01-01',
    '',
    ...renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'qa', asker: '아무개', origin: '#어디', elapsedMs: 1000, ...usageFields(one) }).split('\n'),
    ...acctBlock.split('\n'),
  ];
  const scan = scanLines(md, 'fixture.md');
  assert.deepEqual(scan.bad, [], JSON.stringify(scan.bad));
  assert.equal(scan.entries.length, 1, '사용량 기록은 회차가 아니다');
  /* **앞 회차의 금액이 사용량 기록으로 덮이면 안 된다.** `####` 에서 앞 회차를 안 놓으면
   * 그 블록의 금액 줄이 `cur` 을 타고 여기 와서 붙는다 — 두 회차의 돈이 섞인다. */
  // 렌더가 `약 $` 를 소수 셋째 자리까지만 적으므로 되읽은 값은 그 반올림분이다.
  assert.equal(scan.entries[0].tok.usd, Number(one.costUsd.toFixed(3)));
  assert.equal(scan.entries[0].tok.model, one.byModel.map((r) => r.model).join('+'));
  // 그리고 그 기록의 돈은 **버려지지 않고** 「못 쪼갠 돈」으로 남는다.
  assert.equal(scan.skipped.length, 1, JSON.stringify(scan.skipped));
  assert.equal(scan.skipped[0].usd, incomplete.knownCostUsd);
  // 못 읽은 줄은 숫자를 내지 말라는 신호다 — 조용히 넘어가면 합계가 모르게 적어진다.
  assert.equal(scanLines(['## 2026-01-01', '> **비용**'], 'f.md').bad.length, 1);
  // 어느 회차 것인지 모르는 비용 줄도 추측하지 않는다.
  assert.equal(scanLines(['## 2026-01-01', fullLine], 'f.md').bad.length, 1);
  /* **회차가 아닌 블록 안의 비용 줄은 앞 회차에 붙으면 안 된다.**
   *
   * 지금 나가는 사용량 기록에는 모델이 없어서 `cur` 을 건드릴 일이 없지만, 그건 **오늘의
   * 우연**이지 계약이 아니다. `####` 블록 안에 모델이 실린 줄이 한 번만 들어오면 앞 회차의
   * 숫자가 통째로 그것으로 바뀐다 — 두 회차의 돈이 섞이고 아무 표시가 없다. 가드가 막는
   * 것이 이것이고, 그 계약을 여기서 못 박는다 (2026-09-16 변이 시험 M13). */
  const intruded = scanLines([
    '## 2026-01-01',
    ...renderEntry({ at: '2026-01-01T00:00:00Z', kind: 'qa', asker: '아무개', origin: '#어디', elapsedMs: 1000, ...usageFields(one) }).split('\n'),
    '#### 회차가 아닌 블록',
    '',
    fullLine,
  ], 'f.md');
  assert.equal(intruded.entries.length, 1);
  assert.equal(intruded.entries[0].tok.usd, Number(one.costUsd.toFixed(3)), '앞 회차의 금액이 덮였습니다');
  assert.equal(intruded.bad.length, 1, '회차 밖 비용줄은 못 읽은 줄로 올려야 합니다');
});
await check('live plan is synthetic, capped and rejects altered limits/prices', async () => {
  const models = Object.fromEntries(['qa', 'daily', 'weekly', 'summaryCheck'].map((role) => [role, config.models.qa]));
  const plan = await syntheticPlan(models);
  assert.equal(plan.length, 4);
  assert.ok(plan.every((r) => r.params.max_tokens === 512 && !r.params.tools));
  assert.ok(plan.every((r) => !JSON.stringify(r).includes('SECRET_MARKER')));
  // Date-sensitive execution guard is intentionally evaluated with an explicit
  // fake clock here; the real live command uses the actual UTC date.
  const nativeDate = globalThis.Date;
  globalThis.Date = class extends nativeDate {
    constructor(...args) { super(...(args.length ? args : ['2026-09-28T00:00:00Z'])); }
  };
  try {
    assert.ok(validateLivePlan(plan, [100, 100, 100, 100], '2026-09-28') < 2);
    for (const mutate of [
      (p) => { p[0].params.max_tokens = 513; },
      (p) => { p[0].params.model = 'unpriced'; },
      (p) => { p[0].params.tools = []; },
      (p) => { p[0].role = 'weekly'; },
    ]) {
      const changed = structuredClone(plan); mutate(changed);
      assert.throws(() => validateLivePlan(changed, [100, 100, 100, 100], '2026-09-28'));
    }
    assert.throws(() => validateLivePlan(plan, [100, 100, 100, 4097], '2026-09-28'));
    assert.throws(() => validateLivePlan(plan, [100, 100, 100, 100], '2026-09-27'));
  } finally { globalThis.Date = nativeDate; }
});
console.log('\n' + passed + ' accounting checks passed; live model calls 0.');
