#!/usr/bin/env node
/**
 * v3 계약의 **Node 거울**이 Python 원본과 같은가.
 *
 *   node scripts/check-rpc-wire.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 ──
 *
 * 상수를 베껴 적은 파일은 **원본이 바뀌어도 안 빨개진다.** 스키마 이름이 한 글자
 * 달라지면 저쪽은 400 을 내고, 우리 로그에는 「요청 계약 위반」 한 줄만 남는다 —
 * 무엇이 달라졌는지는 안 적혀 있다.
 *
 * 그래서 여기서는 **Python 소스를 읽어** 값을 꺼내 대조한다. 손으로 적은 기대값을
 * 두면 그 기대값까지 같이 낡는다.
 *
 * 서명도 같은 축이다. `canonical` 이 만드는 바이트가 한 자라도 다르면 증상은 「401」
 * 하나뿐이라 시계·키·줄바꿈 중 무엇이 틀렸는지 구별할 수 없다. 여기서 **Python 이
 * 만든 값과 직접 비교**한다(파이썬이 있으면).
 */
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import * as wire from '../src/rpc/wire.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO = path.resolve(HERE, '..', '..', '..');
const PY_V3 = path.join(REPO, 'src', 'tybot', 'specialist_wire_v3.py');
const PY_V2 = path.join(REPO, 'src', 'tybot', 'specialist_wire.py');

let failed = 0;
const results = [];
async function test(name, fn) {
  try { await fn(); results.push(`  ✓ ${name}`); }
  catch (err) { failed += 1; results.push(`  ✗ ${name}\n      ${err.message.split('\n')[0]}`); }
}

const v3 = fs.readFileSync(PY_V3, 'utf8');
const v2 = fs.readFileSync(PY_V2, 'utf8');

/** `NAME = "값"` 또는 `NAME = 숫자` 를 소스에서 꺼낸다. 주석·문자열 안은 안 본다. */
function pyConst(source, name) {
  const m = source.match(new RegExp(`^${name}\\s*=\\s*(.+)$`, 'm'));
  if (!m) throw new Error(`Python 상수를 찾지 못했습니다: ${name}`);
  const raw = m[1].split('#')[0].trim();
  const quoted = raw.match(/^"([^"]*)"$/);
  if (quoted) return quoted[1];
  return Number.parseInt(raw.replace(/_/g, ''), 10);
}

/** `NAME = ("a", "b")` 튜플. */
function pyTuple(source, name) {
  const m = source.match(new RegExp(`^${name}\\s*=\\s*\\(([\\s\\S]*?)\\)`, 'm'));
  if (!m) throw new Error(`Python 튜플을 찾지 못했습니다: ${name}`);
  return [...m[1].matchAll(/"([^"]*)"/g)].map((x) => x[1]);
}

console.log('\n[1/6] 스키마 이름과 경로가 Python 과 같은가');
await test('스키마 넷', () => {
  assert.equal(wire.SCHEMA_REQUEST, pyConst(v3, 'SCHEMA_REQUEST'));
  assert.equal(wire.SCHEMA_TOOL_CALL, pyConst(v3, 'SCHEMA_TOOL_CALL'));
  assert.equal(wire.SCHEMA_TOOL_RESULT, pyConst(v3, 'SCHEMA_TOOL_RESULT'));
  assert.equal(wire.SCHEMA_RESPONSE, pyConst(v3, 'SCHEMA_RESPONSE'));
});
await test('엔드포인트와 소켓', () => {
  assert.equal(wire.ANSWER_ENDPOINT, pyConst(v3, 'ANSWER_ENDPOINT'));
  assert.equal(wire.CANCEL_ENDPOINT, pyConst(v3, 'CANCEL_ENDPOINT'));
  assert.equal(wire.TOOL_ENDPOINT, pyConst(v3, 'TOOL_ENDPOINT'));
  assert.equal(wire.TOOL_SOCKET_PATH, pyConst(v3, 'TOOL_SOCKET_PATH'));
  assert.equal(wire.HEALTH_ENDPOINT, pyConst(v2, 'HEALTH_PATH'));
  assert.equal(wire.CONTRACT_VERSION, pyConst(v2, 'CONTRACT_VERSION'));
});
await test('상한과 시계 오차', () => {
  assert.equal(wire.MAX_REQUEST_BYTES, 256 * 1024);
  assert.equal(wire.MAX_RESPONSE_BYTES, 64 * 1024);
  assert.equal(wire.MAX_TOOL_RESULT_BYTES, 32 * 1024);
  assert.equal(wire.MAX_OUTPUT_CHARS, pyConst(v3, 'MAX_OUTPUT_CHARS'));
  assert.equal(wire.DEADLINE_MS, pyConst(v3, 'DEADLINE_MS'));
  assert.equal(wire.CLOCK_SKEW_SECONDS, pyConst(v2, 'CLOCK_SKEW_SECONDS'));
  assert.equal(wire.HEALTH_DEADLINE_MS, pyConst(v2, 'HEALTH_DEADLINE_MS'));
});
await test('도구·상태 목록', () => {
  assert.deepEqual([...wire.TOOLS], pyTuple(v3, 'TOOLS'));
  assert.deepEqual([...wire.DEFAULT_TOOLS], pyTuple(v3, 'DEFAULT_TOOLS'));
  // `fetch_recent_slack` 은 기본에서 빠져 있어야 한다(계약 §10.2 — 수집 주인이 둘이 되는 것).
  assert.ok(!wire.DEFAULT_TOOLS.includes('fetch_recent_slack'));
  for (const s of ['answered', 'no_evidence', 'refused', 'failed']) {
    assert.ok(wire.STATUSES.includes(s), s);
  }
});

console.log('[2/6] 서명이 Python 과 바이트로 같은가');
const SECRET = Buffer.from('0123456789abcdef0123456789abcdef0123456789abcdef', 'utf8');
await test('canonical 모양', () => {
  const body = Buffer.from('{"a":1}', 'utf8');
  const got = wire.canonical({ method: 'post', path: '/v3/tools/call', timestamp: 1700000000, nonce: 'n1', body })
    .toString('utf8');
  const digest = crypto.createHash('sha256').update(body).digest('hex');
  assert.equal(got, ['POST', '/v3/tools/call', '1700000000', 'n1', digest].join('\n'));
});
await test('Python 이 만든 서명과 일치', () => {
  const py = spawnSync(process.env.PYTHON || 'python', ['-c', [
    'import hashlib,hmac,sys',
    'body=b\'{"a":1}\'',
    'secret=sys.argv[1].encode()',
    'msg="\\n".join(["POST","/v3/tools/call","1700000000","n1",hashlib.sha256(body).hexdigest()]).encode()',
    'print(hmac.new(secret,msg,hashlib.sha256).hexdigest())',
    'print(hmac.new(secret,("req-1\\n"+hashlib.sha256(body).hexdigest()).encode(),hashlib.sha256).hexdigest())',
  ].join('\n'), SECRET.toString('utf8')], { encoding: 'utf8' });
  if (py.status !== 0) {
    // 파이썬이 없는 환경에서는 **건너뛴 사실을 남긴다.** 조용히 통과시키면
    // 「맞았다」 와 「안 쟀다」 가 같은 모양이 된다.
    results.push('      · python 없음 — 교차 검증 건너뜀 (못 쟀다)');
    return;
  }
  // 줄을 `\n` 으로만 가르지 않는다 — Windows 파이썬은 `\r\n` 으로 찍고, 남은 `\r` 는
  // 눈에 안 보이는 한 글자라 「서명이 다르다」 로만 나타난다.
  const [reqSig, resSig] = py.stdout.split(/\r?\n/).map((s) => s.trim()).filter(Boolean);
  const body = Buffer.from('{"a":1}', 'utf8');
  assert.equal(
    wire.sign(SECRET, wire.canonical({ method: 'POST', path: '/v3/tools/call', timestamp: 1700000000, nonce: 'n1', body })),
    reqSig,
  );
  assert.equal(wire.responseSignature(SECRET, { requestId: 'req-1', body }), resSig);
});
await test('safeEqualHex 는 길이가 달라도 안 던진다', () => {
  assert.equal(wire.safeEqualHex('abc', 'abc'), true);
  assert.equal(wire.safeEqualHex('abc', 'abcdef'), false);
  assert.equal(wire.safeEqualHex('', undefined), true); // 둘 다 빈 값
  assert.equal(wire.safeEqualHex('abc', undefined), false);
});

console.log('[3/6] 요청을 관대하게 받지 않는가');
const okRequest = (over = {}) => Buffer.from(JSON.stringify({
  schema: wire.SCHEMA_REQUEST,
  request_id: 'r1',
  authorization_id: 'auth-1',
  workspace: 'tyit',
  question: '지난주 정산 금액이 얼마였지',
  evidence: [],
  follow_up: [],
  tools: {
    token: 'tok', endpoint: wire.TOOL_ENDPOINT, socket: wire.TOOL_SOCKET_PATH,
    allow: ['search', 'read_channel', 'read_document'],
    budget: { max_calls: 12, max_per_tool: 4, max_chars: 60000, max_seconds: 45 },
  },
  limits: { deadline_ms: 90000, max_output_chars: 3000 },
  ...over,
}), 'utf8');

await test('정상 요청을 읽는다', () => {
  const r = wire.parseAnswerRequest(okRequest());
  assert.equal(r.requestId, 'r1');
  assert.equal(r.workspace, 'tyit');
  assert.equal(r.token, 'tok');
  assert.deepEqual([...r.allow], ['search', 'read_channel', 'read_document']);
  assert.equal(r.maxOutputChars, 3000);
});
await test('스키마가 다르면 거절', () => {
  assert.throws(() => wire.parseAnswerRequest(okRequest({ schema: 'tybot.specialist.request.v2' })), wire.WireViolation);
});
await test('도구 토큰이 없으면 거절', () => {
  assert.throws(() => wire.parseAnswerRequest(okRequest({
    tools: { token: '', endpoint: wire.TOOL_ENDPOINT, allow: ['search'] },
  })), /도구 토큰/);
});
await test('모르는 도구를 허용하면 거절', () => {
  assert.throws(() => wire.parseAnswerRequest(okRequest({
    tools: { token: 't', endpoint: wire.TOOL_ENDPOINT, allow: ['search', 'rm_rf'] },
  })), /모르는 도구/);
});
await test('소켓 경로를 요청이 바꾸려 하면 거절', () => {
  assert.throws(() => wire.parseAnswerRequest(okRequest({
    tools: { token: 't', endpoint: wire.TOOL_ENDPOINT, socket: '/tmp/evil.sock', allow: ['search'] },
  })), /소켓 경로/);
});
await test('빈 질문·빈 workspace 거절', () => {
  assert.throws(() => wire.parseAnswerRequest(okRequest({ question: '   ' })), /question/);
  assert.throws(() => wire.parseAnswerRequest(okRequest({ workspace: '' })), /workspace/);
});
await test('요청 상한', () => {
  const big = Buffer.alloc(wire.MAX_REQUEST_BYTES + 1, 0x20);
  assert.throws(() => wire.parseAnswerRequest(big), /상한/);
});

console.log('[4/6] 응답을 만들 때 규칙을 지키는가');
await test('answered 는 근거가 있어야 한다', () => {
  assert.throws(() => wire.buildAnswerResponse({
    requestId: 'r1', status: 'answered', answer: '네', usedEvidence: [],
  }), /근거 없이/);
});
await test('answered 는 비어 있으면 안 된다', () => {
  assert.throws(() => wire.buildAnswerResponse({
    requestId: 'r1', status: 'answered', answer: '  ', usedEvidence: ['e1'],
  }), /비었/);
});
await test('근거 ID 모양을 본다', () => {
  assert.throws(() => wire.buildAnswerResponse({
    requestId: 'r1', status: 'answered', answer: '네', usedEvidence: ['x7'],
  }), /형식/);
});
await test('출력 상한을 넘기면 던진다', () => {
  assert.throws(() => wire.buildAnswerResponse({
    requestId: 'r1', status: 'answered', answer: 'a'.repeat(31), usedEvidence: ['e1'], maxOutputChars: 30,
  }), /상한/);
});
await test('no_evidence 는 근거 없이도 된다', () => {
  const raw = wire.buildAnswerResponse({ requestId: 'r1', status: 'no_evidence', answer: '못 찾았습니다.' });
  const got = JSON.parse(raw.toString('utf8'));
  assert.equal(got.schema, wire.SCHEMA_RESPONSE);
  assert.equal(got.status, 'no_evidence');
  assert.deepEqual(got.used_evidence, []);
});
await test('health 응답이 저쪽 validate_health 를 지난다', () => {
  const got = JSON.parse(wire.buildHealthResponse({ version: 'abc123' }).toString('utf8'));
  assert.equal(got.status, 'ok');
  assert.equal(got.contract_version, pyConst(v2, 'CONTRACT_VERSION'));
  assert.equal(got.version, 'abc123');
});

console.log('[5/6] 인용과 금지 문구');
await test('발급한 것과 지어낸 것을 가른다', () => {
  const issued = new Set(['e1', 'e7']);
  const got = wire.collectCitations('값은 3입니다[e7]. 그리고[e9] 또[e7].', issued);
  assert.deepEqual(got.known, ['e7']);
  assert.deepEqual(got.unknown, ['e9']);
});
await test('인용이 없으면 둘 다 빈 목록', () => {
  const got = wire.collectCitations('근거 없이 적은 문장.', new Set(['e1']));
  assert.deepEqual(got.known, []);
  assert.deepEqual(got.unknown, []);
});
await test('금지 문구를 잡는다', () => {
  assert.ok(wire.forbiddenReason('값은 3입니다.\n출처: [전산팀]공지'));
  assert.ok(wire.forbiddenReason('https://x.slack.com/archives/C01/p1'));
  assert.ok(wire.forbiddenReason('file:///var/lib/tybot/archive/a.md'));
  assert.ok(wire.forbiddenReason('<SCRIPT>alert(1)</SCRIPT>'));
  assert.equal(wire.forbiddenReason('값은 3입니다[e7].'), null);
});

console.log('[6/6] 도구 호출·결과');
await test('도구 호출 본문 모양', () => {
  const got = JSON.parse(wire.toolCallBody({
    requestId: 'r1', token: 't', callId: 't1', tool: 'search', args: { query: '정산' },
  }).toString('utf8'));
  assert.equal(got.schema, wire.SCHEMA_TOOL_CALL);
  assert.deepEqual(got, {
    schema: wire.SCHEMA_TOOL_CALL, request_id: 'r1', token: 't', call_id: 't1',
    tool: 'search', args: { query: '정산' },
  });
});
await test('모르는 도구는 본문을 못 만든다', () => {
  assert.throws(() => wire.toolCallBody({ requestId: 'r1', token: 't', callId: 't1', tool: 'rm' }), /모르는 도구/);
});
await test('도구 결과에서 근거 ID 만 거둔다', () => {
  const raw = Buffer.from(JSON.stringify({
    schema: wire.SCHEMA_TOOL_RESULT, call_id: 't1', ok: true, text: '…',
    evidence: [{ id: 'e7', kind: 'channel_line' }, { id: 'nope', kind: 'x' }, { kind: 'y' }],
    budget: { calls_left: 11 },
  }), 'utf8');
  const got = wire.parseToolResult(raw);
  assert.equal(got.ok, true);
  assert.deepEqual(got.evidence.map((e) => e.id), ['e7']);
});
await test('ok:false 는 사유와 함께 읽힌다', () => {
  const raw = Buffer.from(JSON.stringify({
    schema: wire.SCHEMA_TOOL_RESULT, call_id: 't1', ok: false, text: '', reason: 'expired-token',
    evidence: [], budget: {},
  }), 'utf8');
  const got = wire.parseToolResult(raw);
  assert.equal(got.ok, false);
  assert.equal(got.reason, 'expired-token');
});

console.log(results.join('\n'));
if (failed) {
  console.error(`\n✗ ${failed}건 어긋남`);
  process.exit(1);
}
console.log('\n✓ v3 계약 거울이 Python 원본과 맞는다');
