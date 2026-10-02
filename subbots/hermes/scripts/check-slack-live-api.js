#!/usr/bin/env node
/** R3h: offline Slack paging/cache contracts and full facade integration.
 * --baseline-stdin reads an independently retained slack-live.js as a JSON string.
 * No real config, archive, Slack client or filesystem writes are used.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createSlackLiveApi } from '../src/slack/live-api.js';
import { createSlackLiveWindow } from '../src/slack/live-window.js';
import { createSlackLiveRender } from '../src/slack/live-render.js';

const source = fs.readFileSync(new URL('../src/slack-live.js', import.meta.url), 'utf8');
const names = ['getSelfId', 'getUserMap', 'fetchAllReplies', 'listBotChannels', 'listSlackChannels'];
/* 합성 스코프의 개명 되짚기 — `config.js` 의 `normalizeChannel` + `canonWith` 와 같은 식이다.
 * 지도는 인자로 받은 가짜 Map 뿐이라 실물 `config.json`·`.sync-state.json` 을 안 읽는다. */
const canonOf = (name, map) => {
  const n = String(name || '').replace(/^#/, '').trim();
  return map.get(n) ?? n;
};
function facade(text, console, { skipChannels = [], nameMap = new Map() } = {}) {
  const config = { timezone: 'UTC', limits: {}, digest: { skipChannels } };
  const deps = { config, ROOT: '/fixture', FULL_ACCESS: {}, path,
    fs: new Proxy({}, { get() { throw new Error('unexpected filesystem access'); } }),
    canSeePrivateChannel: () => false, isPrivateChannel: () => false,
    normalizeChannel: s => s, redactPrivateMentions: s => s,
    canonicalChannel: (name, map = nameMap) => canonOf(name, map),
    currentChannelNames: () => nameMap,
    BOT_ANSWER_MARK: '(bot omitted)', listAllChannels: () => [],
    createSlackLiveApi: opts => createSlackLiveApi({ console, ...opts }),
    createSlackLiveRender: opts => createSlackLiveRender({ console, ...opts }),
    createSlackLiveWindow: opts => createSlackLiveWindow({ console, ...opts }), console };
  const code = text.replace(/^import\s[\s\S]*?;\r?\n/gm, '')
    .replace(/^export \{[^}]*\};?\r?$/gm, '')
    .replace(/^export /gm, '');
  return compileFunction(code + '\nreturn {' + [...names, 'fetchWindow', 'fetchThreadContext'].join(',') + '};',
    Object.keys(deps))(...Object.values(deps));
}
const clone = x => structuredClone(x);
async function suite(make) {
  const results = [];
  const savedNow = Date.now;
  let now = 1700000000000;
  Date.now = () => now;
  function fixture(opts) {
    const trace = [];
    const queues = {};
    const console = { warn: (...a) => trace.push(['warn', ...a]) };
    const api = make(console, opts);
    assert.deepEqual(trace, []);
    const call = name => async args => {
      trace.push([name, clone(args)]);
      const q = queues[name];
      assert.ok(q?.length, 'unexpected API call ' + name);
      const result = q.shift();
      if (result instanceof Error) throw result;
      return clone(result);
    };
    const client = { auth: { test: call('auth') }, users: {
      list: call('users'), conversations: call('joined') },
      conversations: { list: call('channels'), replies: call('replies'), history: call('history') } };
    return { api, client, trace, queues };
  }
  async function check(name, fn, opts) {
    now = 1700000000000;
    const f = fixture(opts);
    const value = await fn(f);
    for (const q of Object.values(f.queues)) assert.equal(q.length, 0, name + ': unused response');
    results.push({ name, value: clone(value), trace: clone(f.trace) });
  }
  const error = () => Object.assign(new Error('fixture failure'), { data: { error: 'fixture_error' } });
  try {
    await check('self success is shared across clients', async f => {
      f.queues.auth = [{ user_id: 'SELF' }];
      assert.equal(await f.api.getSelfId(f.client), 'SELF');
      assert.equal(await f.api.getSelfId({}), 'SELF');
    });
    await check('self failure retries', async f => {
      f.queues.auth = [error(), { user_id: 'RECOVERED' }];
      assert.equal(await f.api.getSelfId(f.client), '');
      assert.equal(await f.api.getSelfId(f.client), 'RECOVERED');
    });
    await check('empty self ID retries', async f => {
      f.queues.auth = [{}, { user_id: '' }, { user_id: 'SELF' }];
      assert.equal(await f.api.getSelfId(f.client), '');
      assert.equal(await f.api.getSelfId(f.client), '');
      assert.equal(await f.api.getSelfId(f.client), 'SELF');
    });
    await check('replies paginate and keep first duplicate', async f => {
      f.queues.replies = [
        { messages: [{ ts: '1', text: 'parent' }, { ts: '2', text: 'first' }],
          response_metadata: { next_cursor: 'next' } },
        { messages: [{ ts: '1', text: 'again' }, { ts: '2', text: 'changed' }, { ts: '3' }] }];
      const value = await f.api.fetchAllReplies(f.client, { channel: 'C1', ts: '1' });
      assert.deepEqual(value, [{ ts: '1', text: 'parent' }, { ts: '2', text: 'first' }, { ts: '3' }]);
      assert.deepEqual(f.trace.map(x => x[1]), [
        { channel: 'C1', ts: '1', limit: 200, cursor: undefined },
        { channel: 'C1', ts: '1', limit: 200, cursor: 'next' }]);
      return value;
    });
    await check('empty reply page follows cursor', async f => {
      f.queues.replies = [{ response_metadata: { next_cursor: 'next' } }, { messages: [] }];
      assert.deepEqual(await f.api.fetchAllReplies(f.client, { channel: 'C1', ts: '1' }), []);
    });
    for (const partial of [false, true]) {
      await check('reply failure rejects partial=' + partial, async f => {
        const err = error();
        f.queues.replies = [...(partial ? [{ messages: [{ ts: '1' }],
          response_metadata: { next_cursor: 'next' } }] : []), err];
        await assert.rejects(f.api.fetchAllReplies(f.client, { channel: 'C1', ts: '1' }), e => e === err);
      });
    }
    await check('user pages and name fallback precedence', async f => {
      f.queues.users = [
        { members: [{ id: 'U1', profile: { display_name: 'Display', real_name: 'Real' }, name: 'Name' },
          { id: 'U2', profile: { display_name: '', real_name: 'Real' } }],
          response_metadata: { next_cursor: 'next' } },
        { members: [{ id: 'U3', name: 'Name' }, { id: 'U4' }, { id: 'U1', name: 'Updated' }] }];
      const map = await f.api.getUserMap(f.client);
      assert.deepEqual([...map], [['U1', 'Updated'], ['U2', 'Real'], ['U3', 'Name'], ['U4', 'U4']]);
      assert.equal(await f.api.getUserMap({}), map, 'same Map instance');
      map.set('LOCAL', 'shared');
      assert.equal((await f.api.getUserMap({})).get('LOCAL'), 'shared');
      return [...map];
    });
    await check('user TTL strict boundary', async f => {
      f.queues.users = [{ members: [{ id: 'U1' }] }, { members: [{ id: 'U2' }] }];
      const first = await f.api.getUserMap(f.client);
      now += 3599999;
      assert.equal(await f.api.getUserMap({}), first);
      now += 1;
      const second = await f.api.getUserMap(f.client);
      assert.notEqual(second, first);
      return [...second];
    });
    await check('force and maxAge override refresh', async f => {
      f.queues.users = [{}, {}, {}, {}];
      const a = await f.api.getUserMap(f.client);
      const b = await f.api.getUserMap(f.client, { force: true });
      const c = await f.api.getUserMap(f.client, { maxAgeMs: 0 });
      now += 10;
      const d = await f.api.getUserMap(f.client, { maxAgeMs: 10 });
      assert.notEqual(a, b); assert.notEqual(b, c); assert.notEqual(c, d);
    });
    await check('empty user map is cached', async f => {
      f.queues.users = [{ response_metadata: { next_cursor: 'next' } }, {}];
      const map = await f.api.getUserMap(f.client);
      assert.equal(map.size, 0);
      assert.equal(await f.api.getUserMap({}), map);
    });
    for (const partial of [false, true]) {
      await check('initial user failure rejects partial=' + partial, async f => {
        const err = error();
        f.queues.users = [...(partial ? [{ members: [{ id: 'PARTIAL' }],
          response_metadata: { next_cursor: 'next' } }] : []), err, { members: [{ id: 'OK' }] }];
        await assert.rejects(f.api.getUserMap(f.client), e => e === err);
        assert.deepEqual([...(await f.api.getUserMap(f.client))], [['OK', 'OK']]);
      });
    }
    await check('failed refresh retains old map without renewing TTL', async f => {
      f.queues.users = [{ members: [{ id: 'OLD' }] },
        { members: [{ id: 'PARTIAL' }], response_metadata: { next_cursor: 'next' } },
        error(), { members: [{ id: 'NEW' }] }];
      const old = await f.api.getUserMap(f.client);
      now += 3600000;
      assert.equal(await f.api.getUserMap(f.client), old);
      assert.deepEqual([...old], [['OLD', 'OLD']]);
      const fresh = await f.api.getUserMap(f.client);
      assert.deepEqual([...fresh], [['NEW', 'NEW']]);
      assert.deepEqual(f.trace.find(x => x[0] === 'warn'),
        ['warn', '사용자 목록 갱신 실패 — 이전 것을 씁니다:', 'fixture_error']);
    });
    await check('refresh warning falls back to error message', async f => {
      f.queues.users = [{}, new Error('plain failure')];
      const old = await f.api.getUserMap(f.client);
      assert.equal(await f.api.getUserMap(f.client, { force: true }), old);
      assert.equal(f.trace.at(-1).at(-1), 'plain failure');
    });
    for (const [method, endpoint, members] of [
      ['listBotChannels', 'joined', false], ['listSlackChannels', 'channels', true]]) {
      await check(method + ' pages sort and preserve fields', async f => {
        f.queues[endpoint] = [
          { channels: [{ id: 'CZ', name: 'z', is_private: true, num_members: 7 }],
            response_metadata: { next_cursor: 'next' } },
          { channels: [{ id: 'CA', name: 'a', num_members: 0 }, { id: 'CA', name: 'a' }] }];
        const value = await f.api[method](f.client);
        assert.deepEqual(value, [
          { id: 'CA', name: 'a', isPrivate: false, ...(members ? { members: 0 } : {}) },
          { id: 'CA', name: 'a', isPrivate: false, ...(members ? { members: undefined } : {}) },
          { id: 'CZ', name: 'z', isPrivate: true, ...(members ? { members: 7 } : {}) }]);
        assert.deepEqual(f.trace.map(x => x[1]), [undefined, 'next'].map(cursor => ({
          types: 'public_channel,private_channel', exclude_archived: true, limit: 200, cursor })));
        return value;
      });
      await check(method + ' empty pages', async f => {
        f.queues[endpoint] = [{ response_metadata: { next_cursor: 'next' } }, {}];
        assert.deepEqual(await f.api[method](f.client), []);
      });
      for (const partial of [false, true]) {
        await check(method + ' failure partial=' + partial, async f => {
          const err = error();
          f.queues[endpoint] = [...(partial ? [{ channels: [{ id: 'C1', name: 'a' }],
            response_metadata: { next_cursor: 'next' } }] : []), err];
          await assert.rejects(f.api[method](f.client), e => e === err);
        });
      }
    }
    if (make.integration) {
      for (const fail of ['none', 'history', 'replies']) {
        await check('facade fetchWindow and thread ' + fail, async f => {
          f.queues.users = [{ members: [{ id: 'U1', name: 'Person' }] }];
          f.queues.auth = [{ user_id: 'SELF' }];
          f.queues.joined = [{ channels: [{ id: 'C1', name: 'a' }] }];
          const parent = { ts: '1700000000', text: 'hello', user: 'U1', reply_count: 1 };
          const reply = { ts: '1700000001', text: 'followup', user: 'U1' };
          f.queues.history = [fail === 'history' ? error() : { messages: [parent] }];
          f.queues.replies = [
            ...(fail === 'history' ? [] : [fail === 'replies' ? error() : { messages: [parent, reply] }]),
            { messages: [parent, reply] }];
          const entries = await f.api.fetchWindow(f.client, { oldest: 1699999999, latest: 1700000010 });
          assert.equal(entries.length, 1);
          if (fail === 'history') assert.equal(entries[0].error, 'fixture_error');
          else {
            assert.equal(entries[0].messages[0].text, 'hello');
            assert.equal(entries[0].messages[0].replies.length, fail === 'replies' ? 0 : 1);
          }
          const thread = await f.api.fetchThreadContext(f.client, {
            channel: 'C1', threadTs: parent.ts, skipTs: reply.ts });
          assert.equal(thread.count, 1);
          assert.match(thread.text, /Person.*hello/);
          assert.equal(f.trace.filter(x => x[0] === 'users').length, 1);
          assert.equal(f.trace.filter(x => x[0] === 'auth').length, 1);
          return { entries, thread };
        });
      }
      /* skipChannels 는 개명을 되짚어 걸러야 한다 (2026-09-16). 설정에는 **옛** 철자가 있고
       * 슬랙은 **현재** 이름을 준다 — 글자 그대로 대면 안 다루기로 한 채널이 요약 재료로
       * 다시 실린다. 가짜 지도로 재므로 실물 아카이브도 진짜 채널 이름도 안 쓴다. */
      await check('개명한 skip 채널은 fetchWindow 가 아예 안 읽는다', async f => {
        f.queues.users = [{ members: [{ id: 'U1', name: 'Person' }] }];
        f.queues.auth = [{ user_id: 'SELF' }];
        f.queues.joined = [{ channels: [
          { id: 'C1', name: 'fixture-renamed' }, { id: 'C2', name: 'fixture-keep' }] }];
        // 응답은 남는 채널 몫 하나뿐이다 — skip 이 안 걸리면 '예상 밖 호출' 로 터진다.
        f.queues.history = [{ messages: [{ ts: '1700000000', text: 'kept', user: 'U1' }] }];
        const entries = await f.api.fetchWindow(f.client, { oldest: 1699999999, latest: 1700000010 });
        assert.deepEqual(entries.map(e => e.channel), ['fixture-keep']);
        assert.deepEqual(f.trace.filter(x => x[0] === 'history').map(x => x[1].channel), ['C2']);
        return entries;
      }, { skipChannels: ['#fixture-old '], nameMap: new Map([['fixture-old', 'fixture-renamed']]) });
    }
    return results;
  } finally { Date.now = savedNow; }
}
const direct = await suite(console => createSlackLiveApi({ console }));
const viaFacade = (console, opts) => facade(source, console, opts);
viaFacade.integration = true;
const integrated = await suite(viaFacade);
assert.deepEqual(integrated.slice(0, direct.length), direct, 'direct vs facade contracts');
if (process.argv.includes('--baseline-stdin')) {
  const baseline = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.equal(typeof baseline, 'string');
  const oldFacade = (console, opts) => facade(baseline, console, opts);
  oldFacade.integration = true;
  assert.deepEqual(await suite(oldFacade), integrated, 'independent baseline vs candidate');
  console.log('Baseline SHA256: ' + createHash('sha256').update(baseline).digest('hex'));
}
console.log('Slack live API: ' + direct.length + ' contracts + ' +
  (integrated.length - direct.length) + ' facade integration cases passed.');
