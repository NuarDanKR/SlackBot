#!/usr/bin/env node
/**
 * Codex R1b bounded smoke test. Default: capture synthetic payloads, NO network.
 * --execute --pricing-verified YYYY-MM-DD: requires separate human authorization.
 * 4 free token counts, then at most 4 inference requests (one per role).
 * No archives/prompts/logs/Slack/VM/Git writes. API retries and fallback disabled.
 */
import assert from 'node:assert/strict';
import { pathToFileURL } from 'node:url';
import Anthropic from '@anthropic-ai/sdk';
import { createDigestGenerator } from '../src/llm/digest.js';
import { createTextModel, createJsonModel } from '../src/llm/provider.js';
import { createSummaryChecker } from '../src/llm/summary-check.js';
import { createUsageCollector } from '../src/llm/usage.js';
import { estimateCost } from '../src/format.js';

export const PRICE_SOURCE = 'https://platform.claude.com/docs/en/about-claude/pricing';
/* 이 표는 **사람이 공시 가격을 다시 보고 손으로 적는 자리**다 — `src/format.js` 의 RATES 를
 * 가져다 쓰지 않는다. 가져다 쓰면 그쪽 오타가 유료 호출 한도를 그대로 통과시킨다.
 * 대신 아래 `validateLivePlan` 이 둘이 **같은지** 본다. 2026-09-15 에 Sonnet 5 단가를
 * format.js 에서만 올리고 여기를 잊어, 하필 한도를 재는 자리가 옛 단가로 남아 있었다. */
const VERIFIED_RATES = {
  // 캐시 읽기 배수가 표준(아래 VERIFIED_CACHE.read)과 다른 모델만 read 를 적는다 (2026-09-28 공시 확인).
  'claude-opus-5-5': { input: 4, output: 20, read: 0.05 },
  'claude-opus-5': { input: 5, output: 25 },
  'claude-opus-4-8': { input: 5, output: 25 },
  // Sonnet 5 는 $2/$10 이 정가로 확정됐다 — 예고됐던 9/1 $3/$15 인상은 취소 (2026-09-28 공시 확인).
  'claude-sonnet-5': { input: 2, output: 10 },
  'claude-haiku-4-5': { input: 1, output: 5 },
};
/* 캐시 단가는 입력 단가의 배수다 — 이것도 **사람이 공시를 보고 손으로 적는다.**
 * `format.js` 의 WRITE_5M·WRITE_1H·읽기 0.1 은 저기 한 곳에만 있어서, 2026-09-19 감사에서
 * 셋을 동시에 망가뜨려도(1.25→2.5, 2→4, 0.1→0.5) 상시 검사 35개가 전부 초록이었다.
 * 안 걸리던 이유가 두 겹이다 — 검사의 기대값을 `estimateCost` 자신으로 계산해서 단가가
 * 틀리면 기대값도 같이 틀어졌고, 월 비용 분해 도구도 같은 함수에서 단가를 되받아
 * 로그·월보고·분해표가 **다 같이 틀려 서로 어긋나지도 않았다.** */
const VERIFIED_CACHE = { write5m: 1.25, write1h: 2, read: 0.1 };
const DAY = '2026-09-28';
export function validateLivePlan(plan, counts, verifiedDay) {
  /* 두 단가표가 갈리면 여기서 멈춘다 — 한쪽만 고치고 지나가는 것이 이 자리의 사고였다.
   * **표 전체를 본다.** 계획에 든 모델만 대면 그 회차에 안 쓰인 모델(예: Sonnet 5)이
   * 옛 단가로 남아 있어도 통과한다 — 고치려던 바로 그 건이 그 모양이었다. */
  for (const [model, rate] of Object.entries(VERIFIED_RATES)) {
    assert.equal(estimateCost(model, { input_tokens: 1e6 }).usd, rate.input, `Input rate disagrees with src/format.js: ${model}`);
    assert.equal(estimateCost(model, { output_tokens: 1e6 }).usd, rate.output, `Output rate disagrees with src/format.js: ${model}`);
    /* 캐시 세 칸도 같이 본다. **`assert.equal` 을 쓰면 안 된다** — 읽기 배수 0.1 은 2진수로
     * 딱 떨어지지 않아서 (1e6 × 5 × 0.1) / 1e6 이 0.5 가 아니라 0.5000000000000001 이 된다.
     * 감사 중 이 반올림 때문에 한 변조가 「잡혔다」고 잘못 읽힌 적이 있다. 자릿수로 견준다. */
    for (const [label, usage, want] of [
      ['5m cache write', { cache_creation: { ephemeral_5m_input_tokens: 1e6 } }, rate.input * VERIFIED_CACHE.write5m],
      ['1h cache write', { cache_creation: { ephemeral_1h_input_tokens: 1e6 } }, rate.input * VERIFIED_CACHE.write1h],
      ['cache read', { cache_read_input_tokens: 1e6 }, rate.input * (rate.read ?? VERIFIED_CACHE.read)],
    ]) {
      const got = estimateCost(model, usage).usd;
      assert.ok(Math.abs(got - want) <= want * 1e-9,
        `${label} rate disagrees with src/format.js: ${model} (got ${got}, verified table says ${want})`);
    }
  }
  assert.equal(verifiedDay, DAY, 'Pricing verification date must match the audited table');
  assert.equal(new Date().toISOString().slice(0, 10), DAY, 'Stale price table: re-review required');
  assert.equal(plan.length, 4);
  assert.equal(counts.length, 4);
  assert.deepEqual(plan.map((r) => r.role), ['qa', 'daily', 'weekly', 'summaryCheck']);
  let ceilingUsd = 0;
  for (let i = 0; i < plan.length; i++) {
    const p = plan[i].params, rate = VERIFIED_RATES[p.model];
    assert.ok(rate, 'Unverified model rate');
    assert.equal(p.max_tokens, 512);
    assert.ok(!p.tools && !p.cache_control && !p.speed, 'No paid tools/cache/fast mode');
    assert.equal(p.service_tier, 'standard_only');
    assert.equal(p.messages.length, 1);
    assert.ok(Buffer.byteLength(JSON.stringify(p), 'utf8') <= 8192, 'Synthetic payload too large');
    assert.ok(Number.isSafeInteger(counts[i]) && counts[i] >= 0 && counts[i] <= 4096, 'Invalid/unexpected token count');
    // Token counts are estimates. More than double the count plus 16k tokens
    // of headroom covers small count/schema/system overhead; 10% geo premium
    // is included conservatively even when global is explicitly requested.
    ceilingUsd += ((2 * counts[i] + 16384) * rate.input + 512 * rate.output) * 1.1 / 1e6;
  }
  assert.ok(ceilingUsd <= 2, 'Cannot establish the authorized USD 2 bound');
  return ceilingUsd;
}

export async function syntheticPlan(models) {
  const cfg = { models: Object.fromEntries(['qa', 'daily', 'weekly', 'summaryCheck'].map((role) => {
    const m = models[role] || (role === 'summaryCheck' ? models.qa : null);
    assert.ok(m?.id, 'Missing role configuration: ' + role);
    return [role, { id: m.id, effort: m.effort, maxTokens: 512 }];
  })) };
  const plan = [];
  const capture = (role) => ({
    messages: {
      create: async (params) => {
        plan.push({ role, params });
        return { model: params.model, stop_reason: 'end_turn', content: [{ type: 'text', text: '{"findings":[]}' }],
          usage: { input_tokens: 0, output_tokens: 0 } };
      },
      stream: (params) => ({ finalMessage: () => capture(role).messages.create(params) }),
    },
  });
  plan.push({ role: 'qa', params: {
    model: cfg.models.qa.id, max_tokens: 512,
    output_config: { effort: cfg.models.qa.effort },
    system: [{ type: 'text', text: 'Synthetic API smoke test. Answer briefly.' }],
    messages: [{ role: 'user', content: 'What is 2 plus 2? Reply with the number only.' }],
  } });
  for (const kind of ['daily', 'weekly']) {
    await createDigestGenerator({
      config: cfg, textModel: (spec) => createTextModel(spec, { client: () => capture(kind) }),
      promptFile: () => 'Summarize the synthetic conversation in one sentence.',
      logUsage() {}, estimateCost, errLabel: () => 'fixture', sleep: async () => { throw new Error('Retries disabled'); },
    })({ kind, transcript: 'Person A: The sample task is complete. Person B: Confirmed.',
      windowLabel: 'Synthetic interval', stats: '2 synthetic messages' });
  }
  await createSummaryChecker({
    config: cfg, jsonModel: (spec) => createJsonModel(spec, { client: () => capture('summaryCheck') }), estimateCost, logUsage() {},
    promptFile: () => 'Compare synthetic summary and transcript. If consistent return {"findings":[]}.',
  })({ channel: 'synthetic', summary: 'The sample task is complete.', transcript: 'The sample task is complete.' });
  return plan.map(({ role, params }) => ({
    role, params: { ...params, service_tier: 'standard_only',
      ...(params.model === 'claude-haiku-4-5' ? {} : { inference_geo: 'global' }) },
  }));
}

async function main() {
  const args = process.argv.slice(2);
  const execute = args[0] === '--execute';
  if (args.length && (!execute || args.length !== 3 || args[1] !== '--pricing-verified')) {
    throw new Error('Usage: node scripts/check-live-llm.js [--execute --pricing-verified YYYY-MM-DD]');
  }
  // Read only model settings. Never load claude.js, archive readers or production prompts.
  const { config, requireEnv } = await import('../src/config.js');
  const plan = await syntheticPlan(config.models);
  for (const { params } of plan) assert.ok(VERIFIED_RATES[params.model], 'Unknown model price; no requests sent');
  if (!execute) {
    console.log(JSON.stringify({ mode: 'offline-plan', inferenceRequests: 0, roles: plan.map((r) => ({ role: r.role, model: r.params.model, maxTokens: 512 })) }));
    return;
  }
  // Validate the fixed table/date and conservative worst allowed counts before
  // even the free preflight. Then validate again with actual token estimates.
  validateLivePlan(plan, [4096, 4096, 4096, 4096], args[2]);
  const { ANTHROPIC_API_KEY } = requireEnv(['ANTHROPIC_API_KEY']);
  let inferenceRequests = 0, countRequests = 0;
  const nativeFetch = globalThis.fetch;
  const client = new Anthropic({
    apiKey: ANTHROPIC_API_KEY, baseURL: 'https://api.anthropic.com',
    maxRetries: 0, timeout: 60000,
    fetch: async (url, init) => {
      const u = new URL(typeof url === 'string' ? url : url.url || url.href);
      assert.equal(u.origin, 'https://api.anthropic.com');
      if (u.pathname === '/v1/messages/count_tokens') {
        assert.ok(++countRequests <= 4, 'Token count cap exceeded');
      } else {
        assert.equal(u.pathname, '/v1/messages');
        assert.ok(++inferenceRequests <= 4, 'Inference request cap exceeded');
      }
      return nativeFetch(url, { ...init, redirect: 'error' });
    },
  });
  const counts = [];
  for (const { params } of plan) {
    const r = await client.messages.countTokens({ model: params.model, system: params.system, messages: params.messages });
    counts.push(r.input_tokens);
  }
  const ceilingUsd = validateLivePlan(plan, counts, args[2]);
  console.log(JSON.stringify({ mode: 'preflight', countRequests, inferenceRequests, counts, ceilingUsd, priceSource: PRICE_SOURCE }));
  const ledger = createUsageCollector();
  const results = [];
  try {
    for (const { role, params } of plan) {
      let final;
      try { final = await client.messages.create(params); }
      catch (err) { ledger.failed(params.model); throw err; }
      ledger.record(final, params.model);
      results.push({ role, model: final.model, stopReason: final.stop_reason, usage: final.usage });
      assert.ok(final.usage?.output_tokens <= 512, 'Unexpected output usage; stop remaining requests');
      assert.ok(ledger.snapshot().complete, 'Unknown returned model/usage; stop remaining requests');
      assert.ok(ledger.snapshot().knownCostUsd <= ceilingUsd, 'Observed cost exceeded preflight; stop remaining requests');
      assert.equal(final.stop_reason, 'end_turn', 'Incomplete/refused result; do not retry');
      if (role === 'summaryCheck') {
        const text = final.content.filter((b) => b.type === 'text').map((b) => b.text).join('\n');
        assert.ok(Array.isArray(JSON.parse(text).findings));
      }
    }
  } finally {
    console.log(JSON.stringify({ mode: 'result', countRequests, inferenceRequests, results, accounting: ledger.snapshot() }));
  }
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch((err) => { console.error('Live check stopped:', err?.status || '', err?.name || 'Error'); process.exitCode = 1; });
}
