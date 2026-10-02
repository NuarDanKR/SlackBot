#!/usr/bin/env node
/**
 * 요약 생성·대조의 오프라인 계약 검사.
 *
 * node scripts/check-llm-summary.js
 * node scripts/check-llm-summary.js --baseline <full-commit-sha> --facade
 *
 * 기본 검사는 가명 config·가짜 모델·즉시 완료하는 대기로만 수행한다.
 * --baseline은 지정 커밋의 claude.js를 git show로 읽어 같은 입력의 전체
 * 요청·로그·대기·반환/오류를 비교한다. Git이 없으면 기본 검사만 실행할 수 있다.
 * --facade는 실제 config/프롬프트와 claude.js의 연결도 읽어 검사한다.
 * SDK create/stream을 가짜로 교체하고 fetch를 차단하며 파일·Slack에 쓰지 않는다.
 * Codex R1b: 비용·사용량 기록 차이는 별도로 검증하고 요청/제어 흐름은 기준판과 비교한다.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { execFileSync } from 'node:child_process';
import { compileFunction } from 'node:vm';
import { fileURLToPath } from 'node:url';
import { APIConnectionError } from '@anthropic-ai/sdk';
import { createDigestGenerator } from '../src/llm/digest.js';
import { createSummaryChecker } from '../src/llm/summary-check.js';
import { createTextModel, createJsonModel } from '../src/llm/provider.js';
import { createUsageCollector, attachUsage } from '../src/llm/usage.js';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const source = fs.readFileSync(new URL('../src/claude.js', import.meta.url), 'utf8');
const clone = (value) => JSON.parse(JSON.stringify(value));
const CONFIG = {
  models: {
    qa: { id: 'qa-fixture', maxTokens: 3200, effort: 'medium' },
    summaryCheck: { id: 'check-fixture', maxTokens: 1200, effort: 'low' },
    daily: { id: 'daily-fixture', maxTokens: 1600, effort: 'high' },
    weekly: { id: 'weekly-fixture', maxTokens: 2400, effort: 'medium' },
    fallback: 'fallback-fixture',
  },
};
const USAGE = { input_tokens: 11, output_tokens: 7 };
const OPTIONS = {
  kind: 'daily', transcript: '원문 대화', windowLabel: '고정 기간', stats: '메시지 1건',
};
const SUMMARY = { channel: '채널가', summary: '이전 요약', transcript: '새 원문' };
const FINDING = { type: '새 쟁점', where: '항목가', was: '', now: '확인 필요', evidence: '새 원문' };
const response = (text, stop = 'end_turn', model = 'result-fixture') => ({
  model, stop_reason: stop, content: [{ type: 'text', text }], usage: clone(USAGE),
});
const apiError = (fields) => Object.assign(new Error('검사용 실패'), fields);
const ERR_MARK = '/** 시도 이력·실패 집계';
const DEP_NAMES = [
  'config', 'anthropic', 'promptFile', 'logUsage', 'estimateCost', 'sleep',
  'APIConnectionError', 'console', 'createDigestGenerator', 'createSummaryChecker',
  'createTextModel', 'createJsonModel',
];

/* 기준판은 **그때의 digest.js 도 함께** 읽어 평가한다. 진입점(claude.js)만 옛 판으로 갈면
 * 옛 배선이 넘기는 의존성 이름과 지금 모듈이 받는 이름이 어긋나(anthropic → textModel)
 * 비교 자체가 성립하지 않고, 그 실패가 「계약이 달라졌다」로 잘못 읽힌다. 호출 자리를 통째로
 * 그때 것으로 세워야 요청·대기·경고·반환이 정말 그대로인지가 증명된다.
 * import 는 facadeFactory 와 같은 방식으로 떼어내 주입한다 — 파일을 쓰지 않는다. */
function baselineDigestGenerator(sha) {
  const text = execFileSync('git', ['-C', ROOT, 'show', sha + ':src/llm/digest.js'], {
    encoding: 'utf8', maxBuffer: 1024 * 1024,
  }).replace(/\r\n/g, '\n')
    .replace(/^import [^;]*;$/gm, '')
    .replace(/^export (?=(?:async function|function|const) )/gm, '');
  assert.ok(!/^import\b/m.test(text), '기준판 digest.js 의 import 경계를 확인할 수 없습니다');
  const names = ['createUsageCollector', 'attachUsage', 'APIConnectionError'];
  const evaluate = compileFunction(text + '\nreturn createDigestGenerator;', names);
  return evaluate(createUsageCollector, attachUsage, APIConnectionError);
}

/* 기준판은 그때의 summary-check.js 도 함께 읽어 평가한다 (baselineDigestGenerator 와 같은 이유).
 * 이렇게 해야 「옛 SDK 직통 경로 대 새 창구 경로」의 대조가 되고, 둘을 이어 붙여 돌리면
 * 기준판까지 새 창구를 통과해 증명이 무력해진다. 파일을 쓰지 않는다. */
function baselineSummaryChecker(sha) {
  const text = execFileSync('git', ['-C', ROOT, 'show', sha + ':src/llm/summary-check.js'], {
    encoding: 'utf8', maxBuffer: 1024 * 1024,
  }).replace(/\r\n/g, '\n')
    .replace(/^import [^;]*;$/gm, '')
    .replace(/^export (?=(?:async function|function|const) )/gm, '');
  assert.ok(!/^import\b/m.test(text), '기준판 summary-check.js 의 import 경계를 확인할 수 없습니다');
  const names = ['createUsageCollector', 'attachUsage'];
  const evaluate = compileFunction(text + '\nreturn createSummaryChecker;', names);
  return evaluate(createUsageCollector, attachUsage);
}

// 진입점의 실제 연결 코드를 합성 의존성과 함께 평가한다. 함수/주석 경계가 달라지면
// 잘못된 부분을 조용히 시험하지 않고 실패한다. 실제 ESM import는 --facade에서 별도 검사한다.
function facadeFactory(text, makeDigest = createDigestGenerator, makeChecker = createSummaryChecker) {
  const start = text.indexOf(ERR_MARK);
  assert.ok(start >= 0 && text.indexOf(ERR_MARK, start + ERR_MARK.length) < 0, '유일한 함수 경계가 필요합니다');
  let body = text.slice(start).replace(/\r\n/g, '\n');
  body = body.replace(/^export (?=(?:async function|function|const) )/gm, '');
  const evaluate = compileFunction(body + '\nreturn { generateDigest, compareSummary, errLabel };', DEP_NAMES);
  return (deps) => evaluate(...DEP_NAMES.map((key) => ({
    ...deps, APIConnectionError,
    createDigestGenerator: (options) => makeDigest({ ...options, sleep: deps.sleep }),
    createSummaryChecker: makeChecker,
    createTextModel,
    createJsonModel,
  })[key]));
}
const currentFactory = facadeFactory(source);
let baselineFactory = null;
let baselineSha = null;
let withFacade = false;
const argv = process.argv.slice(2);
for (let i = 0; i < argv.length; i++) {
  if (argv[i] === '--facade') withFacade = true;
  else if (argv[i] === '--baseline' && /^[0-9a-f]{40}$/.test(argv[i + 1] || '')) {
    baselineSha = argv[++i];
    const original = execFileSync('git', ['-C', ROOT, 'show', baselineSha + ':src/claude.js'], {
      encoding: 'utf8', maxBuffer: 1024 * 1024,
    });
    baselineFactory = facadeFactory(original, baselineDigestGenerator(baselineSha), baselineSummaryChecker(baselineSha));
  } else throw new Error('사용법: node scripts/check-llm-summary.js [--baseline <full-sha>] [--facade]');
}

function normalizeError(err) {
  return {
    name: err.name, message: err.message,
    ...(err.hermesType ? { hermesType: err.hermesType } : {}),
    ...(err.hermesAttempts ? { hermesAttempts: clone(err.hermesAttempts) } : {}),
  };
}
async function replay(factory, kind, makeSteps, options, configure = () => {}) {
  const trace = [];
  const config = clone(CONFIG);
  configure(config);
  const steps = makeSteps();
  let cursor = 0;
  const take = () => {
    assert.ok(cursor < steps.length, '예상 밖 모델 호출');
    return steps[cursor++];
  };
  const finish = (step) => {
    if (step.error) throw step.error;
    return clone(step.reply);
  };
  const client = { messages: {
    create: async (params) => {
      trace.push(['create', clone(params)]);
      return finish(take());
    },
    stream: (params) => {
      trace.push(['stream', clone(params)]);
      const step = take();
      if (step.atStream) throw step.error;
      return { finalMessage: async () => {
        trace.push(['finalMessage']);
        return finish(step);
      } };
    },
  } };
  const deps = {
    config,
    anthropic: () => client,
    promptFile: (name) => { trace.push(['prompt', name]); return 'prompt:' + name; },
    logUsage: (...args) => trace.push(['usage', ...clone(args)]),
    estimateCost: (...args) => { trace.push(['cost', ...clone(args)]); return { usd: 0.125, rateKnown: true, rateBasis: 'fixture' }; },
    sleep: async (ms) => { trace.push(['sleep', ms]); },
    console: { warn: (...args) => trace.push(['warn', ...args]) },
  };
  // 새 모듈의 console도 기록한다. 이 스크립트는 한 프로세스에서 순서대로만 실행한다.
  const savedWarn = console.warn;
  console.warn = deps.console.warn;
  let outcome;
  let caught;
  try {
    const service = factory(deps);
    const result = await service[kind](clone(options));
    outcome = { result: clone(result) };
  } catch (err) {
    caught = err;
    outcome = { error: normalizeError(err) };
  } finally {
    console.warn = savedWarn;
  }
  return { trace, outcome, cursor, caught, steps };
}

const events = (r, name) => r.trace.filter((e) => e[0] === name);
const request = (r) => events(r, 'stream')[0]?.[1] || events(r, 'create')[0]?.[1];
const attempts = (models, errors) => models.map((model, i) => ({
  model, waitMs: [0, 60000, 180000, 0][i], errorType: errors[i],
}));
let count = 0;
async function check(name, kind, makeSteps, options, verify, configure) {
  const actual = await replay(currentFactory, kind, makeSteps, options, configure);
  verify(actual);
  if (baselineFactory) {
    const before = await replay(baselineFactory, kind, makeSteps, options, configure);
    // Accounting is intentionally changed in R1b. Keep payloads, waits, warnings,
    // output text/refusal and exception identity/type under the original contract.
    const contract = (r) => {
      const outcome = clone(r.outcome);
      if (outcome.result && !Array.isArray(outcome.result)) {
        for (const key of ['accounting', 'costUsd']) delete outcome.result[key];
        if (outcome.result.refused) {
          delete outcome.result.usage; delete outcome.result.model;
        }
      }
      return { trace: r.trace.filter((e) => !['usage', 'cost'].includes(e[0])), outcome, calls: r.cursor };
    };
    assert.deepEqual(contract(actual), contract(before), name + ': 비회계 계약이 기준판과 다릅니다');
  }
  count += 1;
  console.log('  ✓ ' + name);
}
const one = (reply) => () => [{ reply }];
for (const kind of ['daily', 'weekly']) {
  await check(kind + ' 정상 요청·반환·사용량', 'generateDigest',
    one(response('  답변 \n')), { ...OPTIONS, kind }, (r) => {
      assert.equal(r.outcome.result.text, '답변');
      assert.equal(r.outcome.result.refused, false);
      assert.equal(r.outcome.result.costUsd, 0.125);
      assert.deepEqual(r.outcome.result.usage, USAGE);
      assert.deepEqual(request(r), {
        model: CONFIG.models[kind].id, max_tokens: CONFIG.models[kind].maxTokens,
        output_config: { effort: CONFIG.models[kind].effort },
        system: [{ type: 'text', text: 'prompt:' + kind }],
        messages: [{ role: 'user', content:
          "[기간] 고정 기간\n[수집 현황] 메시지 1건\n\n아래는 이 기간에 오간 슬랙 대화 원문입니다. 스레드 답글은 '└' 로 표시되어 있습니다.\n\n---\n\n원문 대화\n",
        }],
      });
      assert.deepEqual(r.outcome.result.attempts, attempts([CONFIG.models[kind].id], [null]));
      assert.equal(events(r, 'usage').length, 1);
      assert.deepEqual(events(r, 'usage')[0], ['usage', `digest:${kind}`, 'result-fixture', USAGE]);
      assert.equal(events(r, 'sleep').length, 0);
    });
}
await check('첨부·OCR·요지·본문 없는 문서', 'generateDigest', one(response('답변')), {
  ...OPTIONS, documents: [
    { project: '사업장가', title: '문서가', kind: '검토', date: '2026-01-02', file: '가.pdf', gist: '요지', ocr: true, text: '본문' },
    { project: '사업장나', title: '문서나' },
  ],
}, (r) => {
  const content = request(r).messages[0].content;
  assert.ok(content.endsWith(
    '\n\n---\n\n아래는 이 기간에 슬랙에 올라온 첨부 문서입니다. 위 대화와 달리 **문서에 적힌 것**입니다.\n\n' +
    '**사업장가 · 문서가** (검토 · 문서일 2026-01-02 · 가.pdf)\n요지: 요지\n' +
    '⚠️ 스캔본 OCR — 조문·금액을 그대로 인용하지 말 것\n\n본문\n\n' +
    '**사업장나 · 문서나**\n(본문은 길이 상한으로 싣지 않았습니다 — 올라왔다는 사실과 요지까지만 쓰세요)'));
});
await check('빈 첨부·비텍스트 응답 제외', 'generateDigest', one({
  ...response(''), content: [{ type: 'thinking', thinking: '비출력' }, { type: 'text', text: ' 앞 ' }, { type: 'text', text: '뒤 ' }],
}), { ...OPTIONS, documents: [] }, (r) => {
  assert.equal(r.outcome.result.text, '앞 \n뒤');
  assert.ok(!request(r).messages[0].content.includes('첨부 문서입니다'));
});
await check('본문 없는 정상 종료', 'generateDigest', one({ ...response(''), content: [] }), OPTIONS,
  (r) => assert.equal(r.outcome.result.text, ''));
await check('요약 거절: 반환 보존·비용 누락 수정', 'generateDigest', one(response('', 'refusal')), OPTIONS, (r) => {
  assert.equal(r.outcome.result.text, '');
  assert.equal(r.outcome.result.refused, true);
  assert.deepEqual(r.outcome.result.attempts, attempts(['daily-fixture'], [null]));
  assert.equal(r.outcome.result.accounting.costUsd, 0.125);
  assert.equal(events(r, 'usage').length, 1);
  assert.deepEqual(events(r, 'usage')[0], ['usage', 'digest:daily', 'result-fixture', USAGE]);
  assert.equal(r.outcome.result.model, 'result-fixture');
  assert.deepEqual(r.outcome.result.usage, USAGE);
  assert.equal(r.cursor, 1);
});
await check('요약 출력 상한: 오류·사용량·시도 이력', 'generateDigest', one(response('잘린 답', 'max_tokens')), OPTIONS, (r) => {
  assert.equal(r.outcome.error.hermesType, 'max_tokens');
  assert.match(r.outcome.error.message, /max_tokens\(1600\)/);
  assert.deepEqual(r.outcome.error.hermesAttempts, attempts(['daily-fixture'], [null]));
  assert.equal(events(r, 'usage').length, 1);
});
await check('재시도 3회 후 fallback 성공·대기 순서', 'generateDigest', () => [
  { error: apiError({ type: 'overloaded_error' }) },
  { error: apiError({ status: 503 }), atStream: true },
  { error: new APIConnectionError({ message: '검사용 연결 오류' }) },
  { reply: response('대체 답', 'end_turn', 'fallback-fixture') },
], OPTIONS, (r) => {
  assert.equal(r.outcome.result.text, '대체 답');
  assert.equal(r.outcome.result.model, 'fallback-fixture');
  assert.deepEqual(events(r, 'sleep'), [['sleep', 60000], ['sleep', 180000]]);
  assert.deepEqual(r.trace.filter((e) => ['stream', 'sleep'].includes(e[0])).map((e) =>
    e[0] === 'sleep' ? e : ['stream', e[1].model]), [
    ['stream', 'daily-fixture'], ['sleep', 60000], ['stream', 'daily-fixture'],
    ['sleep', 180000], ['stream', 'daily-fixture'], ['stream', 'fallback-fixture'],
  ]);
  assert.deepEqual(r.outcome.result.attempts, attempts(
    ['daily-fixture', 'daily-fixture', 'daily-fixture', 'fallback-fixture'],
    ['overloaded_error', 'http_503', 'APIConnectionError', null]));
});
for (const fields of [{ status: 408 }, { status: 409 }, { status: 429 }, { status: 500 },
  { type: 'rate_limit_error' }, { type: 'api_error' }]) {
  await check('재시도 가능한 오류 ' + JSON.stringify(fields), 'generateDigest',
    () => [{ error: apiError(fields) }, { reply: response('복구') }], OPTIONS, (r) => {
      assert.equal(r.outcome.result.text, '복구');
      assert.equal(r.cursor, 2);
      assert.deepEqual(events(r, 'sleep'), [['sleep', 60000]]);
    });
}
for (const fields of [{ status: 400 }, { status: 401 }, { type: 'invalid_request_error' }, {}]) {
  await check('즉시 실패 ' + JSON.stringify(fields), 'generateDigest',
    () => [{ error: apiError(fields) }], OPTIONS, (r) => {
      assert.equal(r.cursor, 1);
      assert.equal(r.caught, r.steps[0].error);
      assert.equal(r.outcome.error.hermesAttempts.length, 1);
      assert.equal(events(r, 'sleep').length, 0);
    });
}
for (const fallback of [true, false]) {
  await check('최종 실패 ' + (fallback ? 'fallback 있음' : 'fallback 없음'), 'generateDigest',
    () => Array.from({ length: fallback ? 4 : 3 }, () => ({ error: apiError({ type: 'api_error' }) })),
    OPTIONS, (r) => {
      assert.equal(r.cursor, fallback ? 4 : 3);
      assert.equal(r.caught, r.steps[r.cursor - 1].error);
      assert.equal(r.outcome.error.hermesAttempts.length, r.cursor);
      assert.deepEqual(events(r, 'sleep'), [['sleep', 60000], ['sleep', 180000]]);
    }, (config) => { if (!fallback) delete config.models.fallback; });
}
for (const stop of ['refusal', 'max_tokens']) {
  await check('fallback ' + stop, 'generateDigest', () => [
    ...Array.from({ length: 3 }, () => ({ error: apiError({ type: 'api_error' }) })),
    { reply: response('대체 답', stop, 'fallback-fixture') },
  ], OPTIONS, (r) => {
    assert.equal(r.cursor, 4);
    if (stop === 'refusal') {
      assert.equal(r.outcome.result.refused, true);
      assert.equal(r.outcome.result.attempts.length, 4);
      assert.equal(events(r, 'usage').length, 1);
    } else {
      assert.equal(r.outcome.error.hermesType, 'max_tokens');
      assert.equal(r.outcome.error.hermesAttempts.length, 4);
      assert.equal(events(r, 'usage')[0][2], 'fallback-fixture');
    }
  });
}
for (const fallback of [false, true]) {
  await check('대조 정상·스키마 ' + (fallback ? 'qa 설정 폴백' : '전용 설정'), 'compareSummary',
    one(response(JSON.stringify({ findings: [FINDING] }))), SUMMARY, (r) => {
      assert.deepEqual(r.outcome.result, [FINDING]);
      const p = request(r);
      assert.equal(p.model, fallback ? CONFIG.models.qa.id : CONFIG.models.summaryCheck.id);
      assert.equal(p.max_tokens, fallback ? CONFIG.models.qa.maxTokens : CONFIG.models.summaryCheck.maxTokens);
      assert.equal(p.output_config.effort, fallback ? 'medium' : 'low');
      assert.equal(p.output_config.format.type, 'json_schema');
      assert.deepEqual(p.output_config.format.schema.required, ['findings']);
      assert.equal(p.output_config.format.schema.additionalProperties, false);
      const item = p.output_config.format.schema.properties.findings.items;
      assert.deepEqual(item.required, ['type', 'where', 'was', 'now', 'evidence']);
      assert.deepEqual(item.properties.type.enum, ['숫자·일정', '새 쟁점', '끝난 쟁점']);
      assert.equal(item.additionalProperties, false);
      assert.deepEqual(p.system, [{ type: 'text', text: 'prompt:summary-check' }]);
      assert.equal(p.messages[0].content, '[채널] #채널가\n\n## 현재 상단 요약\n\n이전 요약\n\n## 이번에 새로 들어온 메시지\n\n새 원문');
      assert.equal(events(r, 'usage').length, 1);
      assert.deepEqual(events(r, 'usage')[0], ['usage', 'summary-check', 'result-fixture', USAGE]);
    }, (config) => { if (fallback) delete config.models.summaryCheck; });
}
await check('대조 요약 없음', 'compareSummary', one(response('{"findings":[]}')),
  { ...SUMMARY, summary: '' }, (r) => assert.ok(request(r).messages[0].content.includes('(요약 없음)')));
await check('대조 거절: 빈 목록 보존·비용 누락 수정', 'compareSummary', one(response('', 'refusal')), SUMMARY, (r) => {
  assert.deepEqual(r.outcome.result, []);
  assert.equal(events(r, 'usage').length, 1);
  assert.deepEqual(events(r, 'usage')[0], ['usage', 'summary-check', 'result-fixture', USAGE]);
});
await check('대조 상한은 JSON 파싱보다 먼저 실패', 'compareSummary', one(response('{', 'max_tokens')), SUMMARY, (r) => {
  assert.match(r.outcome.error.message, /max_tokens\(1200\)/);
  /* 이 문장은 **실패하면 DM 에 그대로 나가는 안내문**이다 — 받는 사람이 무엇을 고쳐야
   * 하는지가 여기 한 줄에 달려 있다. 숫자(1200)만 재고 있던 동안에는 안내 문구를 엉뚱한
   * 것으로 바꿔도 상시 검사가 전부 초록이었다 (2026-09-19 감사). 고칠 자리를 짚는
   * 부분까지 못박는다. */
  assert.match(r.outcome.error.message, /config\.json 의 `models\.summaryCheck\.maxTokens` 를 올려야 합니다/,
    '상한 초과 안내문이 고칠 자리를 짚어야 합니다');
  assert.equal(events(r, 'usage').length, 1);
  assert.deepEqual(events(r, 'usage')[0], ['usage', 'summary-check', 'result-fixture', USAGE]);
});
for (const text of ['not-json', '']) {
  await check('대조 잘못된 JSON ' + JSON.stringify(text), 'compareSummary', one(response(text)), SUMMARY,
    (r) => assert.match(r.outcome.error.message, /요약 대조 응답이 JSON 이 아닙니다/));
}
for (const text of ['{}', '{"findings":null}', '{"findings":"unexpected"}']) {
  await check('대조 findings 비배열 ' + text, 'compareSummary', one(response(text)), SUMMARY,
    (r) => assert.deepEqual(r.outcome.result, []));
}
await check('대조 모델 오류는 같은 객체로 전파', 'compareSummary',
  () => [{ error: apiError({ status: 503 }) }], SUMMARY, (r) => {
    assert.equal(r.caught, r.steps[0].error);
    assert.equal(r.cursor, 1);
    assert.equal(events(r, 'sleep').length, 0);
  });

if (withFacade) {
  const savedFetch = globalThis.fetch;
  const savedKey = process.env.ANTHROPIC_API_KEY;
  let networkAttempts = 0;
  globalThis.fetch = async () => { networkAttempts += 1; throw new Error('검사 중 네트워크 호출 금지'); };
  process.env.ANTHROPIC_API_KEY ||= 'check-stub-not-used';
  const { default: Anthropic } = await import('@anthropic-ai/sdk');
  const proto = Object.getPrototypeOf(new Anthropic({ apiKey: 'check-stub' }).messages);
  const savedCreate = proto.create;
  const savedStream = proto.stream;
  const savedLog = console.log;
  const payloads = [];
  proto.create = async function (params) {
    payloads.push(['create', clone(params)]);
    return response('{"findings":[]}');
  };
  proto.stream = function (params) {
    payloads.push(['stream', clone(params)]);
    return { finalMessage: async () => response('연결 검사') };
  };
  try {
    console.log = () => {};
    const { config } = await import('../src/config.js');
    const facade = await import('../src/claude.js');
    for (const kind of ['daily', 'weekly']) {
      assert.equal((await facade.generateDigest({ ...OPTIONS, kind })).text, '연결 검사');
      const p = payloads.at(-1)[1];
      assert.equal(p.model, config.models[kind].id);
      assert.equal(p.max_tokens, config.models[kind].maxTokens);
      assert.equal(p.output_config.effort, config.models[kind].effort);
      assert.equal(p.system[0].text, facade.renderPrompt(kind));
    }
    assert.deepEqual(await facade.compareSummary(SUMMARY), []);
    const p = payloads.at(-1)[1];
    assert.equal(p.model, (config.models.summaryCheck || config.models.qa).id);
    assert.equal(p.system[0].text, facade.renderPrompt('summary-check'));
    assert.equal(payloads.length, 3);
    assert.equal(networkAttempts, 0);
  } finally {
    proto.create = savedCreate;
    proto.stream = savedStream;
    console.log = savedLog;
    globalThis.fetch = savedFetch;
    if (savedKey === undefined) delete process.env.ANTHROPIC_API_KEY;
    else process.env.ANTHROPIC_API_KEY = savedKey;
  }
  console.log('  ✓ 실제 claude.js ESM 연결·실제 프롬프트·설정 (모델 요청 3건은 SDK 가짜 응답)');
}
console.log('\n' + count + '개 계약 사례 통과' +
  (baselineSha ? ' · 기준판 ' + baselineSha + '와 비회계 요청/반환/오류/대기 일치 (비용·로그 차이는 별도 검사)' : '') +
  ' · 실제 모델 API 호출 0회');
