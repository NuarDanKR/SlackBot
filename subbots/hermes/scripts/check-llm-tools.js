#!/usr/bin/env node
/**
 * Codex R1e: memory-only tool contracts with real SDK betaTool conversion.
 * All readers and Slack boundaries are fake. No files, API or Git writes.
 */
import assert from 'node:assert/strict';
import { createToolBuilder } from '../src/llm/tools.js';

const clone = v => JSON.parse(JSON.stringify(v));
const ACCESS = { allowed: ['public'] };
const empty = () => ({ hits: [], outside: [] });
const ahit = (channel = 'public') => ({ channel, text: 'archive text', score: 1, termCount: 2 });
const dhit = () => ({ project: 'project', document: 'doc', title: 'Title', text: 'document text' });
function fixture(factory) {
  const trace = [], touched = new Set(), sizes = [], narrows = [];
  const config = { limits: {} };
  const state = {
    docs: true, archive: empty(), documents: empty(), hidden: false,
    channels: [{ name: 'public', isPrivate: false }],
    entries: [{ channel: 'public', text: 'fresh' }],
    channelResult: { channel: 'public', text: 'channel text' },
    documentResult: { project: 'project', document: 'doc', title: 'Title', text: 'body' },
  };
  const log = (name, args) => trace.push([name, clone(args)]);
  const deps = {
    config, BLOCKED_NOTE: 'BLOCKED', DEFAULT_LIVE_FETCH_MAX_DAYS: 14,
    canSee: (a, c) => a.allowed.includes(c),
    canSeePrivateChannel: (a, c) => a.allowed.includes(c),
    isPrivateChannel: c => c.startsWith('secret'),
    matchesHiddenPrivate: (c, a) => { log('hidden', { c, a }); return state.hidden; },
    truncMarker: msg => 'TRUNCATED ' + msg,
    listReadableChannels: () => ['public', 'secret'],
    /* `tools.js` 가 받는 이름 그대로여야 한다. 근본 수정(2026-09-16)이 `listProjects` 를
     * `narrowableProjects` 로 바꿨는데 여기가 옛 이름이라 `autoNarrow` 가 없는 함수를
     * 불러 **이 검사 전체가 2번 항목에서 죽어 있었다** — 뒤 항목은 아예 안 돌았다. */
    narrowableProjects: a => { log('projects', a); return ['project']; },
    resolveChannel: c => ({ ok: true, name: c.replace(/^#/, '') }),
    resolveProject: p => ({ ok: true, name: p }),
    detectPlace: (q, candidates) => { log('detect', { q, candidates }); return q === 'narrow' ? candidates[0] : null; },
    hasDocuments: () => state.docs,
    searchArchive: args => {
      log('searchArchive', args);
      if (state.searchError) throw state.searchError;
      return clone(state.archiveFn ? state.archiveFn(args) : state.archive);
    },
    searchDocuments: args => { log('searchDocuments', args); return clone(state.documentFn ? state.documentFn(args) : state.documents); },
    readChannel: args => { log('readChannel', args); if (state.readError) throw state.readError; return clone(state.channelResult); },
    readDocument: args => { log('readDocument', args); return clone(state.documentResult); },
    markArchivedAttachments: (text, access) => { log('mark', { text, access }); return text + ' [marked]'; },
    recentWindow: days => ({ days, oldest: '100', latest: '200' }),
    listBotChannels: async client => { log('listBotChannels', client); return clone(state.channels); },
    fetchWindow: async (client, args) => {
      log('fetchWindow', { client, args });
      if (state.fetchError) throw state.fetchError;
      if (state.gate) await state.gate;
      return clone(state.entries);
    },
    formatTranscript: entries => { log('formatTranscript', entries); return 'fresh text'; },
  };
  const ctx = factory(deps);
  assert.deepEqual(trace, [], 'factory creation has no reader/Slack calls');
  const build = (opts = {}) => ctx.buildTools({ access: ACCESS, touched, sizes, narrows, ...opts });
  const run = (name, input, opts) => build(opts).find(t => t.name === name).run(input);
  return { ctx, deps, config, state, trace, touched, sizes, narrows, build, run };
}
const calls = (f, name) => f.trace.filter(t => t[0] === name).map(t => t[1]);
const cases = [
  ['schema order and conditional tools', async f => {
    const plain = f.build();
    assert.deepEqual(plain.map(t => t.name), ['search', 'read_channel', 'read_document']);
    const full = f.build({ slackClient: { fake: true } });
    assert.deepEqual(full.map(t => t.name), ['search', 'read_channel', 'read_document', 'fetch_recent_slack']);
    assert.deepEqual(full[0].input_schema.required, ['query']);
    assert.ok(full[2].input_schema.properties.section);
    assert.match(full[3].input_schema.properties.days.description, /1~14/);
    f.config.limits.liveFetchMaxDays = 9;
    assert.match(f.build({ slackClient: {} })[3].input_schema.properties.days.description, /1~9/);
    f.state.docs = false;
    assert.deepEqual(f.build().map(t => t.name), ['search', 'read_channel']);
    return [clone(plain), clone(full)];
  }],
  ['auto narrow only sees allowed candidates and supports dynamic off', async f => {
    const on = f.ctx.autoNarrow('narrow', ACCESS);
    assert.deepEqual(on, { channel: 'public', project: 'project' });
    assert.deepEqual(calls(f, 'detect')[0].candidates, ['public']);
    /* 문서 축 후보를 내주는 쪽에 **access 가 실제로 닿는가** (존재 확인 오라클, 2026-09-16).
     * 되돌아가는 모양마다 빨개지는 자리가 다르다 — 옛 이름(`listProjects`)으로 되돌리면 위
     * `autoNarrow` 호출이 TypeError 로 먼저 죽고, 이름은 두고 인자만 빼면 `clone(undefined)`
     * 가 먼저 던진다. 이 단언이 직접 잡는 것은 **엉뚱한 값을 넘긴** 경우다. 셋 다 빨개진다.
     * **거르기가 맞는지까지는 여기서 못 본다** — 여기 `narrowableProjects` 는 합성이라 늘
     * 같은 것을 낸다. 실제로 걸러지는지는 실물로 재는 check-auto-narrow.js 의 `[6/9]` 다. */
    assert.deepEqual(calls(f, 'projects')[0], ACCESS, 'access must reach the document candidate source');
    f.config.limits.autoNarrow = false;
    const count = f.trace.length, off = f.ctx.autoNarrow('narrow', ACCESS);
    assert.deepEqual(off, { channel: null, project: null }); assert.equal(f.trace.length, count);
    return [on, off];
  }],
  ['combined results, partial, outside, cards and attachment marking', async f => {
    f.state.archive = { hits: [ahit()], outside: [ahit('outside')], partial: true };
    f.state.documents = { hits: [dhit()], outside: [],
      cards: [{ project: 'company', title: 'Card', date: '', chars: 1000, sections: 2 },
        { project: 'company', title: 'Flat', date: '2026-01-01', chars: 3000, sections: 0 }] };
    const out = await f.run('search', { query: 'fixture', where: 'project' });
    for (const text of ['일부만 맞은 결과', '[marked]', '다른 사업장', '전사 종합 문서', '절 목차', '통째로']) assert.ok(out.includes(text));
    assert.deepEqual([...f.touched], ['public', 'outside', '📄 project/doc']);
    assert.equal(f.sizes[0].chars, out.length); assert.equal(f.narrows.length, 1);
    return out;
  }],
  ['auto fallback keeps old request flow', async f => {
    f.state.archiveFn = args => args.channel ? empty() : { hits: [ahit()] };
    f.state.documentFn = args => args.project ? empty() : { hits: [dhit()] };
    const out = await f.run('search', { query: 'narrow' });
    assert.equal(calls(f, 'searchArchive').length, 2); assert.equal(calls(f, 'searchDocuments').length, 2);
    assert.deepEqual(f.narrows, [{ widenedFrom: '대화·문서' }]);
    assert.match(out, /좁히지 않고 다시 찾았습니다/);
    return out;
  }],
  ['outside hit prevents auto fallback', async f => {
    f.state.archive = { hits: [], outside: [ahit()] };
    const out = await f.run('search', { query: 'narrow', only: 'archive' });
    assert.equal(calls(f, 'searchArchive').length, 1);
    assert.match(out, /다른 사업장/);
    return out;
  }],
  ['explicit scope and document-only combinations', async f => {
    const a = await f.run('search', { query: 'narrow', where: 'project', document: 'doc' });
    assert.equal(calls(f, 'searchArchive').length, 0); assert.equal(calls(f, 'detect').length, 0);
    const b = await f.run('search', { query: 'narrow', document: 'doc' });
    assert.equal(calls(f, 'searchArchive').length, 1); assert.equal(calls(f, 'detect').length, 0);
    const c = await f.run('search', { query: 'q', where: 'project', document: 'doc', only: 'archive' });
    assert.match(c, /아무 것도 검색하지 않았습니다/);
    return [a, b, c];
  }],
  ['no documents produces not-searched guidance', async f => {
    f.state.docs = false;
    const out = await f.run('search', { query: 'q', only: 'documents' });
    assert.match(out, /아카이브에 없다는 뜻이 아닙니다/); return out;
  }],
  ['candidate noise reduced but blocked note retained', async f => {
    f.state.archive = { hits: [], note: '없음 후보: candidate' };
    f.state.documents = { hits: [dhit()] };
    const a = await f.run('search', { query: 'q', where: 'project' });
    assert.ok(!a.includes('candidate'));
    f.state.archive.note = 'BLOCKED';
    const b = await f.run('search', { query: 'q', where: 'project' });
    assert.ok(b.includes('BLOCKED')); return [a, b];
  }],
  ['channel read month, marker and truncation', async f => {
    f.state.channelResult.month = '2026-01'; f.state.channelResult.truncated = true;
    const out = await f.run('read_channel', { channel: 'public', month: '2026-01' });
    assert.match(out, /\[marked\]/); assert.match(out, /TRUNCATED/);
    assert.equal(calls(f, 'readChannel')[0].month, '2026-01');
    return out;
  }],
  ['document section sheet month and hint forwarding', async f => {
    f.state.documentResult = { ...f.state.documentResult, month: '2026-01', section: '12', hint: 'fixture hint' };
    const args = { project: 'project', document: 'doc', month: '2026-01', section: '12', sheet: '2' };
    const out = await f.run('read_document', args);
    assert.deepEqual(calls(f, 'readDocument')[0], { ...args, access: ACCESS });
    assert.match(out, /2026-01 \/ 12/); assert.match(out, /fixture hint/);
    return out;
  }],
  /* `week` 가 스키마에 있고 **reader 까지 살아서 닿나** (2026-09-20).
   * 이 한 단어가 `run()` 에서 빠지면 봇이 주를 골라 불러도 reader 는 못 받아 **주 목차를
   * 다시 준다** — 같은 왕복을 무한히 돈다. 그런데 `read.js` 쪽 시험(check-doc-week.js)은
   * 팩토리를 직접 부르므로 이 배선을 한 글자도 안 잰다(회의적 검증 2026-09-20). */
  ['document week reaches the reader and shows in the header', async f => {
    assert.ok(f.build().find(t => t.name === 'read_document').input_schema.properties.week);
    f.state.documentResult = { ...f.state.documentResult, month: '2026-01', week: '2026-01-05~01-11' };
    const args = { project: 'project', document: 'doc', month: '2026-01', week: '2026-01-05' };
    const out = await f.run('read_document', args);
    assert.deepEqual(calls(f, 'readDocument')[0], { ...args, access: ACCESS });
    assert.match(out, /2026-01 \/ 2026-01-05~01-11/);
    return out;
  }],
  ['reader error returned without adding evidence', async f => {
    f.state.channelResult = { error: 'BLOCKED' }; f.state.documentResult = { error: 'BLOCKED' };
    const out = [await f.run('read_channel', { channel: 'secret' }), await f.run('read_document', { project: 'p', document: 'd' })];
    assert.deepEqual(out, ['BLOCKED', 'BLOCKED']); assert.equal(f.touched.size, 0); return out;
  }],
  ['thrown search records one narrow slot but no size', async f => {
    const err = new Error('search failed'); f.state.searchError = err;
    await assert.rejects(f.run('search', { query: 'q' }), e => e === err);
    assert.deepEqual(f.narrows, [{}]); assert.deepEqual(f.sizes, []); return 'same error';
  }],
  ['omitted sizes and narrows are supported', async f => {
    const out = await f.run('search', { query: 'q' }, { sizes: undefined, narrows: undefined });
    assert.equal(f.sizes.length, 0); assert.equal(f.narrows.length, 0); return out;
  }],
  ['hidden private channel blocks before Slack listing', async f => {
    f.state.hidden = true;
    const out = await f.run('fetch_recent_slack', { days: 2, channel: 'secret' }, { slackClient: {} });
    assert.equal(out, 'BLOCKED'); assert.equal(calls(f, 'listBotChannels').length, 0);
    assert.equal(calls(f, 'fetchWindow').length, 0); return out;
  }],
  ['listed private channel blocks before transcript fetch', async f => {
    f.state.channels = [{ name: 'secret', isPrivate: true }];
    const out = await f.run('fetch_recent_slack', { days: 2, channel: 'secret' }, { slackClient: {} });
    assert.equal(out, 'BLOCKED'); assert.equal(calls(f, 'fetchWindow').length, 0); return out;
  }],
  ['missing Slack channel and empty fetch', async f => {
    const a = await f.run('fetch_recent_slack', { days: 2, channel: 'missing' }, { slackClient: {} });
    assert.match(a, /invite/); assert.equal(calls(f, 'fetchWindow').length, 0);
    f.state.entries = [];
    const b = await f.run('fetch_recent_slack', { days: 2 }, { slackClient: {} });
    assert.equal(b, '최근 2일간 새 대화가 없습니다.'); return [a, b];
  }],
  ['live fetch preserves permission and lookback', async f => {
    const a = await f.run('fetch_recent_slack', { days: 2, channel: 'public' }, { slackClient: { fake: true } });
    assert.equal(calls(f, 'fetchWindow')[0].args.threadLookbackDays, 7);
    assert.deepEqual(calls(f, 'fetchWindow')[0].args.access, ACCESS);
    f.config.limits.qaThreadLookbackDays = 4;
    const b = await f.run('fetch_recent_slack', { days: 3 }, { slackClient: {} });
    assert.equal(calls(f, 'fetchWindow')[1].args.threadLookbackDays, 4);
    assert.deepEqual([...f.touched], ['public']); return [a, b];
  }],
  ['parallel completion and per-build record isolation', async f => {
    let release; f.state.gate = new Promise(r => { release = r; });
    const tools = f.build({ slackClient: {} });
    const slow = tools.find(t => t.name === 'fetch_recent_slack').run({ days: 2 });
    const fast = await tools.find(t => t.name === 'read_channel').run({ channel: 'public' });
    assert.deepEqual(f.sizes.map(s => s.name), ['read_channel']);
    release(); const later = await slow;
    assert.deepEqual(f.sizes.map(s => s.name), ['read_channel', 'fetch_recent_slack']);
    const other = { touched: new Set(), sizes: [], narrows: [] };
    const separate = f.build(other);
    assert.notEqual(separate[0], tools[0]);
    await separate[0].run({ query: 'q' });
    assert.equal(other.narrows.length, 1); assert.equal(f.narrows.length, 0);
    return [fast, later, other.sizes, [...other.touched]];
  }],
];
async function run(factory, fn) {
  const f = fixture(factory);
  const result = await fn(f);
  return clone({ result, trace: f.trace, touched: [...f.touched], sizes: f.sizes, narrows: f.narrows });
}
const saved = globalThis.fetch;
globalThis.fetch = async () => { throw new Error('network forbidden'); };
try {
  for (const [name, fn] of cases) {
    await run(createToolBuilder, fn);
    console.log('  ✓ ' + name);
  }
  console.log(cases.length + ' tool contracts passed; live API calls 0; disk writes 0.');
} finally { globalThis.fetch = saved; }
