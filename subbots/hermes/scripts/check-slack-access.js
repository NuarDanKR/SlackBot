#!/usr/bin/env node
/** Codex R3e: Slack access contracts using fake clients and an in-memory clock.
 * --baseline-stdin accepts a JSON string of independently retained index.js.
 * The real entry point is read as text, never imported or started.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createSlackAccess } from '../src/slack/access.js';

const indexSource = fs.readFileSync(new URL('../src/index.js', import.meta.url), 'utf8');
const configSource = fs.readFileSync(new URL('../src/config.js', import.meta.url), 'utf8');
function pureFunction(name) {
  const found = configSource.match(new RegExp('export function ' + name + '\\([^]*?\\n\\}'));
  assert.ok(found, 'config boundary: ' + name);
  return found[0].replace(/^export /, '');
}
const { normalizeChannel, accessFor, PUBLIC_ACCESS } = compileFunction(
  pureFunction('normalizeChannel') + '\n' + pureFunction('accessFor') +
  '\nreturn {normalizeChannel, accessFor, PUBLIC_ACCESS: accessFor([])};',
)();
function wiring(source) {
  const start = source.indexOf('const memberCache =');
  const end = source.indexOf('/* ── 질문 처리', start);
  assert.ok(start >= 0 && end > start, 'access boundary in index');
  const block = source.slice(start, end);
  return deps => {
    // Preserve the real singleton declaration and verify the real binding.
    const result = compileFunction(block +
      '\nreturn {resolveAccess, memberCache};', Object.keys(deps))(...Object.values(deps));
    return { api: { resolveAccess: result.resolveAccess }, cache: result.memberCache };
  };
}
const direct = deps => {
  const cache = { at: 0, byChannel: new Map() };
  return { api: createSlackAccess({ ...deps, memberCache: cache }), cache };
};
const nowStart = 1700000000000;
const privateNames = ['fixture-a', 'fixture-b'];
function clients(trace, overrides = {}) {
  const pages = {
    root: { channels: [{ id: 'CA', name: 'fixture-a' }, { id: 'CX', name: 'not-listed' }],
      response_metadata: { next_cursor: 'more' } },
    more: { channels: [{ id: 'CB', name: 'fixture-b' }], response_metadata: { next_cursor: '' } },
  };
  const members = {
    CA: { members: ['U1'], response_metadata: { next_cursor: 'members-more' } },
    'CA:members-more': { members: ['U1', 'U2'] },
    CB: { members: ['U2', 'U3'] },
  };
  return { conversations: {
    async list(args) {
      trace.push(['list', structuredClone(args)]);
      return overrides.list ? overrides.list(args) : structuredClone(pages[args.cursor || 'root']);
    },
    async members(args) {
      trace.push(['members', structuredClone(args)]);
      return overrides.members ? overrides.members(args) :
        structuredClone(members[args.channel + (args.cursor ? ':' + args.cursor : '')]);
    },
    async info(args) {
      trace.push(['info', structuredClone(args)]);
      return overrides.info ? overrides.info(args) : { channel: { name: 'public', is_private: false } };
    },
  } };
}
function snapshot(cache) {
  return { at: cache.at, byChannel: [...cache.byChannel].map(([name, ids]) => [name, [...ids]]) };
}
async function run(make, label) {
  let now = nowStart;
  const nativeNow = Date.now;
  Date.now = () => now;
  const results = [];
  function fixture(overrides = {}) {
    const trace = [];
    const config = { privateChannels: [...privateNames] };
    // 개명 흉내 — `fixture-was` 는 이 워크스페이스의 아카이브 이름이고 지금 이름은 `fixture-a`.
    const canonicalChannel = (n) => (normalizeChannel(n) === 'fixture-was' ? 'fixture-a' : normalizeChannel(n));
    const deps = { config, normalizeChannel, canonicalChannel, accessFor, PUBLIC_ACCESS, createSlackAccess,
      console: { warn(...args) { trace.push(['warn', ...args]); } } };
    const { api, cache } = make(deps);
    assert.deepEqual(trace, [], 'no client calls at factory creation');
    const client = clients(trace, overrides);
    return { api, cache, client, trace, config };
  }
  async function ask(f, options = {}) {
    f.trace.length = 0;
    const input = { channelId: 'DM', channelType: 'im', userId: 'U1', ...options };
    const before = structuredClone(input);
    const result = await f.api.resolveAccess(f.client, input);
    assert.deepEqual(input, before, 'request input unchanged, including first call');
    assert.equal(result.access.full, false);
    results.push({ result: { channels: [...result.access.channels], origin: result.origin,
      publicIdentity: result.access === PUBLIC_ACCESS }, trace: structuredClone(f.trace), cache: snapshot(f.cache) });
    return result;
  }
  try {
    {
      const f = fixture();
      const result = await ask(f);
      assert.deepEqual([...result.access.channels], ['fixture-a']);
      assert.equal(result.origin, 'DM (1:1 대화)');
      assert.deepEqual(f.trace.map(t => t[0]), ['list', 'members', 'members', 'list', 'members']);
      assert.deepEqual(f.trace[0][1], {
        types: 'private_channel', exclude_archived: true, limit: 200, cursor: undefined,
      });
      assert.equal(f.trace[2][1].cursor, 'members-more');
      assert.equal(f.trace[3][1].cursor, 'more');
      assert.equal(f.cache.at, nowStart);
      const map = f.cache.byChannel;
      assert.deepEqual([...(await ask(f, { userId: 'U2' })).access.channels], privateNames);
      assert.equal(f.trace.length, 0); assert.equal(f.cache.byChannel, map);
      assert.deepEqual([...(await ask(f, { userId: 'U3' })).access.channels], ['fixture-b']);
      assert.deepEqual([...(await ask(f, { userId: 'stranger' })).access.channels], []);
      now += 599999;
      await ask(f); assert.equal(f.trace.length, 0, 'cache within TTL');
      now += 1;
      await ask(f); assert.equal(f.trace[0][0], 'list', 'refresh at exact TTL');
      assert.notEqual(f.cache.byChannel, map); assert.equal(f.cache.at, now);
      now -= 1000;
      await ask(f); assert.equal(f.trace.length, 0, 'existing backwards-clock policy');
    }
    // A partial or failed refresh must not publish incomplete membership or
    // serve the stale map for that failed request.
    for (const failAt of ['first-list', 'second-list', 'first-members', 'second-members']) {
      now = nowStart;
      const f = fixture();
      await ask(f);
      const oldMap = f.cache.byChannel, oldTime = f.cache.at, oldSnapshot = snapshot(f.cache);
      now += 600000;
      let failed = true;
      f.client = clients(f.trace, {
        list(args) {
          if (failed && ((failAt === 'first-list' && !args.cursor) ||
            (failAt === 'second-list' && args.cursor))) throw { data: { error: 'fixture_list_error' } };
          return args.cursor ? { channels: [{ id: 'CB', name: 'fixture-b' }] } :
            { channels: [{ id: 'CA', name: 'fixture-a' }], response_metadata: { next_cursor: 'more' } };
        },
        members(args) {
          if (failed && ((failAt === 'first-members' && args.channel === 'CA') ||
            (failAt === 'second-members' && args.channel === 'CB'))) throw new Error('fixture_members_error');
          return { members: ['U1'] };
        },
      });
      const blocked = await ask(f);
      assert.deepEqual([...blocked.access.channels], []);
      assert.equal(f.trace.at(-1)[0], 'warn');
      assert.equal(f.cache.byChannel, oldMap); assert.equal(f.cache.at, oldTime);
      assert.deepEqual(snapshot(f.cache), oldSnapshot);
      failed = false;
      const recovered = await ask(f);
      assert.deepEqual([...recovered.access.channels], privateNames);
      assert.equal(f.trace[0][0], 'list', 'failed refresh was not cached');
      assert.notEqual(f.cache.byChannel, oldMap);
    }
    for (const response of [{}, { channels: [] }, { channels: [{ id: 'X', name: 'unknown' }] }]) {
      now = nowStart;
      const f = fixture({ list: () => structuredClone(response) });
      assert.deepEqual([...(await ask(f)).access.channels], []);
      assert.equal(f.trace.length, 1);
      await ask(f); assert.equal(f.trace.length, 0, 'successful empty membership is cached');
    }
    {
      now = nowStart;
      const f = fixture({ list: () => ({ channels: [{ id: 'CA', name: 'fixture-a' }] }),
        members: () => ({}) });
      assert.deepEqual([...(await ask(f)).access.channels], []);
      assert.deepEqual([...f.cache.byChannel.get('fixture-a')], []);
    }
    for (const [info, want, origin] of [
      [{ name: 'public', is_private: false }, [], '#public (공개 채널)'],
      [{ name: 'fixture-a', is_private: true }, ['fixture-a'], '#fixture-a (비공개 채널)'],
      [{ name: '#fixture-b ', is_private: true }, ['fixture-b'], '#fixture-b (비공개 채널)'],
      [{ name: 'unlisted-private', is_private: true }, [], '#unlisted-private (비공개 채널)'],
      // Preserve the configured-name rule; Slack's flag supplies the label.
      [{ name: 'fixture-a', is_private: false }, ['fixture-a'], '#fixture-a (공개 채널)'],
      [{}, [], '# (공개 채널)'],
    ]) {
      const f = fixture({ info: () => ({ channel: structuredClone(info) }) });
      const result = await ask(f, { channelId: 'C', channelType: 'channel' });
      assert.deepEqual([...result.access.channels], want); assert.equal(result.origin, origin);
      assert.deepEqual(f.trace, [['info', { channel: 'C' }]]);
      if (!want.length) assert.equal(result.access, PUBLIC_ACCESS);
      assert.equal(f.cache.at, 0);
    }
    for (const channelType of ['channel', 'mpim', undefined]) {
      const f = fixture({ info: () => { throw new Error('fixture info failure'); } });
      const result = await ask(f, { channelType });
      assert.equal(result.access, PUBLIC_ACCESS); assert.equal(result.origin, '알 수 없는 채널');
      assert.equal(f.trace.length, 1);
    }
    {
      now = nowStart;
      const f = fixture();
      await ask(f);
      f.config.privateChannels = ['fixture-b'];
      // Existing cache remains authoritative until it expires.
      assert.deepEqual([...(await ask(f)).access.channels], ['fixture-a']);
      now += 600000;
      assert.deepEqual([...(await ask(f)).access.channels], []);
      assert.deepEqual([...f.cache.byChannel.keys()], ['fixture-b']);
    }
    {
      now = nowStart;
      const first = fixture(), second = fixture({ members: () => ({ members: ['other'] }) });
      await ask(first); await ask(second);
      assert.notEqual(first.cache.byChannel, second.cache.byChannel);
      assert.deepEqual([...(await ask(first)).access.channels], ['fixture-a']);
      assert.deepEqual([...(await ask(second)).access.channels], []);
    }
    {
      // config 에 옛 이름(개명 전 아카이브 이름)이 적혀 있어도, 지금 이름으로 온 멤버 조회는
      // 같은 채널로 맞아떨어져야 한다.
      now = nowStart;
      const f = fixture();
      f.config.privateChannels = ['fixture-was', 'fixture-b'];
      const result = await ask(f);
      assert.ok(result.access.channels.has('fixture-a'),
        'config 에 옛 이름이 있으면 멤버가 자기 채널을 못 읽는다');
    }
    {
      // config 에 옛 이름이 적혀 있어도, 채널에서 직접 온 질문(비-DM)도 지금 이름으로
      // 맞아떨어져야 한다 — 공개로 떨어지면 안 된다.
      now = nowStart;
      const f = fixture({ info: () => ({ channel: { name: 'fixture-a', is_private: true } }) });
      f.config.privateChannels = ['fixture-was', 'fixture-b'];
      const result = await ask(f, { channelId: 'C', channelType: 'channel' });
      assert.ok(result.access.channels.has('fixture-a') && result.access !== PUBLIC_ACCESS,
        'config 에 옛 이름이 있으면 채널 직접 질문이 공개로 떨어진다');
    }
    console.log('PASS ' + label + ': ' + results.length + ' access/cache scenarios');
    return results;
  } finally { Date.now = nativeNow; }
}
const actual = await run(direct, 'access factory');
assert.deepEqual(actual, await run(wiring(indexSource), 'real entry-point binding'));
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.equal(typeof source, 'string');
  assert.ok(!source.includes('createSlackAccess'), 'baseline must precede extraction');
  assert.deepEqual(actual, await run(wiring(source), 'pre-extraction baseline'));
  console.log('PASS exact access, cache state and client traces; baseline SHA256=' +
    createHash('sha256').update(source).digest('hex'));
}
console.log('PASS disk writes, Slack/model calls and bot starts 0.');
