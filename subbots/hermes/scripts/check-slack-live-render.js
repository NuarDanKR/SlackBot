#!/usr/bin/env node
/** R3i: deterministic rendering/cache contracts; all archive/Slack inputs are fake.
 * --baseline-stdin accepts independently retained slack-live.js as a JSON string.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createSlackLiveRender } from '../src/slack/live-render.js';
import { createSlackLiveApi } from '../src/slack/live-api.js';
import { createSlackLiveWindow } from '../src/slack/live-window.js';

const source = fs.readFileSync(new URL('../src/slack-live.js', import.meta.url), 'utf8');
const names = ['renderText', 'stamp', 'normalizeMessage', 'formatTranscript'];
/* 합성 스코프의 개명 되짚기 — `config.js` 의 `normalizeChannel` + `canonWith` 와 같은 식이다.
 * 지도는 인자로 받은 가짜 Map 뿐이라 실물 `config.json`·`.sync-state.json` 을 안 읽는다. */
const canonOf = (name, map) => {
  const n = String(name || '').replace(/^#/, '').trim();
  return map.get(n) ?? n;
};
function facade(text, deps, { skipChannels = [], nameMap = new Map() } = {}) {
  const config = { timezone: 'UTC', limits: {}, digest: { skipChannels } };
  const injected = { ...deps, config, ROOT: '/fixture', path, FULL_ACCESS: {},
    fs: new Proxy({}, { get() { throw new Error('unexpected file access'); } }),
    canSeePrivateChannel: () => false, isPrivateChannel: () => false,
    normalizeChannel: s => s, redactPrivateMentions: s => s,
    canonicalChannel: (name, map = nameMap) => canonOf(name, map),
    currentChannelNames: () => nameMap,
    BOT_ANSWER_MARK: '(bot omitted)',
    createSlackLiveApi: opts => createSlackLiveApi({ console: deps.console, ...opts }),
    createSlackLiveRender: opts => createSlackLiveRender({ console: deps.console, ...opts }),
    createSlackLiveWindow: opts => createSlackLiveWindow({ console: deps.console, ...opts }) };
  const code = text.replace(/^import\s[\s\S]*?;\r?\n/gm, '')
    .replace(/^export \{[^}]*\};?\r?$/gm, '').replace(/^export /gm, '');
  return compileFunction(code + '\nreturn {' +
    [...names, 'fetchWindow', 'fetchThreadContext', 'dailyWindow', 'weeklyWindow', 'recentWindow'].join(',') +
    '};', Object.keys(injected))(...Object.values(injected));
}
const clone = x => structuredClone(x);
async function suite(make) {
  const results = [];
  const nativeNow = Date.now;
  let now = 1700000000000;
  Date.now = () => now;
  function fixture(opts) {
    const trace = [], unexpected = [];
    const state = { channels: [{ id: 'C1', name: 'fixture-a' }], failure: false };
    const deps = {
      listAllChannels() {
        trace.push(['channels']);
        if (state.failure) throw new Error('fixture failure');
        return clone(state.channels);
      },
      console: { warn: (...args) => trace.push(['warn', ...args]) },
    };
    const api = make(deps, opts);
    assert.deepEqual(trace, [], 'factory must not read archive');
    return { api, state, trace, unexpected, users: new Map([['U1', 'Person'], ['W1', 'Guest']]) };
  }
  async function check(name, fn, opts) {
    now = 1700000000000;
    const f = fixture(opts);
    const value = await fn(f);
    assert.deepEqual(f.unexpected, [], 'no swallowed unexpected calls: ' + name);
    results.push({ name, value: clone(value), trace: clone(f.trace) });
  }
  try {
    for (const [name, input, expected] of [
      ['empty', null, ''],
      ['trim', '  hello \n ', 'hello'],
      ['mentions', '<@U1> <@W1|old> <@U2> <@X1>', '@Person @Guest @U2 <@X1>'],
      ['links', '<https://example.test/a|label> <http://example.test/b>', '[label](https://example.test/a) http://example.test/b'],
      ['entity order', '&lt;@U1&gt; &amp;lt; &amp;gt; A&amp;B', '<@U1> &lt; &gt; A&B'],
      ['supplied channel name', '<#C1|given> <#C2|other>', '#given #other'],
      ['mapped channel', '<#C1> <#C1|>', '#fixture-a #fixture-a'],
      ['unresolved line', 'before\nsecret <#C9> rest\nafter', 'before\nafter'],
      ['unresolved multiple', 'a <#C8> <#C9>\nsafe\nb <#C7>', 'safe'],
      ['CRLF', 'before\r\nsecret <#C9>\r\nafter', 'before\r\nafter'],
      ['escaped channel stays literal', '&lt;#C9&gt;', '<#C9>'],
    ]) {
      await check('render ' + name, f => {
        const value = f.api.renderText(input, f.users);
        assert.equal(value, expected);
        const warnings = f.trace.filter(x => x[0] === 'warn');
        if (name.startsWith('unresolved') || name === 'CRLF') {
          assert.equal(warnings.length, 1);
          assert.ok(!JSON.stringify(warnings).match(/C[789]|secret/));
          assert.equal(warnings[0][1], '못 푼 채널 링크가 든 줄 ' +
            (name === 'unresolved multiple' ? 2 : 1) + '건을 뺐습니다');
        } else assert.equal(warnings.length, 0);
        if (name === 'supplied channel name') assert.equal(f.trace.length, 0);
        return value;
      });
    }
    await check('channel cache boundary is strictly greater than five minutes', f => {
      assert.equal(f.api.renderText('<#C1>', f.users), '#fixture-a');
      f.state.channels = [{ id: 'C1', name: 'renamed' }];
      now += 300000;
      assert.equal(f.api.renderText('<#C1>', f.users), '#fixture-a');
      now += 1;
      assert.equal(f.api.renderText('<#C1>', f.users), '#renamed');
      assert.equal(f.trace.filter(x => x[0] === 'channels').length, 2);
    });
    await check('failed refresh replaces old channel map and caches failure', f => {
      assert.equal(f.api.renderText('<#C1>', f.users), '#fixture-a');
      now += 300001;
      f.state.failure = true;
      assert.equal(f.api.renderText('<#C1>', f.users), '');
      f.state.failure = false;
      assert.equal(f.api.renderText('<#C1>', f.users), '');
      now += 300001;
      assert.equal(f.api.renderText('<#C1>', f.users), '#fixture-a');
      assert.equal(f.trace.filter(x => x[0] === 'channels').length, 3);
    });
    await check('initial channel read failure expires', f => {
      f.state.failure = true;
      assert.equal(f.api.renderText('<#C1>', f.users), '');
      f.state.failure = false;
      now += 300000;
      assert.equal(f.api.renderText('<#C1>', f.users), '');
      now++;
      assert.equal(f.api.renderText('<#C1>', f.users), '#fixture-a');
    });
    await check('empty map and unnamed entries remain unresolved', f => {
      f.state.channels = [{ id: 'C1' }, { id: 'C1', name: '' }];
      assert.equal(f.api.renderText('<#C1>', f.users), '');
      assert.equal(f.api.renderText('<#C1>', f.users), '');
      assert.equal(f.trace.filter(x => x[0] === 'channels').length, 1);
    });
    await check('last duplicate channel name wins', f => {
      f.state.channels.push({ id: 'C1', name: 'new-name' });
      assert.equal(f.api.renderText('<#C1>', f.users), '#new-name');
    });
    for (const [ts, tz, expected] of [
      ['0', 'UTC', '1970-01-01 00:00'],
      ['0.999999', 'Asia/Seoul', '1970-01-01 09:00'],
      ['1704067199.999', 'UTC', '2023-12-31 23:59'],
      ['1704067200', 'Asia/Seoul', '2024-01-01 09:00'],
      ['1710053999', 'America/New_York', '2024-03-10 01:59'],
      ['1710054000', 'America/New_York', '2024-03-10 03:00'],
    ]) {
      await check('stamp ' + ts + ' ' + tz, f => {
        const value = f.api.stamp(ts, tz);
        assert.equal(value, expected);
        return value;
      });
    }
    await check('invalid timestamp and timezone still throw', f => {
      assert.throws(() => f.api.stamp('invalid', 'UTC'), RangeError);
      assert.throws(() => f.api.stamp('0', 'Fixture/Invalid'), RangeError);
    });
    for (const [fields, author] of [
      [{ user: 'U1' }, 'Person'], [{ user: 'U2' }, 'U2'],
      [{ username: 'Named' }, 'Named'], [{ bot_profile: { name: 'Profile' } }, 'Profile'],
      [{}, '봇'], [{ bot_id: 'B1', username: 'Named' }, 'Named (봇)'],
      [{ bot_id: 'B1', user: 'U1' }, 'Person'],
    ]) {
      await check('normalize author ' + JSON.stringify(fields), f => {
        const input = { ts: '0', text: ' <@U1> <#C1> ', files: [{ name: 'a.txt' }, {}, { name: '' }],
          reply_count: 2, ...fields };
        const before = clone(input);
        const value = f.api.normalizeMessage(input, f.users, 'UTC');
        assert.deepEqual(value, { ts: '0', when: '1970-01-01 00:00', author,
          text: '@Person #fixture-a', files: ['a.txt'], replyCount: 2, replies: [] });
        assert.deepEqual(input, before, 'input not mutated');
        value.files.push('changed');
        assert.deepEqual(input, before, 'output does not share file array');
        return value;
      });
    }
    await check('normalize defaults', f => {
      assert.deepEqual(f.api.normalizeMessage({ ts: '0' }, f.users, 'UTC'),
        { ts: '0', when: '1970-01-01 00:00', author: '봇', text: '', files: [], replyCount: 0, replies: [] });
    });
    await check('transcript exact layout', f => {
      const entries = [
        { channel: 'failed', error: 'fixture_error' },
        { channel: 'empty', messages: [] },
        { channel: 'private', isPrivate: true, messages: [
          { when: 'DATE', author: 'Person', text: 'body', parentOutsideWindow: true, files: ['a', 'b'], replies: [
            { when: 'REPLY', author: 'Guest', text: '', files: ['c', 'd'] },
            { when: 'LAST', author: 'Guest', text: 'reply', files: [] }] },
          { when: 'END', author: 'Person', text: '', files: [], replies: [] }] }];
      const before = clone(entries);
      const value = f.api.formatTranscript(entries);
      const expected = [
        '## #failed\n(읽기 실패: fixture_error)\n',
        '## #empty  — 0건\n',
        '## #private 🔒  — 2건\n',
        '**DATE · Person** _(기간 밖 원글 — 이번 기간에 답글만 달림)_',
        'body', '📎 첨부: `a`, `b`',
        '> └ **REPLY · Guest** —  📎 c, d',
        '> └ **LAST · Guest** — reply', '', '**END · Person**', '',
      ].join('\n');
      assert.equal(value, expected);
      assert.deepEqual(entries, before);
      assert.equal(f.api.formatTranscript([]), '');
      return value;
    });
    if (make.integration) {
      await check('window thread and direct rendering share channel cache', async f => {
        const msg = { ts: '1700000000', text: '<@U1> <#C1>', user: 'U1', reply_count: 1 };
        const reply = { ts: '1700000001', text: 'reply <#C1>', user: 'U1' };
        const queues = {
          users: [{ members: [{ id: 'U1', name: 'Person' }] }],
          auth: [{ user_id: 'SELF' }],
          history: [{ messages: [msg] }],
          replies: [{ messages: [msg, reply] }, { messages: [msg, reply] }],
        };
        const call = name => async args => {
          f.trace.push([name, clone(args)]);
          if (!queues[name]?.length) {
            f.unexpected.push(name);
            throw new Error('unexpected ' + name);
          }
          return clone(queues[name].shift());
        };
        const client = { auth: { test: call('auth') }, users: { list: call('users') },
          conversations: { history: call('history'), replies: call('replies') } };
        const entries = await f.api.fetchWindow(client, { oldest: 1699999999, latest: 1700000010,
          channels: [{ id: 'C1', name: 'fixture-a' }] });
        assert.equal(entries[0].messages[0].text, '@Person #fixture-a');
        assert.equal(entries[0].messages[0].replies[0].text, 'reply #fixture-a');
        const thread = await f.api.fetchThreadContext(client, { channel: 'C1', threadTs: msg.ts });
        assert.equal(thread.count, 2);
        assert.match(thread.text, /@Person #fixture-a/);
        assert.equal(f.api.renderText('<#C1>', f.users), '#fixture-a');
        assert.equal(f.trace.filter(x => x[0] === 'channels').length, 1);
        for (const q of Object.values(queues)) assert.equal(q.length, 0);
        return { entries, thread, transcript: f.api.formatTranscript(entries) };
      });
      await check('period functions use moved stamp', f => {
        const date = new Date('2024-01-03T12:00:00Z');
        const daily = f.api.dailyWindow(date, 'UTC', 24, { sentThrough: null, maxHours: 48 });
        const weekly = f.api.weeklyWindow(date, 'UTC');
        const recent = f.api.recentWindow(2, date, 'UTC');
        assert.equal(daily.label, '2024-01-02 12:00 ~ 2024-01-03 12:00');
        assert.equal(weekly.label, '2024-01-01 ~ 2024-01-03');
        assert.equal(recent.days, 2);
        return { daily, weekly, recent };
      });
      /* skipChannels 는 개명을 되짚어 걸러야 한다 (2026-09-16) — 설정에는 옛 철자가 있고
       * 슬랙은 현재 이름을 준다. 못 거르면 안 다루기로 한 채널이 요약 재료로 실린다. */
      await check('개명한 skip 채널은 fetchWindow 가 아예 안 읽는다', async f => {
        const client = {
          auth: { test: async () => ({ user_id: 'SELF' }) },
          users: { list: async () => ({ members: [] }) },
          conversations: {
            history: async () => { f.unexpected.push('history'); return { messages: [] }; },
            replies: async () => { f.unexpected.push('replies'); return { messages: [] }; },
          },
        };
        const entries = await f.api.fetchWindow(client, { oldest: 1699999999, latest: 1700000010,
          channels: [{ id: 'C1', name: 'fixture-renamed' }] });
        assert.deepEqual(entries, []);
        return entries;
      }, { skipChannels: ['#fixture-old '], nameMap: new Map([['fixture-old', 'fixture-renamed']]) });
    }
    return results;
  } finally { Date.now = nativeNow; }
}
const direct = await suite(createSlackLiveRender);
const via = (deps, opts) => facade(source, deps, opts);
via.integration = true;
const integrated = await suite(via);
assert.deepEqual(integrated.slice(0, direct.length), direct, 'module vs facade');
if (process.argv.includes('--baseline-stdin')) {
  const old = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.equal(typeof old, 'string');
  const baseline = (deps, opts) => facade(old, deps, opts);
  baseline.integration = true;
  assert.deepEqual(await suite(baseline), integrated, 'independent baseline vs candidate');
  console.log('Baseline SHA256: ' + createHash('sha256').update(old).digest('hex'));
}
console.log('Slack live rendering: ' + direct.length + ' contracts + ' +
  (integrated.length - direct.length) + ' integration cases passed.');
