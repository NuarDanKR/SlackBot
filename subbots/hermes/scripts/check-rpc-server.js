#!/usr/bin/env node
/**
 * RPC worker 를 **실제로 띄워** 한 바퀴 돌린다.
 *
 *   node scripts/check-rpc-server.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 무엇을 재는가 ──
 *
 *   1. 정상 답변 — 도구를 돌고, 인용한 근거만 `used_evidence` 에 담고, 서명해 돌려준다
 *   2. 근거 없음 — 인용이 없으면 `answered` 로 나가지 않는다
 *   3. 지어낸 인용 — 받지 않은 `[eN]` 이 섞이면 **답 전체를 버린다**
 *   4. 서명·시각·nonce — 만료·재사용·위조를 전부 401 로 접는다
 *   5. 스키마·request_id·workspace — 어긋나면 400
 *   6. 출력 상한 — 넘치면 자르되 **자른 뒤에** 인용을 거둔다
 *   7. health — 모델을 안 부르고 2초 안에
 *   8. 같은 request_id 중복 — 409
 *   9. 취소 — `/v3/cancel` 뒤 요청이 `failed` 로 접힌다
 *
 * ── 소켓을 어디에 여는가 ──
 *
 * 운영은 Unix socket 이다. 개발 PC(Windows)는 `AF_UNIX` listen 이 막혀 있어
 * **named pipe** 에 연다 — `listen(path)` 과 `http.request({socketPath})` 는 양쪽에서
 * 같은 API 라 재는 것이 달라지지 않는다. 바뀌는 것은 경로 모양과 파일 권한뿐이고,
 * 권한은 운영 unit 이 쥔다(`deploy/hermes-rpc@.service`).
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { createRpcAnswerer } from '../src/rpc/answer.js';
import { createRpcServer } from '../src/rpc/server.js';
import { createToolClient } from '../src/rpc/tool-client.js';
import { buildRemoteTools } from '../src/rpc/tools.js';
import { lazyTerms } from '../src/domain.js';
import * as wire from '../src/rpc/wire.js';

const SECRET = crypto.randomBytes(32);
const WORKSPACE = 'tyit';
const TERMS = lazyTerms('pf-construction');

let failed = 0;
const lines = [];
async function test(name, fn) {
  const at = Date.now();
  try { await fn(); lines.push(`  ✓ ${name} (${Date.now() - at}ms)`); }
  catch (err) { failed += 1; lines.push(`  ✗ ${name}\n      ${String(err.message).split('\n')[0]}`); }
}

/* ── 자리 ──────────────────────────────────────────────────────────────── */
const WIN = process.platform === 'win32';
const tmp = WIN ? null : fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-rpc-'));
let seq = 0;
const place = (name) => (WIN
  ? `\\\\.\\pipe\\hermes-rpc-${process.pid}-${name}-${seq += 1}`
  : path.join(tmp, `${name}-${seq += 1}.sock`));
const listenOpts = WIN ? { mode: null, prepareDir: false } : {};

/* ── 가짜 TYBot 도구 서버 ──────────────────────────────────────────────── */
/**
 * 저쪽 `specialist_v3_tool_server.ToolServer` 의 **최소 거울**. 서명·토큰·요청 대조를
 * 같은 규칙으로 본다 — 느슨하게 만들면 우리 클라이언트가 틀려도 통과한다.
 */
function fakeToolServer({ token, requestId, reply }) {
  const seen = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const body = Buffer.concat(chunks);
      const send = (status, payload, sign = false) => {
        const headers = { 'Content-Type': 'application/json', 'Content-Length': payload.length };
        if (sign) headers['X-TYBot-Signature'] = wire.responseSignature(SECRET, { requestId, body: payload });
        res.writeHead(status, headers);
        res.end(payload);
      };
      if (req.url !== wire.TOOL_ENDPOINT) return send(404, Buffer.from('{}'));
      const stamp = Number.parseInt(req.headers['x-tybot-timestamp'] || '0', 10);
      const nonce = req.headers['x-tybot-nonce'] || '';
      const expected = wire.sign(SECRET, wire.canonical({
        method: 'POST', path: wire.TOOL_ENDPOINT, timestamp: stamp, nonce, body,
      }));
      if (!wire.safeEqualHex(expected, req.headers['x-tybot-signature'] || '')) {
        return send(401, Buffer.from('{"error":"unauthorized"}'));
      }
      const call = JSON.parse(body.toString('utf8'));
      seen.push(call);
      if (call.token !== token || call.request_id !== requestId) {
        return send(200, wireRefusal(call.call_id, 'request-mismatch'), true);
      }
      return send(200, reply(call), true);
    });
  });
  return { server, seen };
}

const wireResult = (callId, text, evidence = []) => Buffer.from(JSON.stringify({
  schema: wire.SCHEMA_TOOL_RESULT, call_id: callId, ok: true, text, evidence, budget: {},
}), 'utf8');
const wireRefusal = (callId, reason) => Buffer.from(JSON.stringify({
  schema: wire.SCHEMA_TOOL_RESULT, call_id: callId, ok: false, text: '', reason, evidence: [], budget: {},
}), 'utf8');

/* ── 대본 모델 ─────────────────────────────────────────────────────────── */
/**
 * `llm/provider.js` 의 도구 왕복 창구와 **같은 모양**만 흉내 낸다. 모델을 부르지
 * 않으므로 이 검사는 네트워크도 API 키도 쓰지 않는다.
 */
function scriptedSession(script) {
  return () => ({
    open({ tools }) {
      let i = 0;
      let pending = null;
      return {
        previousMessageId: null,
        [Symbol.asyncIterator]() {
          return {
            async next() {
              if (i >= script.length) return { done: true, value: undefined };
              const step = script[i++];
              pending = step.calls || null;
              return {
                done: false,
                value: {
                  text: step.text || '',
                  stopReason: step.refusal ? 'refusal' : (step.calls ? 'tool_use' : 'end_turn'),
                  usage: { input_tokens: 10, output_tokens: 5 },
                  toolUses: (step.calls || []).map((c, n) => ({ id: `tu${n}`, name: c.name, input: c.input })),
                  raw: { id: null, role: 'assistant', content: [] },
                },
              };
            },
            return: async () => ({ done: true, value: undefined }),
          };
        },
        async generateToolResponse() {
          if (!pending) return null;
          const out = [];
          for (const c of pending) {
            const tool = tools.find((t) => t.name === c.name);
            if (!tool) { out.push('(알 수 없는 도구)'); continue; }
            out.push(await tool.run(c.input, { toolUse: { id: 'tu' } }));
          }
          pending = null;
          return { role: 'user', content: out.map((text) => ({ type: 'tool_result', content: text })) };
        },
        continueWith() {},
      };
    },
  });
}

/* ── 배선 ──────────────────────────────────────────────────────────────── */
const openServers = [];
async function bootWorker({ script, toolSocket }) {
  const answerer = createRpcAnswerer({
    toolSession: scriptedSession(script),
    buildTools: buildRemoteTools,
    createClient: ({ requestId, token, allow }) => createToolClient({
      socketPath: toolSocket, secret: SECRET, requestId, token, allow, timeoutMs: 5_000,
    }),
    renderPrompt: () => '시스템 프롬프트(시험)',
    model: { id: 'stub', maxTokens: 100, effort: 'low' },
    terms: TERMS,
    console: { error() {}, warn() {} },
  });
  const worker = createRpcServer({
    secret: SECRET, workspace: WORKSPACE, version: 'test-1', answerer,
    console: { error() {}, warn() {} },
  });
  const at = place('worker');
  await worker.listen(at, listenOpts);
  openServers.push(worker);
  return { worker, socketPath: at };
}

async function bootTools(opts) {
  const { server, seen } = fakeToolServer(opts);
  const at = place('tools');
  await new Promise((r, j) => { server.once('error', j); server.listen(at, r); });
  openServers.push({ close: () => new Promise((r) => server.close(r)) });
  return { socketPath: at, seen };
}

/** 서명해서 POST 한다. `headers` 로 서명 자체를 망가뜨릴 수 있다. */
function callWorker(socketPath, route, body, { headers, timeoutMs = 8_000 } = {}) {
  const signed = { ...wire.signatureHeaders(SECRET, { path: route, body }), ...headers };
  return new Promise((resolve, reject) => {
    const req = http.request({
      socketPath, path: route, method: 'POST',
      headers: { Host: 'tybot', Connection: 'close', 'Content-Length': body.length, ...signed },
    }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks) }));
    });
    req.setTimeout(timeoutMs, () => { req.destroy(); reject(new Error('timeout')); });
    req.on('error', reject);
    req.end(body);
  });
}

const requestBody = (over = {}) => Buffer.from(JSON.stringify({
  schema: wire.SCHEMA_REQUEST,
  request_id: 'r-1',
  authorization_id: 'auth-1',
  workspace: WORKSPACE,
  question: '지난주 정산 금액이 얼마였지',
  evidence: [],
  follow_up: [],
  tools: {
    token: 'tok-1', endpoint: wire.TOOL_ENDPOINT, socket: wire.TOOL_SOCKET_PATH,
    allow: ['search', 'read_channel', 'read_document'],
    budget: { max_calls: 12, max_per_tool: 4, max_chars: 60000, max_seconds: 45 },
  },
  limits: { deadline_ms: 90000, max_output_chars: 3000 },
  ...over,
}), 'utf8');

/** 답을 검사한다 — **저쪽이 하는 것과 같은 순서로.** 서명을 먼저 본다. */
function readAnswer(reply, { requestId = 'r-1', issued = new Set() } = {}) {
  assert.equal(reply.status, 200);
  assert.ok(
    wire.safeEqualHex(wire.responseSignature(SECRET, { requestId, body: reply.body }), reply.headers['x-tybot-signature'] || ''),
    '응답 서명이 맞지 않습니다',
  );
  const got = JSON.parse(reply.body.toString('utf8'));
  assert.equal(got.schema, wire.SCHEMA_RESPONSE);
  assert.equal(got.request_id, requestId);
  assert.ok(wire.STATUSES.includes(got.status), `모르는 상태: ${got.status}`);
  for (const id of got.used_evidence) {
    assert.ok(issued.has(id), `발급하지 않은 근거가 응답에 있습니다: ${id}`);
  }
  if (got.status === 'answered') assert.ok(got.used_evidence.length, 'answered 인데 근거가 없습니다');
  return got;
}

console.log(`\n자리: ${WIN ? 'named pipe (개발 PC)' : `unix socket (${tmp})`}\n`);

/* ── 1. 정상 ───────────────────────────────────────────────────────────── */
console.log('[1/10] 정상 답변');
await test('TYBot 이 발급한 근거 ID를 모델에게 보여 준다', async () => {
  const [search] = buildRemoteTools({
    client: {
      async call() {
        return {
          text: '권한 안의 원문',
          evidence: [{ id: 'e7', kind: 'channel_line' }, { id: 'e8', kind: 'document' }],
          halted: false,
        };
      },
    },
    allow: ['search'], terms: TERMS, trace: [],
  });
  const rendered = await search.run({ query: '정산' });
  assert.match(rendered, /권한 안의 원문/);
  assert.match(rendered, /\[e7\] \[e8\]/);
  assert.ok(!rendered.includes('channel_line'));
});

await test('도구를 돌고 인용한 근거만 담는다', async () => {
  const tools = await bootTools({
    token: 'tok-1', requestId: 'r-1',
    reply: (call) => wireResult(call.call_id, '7월 잔금 12억 4천만원', [
      { id: 'e7', kind: 'channel_line' }, { id: 'e8', kind: 'document' },
    ]),
  });
  const { socketPath } = await bootWorker({
    toolSocket: tools.socketPath,
    script: [
      { calls: [{ name: 'search', input: { query: '정산' } }] },
      { text: '7월 잔금은 12억 4천만원입니다[e7].' },
    ],
  });
  const got = readAnswer(await callWorker(socketPath, wire.ANSWER_ENDPOINT, requestBody()), {
    issued: new Set(['e7', 'e8']),
  });
  assert.equal(got.status, 'answered');
  // 모델이 **실제로 인용한 것만** 담는다. 받은 것을 전부 담으면 「읽었다」 와
  // 「썼다」 가 같아지고, 출처가 답변 내용과 어긋난다.
  assert.deepEqual(got.used_evidence, ['e7']);
  assert.equal(tools.seen.length, 1);
  assert.equal(tools.seen[0].tool, 'search');
  assert.equal(tools.seen[0].token, 'tok-1');
});

/* ── 2. 근거 없음 ──────────────────────────────────────────────────────── */
console.log('[2/10] 근거 없이 답하지 않는다');
await test('인용이 없으면 answered 가 아니다', async () => {
  const tools = await bootTools({ token: 'tok-1', requestId: 'r-1', reply: (c) => wireResult(c.call_id, '(없음)') });
  const { socketPath } = await bootWorker({
    toolSocket: tools.socketPath,
    script: [
      { calls: [{ name: 'search', input: { query: 'x' } }] },
      { text: '아마 12억쯤 됐던 것 같습니다.' },
    ],
  });
  const got = readAnswer(await callWorker(socketPath, wire.ANSWER_ENDPOINT, requestBody()));
  assert.equal(got.status, 'no_evidence');
  assert.deepEqual(got.used_evidence, []);
});

await test('정상 RPC 완료 로그는 계측만 남기고 질문과 원문은 남기지 않는다', async () => {
  const lines = [];
  const answerer = {
    async answer() {
      return {
        status: 'no_evidence', answer: '비밀 원문', usedEvidence: [], uncertain: [],
        usage: {
          model: 'stub', stop_reason: 'max_tokens', input_tokens: 120,
          output_tokens: 80, tool_calls: 1, elapsed_ms: 321,
        },
        trace: [{ name: 'search', chars: 456 }],
      };
    },
  };
  const worker = createRpcServer({
    secret: SECRET, workspace: WORKSPACE, version: 'x', answerer,
    console: { info: (line) => lines.push(line), error() {}, warn() {} },
  });
  const at = place('observability');
  await worker.listen(at, listenOpts);
  openServers.push(worker);

  const got = readAnswer(await callWorker(at, wire.ANSWER_ENDPOINT, requestBody()));
  assert.equal(got.status, 'no_evidence');
  assert.equal(lines.length, 1);
  assert.match(lines[0], /status=no_evidence/);
  assert.match(lines[0], /stop_reason=max_tokens/);
  assert.match(lines[0], /tool_calls=1 tools=search=1\/456/);
  assert.ok(!lines[0].includes('지난주 정산'));
  assert.ok(!lines[0].includes('비밀 원문'));
});

/* ── 3. 지어낸 인용 ────────────────────────────────────────────────────── */
console.log('[3/10] 지어낸 인용은 답 전체를 버린다');
await test('받지 않은 [eN] 이 섞이면 failed', async () => {
  const tools = await bootTools({
    token: 'tok-1', requestId: 'r-1',
    reply: (c) => wireResult(c.call_id, '본문', [{ id: 'e7', kind: 'channel_line' }]),
  });
  const { socketPath } = await bootWorker({
    toolSocket: tools.socketPath,
    script: [
      { calls: [{ name: 'search', input: { query: 'x' } }] },
      { text: '12억입니다[e7]. 그리고 만기는 9월입니다[e99].' },
    ],
  });
  const got = readAnswer(await callWorker(socketPath, wire.ANSWER_ENDPOINT, requestBody()));
  assert.equal(got.status, 'failed');
  assert.deepEqual(got.used_evidence, []);
  // 문장도 안 나간다 — 지어낸 근거에 기댄 문장을 추려 보낼 방법이 없다.
  assert.equal(got.answer, '');
});
await test('출처 줄을 붙이면 failed', async () => {
  const tools = await bootTools({
    token: 'tok-1', requestId: 'r-1',
    reply: (c) => wireResult(c.call_id, '본문', [{ id: 'e7', kind: 'channel_line' }]),
  });
  const { socketPath } = await bootWorker({
    toolSocket: tools.socketPath,
    script: [
      { calls: [{ name: 'search', input: { query: 'x' } }] },
      { text: '12억입니다[e7].\n출처: [전산팀]공지, 📄정산(2026-07-01)' },
    ],
  });
  const got = readAnswer(await callWorker(socketPath, wire.ANSWER_ENDPOINT, requestBody()));
  assert.equal(got.status, 'failed');
});

/* ── 4. 서명·시각·nonce ────────────────────────────────────────────────── */
console.log('[4/10] 서명·시각·nonce');
const guard = await (async () => {
  const tools = await bootTools({ token: 'tok-1', requestId: 'r-1', reply: (c) => wireResult(c.call_id, 'x') });
  return bootWorker({ toolSocket: tools.socketPath, script: [{ text: '없습니다.' }] });
})();

await test('서명이 틀리면 401', async () => {
  const r = await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, requestBody(), {
    headers: { 'X-TYBot-Signature': 'f'.repeat(64) },
  });
  assert.equal(r.status, 401);
});
await test('시각이 오래되면 401', async () => {
  const body = requestBody();
  const old = Math.floor(Date.now() / 1000) - (wire.CLOCK_SKEW_SECONDS + 5);
  const r = await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, body, {
    headers: wire.signatureHeaders(SECRET, { path: wire.ANSWER_ENDPOINT, body, now: old }),
  });
  assert.equal(r.status, 401);
});
await test('미래 시각도 401', async () => {
  const body = requestBody();
  const ahead = Math.floor(Date.now() / 1000) + (wire.CLOCK_SKEW_SECONDS + 5);
  const r = await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, body, {
    headers: wire.signatureHeaders(SECRET, { path: wire.ANSWER_ENDPOINT, body, now: ahead }),
  });
  assert.equal(r.status, 401);
});
await test('nonce 를 다시 쓰면 401', async () => {
  const body = requestBody({ request_id: 'r-nonce' });
  const headers = wire.signatureHeaders(SECRET, { path: wire.ANSWER_ENDPOINT, body, nonce: 'fixed-nonce-1' });
  const first = await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, body, { headers });
  assert.equal(first.status, 200);
  const again = await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, body, { headers });
  assert.equal(again.status, 401);
});
await test('본문을 바꾸면 401', async () => {
  const body = requestBody();
  const headers = wire.signatureHeaders(SECRET, { path: wire.ANSWER_ENDPOINT, body });
  const tampered = requestBody({ question: '다른 질문' });
  const r = await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, tampered, { headers });
  assert.equal(r.status, 401);
});
await test('다른 경로의 서명은 안 통한다', async () => {
  const body = requestBody();
  const headers = wire.signatureHeaders(SECRET, { path: wire.CANCEL_ENDPOINT, body });
  const r = await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, body, { headers });
  assert.equal(r.status, 401);
});

/* ── 5. 스키마·대조 ────────────────────────────────────────────────────── */
console.log('[5/10] 스키마와 대조');
await test('스키마가 다르면 400', async () => {
  const r = await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, requestBody({ schema: 'tybot.specialist.request/v2' }));
  assert.equal(r.status, 400);
});
await test('다른 workspace 요청은 400', async () => {
  const r = await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, requestBody({ workspace: 'mgmt', request_id: 'r-ws' }));
  assert.equal(r.status, 400);
});
await test('모르는 경로는 404', async () => {
  const body = Buffer.from('{}', 'utf8');
  const r = await callWorker(guard.socketPath, '/v3/evil', body, {
    headers: wire.signatureHeaders(SECRET, { path: '/v3/evil', body }),
  });
  assert.equal(r.status, 404);
});
await test('응답 request_id 가 요청과 같다', async () => {
  const got = readAnswer(
    await callWorker(guard.socketPath, wire.ANSWER_ENDPOINT, requestBody({ request_id: 'r-echo' })),
    { requestId: 'r-echo' },
  );
  assert.equal(got.request_id, 'r-echo');
});

/* ── 6. 출력 상한 ──────────────────────────────────────────────────────── */
console.log('[6/10] 출력 상한');
await test('상한을 넘으면 자르고, 잘린 뒤 남은 인용만 담는다', async () => {
  const tools = await bootTools({
    token: 'tok-1', requestId: 'r-1',
    reply: (c) => wireResult(c.call_id, '본문', [
      { id: 'e1', kind: 'channel_line' }, { id: 'e2', kind: 'channel_line' },
    ]),
  });
  const { socketPath } = await bootWorker({
    toolSocket: tools.socketPath,
    script: [
      { calls: [{ name: 'search', input: { query: 'x' } }] },
      // 앞쪽 인용은 살아남고 **뒤쪽 인용은 잘려 나간다.**
      { text: `앞[e1]${'가'.repeat(80)}뒤[e2]` },
    ],
  });
  const got = readAnswer(
    await callWorker(socketPath, wire.ANSWER_ENDPOINT, requestBody({ limits: { deadline_ms: 90000, max_output_chars: 40 } })),
    { issued: new Set(['e1', 'e2']) },
  );
  assert.equal(got.status, 'answered');
  assert.ok(got.answer.length <= 40, `길이 ${got.answer.length}`);
  assert.deepEqual(got.used_evidence, ['e1']);
});

/* ── 7. health ─────────────────────────────────────────────────────────── */
console.log('[7/10] health');
await test('모델을 안 부르고 2초 안에 답한다', async () => {
  // 모델 창구를 **던지게** 꽂는다. 불렸으면 여기서 터진다.
  const tools = await bootTools({ token: 't', requestId: 'r', reply: (c) => wireResult(c.call_id, 'x') });
  const answerer = createRpcAnswerer({
    toolSession: () => { throw new Error('health 가 모델을 불렀습니다'); },
    buildTools: buildRemoteTools,
    createClient: () => createToolClient({
      socketPath: tools.socketPath, secret: SECRET, requestId: 'r', token: 't', allow: ['search'],
    }),
    renderPrompt: () => 'x',
    model: { id: 'stub', maxTokens: 1, effort: 'low' },
    terms: TERMS,
    console: { error() {}, warn() {} },
  });
  const worker = createRpcServer({ secret: SECRET, workspace: WORKSPACE, version: 'v-abc', answerer });
  const at = place('health');
  await worker.listen(at, listenOpts);
  openServers.push(worker);

  const started = Date.now();
  const body = Buffer.from('{}', 'utf8');
  const r = await callWorker(at, wire.HEALTH_ENDPOINT, body, { timeoutMs: wire.HEALTH_DEADLINE_MS });
  const elapsed = Date.now() - started;
  assert.equal(r.status, 200);
  assert.ok(elapsed < wire.HEALTH_DEADLINE_MS, `${elapsed}ms`);
  const got = JSON.parse(r.body.toString('utf8'));
  assert.equal(got.status, 'ok');
  assert.equal(got.contract_version, wire.CONTRACT_VERSION);
  assert.equal(got.version, 'v-abc');
  // 비밀이 새지 않는다.
  assert.ok(!r.body.toString('utf8').includes(SECRET.toString('hex').slice(0, 8)));
});

/* ── 8. 중복 request_id ────────────────────────────────────────────────── */
console.log('[8/10] 같은 request_id 가 겹치면');
await test('두 번째를 409 로 막는다', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const tools = await bootTools({
    token: 'tok-1', requestId: 'r-dup',
    reply: (c) => wireResult(c.call_id, '본문', [{ id: 'e1', kind: 'channel_line' }]),
  });
  const answerer = {
    async answer(request) {
      await held;
      return { status: 'no_evidence', answer: '…', usedEvidence: [], uncertain: [], usage: {} };
    },
  };
  const worker = createRpcServer({ secret: SECRET, workspace: WORKSPACE, version: 'x', answerer });
  const at = place('dup');
  await worker.listen(at, listenOpts);
  openServers.push(worker);

  const body = requestBody({ request_id: 'r-dup' });
  const first = callWorker(at, wire.ANSWER_ENDPOINT, body);
  // 첫 요청이 실제로 접수될 때까지 기다린다. 숫자로 재야 「겹쳤다」 가 보장된다.
  for (let i = 0; i < 200 && worker.inFlightCount === 0; i += 1) {
    await new Promise((r) => setTimeout(r, 5));
  }
  assert.equal(worker.inFlightCount, 1);
  const second = await callWorker(at, wire.ANSWER_ENDPOINT, body);
  assert.equal(second.status, 409);
  release();
  assert.equal((await first).status, 200);
  assert.equal(worker.inFlightCount, 0, '끝난 요청이 안 지워졌습니다');
  void tools;
});

/* ── 9. 취소 ───────────────────────────────────────────────────────────── */
console.log('[9/10] 취소');
await test('/v3/cancel 뒤 그 요청은 failed 로 접힌다', async () => {
  let release;
  const held = new Promise((r) => { release = r; });
  const tools = await bootTools({
    token: 'tok-1', requestId: 'r-cancel',
    reply: (c) => wireResult(c.call_id, '본문', [{ id: 'e1', kind: 'channel_line' }]),
  });
  const answerer = createRpcAnswerer({
    toolSession: scriptedSession([
      { calls: [{ name: 'search', input: { query: 'x' } }] },
      { text: '12억입니다[e1].' },
    ]),
    buildTools: ({ client, allow, terms, trace }) => {
      const built = buildRemoteTools({ client, allow, terms, trace });
      // 첫 도구에서 멈춰 세운다 — 그 사이에 취소를 넣는다.
      const first = built[0];
      const run = first.run;
      first.run = async (...a) => { const out = await run(...a); await held; return out; };
      return built;
    },
    createClient: ({ requestId, token, allow }) => createToolClient({
      socketPath: tools.socketPath, secret: SECRET, requestId, token, allow,
    }),
    renderPrompt: () => 'x',
    model: { id: 'stub', maxTokens: 1, effort: 'low' },
    terms: TERMS,
    console: { error() {}, warn() {} },
  });
  const worker = createRpcServer({ secret: SECRET, workspace: WORKSPACE, version: 'x', answerer });
  const at = place('cancel');
  await worker.listen(at, listenOpts);
  openServers.push(worker);

  const body = requestBody({ request_id: 'r-cancel' });
  const pending = callWorker(at, wire.ANSWER_ENDPOINT, body);
  for (let i = 0; i < 200 && worker.inFlightCount === 0; i += 1) {
    await new Promise((r) => setTimeout(r, 5));
  }
  const cancelBody = Buffer.from(JSON.stringify({ request_id: 'r-cancel' }), 'utf8');
  const cancelled = await callWorker(at, wire.CANCEL_ENDPOINT, cancelBody);
  assert.equal(cancelled.status, 200);
  release();
  const got = readAnswer(await pending, { requestId: 'r-cancel' });
  assert.equal(got.status, 'failed');
});

/* ── 10. deadline ──────────────────────────────────────────────────────── */
console.log('[10/10] deadline');
await test('예산이 끝나면 저쪽이 끊기 전에 failed 로 접는다', async () => {
  // 도구가 안 돌아온다. 예산이 유일한 탈출구다.
  const tools = await bootTools({
    token: 'tok-1', requestId: 'r-deadline',
    reply: () => Buffer.from('{}'), // 안 쓰인다 — 아래에서 붙잡는다
  });
  let stuck;
  const forever = new Promise((r) => { stuck = r; });
  const answerer = createRpcAnswerer({
    toolSession: scriptedSession([
      { calls: [{ name: 'search', input: { query: 'x' } }] },
      { text: '여기까지 못 온다[e1].' },
    ]),
    buildTools: (o) => {
      const built = buildRemoteTools(o);
      built[0].run = async () => { await forever; return '안 옴'; };
      return built;
    },
    createClient: ({ requestId, token, allow }) => createToolClient({
      socketPath: tools.socketPath, secret: SECRET, requestId, token, allow,
    }),
    renderPrompt: () => 'x',
    model: { id: 'stub', maxTokens: 1, effort: 'low' },
    terms: TERMS,
    console: { error() {}, warn() {} },
  });
  const worker = createRpcServer({ secret: SECRET, workspace: WORKSPACE, version: 'x', answerer });
  const at = place('deadline');
  await worker.listen(at, listenOpts);
  openServers.push(worker);

  // `deadline_ms` 는 저쪽이 소켓을 끊는 시각이다. 우리 예산은 그보다 **먼저** 끝나야
  // 우리가 쓴 사유로 돌아온다 — 늦으면 저쪽 로그에 `transport` 만 남는다.
  const started = Date.now();
  const got = readAnswer(
    await callWorker(at, wire.ANSWER_ENDPOINT,
      requestBody({ request_id: 'r-deadline', limits: { deadline_ms: 2600, max_output_chars: 3000 } }),
      { timeoutMs: 2600 }),
    { requestId: 'r-deadline' },
  );
  const elapsed = Date.now() - started;
  assert.equal(got.status, 'failed');
  assert.ok(got.uncertain.some((u) => u.includes('시간')), JSON.stringify(got.uncertain));
  assert.ok(elapsed < 2600, `${elapsed}ms — 저쪽이 끊은 뒤에 답했다`);
  stuck();
});

/* ── 정리 ──────────────────────────────────────────────────────────────── */
for (const s of openServers) await s.close().catch(() => {});
if (tmp) fs.rmSync(tmp, { recursive: true, force: true });

console.log(`\n${lines.join('\n')}`);
if (failed) {
  console.error(`\n✗ ${failed}건 어긋남`);
  process.exit(1);
}
console.log('\n✓ RPC worker 가 계약대로 돈다');
