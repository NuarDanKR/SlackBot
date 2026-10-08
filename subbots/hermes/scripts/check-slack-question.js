#!/usr/bin/env node
/** R3f: memory-only Slack question contracts.
 * --baseline-stdin accepts independently retained pre-extraction index.js.
 * Never imports the application entry point, starts a bot, or calls a service.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createSlackQuestion } from '../src/slack/question.js';
import { usageFields } from '../src/llm/usage.js';

const indexSource = fs.readFileSync(new URL('../src/index.js', import.meta.url), 'utf8');
function wiring(source) {
  const start = source.indexOf('/* ── 질문 처리');
  const end = source.indexOf("app.event('app_mention'", start);
  assert.ok(start >= 0 && end > start, 'question binding boundary');
  const block = source.slice(start, end);
  return deps => compileFunction(block + '\nreturn {handleQuestion};', Object.keys(deps))(...Object.values(deps));
}
function snapshot(value) {
  if (value instanceof Error) return { name: value.name, message: value.message,
    data: snapshot(value.data), hermesAccounting: snapshot(value.hermesAccounting) };
  if (value instanceof Set) return { set: [...value].map(snapshot) };
  if (Array.isArray(value)) return value.map(snapshot);
  if (value && typeof value === 'object')
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, snapshot(v)]));
  return value;
}
const accounting = { usage: { input_tokens: 12, output_tokens: 3 },
  byModel: [{ model: 'fixture-model' }], costUsd: 0.125, knownCostUsd: 0.125,
  complete: true, unknownAttempts: 0 };
const incomplete = { ...accounting, costUsd: null, complete: false, unknownAttempts: 1 };
const specs = [
  { name: 'channel' }, { name: 'DM', channelType: 'im' },
  { name: 'channel thread', thread: true }, { name: 'DM thread', thread: true, channelType: 'im' },
  { name: 'mentions', text: ' <@U123> <@W456|Alias> question ' },
  { name: 'empty', text: ' <@U123|Alias> ' },
  { name: 'empty post failure', text: '', failPost: 1 },
  { name: 'no timestamp', noTs: true },
  { name: 'real name fallback', user: { real_name: 'Real' } },
  { name: 'user ID fallback', user: {} },
  { name: 'user lookup failure', userFail: true },
  { name: 'reaction failures', reactionFail: true },
  { name: 'first post failure', failPost: 1 },
  { name: 'second post failure', failPost: 2 },
  { name: 'error reply failure', modelFail: true, failPost: 1 },
  { name: 'model failure', modelFail: true },
  { name: 'model incomplete usage', modelFail: true, errorAccounting: incomplete },
  { name: 'access failure', accessFail: true },
  { name: 'thread failure', thread: true, threadFail: true },
  { name: 'format failure', formatFail: true },
  { name: 'chunk failure', chunkFail: true },
  { name: 'legacy answer usage', legacy: true },
  { name: 'incomplete answer usage', answerAccounting: incomplete },
  { name: 'Slack error precedence', modelFail: true, errorData: 'platform_' + 'x'.repeat(550) },
];
async function run(factory, label) {
  const results = [];
  for (const spec of specs) {
    const trace = [], logs = [], posts = [];
    const access = { full: false, channels: new Set(['fixture-private']) };
    const event = { user: 'U1', channel: 'C1', channel_type: spec.channelType || 'channel',
      ...(spec.noTs ? {} : { ts: '100.001' }), ...(spec.thread ? { thread_ts: '99.001' } : {}) };
    const text = spec.text ?? 'question';
    const err = new Error('synthetic failure');
    if (spec.errorAccounting) err.hermesAccounting = spec.errorAccounting;
    if (spec.errorData) err.data = { error: spec.errorData };
    const record = (kind, ...args) => trace.push([kind, ...args.map(snapshot)]);
    const response = { text: '**answer**\n\nbody', channels: ['fixture'], toolCalls: [{ name: 'search' }],
      usage: { input_tokens: 99 }, model: 'legacy-model', costUsd: 99,
      refused: true, cacheMiss: [{ type: 'fixture' }], truncated: true,
      cacheBaseline: false, toolLimit: true,
      ...(!spec.legacy ? { accounting: spec.answerAccounting || accounting } : {}) };
    let postCount = 0;
    const client = {
      chat: { async postMessage(args) {
        record('post', args); posts.push(structuredClone(args));
        if (++postCount === spec.failPost) throw err;
      } },
      reactions: {
        async add(args) { record('reaction:add', args); if (spec.reactionFail) throw err; },
        async remove(args) { record('reaction:remove', args); if (spec.reactionFail) throw err; },
      },
      users: { async info(args) {
        record('user', args); if (spec.userFail) throw err;
        return { user: spec.user || { profile: { display_name: 'Display' }, real_name: 'Real' } };
      } },
    };
    const deps = {
      /* 표시 용어(2026-10-08 주입). **두 벌이 필요하다** — 이 검사는 팩토리를 직접
       * 부르고(`terms`), 그 결과를 `index.js` 호출부를 떼어 돌린 것과 대보기 때문이다
       * (그쪽은 `domainTerms(DOMAIN)` 을 스스로 부른다). 값은 같아야 두 길이 같은
       * 결과를 낸다 — 다르면 「두 길이 다르다」 가 아니라 「용어가 다르다」 로 깨진다. */
      terms: { place: '사업장', area: '사업장',
        examples: { EX_A: '사업장가', EX_B: '사업장나', EX_C: '사업장다' } },
      DOMAIN: 'pf-construction',
      domainTerms: () => ({ place: '사업장', area: '사업장', examples: { EX_A: '사업장가', EX_B: '사업장나', EX_C: '사업장다' } }),
      createSlackQuestion,
      async resolveAccess(actualClient, options) {
        assert.equal(actualClient, client); record('access', options);
        if (spec.accessFail) throw err;
        return { access, origin: 'fixture-origin' };
      },
      accessLabel(value) {
        record('label', value); assert.ok(value === access || value === null);
        return value ? 'private fixture' : 'none';
      },
      async fetchThreadContext(actualClient, options) {
        assert.equal(actualClient, client); assert.equal(options.access, access);
        record('thread', options);
        if (spec.threadFail) throw err;
        return { text: 'previous human text', count: 2 };
      },
      async answerQuestion(options) {
        assert.equal(options.slackClient, client); assert.equal(options.access, access);
        record('answer', { ...options, slackClient: 'same-client' });
        if (spec.modelFail) throw err;
        return response;
      },
      /* 채널 멘션 풀기 (2026-09-21 부터). 여기서는 **배선만** 잰다 —
       * 실제 푸는 규칙(권한·못 푸는 ID)은 `check-channel-ids.js` 가 실물로 잰다.
       * 그래서 항등함수로 두고, **access 를 받아서 불리는지**만 못 박는다. */
      resolveChannelMentions(value, actualAccess) {
        assert.equal(actualAccess, access, 'channel mentions resolve under the asker access');
        record('resolveChannelMentions', value);
        return value;
      },
      toSlackMrkdwn(answer) {
        record('format', answer); if (spec.formatFail) throw err; return 'formatted:' + answer;
      },
      chunkForSlack(formatted) {
        record('chunk', formatted); if (spec.chunkFail) throw err; return ['part1', 'part2', 'part3'];
      },
      logConversation(value) { record('log', value); logs.push(structuredClone(value)); },
      permalink(...args) { record('permalink', ...args); return 'https://example.invalid/thread'; },
      usageFields(value) { record('usage', value); return usageFields(value); },
      console: { log(...args) { record('console:log', ...args); },
        error(...args) { record('console:error', ...args); } },
    };
    const { handleQuestion } = factory(deps);
    assert.deepEqual(trace, [], 'factory initialization has no side effects');
    const beforeEvent = structuredClone(event), beforeResponse = structuredClone(response);
    const beforeAccess = structuredClone(access);
    const nativeNow = Date.now;
    let now = 1700000000000;
    Date.now = () => { record('clock'); return now++; };
    let outcome;
    try {
      try { outcome = { value: await handleQuestion({ client, event, text }) }; }
      catch (error) { outcome = { error: snapshot(error) }; }
    } finally { Date.now = nativeNow; }
    assert.deepEqual(event, beforeEvent); assert.deepEqual(response, beforeResponse);
    assert.deepEqual(access, beforeAccess);
    const empty = !text.replace(/<@[UW][A-Z0-9]+(\|[^>]*)?>/g, '').trim();
    if (empty) {
      assert.ok(!trace.some(t => t[0] === 'answer' || t[0] === 'access' || t[0].startsWith('reaction:')));
      assert.equal(logs.length, spec.failPost ? 0 : 1);
      if (spec.failPost) assert.deepEqual(outcome.error, snapshot(err));
      else { assert.equal(logs[0].kind, 'empty'); assert.equal(logs[0].ok, true); }
    } else {
      assert.equal(outcome.error, undefined);
      assert.equal(logs.length, 1, 'one cost/log record per non-empty request');
      assert.equal(trace.at(-1)[0], 'reaction:remove');
      /* 채널 멘션 풀기가 **실제로 불렸나**. 이 한 줄이 없으면 배선을 지워도 관문이
       * 전부 초록이라(2026-09-21 사보타주로 확인) 시험이 절반은 장식이 된다.
       * `access` 를 못 구한 회차(accessFail)는 치환 앞에서 멈추므로 뺀다. */
      if (!spec.accessFail) {
        assert.ok(trace.some(t => t[0] === 'resolveChannelMentions'),
          'question passes through channel-mention resolution');
      } else {
        assert.ok(!trace.some(t => t[0] === 'resolveChannelMentions'),
          'no mention resolution without a resolved access');
      }
      const failed = spec.failPost || spec.modelFail || spec.accessFail || spec.threadFail ||
        spec.formatFail || spec.chunkFail;
      assert.equal(logs[0].ok, !failed);
      assert.equal(logs[0].question, 'question');
      if (!failed) {
        assert.deepEqual(posts.map(p => p.text), ['part1', 'part2', 'part3']);
        assert.equal(logs[0].answer, response.text, 'log preserves unformatted answer');
        assert.equal(logs[0].threadContext, spec.thread ? 2 : 0);
        for (const key of ['refused', 'truncated', 'toolLimit', 'cacheBaseline'])
          assert.equal(logs[0][key], response[key]);
        assert.equal(logs[0].asker, spec.userFail || (spec.user && !spec.user.real_name) ? 'U1' :
          spec.user?.real_name || 'Display');
      }
      const observed = spec.errorAccounting || (!spec.modelFail && !spec.accessFail && !spec.threadFail && !spec.legacy ?
        spec.answerAccounting || accounting : undefined);
      if (observed) {
        assert.equal(logs[0].costUsd, observed.costUsd);
        assert.deepEqual(logs[0].accounting, observed);
      }
      if (spec.failPost === 2) {
        assert.equal(posts.length, 3);
        assert.ok(posts[2].text.startsWith('답변 중 오류'));
        assert.ok(!posts.some(p => p.text === 'part3'));
      }
      if (spec.errorData) {
        assert.equal(logs[0].error, spec.errorData.slice(0, 500));
        assert.ok(posts.at(-1).text.includes(spec.errorData.slice(0, 200)));
        assert.ok(!posts.at(-1).text.includes(spec.errorData.slice(0, 201)));
      }
      if (!spec.thread) assert.ok(!trace.some(t => t[0] === 'thread'));
      if (spec.thread && !spec.accessFail) {
        const threadCall = trace.find(t => t[0] === 'thread');
        assert.equal(threadCall[1].skipTs, event.ts);
        assert.equal(threadCall[1].threadTs, event.thread_ts);
      }
    }
    for (const post of posts) {
      assert.equal(post.channel, event.channel);
      assert.equal(post.thread_ts, event.thread_ts || event.ts);
      assert.equal(post.unfurl_links, false); assert.equal(post.unfurl_media, false);
    }
    results.push({ spec: spec.name, trace, outcome });
  }
  console.log('PASS ' + label + ': ' + results.length + ' question scenarios');
  return results;
}
function eventWiring(source) {
  const start = source.indexOf("app.event('app_mention'");
  const end = source.indexOf('app.error(', start);
  assert.ok(start >= 0 && end > start);
  const handlers = {}, calls = [];
  compileFunction(source.slice(start, end), ['app', 'handleQuestion', 'SKIP_SUBTYPES', 'isBotMessage'])(
    { event(name, fn) { handlers[name] = fn; }, message(fn) { handlers.dm = fn; } },
    async payload => calls.push(payload), new Set(['channel_join']), m => !!m.bot_id,
  );
  return { handlers, calls };
}
async function checkEvents(source) {
  const { handlers, calls } = eventWiring(source);
  const client = {};
  const mention = { channel_type: 'channel', text: 'question' };
  await handlers.app_mention({ event: mention, client });
  assert.equal(calls[0].event, mention); assert.equal(calls[0].client, client);
  await handlers.app_mention({ event: { channel_type: 'im' }, client });
  assert.equal(calls.length, 1, 'DM mention must not trigger duplicate answer');
  for (const message of [{ channel_type: 'channel' }, { channel_type: 'im', bot_id: 'B' },
    ...['channel_join', 'message_changed', 'message_deleted'].map(subtype => ({ channel_type: 'im', subtype }))])
    await handlers.dm({ message, client });
  assert.equal(calls.length, 1);
  const attachment = { channel_type: 'im', subtype: 'file_share' };
  await handlers.dm({ message: attachment, client });
  assert.equal(calls.length, 2); assert.equal(calls[1].event, attachment); assert.equal(calls[1].text, '');
}
const actual = await run(createSlackQuestion, 'question factory');
assert.deepEqual(actual, await run(wiring(indexSource), 'real entry-point binding'));
await checkEvents(indexSource);
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.equal(typeof source, 'string');
  assert.ok(!source.includes('createSlackQuestion'));
  assert.deepEqual(actual, await run(wiring(source), 'pre-extraction baseline'));
  await checkEvents(source);
  console.log('PASS exact posts, model requests, logs, errors and ordering; baseline SHA256=' +
    createHash('sha256').update(source).digest('hex'));
}
console.log('PASS event dispatch; disk writes, Slack/model calls and bot starts 0.');
