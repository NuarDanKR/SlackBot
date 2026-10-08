#!/usr/bin/env node
/**
 * RPC worker 의 **경계**를 잰다 — 무엇을 못 하는가.
 *
 *   node scripts/check-rpc-isolation.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 이 검사가 따로 있나 ──
 *
 * 「Slack 에 안 올린다」 와 「아카이브를 안 읽는다」 는 **지키겠다는 약속**으로는 못
 * 지킨다. 누군가 편의로 `import` 한 줄을 더하는 날이 오고, 그건 기능이 늘어난 것처럼
 * 보이지 답변이 두 번 나가거나 권한 밖 자료가 섞이기 전까지 아무 증상이 없다.
 *
 * 그래서 여기서는 **정적 import 그래프**를 걸어 본다. 부를 수 있는 것이 없으면
 * 「안 부른다」 가 아니라 **못 부른다** 가 된다(인계 요구 6·9).
 *
 *   1. worker 가 닿는 파일 어디에도 Slack·아카이브·문서 저장소가 없다
 *   2. 요청 둘이 서로의 도구 토큰에 닿지 못한다
 *   3. 한 바퀴 도는 동안 slack.com 으로 나가는 요청이 0건이다
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRpcAnswerer } from '../src/rpc/answer.js';
import { createRpcServer } from '../src/rpc/server.js';
import { createToolClient } from '../src/rpc/tool-client.js';
import { buildRemoteTools } from '../src/rpc/tools.js';
import { lazyTerms } from '../src/domain.js';
import * as wire from '../src/rpc/wire.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const ENTRY = path.join(HERE, 'run-rpc-worker.js');

let failed = 0;
const lines = [];
async function test(name, fn) {
  try { await fn(); lines.push(`  ✓ ${name}`); }
  catch (err) { failed += 1; lines.push(`  ✗ ${name}\n      ${String(err.message).split('\n').slice(0, 4).join('\n      ')}`); }
}

/* ── 1. import 그래프 ──────────────────────────────────────────────────── */

/** 주석 안의 `import` 는 세지 않는다 — 설명문에 경로를 적는 것은 흔하고 무해하다. */
function stripComments(source) {
  return source
    .replace(/\/\*[\s\S]*?\*\//g, ' ')
    .replace(/(^|[^:])\/\/[^\n]*/g, '$1 ');
}

/** `import ... from 'x'` · `import 'x'` · `await import('x')` 전부. */
function importsOf(source) {
  const body = stripComments(source);
  const out = new Set();
  for (const re of [
    /\bimport\s+[\s\S]*?\bfrom\s*['"]([^'"]+)['"]/g,
    /\bimport\s*['"]([^'"]+)['"]/g,
    /\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
    /\brequire\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
  ]) {
    for (const m of body.matchAll(re)) out.add(m[1]);
  }
  return [...out];
}

/** worker 진입점에서 닿는 **저장소 안** 파일 전부. */
function walk(entry) {
  const seen = new Map(); // 파일 → 그 파일이 가져온 외부(비상대) 지정자
  const queue = [entry];
  while (queue.length) {
    const file = queue.pop();
    if (seen.has(file)) continue;
    const source = fs.readFileSync(file, 'utf8');
    const external = [];
    for (const spec of importsOf(source)) {
      if (spec.startsWith('.')) {
        const target = path.resolve(path.dirname(file), spec);
        if (!fs.existsSync(target)) throw new Error(`못 찾는 import: ${spec} (${file})`);
        queue.push(target);
      } else {
        external.push(spec);
      }
    }
    seen.set(file, external);
  }
  return seen;
}

const graph = walk(ENTRY);
const rel = (f) => path.relative(ROOT, f).replace(/\\/g, '/');

console.log(`\n[1/3] worker 가 닿는 파일 ${graph.size}개`);
for (const f of [...graph.keys()].sort()) console.log(`      ${rel(f)}`);

await test('Slack 을 가져오지 않는다', () => {
  const bad = [];
  for (const [file, external] of graph) {
    for (const spec of external) {
      if (/^@slack\//.test(spec)) bad.push(`${rel(file)} → ${spec}`);
    }
    if (/slack-live\.js|slack\/(live-api|live-render|access|identity|question)\.js/.test(
      [...importsOf(fs.readFileSync(file, 'utf8'))].join(' '),
    )) bad.push(`${rel(file)} → Slack 모듈`);
  }
  assert.deepEqual(bad, [], `Slack 경로가 열려 있습니다:\n      ${bad.join('\n      ')}`);
});

await test('아카이브·문서 저장소를 가져오지 않는다', () => {
  const forbidden = [
    'src/archive.js', 'src/documents.js', 'src/config.js',
    'src/archive-reader/archiver.js', 'src/ingest/index.js', 'src/digest.js',
  ];
  const touched = new Set([...graph.keys()].map(rel));
  const bad = forbidden.filter((f) => touched.has(f));
  assert.deepEqual(bad, [], `자료 경로가 열려 있습니다: ${bad.join(', ')}`);
});

await test('도구 소켓 경로를 설정에서만 받는다', () => {
  // `TOOL_SOCKET_PATH` 는 상수 하나여야 한다. 요청 본문에서 받아 쓰는 자리가 생기면
  // 그 값이 곧 「아무 소켓이나 부르는 권한」 이다.
  const client = fs.readFileSync(path.join(ROOT, 'src/rpc/tool-client.js'), 'utf8');
  assert.ok(!/request\.(tools|socket)/.test(stripComments(client)), '클라이언트가 요청에서 소켓을 읽습니다');
  const server = fs.readFileSync(path.join(ROOT, 'src/rpc/wire.js'), 'utf8');
  assert.match(server, /도구 소켓 경로가 계약과 다릅니다/, '요청이 준 소켓 경로를 대조하지 않습니다');
});

await test('Anthropic SDK 말고 외부 의존이 없다', () => {
  const external = new Set();
  for (const specs of graph.values()) for (const s of specs) external.add(s);
  const outside = [...external].filter((s) => !s.startsWith('node:') && !s.startsWith('@anthropic-ai/'));
  assert.deepEqual(outside, [], `뜻밖의 외부 의존: ${outside.join(', ')}`);
});

/* ── 2·3. 실제로 한 바퀴 ───────────────────────────────────────────────── */
console.log('\n[2/3] 요청 둘이 서로의 토큰에 닿는가');

const SECRET = crypto.randomBytes(32);
const WORKSPACE = 'tyit';
const TERMS = lazyTerms('pf-construction');
const WIN = process.platform === 'win32';
const tmp = WIN ? null : fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-iso-'));
let seq = 0;
const place = (n) => (WIN
  ? `\\\\.\\pipe\\hermes-iso-${process.pid}-${n}-${seq += 1}`
  : path.join(tmp, `${n}-${seq += 1}.sock`));
const listenOpts = WIN ? { mode: null, prepareDir: false } : {};
const shut = [];

/** 토큰·request_id 를 **그대로 기록하는** 도구 서버. 섞이면 여기 남는다. */
async function toolsServer(seen) {
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      const body = Buffer.concat(chunks);
      const call = JSON.parse(body.toString('utf8'));
      seen.push({ requestId: call.request_id, token: call.token, tool: call.tool });
      const payload = Buffer.from(JSON.stringify({
        schema: wire.SCHEMA_TOOL_RESULT, call_id: call.call_id, ok: true,
        text: `${call.request_id} 의 자료`,
        evidence: [{ id: call.request_id === 'iso-a' ? 'e1' : 'e2', kind: 'channel_line' }],
        budget: {},
      }), 'utf8');
      res.writeHead(200, {
        'Content-Type': 'application/json',
        'Content-Length': payload.length,
        'X-TYBot-Signature': wire.responseSignature(SECRET, { requestId: call.request_id, body: payload }),
      });
      res.end(payload);
    });
  });
  const at = place('tools');
  await new Promise((r, j) => { server.once('error', j); server.listen(at, r); });
  shut.push({ close: () => new Promise((r) => server.close(r)) });
  return at;
}

function scripted(cite) {
  return () => ({
    open({ tools }) {
      let i = 0;
      let pending = null;
      const steps = [
        { calls: [{ name: 'search', input: { query: 'x' } }] },
        { text: `답입니다[${cite}].` },
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
          return { role: 'user', content: out.map((text) => ({ type: 'tool_result', content: text })) };
        },
        continueWith() {},
      };
    },
  });
}

const seen = [];
const toolSocket = await toolsServer(seen);

async function workerFor(cite) {
  const answerer = createRpcAnswerer({
    toolSession: scripted(cite),
    buildTools: buildRemoteTools,
    createClient: ({ requestId, token, allow }) => createToolClient({
      socketPath: toolSocket, secret: SECRET, requestId, token, allow,
    }),
    renderPrompt: () => 'x',
    model: { id: 'stub', maxTokens: 1, effort: 'low' },
    terms: TERMS,
    console: { error() {}, warn() {} },
  });
  const w = createRpcServer({ secret: SECRET, workspace: WORKSPACE, version: 'iso', answerer, console: { error() {}, warn() {} } });
  const at = place('worker');
  await w.listen(at, listenOpts);
  shut.push(w);
  return at;
}

function send(socketPath, body) {
  const headers = wire.signatureHeaders(SECRET, { path: wire.ANSWER_ENDPOINT, body });
  return new Promise((resolve, reject) => {
    const req = http.request({
      socketPath, path: wire.ANSWER_ENDPOINT, method: 'POST',
      headers: { Host: 'tybot', Connection: 'close', 'Content-Length': body.length, ...headers },
    }, (res) => {
      const c = [];
      res.on('data', (x) => c.push(x));
      res.on('end', () => resolve(JSON.parse(Buffer.concat(c).toString('utf8'))));
    });
    req.on('error', reject);
    req.end(body);
  });
}

const body = (id, token) => Buffer.from(JSON.stringify({
  schema: wire.SCHEMA_REQUEST, request_id: id, authorization_id: `auth-${id}`,
  workspace: WORKSPACE, question: '질문', evidence: [], follow_up: [],
  tools: { token, endpoint: wire.TOOL_ENDPOINT, allow: ['search'], budget: {} },
  limits: { deadline_ms: 90000, max_output_chars: 3000 },
}), 'utf8');

/** Slack 으로 나간 요청을 센다. 0 이어야 한다 — **셀 수 있게 만들어 둔다.** */
let slackCalls = 0;
const realFetch = globalThis.fetch;
globalThis.fetch = (input, init) => {
  const url = String(typeof input === 'string' ? input : input?.url || '');
  if (url.includes('slack.com')) slackCalls += 1;
  return realFetch(input, init);
};

await test('두 요청이 각자의 토큰으로만 부른다', async () => {
  const [a, b] = await Promise.all([workerFor('e1'), workerFor('e2')]);
  const [ra, rb] = await Promise.all([
    send(a, body('iso-a', 'token-A')),
    send(b, body('iso-b', 'token-B')),
  ]);
  assert.equal(ra.status, 'answered');
  assert.equal(rb.status, 'answered');
  assert.deepEqual(ra.used_evidence, ['e1']);
  assert.deepEqual(rb.used_evidence, ['e2']);

  const byRequest = new Map();
  for (const call of seen) {
    const tokens = byRequest.get(call.requestId) || new Set();
    tokens.add(call.token);
    byRequest.set(call.requestId, tokens);
  }
  assert.deepEqual([...(byRequest.get('iso-a') || [])], ['token-A']);
  assert.deepEqual([...(byRequest.get('iso-b') || [])], ['token-B']);
  // 한 요청이 상대 토큰으로 부른 적이 **한 번도** 없다.
  assert.ok(!seen.some((c) => (c.requestId === 'iso-a' && c.token !== 'token-A')
    || (c.requestId === 'iso-b' && c.token !== 'token-B')), '토큰이 섞였습니다');
});

console.log('\n[3/3] Slack 호출');
await test('한 바퀴 도는 동안 slack.com 으로 0건', () => {
  assert.equal(slackCalls, 0, `${slackCalls}건 나갔습니다`);
});

globalThis.fetch = realFetch;
for (const s of shut) await s.close().catch(() => {});
if (tmp) fs.rmSync(tmp, { recursive: true, force: true });

console.log(`\n${lines.join('\n')}`);
if (failed) {
  console.error(`\n✗ ${failed}건 어긋남`);
  process.exit(1);
}
console.log('\n✓ worker 경계가 서 있다');
