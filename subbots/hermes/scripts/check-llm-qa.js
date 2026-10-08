#!/usr/bin/env node
/**
 * Codex R1c: synthetic Q&A contracts with the installed SDK's real tool runner.
 * Only messages.create is stubbed; network is blocked. No files or Slack writes.
 * --baseline-stdin accepts the pre-extraction claude.js source as a JSON string,
 * compares complete traces in memory, and prints its normalized source hash.
 * --facade additionally reads real config/archive through the public facade.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createHash } from 'node:crypto';
import { compileFunction } from 'node:vm';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import Anthropic from '@anthropic-ai/sdk';
import { createQuestionAnswerer } from '../src/llm/qa.js';
import { createToolBuilder } from '../src/llm/tools.js';
import { createToolSession } from '../src/llm/provider.js';
import { createUsageCollector, attachUsage } from '../src/llm/usage.js';
import { estimateCost } from '../src/format.js';

const ROOT = fileURLToPath(new URL('../', import.meta.url));
const clone = value => JSON.parse(JSON.stringify(value));
const fixedTime = '2026-01-02T03:04:05Z';
const config = { timezone: 'UTC', models: {
  qa: { id: 'claude-opus-5', maxTokens: 100, effort: 'low' },
  fallback: 'claude-sonnet-5',
}, limits: { maxToolIterations: 4 } };
const usage = { input_tokens: 100, output_tokens: 10 };
const message = (id, stop = 'end_turn', content = [{ type: 'text', text: 'answer' }]) => ({
  id: 'msg_' + id, role: 'assistant', stop_reason: stop, content, usage,
});
const tool = id => message(id, 'tool_use', [
  { type: 'tool_use', id: 'toolu_' + id, name: 'search', input: { query: 'fixture' } },
]);
/* 캐시 토큰을 실은 회차.
 *
 * 이 파일의 고정물 `usage` 는 오래 입력·출력 두 칸만 실었다. 그래서 창구가 회차 usage 에서
 * 캐시 칸 셋(`cache_creation_input_tokens`·`cache_read_input_tokens`·`cache_creation`)을
 * 통째로 떨궈도 **기준판 대조까지 포함해** 아무 검사도 안 빨개졌다 (2026-09-19 감사에서
 * 재현). Q&A 는 캐시 표시가 핵심 설계라 운영 usage 의 대부분이 캐시 토큰이고, 원장은
 * 원문(`turn.raw`)을 따로 받아 **금액은 맞으니** 어디서도 안 갈린다.
 *
 * 네 칸을 서로 다른 수로 둔다 — 같게 두면 떨어뜨림도 뒤바뀜도 안 보인다. */
const cachedUsage = {
  input_tokens: 3, output_tokens: 5,
  cache_creation_input_tokens: 70, cache_read_input_tokens: 900,
  cache_creation: { ephemeral_5m_input_tokens: 70, ephemeral_1h_input_tokens: 0 },
};
const cached = (m) => ({ ...m, usage: cachedUsage });
const request = { question: 'synthetic question', access: { private: false }, origin: 'fixture', asker: 'tester' };
const savedFetch = globalThis.fetch;
globalThis.fetch = async () => { throw new Error('network forbidden'); };

function baselineFactory(source) {
  const start = source.indexOf('const emptyUsage =');
  const end = source.indexOf('/** 시도 이력·실패 집계', start);
  assert.ok(start >= 0 && end > start, 'pre-extraction Q&A boundary');
  const body = source.slice(start, end).replace(/^export /gm, '');
  return deps => {
    class FixedDate extends Date {
      constructor(...args) { super(...(args.length ? args : [fixedTime])); }
      static now() { return new Date(fixedTime).getTime(); }
    }
    const bindings = { ...deps, Date: FixedDate, createUsageCollector, attachUsage, estimateCost };
    return compileFunction(body + '\nreturn answerQuestion;', Object.keys(bindings))(...Object.values(bindings));
  };
}

/* sha 의 src/llm/qa.js 를 그때 모양 그대로 세운다. 진입점만 옛 판으로 갈면 옛 배선이
 * 넘기는 의존성 이름과 지금 모듈이 받는 이름이 어긋나 비교 자체가 성립하지 않고, 그 실패가
 * 「계약이 달라졌다」로 잘못 읽힌다 (digest·요약 대조에서 같은 함정을 두 번 겪었다).
 * import 는 떼어 주입한다 — 파일을 쓰지 않는다. */
function baselineQuestionAnswerer(sha) {
  const text = execFileSync('git', ['-C', ROOT, 'show', sha + ':src/llm/qa.js'], {
    encoding: 'utf8', maxBuffer: 1024 * 1024,
  }).replace(/\r\n/g, '\n')
    .replace(/^import [^;]*;$/gm, '')
    .replace(/^export (?=(?:async function|function|const) )/gm, '');
  assert.ok(!/^import\b/m.test(text), '기준판 qa.js 의 import 경계를 확인할 수 없습니다');
  const names = ['createUsageCollector', 'attachUsage'];
  const evaluate = compileFunction(text + '\nreturn createQuestionAnswerer;', names);
  return evaluate(createUsageCollector, attachUsage);
}

/* 실물 tools.js 의 buildTools — 도구 래퍼 포함 — 를 지나게 하는 빌더. 아래 픽스처
 * buildTools 는 래퍼 자리를 통째로 대신해 칸을 스스로 밀므로, 래퍼 계약(진입 전 좁힘 칸 ·
 * 끝난 뒤 크기 · tool_use id 짝)은 이쪽 시나리오만이 실제로 잰다. 읽기 계층만 스텁이다.
 * `boom`(read_channel)과 CRASH 낱말(search)은 run 을 던지게 해 「칸을 안 남기는 호출」을
 * 만든다 — SDK 는 그 예외를 is_error tool_result 로 바꿔 루프를 잇는다. */
const CRASH = '검사가터뜨리는낱말';
function realToolBuilder() {
  const channels = ['사업장가'];
  return createToolBuilder({
    config,
    canSee: () => true,
    canSeePrivateChannel: () => true,
    isPrivateChannel: () => false,
    matchesHiddenPrivate: () => false,
    BLOCKED_NOTE: 'blocked-note-fixture',
    // 표시 용어는 **주입**이다(2026-10-08). 픽스처도 적어야 한다 — 기본값을 두면
    // 사내 인스턴스가 PF 말투로 말하고, 그건 오류로 안 나타난다.
    terms: { place: '사업장', area: '사업장',
      examples: { EX_A: '사업장가', EX_B: '사업장나', EX_C: '사업장다' } },
    truncMarker: s => `…(${s})`,
    searchArchive: ({ query, channel }) => {
      if (query.includes(CRASH)) throw new Error('synthetic search crash');
      return { hits: [{ channel: channel || channels[0], text: 'hit for ' + query }], partial: false };
    },
    readChannel: ({ channel }) => {
      if (channel === 'boom') throw new Error('synthetic read crash');
      return { channel, text: 'channel body of ' + channel };
    },
    resolveChannel: name => ({ ok: true, name: String(name).replace(/^#/, '') }),
    listReadableChannels: () => channels,
    searchDocuments: () => ({ hits: [] }),
    readDocument: () => ({ error: 'no documents in fixture' }),
    hasDocuments: () => false,
    markArchivedAttachments: text => text,
    narrowableProjects: () => [],
    resolveProject: name => ({ ok: true, name }),
    detectPlace: (query, names) => names.find(n => query.includes(n)) ?? null,
    fetchWindow: async () => [],
    formatTranscript: () => '',
    recentWindow: d => ({ days: d }),
    listBotChannels: async () => [],
    DEFAULT_LIVE_FETCH_MAX_DAYS: 14,
  }).buildTools;
}

async function replay(factory, scenario) {
  const trace = { api: [], runs: [], tools: [], logs: [], results: [] };
  const steps = [...scenario.steps];
  const client = new Anthropic({ apiKey: 'synthetic-only', maxRetries: 0 });
  const failure = new Error('synthetic failure');
  client.beta.messages.create = async params => {
    trace.api.push(clone(params));
    assert.ok(steps.length, 'unexpected API call');
    const step = steps.shift();
    if (step === 'error') throw failure;
    return clone({ model: params.model, ...step });
  };
  const realRunner = client.beta.messages.toolRunner.bind(client.beta.messages);
  client.beta.messages.toolRunner = params => {
    trace.runs.push(clone(params));
    const runner = realRunner(params);
    if (scenario.localError) runner.generateToolResponse = async () => { throw failure; };
    return runner;
  };
  const answer = factory({
    /* 표시 용어(2026-10-08 주입). 기준판(git 고정)은 이 키를 안 쓰므로 그냥 무시한다 —
     * 값이 지금 PF 문구와 같아야 두 판의 출력이 글자까지 같다. */
    terms: { place: '사업장', area: '사업장',
      examples: { EX_A: '사업장가', EX_B: '사업장나', EX_C: '사업장다' } },
    config, anthropic: () => client,
    toolSession: (spec) => createToolSession(spec, { client: () => client }),
    now: () => new Date(fixedTime),
    systemBlocks: access => ({ common: 'common fixture', extra: access.private ? 'private fixture' : '' }),
    lastSyncedAt: () => '2026-01-01', privateQuoteLine: access => access.private ? 'allowed' : 'none',
    BOT_ANSWER_MARK: 'BOT_FIXTURE',
    // 비용 로그는 **표를 달아** 모은다 — 콘솔 줄과 같은 통에 섞여 있으면 시나리오가
    // 그것만 골라 단언할 수 없고, 그래서 오래 기준판 대조에서만 보이는 값이었다.
    logUsage: (...args) => trace.logs.push(clone(['usage', ...args])),
    console: { log: (...args) => trace.logs.push(clone(args)), warn: (...args) => trace.logs.push(clone(args)) },
    buildTools: scenario.buildTools ? scenario.buildTools() : ({ access, touched, sizes, narrows }) => [{
      name: 'search', description: 'synthetic search',
      input_schema: { type: 'object', properties: { query: { type: 'string' } }, required: ['query'] },
      // 래퍼 계약(2026-09-17): 칸에는 tool_use 의 id 가 실리고, qa.js 의
      // attachSizes·attachNarrows 가 순서가 아니라 그 id 로 toolCalls 에 짝짓는다.
      // 이 buildTools 는 실물(tools.js)의 래퍼를 통째로 대신하므로 그 계약도 스스로 지킨다.
      run: (input, ctx) => {
        trace.tools.push({ input: clone(input), access: clone(access) });
        touched.add('fixture');
        sizes.push({ name: 'search', id: ctx?.toolUse?.id, chars: 7 });
        narrows.push({ id: ctx?.toolUse?.id, narrowedTo: 'fixture' });
        return 'fixture';
      },
    }],
  });
  for (const opts of scenario.requests || [request]) {
    try { trace.results.push(await answer(opts)); }
    catch (err) {
      assert.equal(err, failure, 'same exception object');
      trace.results.push({ error: err.message, accounting: err.hermesAccounting });
    }
  }
  assert.equal(steps.length, 0, 'all expected API responses consumed');
  return trace;
}

/* 비용 로그(`logUsage`)로 실제 나간 호출만 골라 낸다.
 *
 * qa.js 는 이것을 세 자리에서 찍는다 — 거절하고 대체 모델로 갈아타기 **직전**(1차 시도분을
 * 먼저 정산한다), 두 번 거절한 뒤, 그리고 정상 종료. 모델 인자가 조용히 틀어지면 지출이
 * 엉뚱한 모델 앞으로 적히는데 에러는 안 난다 — 사람이 비용 로그를 볼 때까지 안 드러난다.
 * 이 값은 오래 기준판 대조에서만 보였다 (2026-09-19). */
const costLog = (t) => t.logs.filter((l) => l[0] === 'usage').map((l) => l.slice(1));

const scenarios = [
  { name: 'normal payload and public access', steps: [message('normal')], check(t) {
    const p = t.api[0];
    assert.equal(p.model, config.models.qa.id);
    assert.equal(p.max_tokens, 100); assert.deepEqual(p.output_config, { effort: 'low' });
    assert.deepEqual(p.system, [{ type: 'text', text: 'common fixture', cache_control: { type: 'ephemeral', ttl: '1h' } }]);
    assert.equal(p.messages[0].content, '[요청 정보]\n- 오늘: 2026-01-02\n- 아카이브 마지막 동기화: 2026-01-01\n- 이 질문이 온 곳: fixture\n- 인용해도 되는 비공개 채널: none\n- 질문자: tester\n\n[질문]\nsynthetic question');
    assert.deepEqual(p.betas, ['cache-diagnosis-2026-04-07']);
    assert.deepEqual(p.diagnostics, { previous_message_id: null });
    assert.equal(t.runs[0].max_iterations, 4);
    assert.equal(t.results[0].text, 'answer'); assert.equal(t.results[0].cacheBaseline, false);
    // 평범한 한 번은 비용 로그도 한 줄이다 — 이름표('qa')와 모델과 토큰까지.
    assert.deepEqual(costLog(t), [['qa', 'claude-opus-5', {
      input_tokens: 100, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0,
      cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 0 },
    }]], '정상 답변: 비용 로그가 한 줄로 그 모델 몫만 실어야 합니다');
  } },
  { name: 'private system and thread context', steps: [message('private')],
    requests: [{ ...request, access: { private: true }, threadContext: 'prior user text' }], check(t) {
      assert.deepEqual(t.api[0].system[1], { type: 'text', text: 'private fixture' });
      assert.match(t.api[0].messages[0].content, /BOT_FIXTURE/);
      assert.match(t.api[0].messages[0].content, /prior user text/);
      assert.match(t.api[0].messages[0].content, /- 인용해도 되는 비공개 채널: allowed/);
    } },
  { name: 'empty answer', steps: [message('empty', 'end_turn', [])], check(t) {
    assert.equal(t.results[0].text, '답변을 만들지 못했습니다. 질문을 조금 더 구체적으로 적어 주세요.');
  } },
  { name: 'multiple tools and rolling cache marks', steps: [tool('a'), tool('b'), tool('c'), message('done')], check(t) {
    assert.equal(t.tools.length, 3); assert.deepEqual(t.results[0].channels, ['fixture']);
    assert.ok(t.results[0].toolCalls.every(c => c.chars === 7 && c.narrowedTo === 'fixture'));
    assert.deepEqual(t.api.map(p => p.messages.flatMap(m => Array.isArray(m.content) ? m.content : []).filter(b => b.cache_control).length), [0, 1, 2, 2]);
    for (const p of t.api.slice(1)) {
      const last = p.messages.at(-1).content.at(-1);
      assert.deepEqual(last.cache_control, { type: 'ephemeral' });
      assert.equal(last.content, 'fixture');
    }
    assert.deepEqual(t.api.map(p => p.diagnostics.previous_message_id), [null, 'msg_a', 'msg_b', 'msg_c']);
    /* **회차 넷이 전부 더해져야 한다.** 여기까지는 비용 로그를 한 줄도 안 보고 있었다 —
     * 옮겨 온 costLog 단언 셋은 1회차짜리(normal)와 폴백 두 개뿐이라, 메인 루프가
     * 마지막 회차만 남기게 망가뜨려도 상시 검사가 전부 초록이었다 (2026-09-19 감사에서
     * 재현). 그것이 `qa.js` 머리말이 적어 둔 옛 사고 — 「검색을 서너 번 도는 질문이면
     * 실제 비용의 1/3만 찍힌다」 — 의 모양 그대로다. 고정물이 회차마다 100/10 이니
     * 네 회차면 400/40 이고, 마지막 회차만 남으면 100/10 으로 떨어져 여기서 갈린다. */
    assert.deepEqual(costLog(t), [['qa', 'claude-opus-5', {
      input_tokens: 400, output_tokens: 40, cache_creation_input_tokens: 0, cache_read_input_tokens: 0,
      cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 0 },
    }]], '도구 왕복 네 회차: 비용 로그가 네 회차를 다 더해야 합니다');
  } },
  /* 한 회차에 도구를 **둘** 부르는 자리.
   *
   * 다른 시나리오의 도구 결과는 전부 블록 한 개짜리다 — 그러면 첫 블록과 끝 블록이 같은
   * 것이라, 표식을 끝에서 첫 블록으로 옮기는 잘못이 안 보인다 (2026-09-19 감사에서 재현:
   * 상시 검사 여섯이 전부 초록). 표식이 첫 블록에 붙으면 뒤 블록들이 캐시 범위 밖이 되고
   * **다음 회차부터 그만큼 정가로 다시 물린다.** 답은 멀쩡히 나와서 아무도 모른다. */
  { name: 'cache mark sits on the last tool result, not the first', steps: [
    message('mt', 'tool_use', [
      { type: 'tool_use', id: 'toolu_m1', name: 'search', input: { query: 'fixture' } },
      { type: 'tool_use', id: 'toolu_m2', name: 'search', input: { query: 'fixture' } },
    ]),
    message('mt_done'),
  ], check(t) {
    const results = t.api[1].messages.at(-1).content;
    assert.equal(results.length, 2, '한 회차에 도구 둘을 부르면 결과 블록도 둘이어야 합니다');
    assert.deepEqual(results.map((b) => Boolean(b.cache_control)), [false, true],
      '캐시 표식은 도구 결과 **마지막** 블록에만 붙어야 합니다');
  } },
  { name: 'cache tokens survive the tool round trip', steps: [cached(tool('ca')), cached(message('cb'))], check(t) {
    const total = {
      input_tokens: 6, output_tokens: 10,
      cache_creation_input_tokens: 140, cache_read_input_tokens: 1800,
      cache_creation: { ephemeral_5m_input_tokens: 140, ephemeral_1h_input_tokens: 0 },
    };
    assert.deepEqual(costLog(t), [['qa', 'claude-opus-5', total]],
      '도구 왕복: 회차의 캐시 토큰이 창구를 지나며 떨어지면 안 됩니다');
    // 원장 쪽(금액을 내는 자리)도 같은 원문을 본다 — 한쪽만 맞는 것을 막는다.
    assert.deepEqual(t.results[0].accounting.usage, total,
      '도구 왕복: 원장 합계의 캐시 토큰이 회차 원문과 달라졌습니다');
  } },
  { name: 'refusal then fallback with tools', steps: [message('ref', 'refusal'), tool('fb'), message('fb_done')], check(t) {
    assert.equal(t.runs.length, 2); assert.equal(t.runs[1].model, config.models.fallback);
    assert.deepEqual(t.runs[1].messages, t.runs[0].messages);
    assert.equal(t.results[0].accounting.records.length, 3);
    assert.equal(t.results[0].refused, false); assert.equal(t.tools.length, 1);
    /* 비용 로그는 **모델마다 한 줄**이고 1차 시도분이 먼저 나간다. 갈아타기 전에 정산하지
     * 않으면 두 모델의 토큰이 한 줄에 뭉쳐 대체 모델 앞으로 전부 적힌다 — 아래 out 20 이
     * 30 이 되는 자리다. 지출을 모델별로 세는 근거가 이 두 줄이다.
     *
     * **usage 객체를 통째로 본다.** 이 단언이 기준판 대조에서 상시 검사로 옮겨질 때(1f64be6)
     * `[이름표, 모델, output_tokens]` 세 칸으로 줄었는데, 기준판은 usage 전체를 대조하고
     * 있었다 — 옮기면서 약해진 것이다. 세 칸만 보면 입력·캐시 토큰이 두 모델 사이에서
     * 뒤섞여도 output 만 맞으면 통과한다. 정산이 가장 위험한 자리가 바로 여기다. */
    assert.deepEqual(costLog(t), [
      ['qa', 'claude-opus-5', {
        input_tokens: 100, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0,
        cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 0 },
      }],
      ['qa', 'claude-sonnet-5', {
        input_tokens: 200, output_tokens: 20, cache_creation_input_tokens: 0, cache_read_input_tokens: 0,
        cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 0 },
      }],
    ], '거절·폴백: 비용 로그는 모델마다 그 모델이 실제로 쓴 만큼만 실어야 합니다');
  } },
  { name: 'double refusal', steps: [message('r1', 'refusal'), message('r2', 'refusal')], check(t) {
    assert.equal(t.results[0].refused, true); assert.equal(t.results[0].text, '이 질문에는 답변할 수 없습니다.');
    assert.equal(t.results[0].accounting.records.length, 2);
    // 답을 못 냈어도 두 모델을 다 썼으므로 두 줄 다 나가야 한다 (거절은 공짜가 아니다).
    // 여기도 usage 를 통째로 본다 — 위와 같은 이유다.
    const refusalUsage = {
      input_tokens: 100, output_tokens: 10, cache_creation_input_tokens: 0, cache_read_input_tokens: 0,
      cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 0 },
    };
    assert.deepEqual(costLog(t),
      [['qa', 'claude-opus-5', refusalUsage], ['qa', 'claude-sonnet-5', refusalUsage]],
      '두 번 거절: 비용 로그가 두 모델 몫으로 각각 나가야 합니다');
  } },
  { name: 'max tokens', steps: [message('cut', 'max_tokens')], check(t) {
    assert.equal(t.results[0].truncated, true); assert.equal(t.results[0].toolLimit, false);
    assert.match(t.results[0].text, /답변이 상한\(100토큰\)/);
  } },
  { name: 'actual SDK iteration limit', steps: [tool('l1'), tool('l2'), tool('l3'), tool('l4')], check(t) {
    assert.equal(t.api.length, 4); assert.equal(t.results[0].toolLimit, true);
    assert.match(t.results[0].text, /자료를 찾아 읽기를 4번/);
    // 상한에 닿아 끝난 질문도 회차 넷을 다 썼다 — 답을 못 냈다고 지출이 없던 일이 되지 않는다.
    assert.deepEqual(costLog(t).map((c) => [c[0], c[1], c[2].input_tokens, c[2].output_tokens]),
      [['qa', 'claude-opus-5', 400, 40]],
      '회차 상한: 상한에 닿아도 쓴 회차만큼 비용 로그에 실려야 합니다');
  } },
  { name: 'first API failure', steps: ['error'], check(t) {
    assert.equal(t.results[0].accounting.unknownAttempts, 1); assert.equal(t.results[0].accounting.costUsd, null);
  } },
  { name: 'partial usage then API failure', steps: [tool('partial'), 'error'], check(t) {
    assert.equal(t.results[0].accounting.records.length, 2);
    assert.ok(t.results[0].accounting.knownCostUsd > 0);
  } },
  { name: 'local tool response failure is not another API attempt', localError: true, steps: [tool('local')], check(t) {
    assert.equal(t.results[0].accounting.records.length, 1);
    assert.equal(t.results[0].accounting.unknownAttempts, 0);
  } },
  { name: 'consecutive question diagnostics and cache miss', requests: [request, request],
    steps: [message('first'), { ...message('second'), diagnostics: { cache_miss_reason: { type: 'fixture', cache_missed_input_tokens: 9 } } }], check(t) {
      assert.equal(t.api[1].diagnostics.previous_message_id, 'msg_first');
      assert.equal(t.results[1].cacheBaseline, true);
      assert.deepEqual(t.results[1].cacheMiss, [{ type: 'fixture', count: 1, tokens: 9 }]);
    } },
  { name: 'invalid message ID is not reused', requests: [request, request],
    steps: [{ ...message('invalid'), id: 'not-a-message' }, message('next')], check(t) {
      assert.equal(t.api[1].diagnostics.previous_message_id, null);
      assert.equal(t.results[1].cacheBaseline, false);
    } },
  /* 모양이 틀린 id 는 **들고 있던 멀쩡한 기준을 덮는다.** 위 시나리오는 질문이 둘뿐이고
   * 틀린 id 앞에 멀쩡한 id 가 없어서, 「덮는다 / 안 덮는다」 두 동작이 똑같이 null 로 나와
   * 갈리지 않는다. 질문 셋(멀쩡 → 틀림 → 평범)이라야 세 번째 요청이 보내는 기준이
   * null(덮음) 인지 'msg_good'(안 덮음) 인지로 갈린다 (Fix round 1 리뷰가 찾은 사각). */
  { name: 'invalid message ID overwrites the stored baseline (three questions)',
    requests: [request, request, request],
    steps: [message('good'), { ...message('bad'), id: 'not-a-message' }, message('third')], check(t) {
      assert.equal(t.api[0].diagnostics.previous_message_id, null);
      assert.equal(t.results[0].cacheBaseline, false);
      // 둘째 질문은 첫째의 멀쩡한 id 를 기준으로 쓴다.
      assert.equal(t.api[1].diagnostics.previous_message_id, 'msg_good');
      assert.equal(t.results[1].cacheBaseline, true);
      // 셋째 질문의 기준은 msg_good 이 **아니다** — 둘째의 틀린 id 가 그 자리를 덮었고,
      // 보낼 때 모양 판정에 걸려 null 이 된다.
      assert.equal(t.api[2].diagnostics.previous_message_id, null);
      assert.equal(t.results[2].cacheBaseline, false);
    } },
  /* 실물 래퍼 계약 (2026-09-17 짝 밀림 수정의 검사). 픽스처 도구가 모든 호출에 같은 값
   * (chars 7 · narrowedTo 'fixture')을 밀면 순서 짝으로 되돌려도 초록이라, 여기서는
   * ① 한 회차에 같은 도구를 여러 번 부르고 호출마다 결과 크기가 다르게 하며
   * ② 그중 하나는 run 이 던져 칸을 안 남기고 ③ 각 호출에 **자기 값**이 붙었는지를
   * 호출별로 단언한다 (every 로 같은 값 확인 금지). 정답지는 다음 회차 API 파라미터에
   * 실제로 실린 tool_result 다 — 받는 쪽이 본 그 글자 수가 chars 여야 한다. */
  { name: 'real wrapper pairs sizes/narrows by tool_use id; thrown call leaves gaps',
    buildTools: realToolBuilder,
    // 추출 전 claude.js 기준선은 순서 짝 시절 코드라 이 시나리오와 대조할 수 없다.
    skipBaseline: true,
    steps: [
      message('rw1', 'tool_use', [
        { type: 'tool_use', id: 'toolu_rw_a', name: 'read_channel', input: { channel: '사업장가' } },
        { type: 'tool_use', id: 'toolu_rw_boom', name: 'read_channel', input: { channel: 'boom' } },
        { type: 'tool_use', id: 'toolu_rw_c', name: 'read_channel', input: { channel: '사업장가-더-긴-이름' } },
      ]),
      message('rw2', 'tool_use', [
        { type: 'tool_use', id: 'toolu_rw_crash', name: 'search', input: { query: '아무낱말 ' + CRASH } },
        { type: 'tool_use', id: 'toolu_rw_narrow', name: 'search', input: { query: '사업장가 잔액' } },
      ]),
      message('rw_done'),
    ],
    check(t) {
      const calls = t.results[0].toolCalls;
      assert.equal(calls.length, 5);
      assert.deepEqual(calls.map(c => c.name), ['read_channel', 'read_channel', 'read_channel', 'search', 'search']);
      // M2: 짝짓기용 내부 id 는 마지막 단(attachNarrows)이 뗀다 — convo-log 의 jsonl 로 새면 안 된다.
      for (const c of calls) assert.ok(!('id' in c), 'internal pairing id leaked into toolCalls: ' + JSON.stringify(c));
      const results = Object.fromEntries(
        t.api.at(-1).messages
          .flatMap(m => (Array.isArray(m.content) ? m.content : []))
          .filter(b => b.type === 'tool_result')
          .map(b => [b.tool_use_id, b]),
      );
      // 호출마다 다른 자기 크기 — 던진 호출(boom)을 사이에 두고도 안 밀린다.
      assert.equal(calls[0].chars, results.toolu_rw_a.content.length);
      assert.equal(calls[2].chars, results.toolu_rw_c.content.length);
      assert.notEqual(calls[0].chars, calls[2].chars, 'fixture must yield per-call distinct sizes');
      assert.equal(results.toolu_rw_boom.is_error, true);
      assert.ok(!('chars' in calls[1]),
        'thrown call must have no chars (a later call\'s size shifted forward): ' + JSON.stringify(calls[1]));
      // 던진 search: 크기도 좁힘도 없어야 하고, 뒤 호출의 좁힘이 앞으로 밀리면 안 된다.
      assert.equal(results.toolu_rw_crash.is_error, true);
      assert.ok(!('chars' in calls[3]) && !('narrowedTo' in calls[3]) && !('widenedFrom' in calls[3]),
        'thrown search must keep an empty slot: ' + JSON.stringify(calls[3]));
      // 좁힌 search: 자기 크기 + 자기 좁힘.
      assert.equal(calls[4].chars, results.toolu_rw_narrow.content.length);
      assert.equal(calls[4].narrowedTo, '대화는 #사업장가');
      assert.ok(!('widenedFrom' in calls[4]));
      assert.ok(t.results[0].channels.includes('사업장가'));
    } },
];

try {
  let baseline;
  let baselineIsStdin = false;
  if (process.argv.includes('--baseline-stdin')) {
    const source = JSON.parse(fs.readFileSync(0, 'utf8'));
    assert.equal(typeof source, 'string');
    baseline = baselineFactory(source);
    baselineIsStdin = true;
    console.log('Baseline normalized SHA-256: ' + createHash('sha256').update(source).digest('hex'));
  }
  const shaAt = process.argv.indexOf('--baseline');
  if (shaAt >= 0) {
    const sha = process.argv[shaAt + 1] || '';
    assert.ok(/^[0-9a-f]{40}$/.test(sha), '사용법: node scripts/check-llm-qa.js --baseline <40자 sha>');
    assert.ok(!baseline, '--baseline 과 --baseline-stdin 을 함께 쓰지 않습니다');
    baseline = baselineQuestionAnswerer(sha);
  }
  for (const scenario of scenarios) {
    const actual = await replay(createQuestionAnswerer, scenario);
    scenario.check(actual);
    // skipBaseline 은 「추출 전 claude.js」 기준판에만 해당한다 (그 판은 순서 짝 시절 코드다).
    // sha 기준판은 그 이후 코드라 모든 시나리오를 대조한다.
    if (baseline && !(baselineIsStdin && scenario.skipBaseline)) {
      assert.deepEqual(actual, await replay(baseline, scenario),
        scenario.name + (baselineIsStdin ? ': pre-extraction trace' : ': 기준판 trace'));
    }
    console.log('  ✓ ' + scenario.name);
  }
  // Each independently built factory must begin with no diagnostics baseline.
  const isolated = await replay(createQuestionAnswerer, scenarios[0]);
  assert.equal(isolated.results[0].cacheBaseline, false);

  if (process.argv.includes('--facade')) {
    const probe = new Anthropic({ apiKey: 'synthetic-only', maxRetries: 0 });
    const proto = Object.getPrototypeOf(probe.beta.messages);
    const original = proto.create;
    const oldKey = process.env.ANTHROPIC_API_KEY;
    process.env.ANTHROPIC_API_KEY ||= 'synthetic-only';
    let calls = 0;
    try {
      proto.create = async function(params) { calls++; return { ...message('facade'), model: params.model }; };
      const { answerQuestion } = await import('../src/claude.js');
      const { PUBLIC_ACCESS } = await import('../src/config.js');
      const result = await answerQuestion({ ...request, access: PUBLIC_ACCESS });
      assert.equal(result.text, 'answer'); assert.equal(calls, 1);
      assert.equal(result.accounting.records.length, 1);
    } finally {
      proto.create = original;
      if (oldKey === undefined) delete process.env.ANTHROPIC_API_KEY;
      else process.env.ANTHROPIC_API_KEY = oldKey;
    }
    console.log('  ✓ actual ESM facade with real SDK runner and stubbed create');
  }
  console.log(scenarios.length + ' Q&A contracts + factory isolation passed; live model calls 0.');
} finally {
  globalThis.fetch = savedFetch;
}
