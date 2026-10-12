#!/usr/bin/env node
/**
 * **공유** RPC worker — 여러 workspace 를 한 프로세스가 받을 때 무엇이 안 섞이나.
 *
 *   node scripts/check-rpc-shared.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 무엇을 재는가 ──
 *
 *   1. 명부(profiles.json) — 비밀 인라인·wildcard·모르는 필드·전역 fallback 거절
 *   2. workspace 선택 — domain·모델·API 키가 그 workspace 것으로 간다
 *   3. 교차 인용 — TYIT 와 MGMT 가 **동시에** 돌 때 남의 근거 ID 를 인용하면 버린다
 *   4. 취소 격리 — 하나를 취소해도 다른 하나는 끝까지 간다
 *   5. 동시성 — 전체·workspace 상한, 초과 429, 완료·오류·취소 뒤 슬롯 반환
 *   6. health — 전체와 workspace 별 준비 상태를 가른다
 *   7. 공유에서도 서명·nonce·중복 거절이 그대로다
 *
 * ── 가짜 키 ──
 *
 * 이 시험이 쓰는 키 문자열은 **아무 데도 안 통하는 글자**다. 진짜처럼 보이는 접두사를
 * 쓰지 않는다 — 커밋 가드가 막기도 하고, 더 중요하게는 나중에 누가 이 파일을 보고
 * 「여기 키가 있네」 라고 읽을 이유를 안 만든다.
 *
 * ── 자리 ──
 *
 * 운영은 Unix socket 이다. 개발 PC(Windows)는 `AF_UNIX` listen 이 막혀 있어 named
 * pipe 에 연다 — `listen(path)` 와 `http.request({socketPath})` 는 같은 API 라 재는
 * 것이 달라지지 않는다.
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { createConcurrencyGate } from '../src/rpc/limits.js';
import { ProfilesError, loadProfiles } from '../src/rpc/profiles.js';
import { createRpcServer } from '../src/rpc/server.js';
import { createToolClient } from '../src/rpc/tool-client.js';
import { buildRemoteTools } from '../src/rpc/tools.js';
import { createRpcAnswerer } from '../src/rpc/answer.js';
import { lazyTerms } from '../src/domain.js';
import { renderRpcPrompt } from '../src/rpc/prompt.js';
import * as wire from '../src/rpc/wire.js';

const SECRET = crypto.randomBytes(32);
/** 아무 데도 안 통하는 글자. 길이 검사를 넘기려고 길게 둔다. */
const FAKE_KEY = 'not-a-real-credential-0123456789';

let failed = 0;
const lines = [];
async function test(name, fn) {
  try { await fn(); lines.push(`  ✓ ${name}`); }
  catch (err) { failed += 1; lines.push(`  ✗ ${name}\n      ${String(err.message).split('\n')[0]}`); }
}

const WIN = process.platform === 'win32';
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-shared-'));
let seq = 0;
const place = (n) => (WIN
  ? `\\\\.\\pipe\\hermes-shared-${process.pid}-${n}-${seq += 1}`
  : path.join(tmp, `${n}-${seq += 1}.sock`));
const listenOpts = WIN ? { mode: null, prepareDir: false } : {};
const shut = [];

/* ── 명부 ──────────────────────────────────────────────────────────────── */
console.log('\n[1/7] 명부 — 설정이 거짓말하지 못하게');

function writeProfiles(obj, { keys = {} } = {}) {
  const dir = fs.mkdtempSync(path.join(tmp, 'p-'));
  const written = {};
  for (const [name, value] of Object.entries(keys)) {
    const at = path.join(dir, `${name}.key`);
    fs.writeFileSync(at, value);
    written[name] = at;
  }
  const file = path.join(dir, 'profiles.json');
  const resolved = JSON.parse(JSON.stringify(obj), (k, v) => (
    typeof v === 'string' && v.startsWith('@KEY:') ? written[v.slice(5)] : v
  ));
  fs.writeFileSync(file, JSON.stringify(resolved, null, 2));
  return file;
}

const goodProfiles = () => writeProfiles({
  tyit: { domain: 'pf-construction', api_key_file: '@KEY:tyit' },
  mgmt: { domain: 'enterprise', api_key_file: '@KEY:mgmt', model: 'claude-opus-5-5', effort: 'high' },
}, { keys: { tyit: `${FAKE_KEY}-tyit\n`, mgmt: `${FAKE_KEY}-mgmt\n` } });

await test('정상 명부를 읽는다 — domain·모델·키가 workspace 별로 갈린다', () => {
  const { profiles, readiness } = loadProfiles(goodProfiles());
  assert.deepEqual([...profiles.keys()].sort(), ['mgmt', 'tyit']);
  assert.equal(profiles.get('tyit').domain, 'pf-construction');
  assert.equal(profiles.get('mgmt').domain, 'enterprise');
  assert.notEqual(profiles.get('tyit').apiKey, profiles.get('mgmt').apiKey);
  // 파일 끝 줄바꿈만 뗀다.
  assert.equal(profiles.get('tyit').apiKey, `${FAKE_KEY}-tyit`);
  assert.deepEqual(readiness(), { tyit: true, mgmt: true });
});

await test('비밀을 명부에 적으면 거절한다', () => {
  for (const bad of ['api_key', 'apiKey', 'secret', 'token']) {
    const file = writeProfiles({ tyit: { domain: 'pf-construction', [bad]: 'xxxx' } });
    assert.throws(() => loadProfiles(file), ProfilesError, bad);
  }
});

await test('wildcard 를 거절한다 — 미등록 workspace 가 그냥 돈다', () => {
  const file = writeProfiles({ '*': { domain: 'enterprise', api_key_file: '@KEY:k' } },
    { keys: { k: `${FAKE_KEY}-k` } });
  assert.throws(() => loadProfiles(file), /쓸 수 있는 workspace 가 하나도/);
});

await test('모르는 필드를 거절한다 — 오타가 설정처럼 보이지 않게', () => {
  const file = writeProfiles({ tyit: { domain: 'enterprise', api_key_file: '@KEY:k', modle: 'x' } },
    { keys: { k: `${FAKE_KEY}-k` } });
  assert.throws(() => loadProfiles(file), /쓸 수 있는 workspace 가 하나도/);
});

await test('domain 이 없거나 오타면 그 workspace 만 떨어진다', () => {
  const file = writeProfiles({
    tyit: { domain: 'pf-construction', api_key_file: '@KEY:tyit' },
    mgmt: { domain: 'pf', api_key_file: '@KEY:mgmt' }, // 「그럴듯한」 값
  }, { keys: { tyit: `${FAKE_KEY}-a`, mgmt: `${FAKE_KEY}-b` } });
  const { profiles, readiness, problems } = loadProfiles(file);
  // **한 칸이 망가져도 나머지는 산다.** 공유 worker 는 재시작이 전부에 영향을 준다.
  assert.deepEqual([...profiles.keys()], ['tyit']);
  assert.deepEqual(readiness(), { tyit: true, mgmt: false });
  assert.ok(problems.some((p) => p.startsWith('mgmt:')));
});

await test('빈 키 파일과 상대경로를 거절한다', () => {
  const empty = writeProfiles({ tyit: { domain: 'enterprise', api_key_file: '@KEY:k' } },
    { keys: { k: '' } });
  assert.throws(() => loadProfiles(empty), /하나도/);
  const rel = writeProfiles({ tyit: { domain: 'enterprise', api_key_file: 'keys/tyit' } });
  assert.throws(() => loadProfiles(rel), /하나도/);
});

await test('빈 명부와 없는 파일을 거절한다', () => {
  assert.throws(() => loadProfiles(writeProfiles({})), /비었/);
  assert.throws(() => loadProfiles(path.join(tmp, 'nope.json')), /읽지 못했/);
  assert.throws(() => loadProfiles(''), /HERMES_RPC_PROFILES_FILE/);
});

/* ── 배선 ──────────────────────────────────────────────────────────────── */

/** `request_id` → 그 요청에 줄 본문·근거. 가짜 TYBot 도구 서버가 쓴다. */
function toolsServerFor(byRequest, seen) {
  return http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const call = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      seen.push({ requestId: call.request_id, token: call.token, tool: call.tool });
      const plan = byRequest[call.request_id] || { text: '(없음)', evidence: [] };
      const payload = Buffer.from(JSON.stringify({
        schema: wire.SCHEMA_TOOL_RESULT, call_id: call.call_id, ok: true,
        text: plan.text, evidence: plan.evidence, budget: {},
      }), 'utf8');
      res.writeHead(200, {
        'Content-Type': 'application/json',
        'Content-Length': payload.length,
        'X-TYBot-Signature': wire.responseSignature(SECRET, { requestId: call.request_id, body: payload }),
      });
      res.end(payload);
    });
  });
}

async function listenOn(server, label) {
  const at = place(label);
  await new Promise((r, j) => { server.once('error', j); server.listen(at, r); });
  shut.push({ close: () => new Promise((r) => server.close(r)) });
  return at;
}

/** 요청별 대본 모델. `answers[request_id]` 가 마지막 문장이다. */
function scriptedSession(answers, hooks = {}) {
  return () => ({
    open({ tools, messages }) {
      // 어느 요청인지는 user 메시지에 실린 질문으로 가른다(요청마다 다르게 준다).
      const asked = String(messages?.[0]?.content || '');
      const id = Object.keys(answers).find((k) => asked.includes(k)) || '';
      let i = 0;
      let pending = null;
      const steps = [
        { calls: [{ name: 'search', input: { query: 'x' } }] },
        { text: answers[id] ?? '없음' },
      ];
      return {
        previousMessageId: null,
        [Symbol.asyncIterator]: () => ({
          async next() {
            if (i >= steps.length) return { done: true, value: undefined };
            const s = steps[i++];
            pending = s.calls || null;
            return { done: false, value: {
              text: s.text || '', stopReason: s.calls ? 'tool_use' : 'end_turn',
              usage: { input_tokens: 1, output_tokens: 1 }, toolUses: [], raw: {},
            } };
          },
          return: async () => ({ done: true, value: undefined }),
        }),
        async generateToolResponse() {
          if (!pending) return null;
          const out = [];
          for (const c of pending) out.push(await tools.find((t) => t.name === c.name).run(c.input, {}));
          pending = null;
          await hooks.afterTools?.(id);
          return { role: 'user', content: out.map((text) => ({ type: 'tool_result', content: text })) };
        },
        continueWith() {},
      };
    },
  });
}

/** 공유 worker 를 띄운다. `profiles` 는 Map, `answers` 는 request_id → 문장. */
async function bootShared({ profiles, answers, byRequest, limits, hooks, readiness }) {
  const seen = [];
  const toolSocket = await listenOn(toolsServerFor(byRequest, seen), 'tools');
  const gate = createConcurrencyGate(limits || { max: 4, maxPerWorkspace: 2 });
  const used = [];

  const route = (ws) => {
    const profile = profiles.get(ws);
    if (!profile) return null;
    used.push({ ws, domain: profile.domain, model: profile.model.id, apiKey: profile.apiKey });
    return createRpcAnswerer({
      toolSession: scriptedSession(answers, hooks),
      buildTools: buildRemoteTools,
      createClient: ({ requestId, token, allow }) => createToolClient({
        socketPath: toolSocket, secret: SECRET, requestId, token, allow,
      }),
      renderPrompt: ({ maxOutputChars }) => renderRpcPrompt({ domain: profile.domain, maxOutputChars }),
      model: profile.model,
      maxIterations: profile.maxIterations,
      terms: lazyTerms(profile.domain),
      console: { error() {}, warn() {} },
    });
  };

  const worker = createRpcServer({
    secret: SECRET, version: 'shared-test', route, limits: gate, readiness,
    console: { error() {}, warn() {} },
  });
  const at = await worker.listen(place('shared'), listenOpts);
  shut.push(worker);
  return { worker, socketPath: at, seen, gate, used };
}

function call(socketPath, route, body, { headers, timeoutMs = 8_000 } = {}) {
  const signed = { ...wire.signatureHeaders(SECRET, { path: route, body }), ...headers };
  return new Promise((resolve, reject) => {
    const req = http.request({
      socketPath, path: route, method: 'POST',
      headers: { Host: 'tybot', Connection: 'close', 'Content-Length': body.length, ...signed },
    }, (res) => {
      const c = [];
      res.on('data', (x) => c.push(x));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(c) }));
    });
    req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('timeout')); });
    req.on('error', reject);
    req.end(body);
  });
}

const ask = (id, ws, token) => Buffer.from(JSON.stringify({
  schema: wire.SCHEMA_REQUEST, request_id: id, authorization_id: `auth-${id}`,
  workspace: ws, question: `질문 ${id}`, evidence: [], follow_up: [],
  tools: { token, endpoint: wire.TOOL_ENDPOINT, allow: ['search'], budget: {} },
  limits: { deadline_ms: 90000, max_output_chars: 3000 },
}), 'utf8');

function readAnswer(reply, requestId) {
  assert.equal(reply.status, 200, `status ${reply.status}`);
  assert.ok(wire.safeEqualHex(
    wire.responseSignature(SECRET, { requestId, body: reply.body }),
    reply.headers['x-tybot-signature'] || '',
  ), '응답 서명이 맞지 않습니다');
  const got = JSON.parse(reply.body.toString('utf8'));
  assert.equal(got.request_id, requestId);
  return got;
}

const waitFor = async (cond) => {
  for (let i = 0; i < 400 && !cond(); i += 1) await new Promise((r) => setTimeout(r, 5));
  assert.ok(cond(), '기다리던 상태가 안 됐습니다');
};

const PROFILES = loadProfiles(goodProfiles()).profiles;

/* ── 2. workspace 선택 ────────────────────────────────────────────────── */
console.log('[2/7] workspace 선택 — 설정이 고르고 요청이 고르지 않는다');

await test('요청 workspace 로 domain·모델·키를 고른다', async () => {
  const s = await bootShared({
    profiles: PROFILES,
    answers: { 'w-tyit': '답[e1].', 'w-mgmt': '답[e2].' },
    byRequest: {
      'w-tyit': { text: 'tyit 자료', evidence: [{ id: 'e1', kind: 'channel_line' }] },
      'w-mgmt': { text: 'mgmt 자료', evidence: [{ id: 'e2', kind: 'channel_line' }] },
    },
  });
  const a = readAnswer(await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('w-tyit', 'tyit', 'tok-A')), 'w-tyit');
  const b = readAnswer(await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('w-mgmt', 'mgmt', 'tok-B')), 'w-mgmt');
  assert.equal(a.status, 'answered');
  assert.equal(b.status, 'answered');
  const byWs = Object.fromEntries(s.used.map((u) => [u.ws, u]));
  assert.equal(byWs.tyit.domain, 'pf-construction');
  assert.equal(byWs.mgmt.domain, 'enterprise');
  assert.notEqual(byWs.tyit.apiKey, byWs.mgmt.apiKey, 'API 키가 같습니다');
});

await test('미등록 workspace 는 400 — 전역 fallback 이 없다', async () => {
  const s = await bootShared({ profiles: PROFILES, answers: {}, byRequest: {} });
  const r = await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('w-x', 'payroll', 'tok'));
  assert.equal(r.status, 400);
  assert.deepEqual(s.used, [], '모르는 workspace 인데 답변기를 만들었습니다');
});

await test('`shared` 는 workspace 가 아니다', async () => {
  const s = await bootShared({ profiles: PROFILES, answers: {}, byRequest: {} });
  assert.equal((await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('w-s', 'shared', 'tok'))).status, 400);
});

/* ── 3. 교차 인용 ─────────────────────────────────────────────────────── */
console.log('[3/7] 교차 인용 — 동시에 돌 때 남의 근거를 못 쓴다');

await test('TYIT 가 MGMT 의 근거 ID 를 인용하면 그 답을 버린다', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const s = await bootShared({
    profiles: PROFILES,
    // tyit 이 **mgmt 가 받은** e9 를 인용한다.
    answers: { 'x-tyit': '답[e9].', 'x-mgmt': '답[e9].' },
    byRequest: {
      'x-tyit': { text: 'tyit 자료', evidence: [{ id: 'e1', kind: 'channel_line' }] },
      'x-mgmt': { text: 'mgmt 자료', evidence: [{ id: 'e9', kind: 'channel_line' }] },
    },
    // 둘이 **실제로 겹치게** 한다 — 순서대로 돌면 「안 섞였다」 가 공짜로 참이 된다.
    hooks: { afterTools: async (id) => { if (id === 'x-tyit') await held; } },
  });
  const tyit = call(s.socketPath, wire.ANSWER_ENDPOINT, ask('x-tyit', 'tyit', 'tok-A'));
  await waitFor(() => s.worker.inFlightCount === 1);
  const mgmt = readAnswer(await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('x-mgmt', 'mgmt', 'tok-B')), 'x-mgmt');
  release();
  const got = readAnswer(await tyit, 'x-tyit');

  assert.equal(mgmt.status, 'answered');
  assert.deepEqual(mgmt.used_evidence, ['e9']);
  // tyit 은 e9 를 **받은 적이 없다.** 발급 집합이 요청마다 따로이므로 지어낸 인용이다.
  assert.equal(got.status, 'failed', '남의 근거를 인용했는데 답이 나갔습니다');
  assert.deepEqual(got.used_evidence, []);
  assert.equal(got.answer, '');
});

await test('도구 호출이 각자 토큰·request_id 로만 나간다', async () => {
  const s = await bootShared({
    profiles: PROFILES,
    answers: { 'y-tyit': '답[e1].', 'y-mgmt': '답[e2].' },
    byRequest: {
      'y-tyit': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] },
      'y-mgmt': { text: 'b', evidence: [{ id: 'e2', kind: 'channel_line' }] },
    },
  });
  await Promise.all([
    call(s.socketPath, wire.ANSWER_ENDPOINT, ask('y-tyit', 'tyit', 'tok-A')),
    call(s.socketPath, wire.ANSWER_ENDPOINT, ask('y-mgmt', 'mgmt', 'tok-B')),
  ]);
  const want = { 'y-tyit': 'tok-A', 'y-mgmt': 'tok-B' };
  assert.ok(s.seen.length >= 2, '도구가 안 불렸습니다');
  for (const c of s.seen) assert.equal(c.token, want[c.requestId], `${c.requestId} 가 ${c.token} 로 불렀습니다`);
});

/* ── 4. 취소 격리 ─────────────────────────────────────────────────────── */
console.log('[4/7] 취소 — 하나를 끊어도 다른 하나는 끝까지');

await test('한 요청을 취소해도 다른 workspace 요청은 완주한다', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const s = await bootShared({
    profiles: PROFILES,
    answers: { 'c-tyit': '답[e1].', 'c-mgmt': '답[e2].' },
    byRequest: {
      'c-tyit': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] },
      'c-mgmt': { text: 'b', evidence: [{ id: 'e2', kind: 'channel_line' }] },
    },
    hooks: { afterTools: async (id) => { if (id === 'c-tyit') await held; } },
  });
  const tyit = call(s.socketPath, wire.ANSWER_ENDPOINT, ask('c-tyit', 'tyit', 'tok-A'));
  await waitFor(() => s.worker.inFlightCount === 1);

  const cancel = Buffer.from(JSON.stringify({ request_id: 'c-tyit' }), 'utf8');
  assert.equal((await call(s.socketPath, wire.CANCEL_ENDPOINT, cancel)).status, 200);
  release();

  const cancelled = readAnswer(await tyit, 'c-tyit');
  assert.equal(cancelled.status, 'failed');
  const ok = readAnswer(await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('c-mgmt', 'mgmt', 'tok-B')), 'c-mgmt');
  assert.equal(ok.status, 'answered');
  assert.deepEqual(ok.used_evidence, ['e2']);
  assert.equal(s.gate.inFlight, 0, '취소 뒤 슬롯이 안 돌아왔습니다');
});

await test('모르는 request_id 취소는 아무것도 안 끊는다', async () => {
  const s = await bootShared({
    profiles: PROFILES,
    answers: { 'c2-tyit': '답[e1].' },
    byRequest: { 'c2-tyit': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] } },
  });
  const other = Buffer.from(JSON.stringify({ request_id: '없는-요청' }), 'utf8');
  assert.equal((await call(s.socketPath, wire.CANCEL_ENDPOINT, other)).status, 200);
  const got = readAnswer(await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('c2-tyit', 'tyit', 'tok')), 'c2-tyit');
  assert.equal(got.status, 'answered');
});

/* ── 5. 동시성 ────────────────────────────────────────────────────────── */
console.log('[5/7] 동시성 — 대기열 없이 429');

await test('게이트 자체 — 두 겹 상한과 중복 반환 방지', () => {
  const gate = createConcurrencyGate({ max: 3, maxPerWorkspace: 2 });
  const a1 = gate.acquire('tyit');
  const a2 = gate.acquire('tyit');
  assert.ok(a1 && a2);
  assert.equal(gate.acquire('tyit'), null, 'workspace 상한을 넘겼는데 받았습니다');
  const b1 = gate.acquire('mgmt');
  assert.ok(b1);
  assert.equal(gate.acquire('mgmt'), null, '전체 상한을 넘겼는데 받았습니다');
  assert.equal(gate.inFlight, 3);
  // **두 번 돌려줘도 한 번만** — 늘어나면 상한이 조용히 무너진다.
  a1(); a1();
  assert.equal(gate.inFlight, 2);
  assert.equal(gate.forWorkspace('tyit'), 1);
  a2(); b1();
  assert.equal(gate.inFlight, 0);
  assert.equal(gate.trackedWorkspaces, 0, '다 끝났는데 workspace 칸이 남았습니다');
  assert.throws(() => createConcurrencyGate({ max: 0, maxPerWorkspace: 1 }), /전체/);
});

await test('workspace 상한을 넘으면 429 이고 다른 workspace 는 통과한다', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const s = await bootShared({
    profiles: PROFILES,
    limits: { max: 4, maxPerWorkspace: 1 },
    answers: { 'q-1': '답[e1].', 'q-2': '답[e1].', 'q-m': '답[e2].' },
    byRequest: {
      'q-1': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] },
      'q-m': { text: 'b', evidence: [{ id: 'e2', kind: 'channel_line' }] },
    },
    hooks: { afterTools: async (id) => { if (id === 'q-1') await held; } },
  });
  const first = call(s.socketPath, wire.ANSWER_ENDPOINT, ask('q-1', 'tyit', 'tok-A'));
  await waitFor(() => s.gate.forWorkspace('tyit') === 1);

  const second = await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('q-2', 'tyit', 'tok-B'));
  assert.equal(second.status, 429, 'workspace 상한을 안 걸었습니다');
  assert.deepEqual(JSON.parse(second.body.toString('utf8')), { error: 'overloaded' });

  // 다른 workspace 는 **자기 잘못이 아니므로** 막히지 않는다.
  const mgmt = readAnswer(await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('q-m', 'mgmt', 'tok-C')), 'q-m');
  assert.equal(mgmt.status, 'answered');

  release();
  readAnswer(await first, 'q-1');
  assert.equal(s.gate.inFlight, 0, '완료 뒤 슬롯이 안 돌아왔습니다');
});

await test('전체 상한을 넘으면 429 — workspace 가 달라도', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const s = await bootShared({
    profiles: PROFILES,
    limits: { max: 1, maxPerWorkspace: 1 },
    answers: { 'g-1': '답[e1].', 'g-2': '답[e2].' },
    byRequest: { 'g-1': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] } },
    hooks: { afterTools: async (id) => { if (id === 'g-1') await held; } },
  });
  const first = call(s.socketPath, wire.ANSWER_ENDPOINT, ask('g-1', 'tyit', 'tok-A'));
  await waitFor(() => s.gate.inFlight === 1);
  const second = await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('g-2', 'mgmt', 'tok-B'));
  assert.equal(second.status, 429);
  release();
  await first;
  assert.equal(s.gate.inFlight, 0);
});

await test('429 는 request_id 를 점유하지 않는다 — 재시도가 409 로 안 바뀐다', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const s = await bootShared({
    profiles: PROFILES,
    limits: { max: 1, maxPerWorkspace: 1 },
    answers: { 'r-1': '답[e1].', 'r-2': '답[e1].' },
    byRequest: {
      'r-1': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] },
      'r-2': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] },
    },
    hooks: { afterTools: async (id) => { if (id === 'r-1') await held; } },
  });
  const first = call(s.socketPath, wire.ANSWER_ENDPOINT, ask('r-1', 'tyit', 'tok-A'));
  await waitFor(() => s.gate.inFlight === 1);
  assert.equal((await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('r-2', 'tyit', 'tok-B'))).status, 429);
  release();
  await first;
  // 같은 id 로 다시 — 이제 자리가 있으니 **200** 이어야 한다. 409 면 429 때 점유한 것이다.
  const again = readAnswer(await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('r-2', 'tyit', 'tok-B')), 'r-2');
  assert.equal(again.status, 'answered');
});

await test('답변기가 던져도 슬롯이 돌아온다', async () => {
  const gate = createConcurrencyGate({ max: 2, maxPerWorkspace: 2 });
  const boom = createRpcServer({
    secret: SECRET, version: 'x',
    route: () => ({ answer: async () => { throw new Error('boom'); } }),
    limits: gate,
    console: { error() {}, warn() {} },
  });
  const at = await boom.listen(place('boom'), listenOpts);
  shut.push(boom);
  const got = readAnswer(await call(at, wire.ANSWER_ENDPOINT, ask('e-1', 'tyit', 'tok')), 'e-1');
  assert.equal(got.status, 'failed');
  assert.equal(gate.inFlight, 0, '오류 뒤 슬롯이 안 돌아왔습니다');
});

/* ── 6. health ────────────────────────────────────────────────────────── */
console.log('[6/7] health — 전체와 workspace 별을 가른다');

await test('전체는 ok 이고 workspace 별 준비 상태가 따로 보인다', async () => {
  const s = await bootShared({
    profiles: PROFILES, answers: {}, byRequest: {},
    readiness: () => ({ tyit: true, mgmt: false }),
  });
  const started = Date.now();
  const r = await call(s.socketPath, wire.HEALTH_ENDPOINT, Buffer.from('{}', 'utf8'),
    { timeoutMs: wire.HEALTH_DEADLINE_MS });
  assert.ok(Date.now() - started < wire.HEALTH_DEADLINE_MS);
  const got = JSON.parse(r.body.toString('utf8'));
  // 저쪽 `validate_health` 가 보는 셋은 그대로다.
  assert.equal(got.status, 'ok');
  assert.equal(got.contract_version, wire.CONTRACT_VERSION);
  assert.equal(got.version, 'shared-test');
  // 한 칸이 죽어도 **전체는 ok** — 멀쩡한 쪽까지 라우팅이 닫히면 안 된다.
  assert.deepEqual(got.workspaces, { tyit: true, mgmt: false });
  // 경로·키·비밀이 안 샌다.
  const text = r.body.toString('utf8');
  assert.ok(!text.includes(FAKE_KEY) && !text.includes('.key')
    && !text.includes(SECRET.toString('hex').slice(0, 8)));
});

await test('개별 worker 의 health 는 workspaces 를 안 싣는다 — 계약 그대로', () => {
  const got = JSON.parse(wire.buildHealthResponse({ version: 'v1' }).toString('utf8'));
  assert.deepEqual(Object.keys(got).sort(), ['contract_version', 'status', 'version']);
});

/* ── 7. 공유에서도 그대로인 것 ────────────────────────────────────────── */
console.log('[7/7] 서명·재생·중복 — 공유라고 느슨해지지 않는다');

const guard = await bootShared({
  profiles: PROFILES,
  answers: { 'h-1': '답[e1].', 'h-n': '답[e1].' },
  byRequest: {
    'h-1': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] },
    'h-n': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] },
  },
});

await test('서명이 틀리면 401', async () => {
  const r = await call(guard.socketPath, wire.ANSWER_ENDPOINT, ask('h-x', 'tyit', 'tok'),
    { headers: { 'X-TYBot-Signature': 'f'.repeat(64) } });
  assert.equal(r.status, 401);
});

await test('nonce 재사용은 401 — workspace 가 달라도 기억은 하나다', async () => {
  const body = ask('h-n', 'tyit', 'tok');
  const headers = wire.signatureHeaders(SECRET, { path: wire.ANSWER_ENDPOINT, body, nonce: 'shared-nonce-1' });
  assert.equal((await call(guard.socketPath, wire.ANSWER_ENDPOINT, body, { headers })).status, 200);
  assert.equal((await call(guard.socketPath, wire.ANSWER_ENDPOINT, body, { headers })).status, 401);
});

await test('같은 request_id 가 겹치면 409', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const s = await bootShared({
    profiles: PROFILES,
    answers: { 'd-1': '답[e1].' },
    byRequest: { 'd-1': { text: 'a', evidence: [{ id: 'e1', kind: 'channel_line' }] } },
    hooks: { afterTools: async () => { await held; } },
  });
  const first = call(s.socketPath, wire.ANSWER_ENDPOINT, ask('d-1', 'tyit', 'tok-A'));
  await waitFor(() => s.worker.inFlightCount === 1);
  assert.equal((await call(s.socketPath, wire.ANSWER_ENDPOINT, ask('d-1', 'tyit', 'tok-A'))).status, 409);
  release();
  await first;
});

await test('소켓 자리가 고정값이다 — 설정에서 안 받는다', async () => {
  const { SHARED_SOCKET_PATH } = await import('../src/rpc/config.js');
  assert.equal(SHARED_SOCKET_PATH, '/run/tybot-subbots/hermes-shared/http.sock');
  const src = fs.readFileSync(new URL('../src/rpc/config.js', import.meta.url), 'utf8');
  assert.ok(!/HERMES_RPC_SHARED_SOCKET|HERMES_RPC_SOCKET_PATH/.test(src),
    '소켓 자리를 환경변수에서 받습니다');
});

/* ── 정리 ─────────────────────────────────────────────────────────────── */
for (const s of shut) await s.close().catch(() => {});
fs.rmSync(tmp, { recursive: true, force: true });

console.log(`\n${lines.join('\n')}`);
if (failed) {
  console.error(`\n✗ ${failed}건 어긋남`);
  process.exit(1);
}
console.log('\n✓ 공유 worker 가 계약대로 돈다');
