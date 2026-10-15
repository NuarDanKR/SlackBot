#!/usr/bin/env node
/**
 * 요약 검토 후보 RPC — **장애를 정상 0건으로 숨기지 않는가.**
 *
 *   node scripts/check-rpc-review.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 이 검사가 있나 ──
 *
 * 검토자는 후보가 없어도 DM 을 받는다. 그래서 **「없었다」 와 「못 만들었다」 를
 * 가르는 것**이 이 기능의 전부다. 둘을 합치면 모델이 죽은 날에도 검토자는
 * 「오늘은 요약할 게 없군요」 를 받고, 그 상태가 며칠 간다 — 아무도 신고하지 않는다.
 *
 * 그래서 여기서 재는 것의 절반은 **`empty` 가 아닌 경우**다.
 *
 * ── 자리 ──
 *
 * 운영은 Unix socket. 개발 PC(Windows)는 `AF_UNIX` listen 이 막혀 named pipe 에
 * 연다 — `listen(path)` 와 `http.request({socketPath})` 는 같은 API 다.
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { createConcurrencyGate } from '../src/rpc/limits.js';
import { createReviewer, parseModelOutput, validateCandidate } from '../src/rpc/review.js';
import { renderReviewPrompt } from '../src/rpc/review-prompt.js';
import { createRpcServer } from '../src/rpc/server.js';
import * as review from '../src/rpc/review-wire.js';
import * as wire from '../src/rpc/wire.js';

const SECRET = crypto.randomBytes(32);
const WORKSPACE = 'tyit';
const MODEL = { id: 'stub-model', maxTokens: 4000, effort: 'high' };

let failed = 0;
const lines = [];
async function test(name, fn) {
  try { await fn(); lines.push(`  ✓ ${name}`); }
  catch (err) { failed += 1; lines.push(`  ✗ ${name}\n      ${String(err.message).split('\n')[0]}`); }
}

const WIN = process.platform === 'win32';
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-review-'));
let seq = 0;
const place = (n) => (WIN
  ? `\\\\.\\pipe\\hermes-review-${process.pid}-${n}-${seq += 1}`
  : path.join(tmp, `${n}-${seq += 1}.sock`));
const listenOpts = WIN ? { mode: null, prepareDir: false } : {};
const shut = [];

/* ── 가상 원문 ─────────────────────────────────────────────────────────── */
const SOURCES = [
  { id: 'r1', at: '2026-10-14 09:12', author: '김과장',
    text: '김해외동 잔금은 12억 4천만원으로 확정했습니다.' },
  { id: 'r2', at: '2026-10-14 10:40', author: '이대리',
    text: '대출 만기는 2026-12-31 로 연장 협의가 끝났습니다.' },
  { id: 'r3', at: '2026-10-14 11:05', author: '박부장',
    text: '감리 지적 사항은 이번 주 안에 회신하기로 했습니다.' },
];
const APPROVED = ['이미 승인된 문장입니다.'];
const DIGEST = review.sourceDigestOf(SOURCES);

const body = (over = {}) => Buffer.from(JSON.stringify({
  schema: review.SCHEMA_REVIEW_REQUEST,
  request_id: 'rv-1',
  workspace: WORKSPACE,
  source_digest: DIGEST,
  sources: SOURCES,
  approved: APPROVED,
  limits: { deadline_ms: 90_000, max_candidates: 10 },
  ...over,
}), 'utf8');

const candidate = (over = {}) => ({
  kind: 'number_or_schedule',
  form: 'quote',
  source_id: 'r1',
  current_text: '',
  proposed_text: '김해외동 잔금은 12억 4천만원으로 확정했습니다.',
  evidence_quote: '김해외동 잔금은 12억 4천만원으로 확정했습니다.',
  ...over,
});

/** 대본 모델. `turn` 을 그대로 돌려주거나 던진다. */
function scriptedModel(turn, { throws, delayMs } = {}) {
  return () => ({
    async generate({ signal }) {
      if (delayMs) {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(resolve, delayMs);
          timer.unref?.();
          signal?.addEventListener?.('abort', () => {
            clearTimeout(timer);
            reject(Object.assign(new Error('aborted'), { name: 'AbortError' }));
          }, { once: true });
        });
      }
      if (throws) throw Object.assign(new Error('provider down'), { code: throws });
      return turn;
    },
  });
}

const modelTurn = (candidates, over = {}) => ({
  text: JSON.stringify({ candidates }),
  stopReason: 'end_turn',
  usage: { input_tokens: 1200, output_tokens: 180 },
  ...over,
});

async function boot({ turn, throws, delayMs, limits, reviewRoute, logs } = {}) {
  const gate = createConcurrencyGate(limits || { max: 4, maxPerWorkspace: 2 });
  const reviewer = createReviewer({
    textModel: scriptedModel(turn, { throws, delayMs }),
    renderPrompt: () => '시스템 프롬프트(시험)',
    model: MODEL,
    console: { error() {}, warn() {} },
  });
  const worker = createRpcServer({
    secret: SECRET, workspace: WORKSPACE, version: 'review-test',
    answerer: { answer: async () => ({ status: 'failed', answer: '', usedEvidence: [], uncertain: [], usage: {} }) },
    reviewer: reviewRoute === null ? undefined : reviewer,
    limits: gate,
    console: logs
      ? { info: (l) => logs.push(l), error: (l) => logs.push(l), warn: (l) => logs.push(l) }
      : { error() {}, warn() {} },
  });
  const at = await worker.listen(place('review'), listenOpts);
  shut.push(worker);
  return { worker, socketPath: at, gate, reviewer };
}

function call(socketPath, route, payload, { headers, timeoutMs = 10_000 } = {}) {
  const signed = { ...wire.signatureHeaders(SECRET, { path: route, body: payload }), ...headers };
  return new Promise((resolve, reject) => {
    const req = http.request({
      socketPath, path: route, method: 'POST',
      headers: { Host: 'tybot', Connection: 'close', 'Content-Length': payload.length, ...signed },
    }, (res) => {
      const c = [];
      res.on('data', (x) => c.push(x));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(c) }));
    });
    req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('timeout')); });
    req.on('error', reject);
    req.end(payload);
  });
}

/** 저쪽이 하는 순서대로 — **서명을 먼저** 본다. */
function readReview(reply, requestId = 'rv-1') {
  assert.equal(reply.status, 200, `status ${reply.status}`);
  assert.ok(wire.safeEqualHex(
    wire.responseSignature(SECRET, { requestId, body: reply.body }),
    reply.headers['x-tybot-signature'] || '',
  ), '응답 서명이 맞지 않습니다');
  const got = JSON.parse(reply.body.toString('utf8'));
  assert.equal(got.schema, review.SCHEMA_REVIEW_RESPONSE);
  assert.equal(got.request_id, requestId);
  assert.equal(got.workspace, WORKSPACE);
  assert.equal(got.source_digest, DIGEST, 'source_digest 가 요청값과 다릅니다');
  assert.ok(review.REVIEW_STATUSES.includes(got.status), `모르는 상태: ${got.status}`);
  for (const key of ['model', 'input_tokens', 'output_tokens', 'stop_reason',
    'elapsed_ms', 'proposed_count', 'accepted_count', 'rejected_counts']) {
    assert.ok(key in got.metrics, `metrics 에 ${key} 가 없습니다`);
  }
  return got;
}

const waitFor = async (cond, ms = 2_000) => {
  const until = Date.now() + ms;
  while (!cond() && Date.now() < until) await new Promise((r) => setTimeout(r, 5));
  assert.ok(cond(), `기다리던 상태가 ${ms}ms 안에 안 됐습니다`);
};

/* ── 1. 정상 ──────────────────────────────────────────────────────────── */
console.log('\n[1/8] 정상 후보');

await test('후보를 만들고 source_id·시각·작성자를 원문에서 채운다', async () => {
  const s = await boot({ turn: modelTurn([candidate(), candidate({
    source_id: 'r2', form: 'abstract', kind: 'number_or_schedule',
    proposed_text: '대출 만기를 2026-12-31 로 연장하기로 협의가 끝났습니다.',
    evidence_quote: '대출 만기는 2026-12-31 로 연장 협의가 끝났습니다.',
  })]) });
  const got = readReview(await call(s.socketPath, review.REVIEW_ENDPOINT, body()));
  assert.equal(got.status, 'ok');
  assert.equal(got.error_code, '');
  assert.equal(got.candidates.length, 2);
  assert.equal(got.metrics.proposed_count, 2);
  assert.equal(got.metrics.accepted_count, 2);
  assert.deepEqual(got.metrics.rejected_counts, {});
  // 시각·작성자는 **요청이 준 원문** 값이다. 모델이 적은 것을 믿지 않는다.
  assert.equal(got.candidates[0].evidence_at, '2026-10-14 09:12');
  assert.equal(got.candidates[0].evidence_author, '김과장');
  assert.equal(got.candidates[1].source_id, 'r2');
});

await test('max_candidates 를 넘기면 자르고 건수로 보고한다', async () => {
  const many = Array.from({ length: 4 }, (_, i) => candidate({
    source_id: 'r3', form: 'quote',
    proposed_text: '감리 지적 사항은 이번 주 안에 회신하기로 했습니다.',
    evidence_quote: '감리 지적 사항은 이번 주 안에 회신하기로 했습니다.',
    current_text: `중복 ${i}`,
  }));
  const s = await boot({ turn: modelTurn([candidate(), ...many]) });
  const got = readReview(await call(s.socketPath, review.REVIEW_ENDPOINT,
    body({ limits: { deadline_ms: 90_000, max_candidates: 2 } })));
  assert.equal(got.status, 'ok');
  assert.ok(got.candidates.length <= 2, `${got.candidates.length}건`);
  assert.equal(got.metrics.proposed_count, 5);
});

/* ── 2. empty 는 **한 경우뿐** ─────────────────────────────────────────── */
console.log('[2/8] empty 는 모델이 0건을 낸 경우뿐');

await test('모델이 빈 목록을 내면 empty', async () => {
  const s = await boot({ turn: modelTurn([]) });
  const got = readReview(await call(s.socketPath, review.REVIEW_ENDPOINT, body()));
  assert.equal(got.status, 'empty');
  assert.equal(got.error_code, '');
  assert.deepEqual(got.candidates, []);
  assert.equal(got.metrics.proposed_count, 0);
});

await test('전부 탈락은 empty 가 아니라 failed 다', async () => {
  // 모델이 **없는 원문 ID** 로 후보를 냈다. 「없었다」 가 아니라 쓸 수 없는 것을 받았다.
  const s = await boot({ turn: modelTurn([candidate({ source_id: 'r9' })]) });
  const got = readReview(await call(s.socketPath, review.REVIEW_ENDPOINT, body()));
  assert.equal(got.status, 'failed');
  assert.equal(got.error_code, 'candidate_validation_failed');
  assert.deepEqual(got.candidates, []);
  assert.equal(got.metrics.proposed_count, 1);
  assert.equal(got.metrics.accepted_count, 0);
  assert.deepEqual(got.metrics.rejected_counts, { unknown_source_id: 1 });
});

for (const [name, turn, code] of [
  ['잘린 출력', modelTurn([candidate()], { stopReason: 'max_tokens' }), 'truncated_output'],
  ['깨진 JSON', { text: '{"candidates": [', stopReason: 'end_turn', usage: {} }, 'invalid_json'],
  ['JSON 아닌 산문', { text: '후보가 없습니다.', stopReason: 'end_turn', usage: {} }, 'invalid_json'],
  ['candidates 가 배열이 아님', { text: '{"candidates":{}}', stopReason: 'end_turn', usage: {} }, 'invalid_json'],
]) {
  await test(`${name}은 empty 가 아니라 failed/${code}`, async () => {
    const s = await boot({ turn });
    const got = readReview(await call(s.socketPath, review.REVIEW_ENDPOINT, body()));
    assert.equal(got.status, 'failed');
    assert.equal(got.error_code, code);
    assert.deepEqual(got.candidates, []);
  });
}

await test('provider 오류는 failed/model_error', async () => {
  const s = await boot({ throws: 'ECONNRESET' });
  const got = readReview(await call(s.socketPath, review.REVIEW_ENDPOINT, body()));
  assert.equal(got.status, 'failed');
  assert.equal(got.error_code, 'model_error');
});

/* ── 3. 후보 검증 ─────────────────────────────────────────────────────── */
console.log('[3/8] 후보 검증 — 느슨해지지 않는다');

const sourceMap = new Map(SOURCES.map((item) => [item.id, item]));
const approvedSet = new Set(APPROVED.map((t) => t.replace(/\s+/g, ' ').trim()));
const check = (over) => validateCandidate(candidate(over), {
  sources: sourceMap, approvedSet, seen: new Set(),
});

await test('인용이 원문에 없으면 버린다', () => {
  assert.equal(check({ evidence_quote: '원문에 없는 문장입니다.' }).reason, 'quote_not_in_source');
});
await test('다른 원문의 문장을 인용하면 사유를 가른다', () => {
  assert.equal(check({
    evidence_quote: '대출 만기는 2026-12-31 로 연장 협의가 끝났습니다.',
    proposed_text: '대출 만기는 2026-12-31 로 연장 협의가 끝났습니다.',
  }).reason, 'quote_from_other_source');
});
await test('quote 인데 문장이 다르면 버린다', () => {
  assert.equal(check({ proposed_text: '잔금이 확정됐습니다.' }).reason, 'quote_mismatch');
});
await test('원문에 없는 숫자는 form 과 무관하게 버린다', () => {
  assert.equal(check({
    source_id: 'r1', form: 'abstract',
    proposed_text: '김해외동 잔금 12억 5천만원이 확정됐습니다.',
  }).reason, 'invented_number');
  // 인용에 있는 숫자는 통과한다.
  assert.equal(check({
    source_id: 'r1', form: 'abstract',
    proposed_text: '잔금 12억 4천만원이 확정됐습니다.',
  }).ok, true);
});
await test('승인 문장만 그대로 베낀 후보는 버린다', () => {
  // 인용은 원문에서 가져왔지만 제안 문장이 이미 승인된 것이다 — 새 사실이 아니다.
  const sources = new Map([['r1', { id: 'r1', at: '', author: '', text: '이미 승인된 문장입니다.' }]]);
  const got = validateCandidate(candidate({
    source_id: 'r1',
    proposed_text: '이미 승인된 문장입니다.',
    evidence_quote: '이미 승인된 문장입니다.',
  }), { sources, approvedSet, seen: new Set() });
  assert.equal(got.reason, 'approved_text_only');
});
await test('모르는 kind·form·빈 인용·빈 제안을 버린다', () => {
  assert.equal(check({ kind: 'etc' }).reason, 'bad_kind');
  assert.equal(check({ form: 'paraphrase' }).reason, 'bad_form');
  assert.equal(check({ evidence_quote: '  ' }).reason, 'missing_quote');
  assert.equal(check({ proposed_text: '' }).reason, 'empty_proposed_text');
});
await test('같은 제안 문장을 두 번 내면 한 번만 받는다', () => {
  const seen = new Set();
  const args = { sources: sourceMap, approvedSet, seen };
  assert.equal(validateCandidate(candidate(), args).ok, true);
  assert.equal(validateCandidate(candidate(), args).reason, 'duplicate_candidate');
});
await test('코드 블록으로 감싼 출력도 읽는다', () => {
  const got = parseModelOutput('```json\n{"candidates":[]}\n```');
  assert.equal(got.ok, true);
  assert.deepEqual(got.candidates, []);
});

/* ── 4. 요청 상한·형식 ────────────────────────────────────────────────── */
console.log('[4/8] 요청 계약 — 관대하게 받지 않는다');

const guard = await boot({ turn: modelTurn([]) });
const rejects = async (name, over, expect = 400) => {
  await test(name, async () => {
    const r = await call(guard.socketPath, review.REVIEW_ENDPOINT, body(over));
    assert.equal(r.status, expect);
  });
};
await rejects('스키마가 다르면 400', { schema: 'tybot.specialist.request.v3' });
await rejects('sources 가 비면 400', { sources: [] });
await rejects('원문 ID 가 중복이면 400', { sources: [SOURCES[0], { ...SOURCES[1], id: 'r1' }] });
await rejects('원문 ID 형식이 틀리면 400', { sources: [{ ...SOURCES[0], id: 'C123' }] });
await rejects('digest 가 hex 64자가 아니면 400', { source_digest: 'abc' });
await rejects('digest 가 대문자면 400', { source_digest: DIGEST.toUpperCase() });
await rejects('원문 합계 상한을 넘으면 400', {
  sources: [{ id: 'r1', at: '', author: '', text: '가'.repeat(review.MAX_SOURCE_CHARS + 1) }],
});
await rejects('승인 문장 합계 상한을 넘으면 400', {
  approved: ['가'.repeat(review.MAX_APPROVED_CHARS + 1)],
});
await rejects('deadline 이 상한을 넘으면 400', {
  limits: { deadline_ms: review.MAX_REVIEW_DEADLINE_MS + 1 },
});
await rejects('max_candidates 가 범위 밖이면 400', { limits: { max_candidates: 11 } });
await rejects('다른 workspace 는 400', { workspace: 'mgmt', request_id: 'rv-ws' });

await test('요청 본문 상한을 넘으면 413', async () => {
  const big = Buffer.alloc(review.MAX_REVIEW_REQUEST_BYTES + 1, 0x20);
  const r = await call(guard.socketPath, review.REVIEW_ENDPOINT, big);
  assert.equal(r.status, 413);
});

/* ── 5. 서명·재생·중복 ───────────────────────────────────────────────── */
console.log('[5/8] 서명·재생·중복 — Q&A 와 같은 규칙');

await test('서명이 틀리면 401', async () => {
  const r = await call(guard.socketPath, review.REVIEW_ENDPOINT, body(),
    { headers: { 'X-TYBot-Signature': 'f'.repeat(64) } });
  assert.equal(r.status, 401);
});
await test('Q&A 경로로 서명하면 검토 경로에서 401 — 경로가 서명에 들어간다', async () => {
  const payload = body();
  const headers = wire.signatureHeaders(SECRET, { path: wire.ANSWER_ENDPOINT, body: payload });
  const r = await call(guard.socketPath, review.REVIEW_ENDPOINT, payload, { headers });
  assert.equal(r.status, 401);
});
await test('nonce 재사용은 401', async () => {
  const payload = body({ request_id: 'rv-nonce' });
  const headers = wire.signatureHeaders(SECRET, {
    path: review.REVIEW_ENDPOINT, body: payload, nonce: 'review-nonce-1',
  });
  assert.equal((await call(guard.socketPath, review.REVIEW_ENDPOINT, payload, { headers })).status, 200);
  assert.equal((await call(guard.socketPath, review.REVIEW_ENDPOINT, payload, { headers })).status, 401);
});

await test('Q&A 와 검토가 같은 request_id 를 쓰면 409', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const gate = createConcurrencyGate({ max: 4, maxPerWorkspace: 4 });
  const worker = createRpcServer({
    secret: SECRET, workspace: WORKSPACE, version: 'x', limits: gate,
    answerer: { answer: async () => { await held; return { status: 'no_evidence', answer: '…', usedEvidence: [], uncertain: [], usage: {} }; } },
    reviewer: { review: async () => ({ status: 'empty', candidates: [], errorCode: '', metrics: {} }) },
    console: { error() {}, warn() {} },
  });
  const at = await worker.listen(place('dup'), listenOpts);
  shut.push(worker);

  const qa = call(at, wire.ANSWER_ENDPOINT, Buffer.from(JSON.stringify({
    schema: wire.SCHEMA_REQUEST, request_id: 'shared-id', authorization_id: 'a',
    workspace: WORKSPACE, question: 'q', evidence: [], follow_up: [],
    tools: { token: 't', endpoint: wire.TOOL_ENDPOINT, allow: ['search'], budget: {} },
    limits: { deadline_ms: 90000, max_output_chars: 3000 },
  }), 'utf8'));
  await waitFor(() => worker.inFlightCount === 1);
  const dup = await call(at, review.REVIEW_ENDPOINT, body({ request_id: 'shared-id' }));
  assert.equal(dup.status, 409, '검토가 Q&A 의 request_id 를 덮어썼습니다');
  release();
  await qa;
});

await test('취소 endpoint 를 공유한다', async () => {
  const s = await boot({ turn: modelTurn([candidate()]), delayMs: 60_000 });
  const pending = call(s.socketPath, review.REVIEW_ENDPOINT,
    body({ request_id: 'rv-cancel', limits: { deadline_ms: 60_000 } }), { timeoutMs: 15_000 });
  await waitFor(() => s.worker.inFlightCount === 1);
  const cancel = Buffer.from(JSON.stringify({ request_id: 'rv-cancel' }), 'utf8');
  assert.equal((await call(s.socketPath, wire.CANCEL_ENDPOINT, cancel)).status, 200);
  const got = readReview(await pending, 'rv-cancel');
  assert.equal(got.status, 'failed');
  assert.equal(got.error_code, 'cancelled');
  await waitFor(() => s.gate.inFlight === 0, 3_000);
});

/* ── 6. 동시 상한을 Q&A 와 **합산** ──────────────────────────────────── */
console.log('[6/8] 동시 상한 — Q&A 와 합산한다');

await test('Q&A 가 자리를 쓰고 있으면 검토가 429', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const gate = createConcurrencyGate({ max: 1, maxPerWorkspace: 1 });
  const worker = createRpcServer({
    secret: SECRET, workspace: WORKSPACE, version: 'x', limits: gate,
    answerer: { answer: async () => { await held; return { status: 'no_evidence', answer: '…', usedEvidence: [], uncertain: [], usage: {} }; } },
    reviewer: { review: async () => ({ status: 'empty', candidates: [], errorCode: '', metrics: {} }) },
    console: { error() {}, warn() {} },
  });
  const at = await worker.listen(place('mix'), listenOpts);
  shut.push(worker);

  const qa = call(at, wire.ANSWER_ENDPOINT, Buffer.from(JSON.stringify({
    schema: wire.SCHEMA_REQUEST, request_id: 'mix-qa', authorization_id: 'a',
    workspace: WORKSPACE, question: 'q', evidence: [], follow_up: [],
    tools: { token: 't', endpoint: wire.TOOL_ENDPOINT, allow: ['search'], budget: {} },
    limits: { deadline_ms: 90000, max_output_chars: 3000 },
  }), 'utf8'));
  await waitFor(() => gate.inFlight === 1);

  // **검토용 별도 풀이 없다.** 있으면 합산이 상한을 넘는다.
  const blocked = await call(at, review.REVIEW_ENDPOINT, body({ request_id: 'mix-rv' }));
  assert.equal(blocked.status, 429, '검토가 별도 슬롯을 썼습니다');
  assert.deepEqual(JSON.parse(blocked.body.toString('utf8')), { error: 'overloaded' });

  release();
  await qa;
  assert.equal(gate.inFlight, 0);
  // 429 는 request_id 를 점유하지 않는다 — 다시 보내면 통과해야 한다.
  const again = readReview(await call(at, review.REVIEW_ENDPOINT, body({ request_id: 'mix-rv' })), 'mix-rv');
  assert.equal(again.status, 'empty');
});

await test('예산 만료는 failed/model_timeout 이고 늦은 settle 전에는 슬롯을 안 준다', async () => {
  // 모델이 중단 신호를 받고도 바로 안 끝난다 — 진짜 SDK 의 정리 시간을 흉내 낸다.
  const state = { live: 0, peak: 0 };
  const slowModel = () => ({
    async generate({ signal }) {
      state.live += 1;
      state.peak = Math.max(state.peak, state.live);
      try {
        await new Promise((resolve, reject) => {
          const timer = setTimeout(resolve, 60_000);
          timer.unref?.();
          signal?.addEventListener?.('abort', () => {
            clearTimeout(timer);
            const tearing = setTimeout(
              () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })), 400,
            );
            tearing.unref?.();
          }, { once: true });
        });
      } finally { state.live -= 1; }
      return modelTurn([]);
    },
  });
  const gate = createConcurrencyGate({ max: 1, maxPerWorkspace: 1 });
  const worker = createRpcServer({
    secret: SECRET, workspace: WORKSPACE, version: 'x', limits: gate,
    answerer: { answer: async () => ({ status: 'failed', answer: '', usedEvidence: [], uncertain: [], usage: {} }) },
    reviewer: createReviewer({
      textModel: slowModel, renderPrompt: () => 'x', model: MODEL,
      console: { error() {}, warn() {} },
    }),
    console: { error() {}, warn() {} },
  });
  const at = await worker.listen(place('slow'), listenOpts);
  shut.push(worker);

  const started = Date.now();
  const got = readReview(await call(at, review.REVIEW_ENDPOINT,
    body({ request_id: 'rv-slow', limits: { deadline_ms: 2600 } }), { timeoutMs: 2600 }), 'rv-slow');
  assert.ok(Date.now() - started < 2600, '저쪽이 끊은 뒤에 답했습니다');
  assert.equal(got.status, 'failed');
  assert.equal(got.error_code, 'model_timeout');

  // 답은 나갔지만 **일은 아직 안 끝났다.** 슬롯이 돌아가 있으면 상한이 무너진다.
  const second = await call(at, review.REVIEW_ENDPOINT, body({ request_id: 'rv-slow2' }));
  assert.equal(second.status, 429, `살아 있는 모델 호출 ${state.live}건인데 새 요청이 통과했습니다`);
  await waitFor(() => state.live === 0, 3_000);
  assert.equal(state.peak, 1, `동시에 산 모델 호출 ${state.peak}건 (상한 1)`);
  await waitFor(() => gate.inFlight === 0, 3_000);
});

/* ── 7. capability · 미지원 worker ────────────────────────────────────── */
console.log('[7/8] capability — 미지원을 조용히 우회하지 않게');

await test('검토기가 있으면 health 가 capability 를 밝힌다', async () => {
  const s = await boot({ turn: modelTurn([]) });
  const r = await call(s.socketPath, wire.HEALTH_ENDPOINT, Buffer.from('{}', 'utf8'),
    { timeoutMs: wire.HEALTH_DEADLINE_MS });
  const got = JSON.parse(r.body.toString('utf8'));
  // 저쪽 validate_health 가 보는 셋은 그대로다.
  assert.equal(got.status, 'ok');
  assert.equal(got.contract_version, wire.CONTRACT_VERSION);
  assert.deepEqual(got.capabilities, [review.CAPABILITY_REVIEW]);
});

await test('검토기가 없으면 capability 가 비고 endpoint 는 404', async () => {
  const worker = createRpcServer({
    secret: SECRET, workspace: WORKSPACE, version: 'x',
    answerer: { answer: async () => ({ status: 'failed', answer: '', usedEvidence: [], uncertain: [], usage: {} }) },
    console: { error() {}, warn() {} },
  });
  const at = await worker.listen(place('nocap'), listenOpts);
  shut.push(worker);
  const r = await call(at, wire.HEALTH_ENDPOINT, Buffer.from('{}', 'utf8'));
  assert.deepEqual(JSON.parse(r.body.toString('utf8')).capabilities, []);
  // **404 다.** 「있는데 늘 실패」 면 TYBot 이 재시도하고, 재시도는 설정 오류를 장애처럼 만든다.
  assert.equal((await call(at, review.REVIEW_ENDPOINT, body())).status, 404);
});

/* ── 8. 로그·프롬프트 ────────────────────────────────────────────────── */
console.log('[8/8] 로그와 프롬프트');

await test('완료 로그에 원문·후보·승인 본문·키가 없다', async () => {
  const logs = [];
  const s = await boot({ turn: modelTurn([candidate()]), logs });
  readReview(await call(s.socketPath, review.REVIEW_ENDPOINT, body()));
  const done = logs.filter((l) => l.includes('검토 완료'));
  assert.equal(done.length, 1, `완료 줄 ${done.length}개:\n${logs.join('\n')}`);
  const line = done[0];
  assert.match(line, new RegExp(`workspace=${WORKSPACE}\\b`));
  assert.match(line, /request=rv-1\b/);
  assert.match(line, /status=ok\b/);
  assert.match(line, /proposed=1 accepted=1/);
  for (const secret of ['12억 4천만원', '김해외동', '김과장', '이미 승인된 문장', SECRET.toString('hex').slice(0, 8)]) {
    assert.ok(!line.includes(secret), `로그에 ${secret} 이 있습니다`);
  }
});

await test('프롬프트가 계약 파일을 그대로 쓴다 — 규칙을 베껴 적지 않는다', () => {
  const contract = fs.readFileSync(
    new URL('../contract/summary-review.md', import.meta.url), 'utf8',
  ).trimEnd();
  const rendered = renderReviewPrompt();
  assert.ok(rendered.startsWith(contract), '계약 본문이 프롬프트 앞에 그대로 있지 않습니다');
  // 덧붙이는 것은 전선 규약 한 가지뿐이다.
  assert.match(rendered.slice(contract.length), /source_id/);
  assert.ok(rendered.slice(contract.length).length < 400, '프롬프트에 규칙이 더 붙었습니다');
  assert.throws(() => renderReviewPrompt({ read: () => '' }), /비었/);
  assert.throws(() => renderReviewPrompt({
    read: () => { throw Object.assign(new Error('x'), { code: 'ENOENT' }); },
  }), /읽지 못했/);
});

await test('응답에 metrics 공통 키가 고정돼 있다', () => {
  const got = JSON.parse(review.buildReviewResponse({
    requestId: 'x', workspace: WORKSPACE, sourceDigest: DIGEST, status: 'empty',
    metrics: { model: 'm', rejected_counts: { bogus_reason: 3, missing_quote: 2 } },
  }).toString('utf8'));
  assert.deepEqual(Object.keys(got.metrics).sort(), [
    'accepted_count', 'elapsed_ms', 'input_tokens', 'model',
    'output_tokens', 'proposed_count', 'rejected_counts', 'stop_reason',
  ]);
  // allowlist 밖 사유는 버린다 — 모델이 만든 문자열이 통계 키가 되면 키가 무한히 는다.
  assert.deepEqual(got.metrics.rejected_counts, { missing_quote: 2 });
});

await test('failed 에 후보를, 비실패에 오류 코드를 못 싣는다', () => {
  assert.throws(() => review.buildReviewResponse({
    requestId: 'x', workspace: WORKSPACE, sourceDigest: DIGEST,
    status: 'failed', errorCode: 'model_error', candidates: [candidate()],
  }), /failed 에는 후보/);
  assert.throws(() => review.buildReviewResponse({
    requestId: 'x', workspace: WORKSPACE, sourceDigest: DIGEST,
    status: 'empty', errorCode: 'model_error',
  }), /오류 코드를 싣지/);
  assert.throws(() => review.buildReviewResponse({
    requestId: 'x', workspace: WORKSPACE, sourceDigest: DIGEST, status: 'failed', errorCode: 'oops',
  }), /allowlist/);
  assert.throws(() => review.buildReviewResponse({
    requestId: 'x', workspace: WORKSPACE, sourceDigest: DIGEST, status: 'ok', candidates: [],
  }), /1~10/);
});

/* ── 정리 ─────────────────────────────────────────────────────────────── */
for (const s of shut) await s.close().catch(() => {});
fs.rmSync(tmp, { recursive: true, force: true });

console.log(`\n${lines.join('\n')}`);
if (failed) {
  console.error(`\n✗ ${failed}건 어긋남`);
  process.exit(1);
}
console.log('\n✓ 검토 후보 RPC 가 계약대로 돈다');
