#!/usr/bin/env node
/**
 * Codex R1d: synthetic prompt/cache contracts, memory-only filesystem.
 * --baseline-stdin compares the pre-extraction prompt block (JSON string).
 * --facade additionally reads real config/templates through compatibility exports.
 * No API calls, Slack, Git operations, disk fixtures or writes.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { compileFunction } from 'node:vm';
import { createPromptContext } from '../src/llm/prompts.js';

const clone = value => JSON.parse(JSON.stringify(value));
const names = 'renderPrompt, lastSyncedAt, briefStampPaths, systemBlocks, privateQuoteLine';
const A = { key: 'a', allowed: ['secret-a'] };
const B = { key: 'b', allowed: ['secret-b'] };
const template = '{{ORG}}|{{OWNER}}|{{EX_A}}|{{EX_B}}|{{EX_C}}|{{EX_Z}}|{{UNKNOWN}}';
const qaTemplate = 'Q {{ARCHIVE_BRIEF}} D {{DOCUMENTS_BRIEF}}';
const stampPaths = ['/data/.git/refs/heads/main', '/data/.git/packed-refs', '/data/slack/index.md', '/data/docs/index.md', '/data/slack/.sync-state.json'];

function baselineFactory(source) {
  const start = source.indexOf('const EXAMPLE_FALLBACK');
  const end = source.indexOf('/* ── 툴 ', start);
  assert.ok(start >= 0 && end > start, 'pre-extraction prompt boundary');
  const body = source.slice(start, end).replace(/^export /gm, '');
  return deps => compileFunction(body + '\nreturn {' + names + '};', Object.keys(deps))(...Object.values(deps));
}

function fixture(factory, options = {}) {
  const trace = [];
  const files = new Map([
    ['/code/src/prompts/qa.md', qaTemplate],
    ['/code/src/prompts/daily.md', template],
    ['/data/slack/.sync-state.json', '{"last_sync":"fixture-time"}'],
  ]);
  const mtimes = new Map(stampPaths.map(p => [p, 1]));
  const config = { privateChannels: ['secret-a', 'secret-b'], promptExamples: {} };
  const state = { documents: true, channels: 1, version: 1, archiveError: false };
  const deps = {
    ROOT: '/code', DATA_ROOT: '/data', ARCHIVE_DIR: '/data/slack',
    DOCS_DIR: options.noDocsDir ? null : '/data/docs', config, path: path.posix,
    fs: {
      readFileSync(p, encoding) {
        trace.push(['read', p, encoding]);
        if (!files.has(p)) throw new Error('missing fixture');
        return files.get(p);
      },
      statSync(p) {
        trace.push(['stat', p]);
        if (!mtimes.has(p)) throw new Error('missing stamp');
        return { mtimeMs: mtimes.get(p) };
      },
    },
    accessLabel(access) { trace.push(['label', access.key]); return access.key; },
    canSeePrivateChannel(access, name) { trace.push(['canSee', name]); return access?.allowed?.includes(name); },
    buildArchiveBriefSplit({ access }) {
      trace.push(['archive', access.key]);
      if (state.archiveError) throw new Error('archive failure');
      return { common: 'archive-' + state.version, extra: 'archive-extra-' + access.key };
    },
    hasDocuments() { trace.push(['hasDocuments']); return state.documents; },
    buildDocumentsBriefSplit({ access }) {
      trace.push(['documents', access.key]);
      return { common: 'docs-' + state.version, extra: 'docs-extra-' + access.key };
    },
    listArchivedChannels() { trace.push(['channels']); return state.channels ? ['fixture'] : []; },
  };
  const ctx = factory(deps);
  assert.deepEqual(trace, [], 'factory creation must not perform I/O or build indexes');
  return { ctx, trace, files, mtimes, config, state, deps };
}
const count = (f, name) => f.trace.filter(row => row[0] === name).length;

const cases = [
  ['lazy creation and stamp path order', f => {
    const paths = f.ctx.briefStampPaths();
    assert.deepEqual(paths, stampPaths); assert.deepEqual(f.trace, []);
    return paths;
  }],
  ['default replacements and unknown placeholders', f => {
    const rendered = f.ctx.renderPrompt('daily');
    assert.equal(rendered, '우리 팀|담당자|사업장가|사업장나|사업장다|{{EX_Z}}|{{UNKNOWN}}');
    assert.deepEqual(f.trace, [['read', '/code/src/prompts/daily.md', 'utf8']]);
    return rendered;
  }],
  ['dynamic settings, whitespace and literal dollar replacement', f => {
    Object.assign(f.config, { org: ' Team ', owner: { label: '<unfilled>', name: ' Name ' },
      promptExamples: { EX_A: ' A ', EX_B: '', EX_C: 42, EX_Z: '$&', invalid: 'discard' } });
    const first = f.ctx.renderPrompt('daily');
    assert.equal(first, 'Team|Name| A |사업장나|사업장다|$&|{{UNKNOWN}}');
    f.config.org = '<unfilled>'; f.config.owner.label = ' Label ';
    f.config.promptExamples = { EX_A: 'NEW' };
    const second = f.ctx.renderPrompt('daily');
    assert.equal(second, '우리 팀|Label|NEW|사업장나|사업장다|{{EX_Z}}|{{UNKNOWN}}');
    assert.equal(count(f, 'read'), 2);
    return [first, second];
  }],
  ['missing template propagates and later read recovers', f => {
    assert.throws(() => f.ctx.renderPrompt('missing'), /missing fixture/);
    f.files.set('/code/src/prompts/missing.md', '{{ORG}}');
    return f.ctx.renderPrompt('missing');
  }],
  ['sync state success, missing, malformed and empty', f => {
    const values = [f.ctx.lastSyncedAt()];
    const p = '/data/slack/.sync-state.json';
    f.files.delete(p); values.push(f.ctx.lastSyncedAt());
    for (const body of ['not json', 'null', '{}', '{"last_sync":""}']) {
      f.files.set(p, body); values.push(f.ctx.lastSyncedAt());
    }
    assert.deepEqual(values, ['fixture-time', ...Array(5).fill('알 수 없음')]);
    return values;
  }],
  ['private names and dynamic allowed-channel list', f => {
    const all = f.ctx.privateQuoteLine({ full: true });
    assert.equal(all, '전부 (이 자리는 본인만 봅니다)'); assert.equal(count(f, 'canSee'), 0);
    const allowed = f.ctx.privateQuoteLine(A);
    assert.match(allowed, /^#secret-a 만/); assert.ok(!allowed.includes('secret-b'));
    const none = f.ctx.privateQuoteLine({ allowed: [] });
    assert.ok(!none.includes('secret-a') && !none.includes('secret-b'));
    f.config.privateChannels = ['secret-c'];
    const changed = f.ctx.privateQuoteLine({ allowed: ['secret-c'] });
    assert.match(changed, /^#secret-c 만/);
    return [all, allowed, none, changed];
  }],
  ['same access cache hit still stats but does not reread', f => {
    const first = f.ctx.systemBlocks(A), second = f.ctx.systemBlocks(A);
    assert.deepEqual(first, { common: 'Q archive-1 D docs-1', extra: 'archive-extra-a\n\n---\n\ndocs-extra-a' });
    assert.deepEqual(second, first);
    assert.equal(count(f, 'stat'), 10); assert.equal(count(f, 'archive'), 1); assert.equal(count(f, 'read'), 1);
    return [first, second];
  }],
  ['access A B A keeps both caches and rebuilds common for B', f => {
    const values = [f.ctx.systemBlocks(A), f.ctx.systemBlocks(B), f.ctx.systemBlocks(A)];
    assert.equal(count(f, 'archive'), 2); assert.equal(count(f, 'documents'), 2); assert.equal(count(f, 'read'), 2);
    assert.deepEqual(values[0], values[2]); assert.match(values[1].extra, /extra-b/);
    assert.ok(!values[1].extra.includes('extra-a'));
    return values;
  }],
  ...stampPaths.map((p, i) => ['stamp change ' + i, f => {
    const first = f.ctx.systemBlocks(A);
    f.mtimes.set(p, 2); f.state.version = 2;
    const second = f.ctx.systemBlocks(A);
    assert.equal(first.common, 'Q archive-1 D docs-1'); assert.equal(second.common, 'Q archive-2 D docs-2');
    assert.equal(count(f, 'archive'), 2);
    return [first, second];
  }]),
  ['missing stamps become zero and later appearance invalidates', f => {
    f.mtimes.clear();
    const first = f.ctx.systemBlocks(A), cached = f.ctx.systemBlocks(A);
    assert.equal(count(f, 'archive'), 1); assert.deepEqual(first, cached);
    f.mtimes.set(stampPaths[0], 5); f.state.version = 2;
    const next = f.ctx.systemBlocks(A);
    assert.equal(next.common, 'Q archive-2 D docs-2'); assert.equal(count(f, 'archive'), 2);
    return [first, cached, next];
  }],
  ['stamp invalidates both access caches', f => {
    f.ctx.systemBlocks(A); f.ctx.systemBlocks(B);
    f.mtimes.set(stampPaths[1], 9); f.state.version = 3;
    const a = f.ctx.systemBlocks(A), b = f.ctx.systemBlocks(B);
    assert.equal(count(f, 'archive'), 4); assert.equal(a.common, 'Q archive-3 D docs-3');
    assert.equal(b.common, a.common);
    return [a, b];
  }],
  ['no documents directory skips null stat', f => {
    f.deps.DOCS_DIR = null;
    const ctx = f.factory(f.deps);
    const paths = ctx.briefStampPaths(), result = ctx.systemBlocks(A);
    assert.deepEqual(paths, [...stampPaths.slice(0, 3), null, stampPaths[4]]);
    assert.equal(count(f, 'stat'), 4);
    return [paths, result];
  }],
  ['empty archive and no documents keep guidance', f => {
    f.state.channels = 0; f.state.documents = false;
    const r = f.ctx.systemBlocks(A);
    assert.match(r.common, /대화 아카이브가 아직 비어 있습니다/);
    assert.match(r.common, /문서 아카이브가 아직 비어 있습니다/);
    assert.equal(r.extra, 'archive-extra-a'); assert.equal(count(f, 'documents'), 0);
    return r;
  }],
  ['reader failure is not cached', f => {
    f.state.archiveError = true;
    assert.throws(() => f.ctx.systemBlocks(A), /archive failure/);
    f.state.archiveError = false;
    const r = f.ctx.systemBlocks(A);
    assert.equal(count(f, 'archive'), 2); assert.equal(count(f, 'read'), 1);
    return r;
  }],
  ['prompt read failure is not cached', f => {
    f.files.delete('/code/src/prompts/qa.md');
    assert.throws(() => f.ctx.systemBlocks(A), /missing fixture/);
    f.files.set('/code/src/prompts/qa.md', qaTemplate);
    const r = f.ctx.systemBlocks(A);
    assert.equal(count(f, 'archive'), 2); assert.equal(count(f, 'read'), 2);
    return r;
  }],
  ['template changes keep old cache until stamp changes', f => {
    const first = f.ctx.systemBlocks(A);
    f.files.set('/code/src/prompts/qa.md', 'changed {{ARCHIVE_BRIEF}}');
    assert.deepEqual(f.ctx.systemBlocks(A), first);
    f.mtimes.set(stampPaths[0], 2);
    const next = f.ctx.systemBlocks(A);
    assert.equal(next.common, 'changed archive-1');
    return [first, next];
  }],
  ['factory isolation', f => {
    const a = f.ctx.systemBlocks(A);
    f.state.version = 2;
    const second = f.factory(f.deps), b = second.systemBlocks(A);
    assert.equal(a.common, 'Q archive-1 D docs-1'); assert.equal(b.common, 'Q archive-2 D docs-2');
    assert.deepEqual(f.ctx.systemBlocks(A), a);
    return [a, b];
  }],
];

function run(factory, fn) {
  const f = fixture(factory); f.factory = factory;
  return { result: clone(fn(f)), trace: f.trace };
}
const savedFetch = globalThis.fetch;
globalThis.fetch = async () => { throw new Error('network forbidden'); };
try {
  let baseline;
  if (process.argv.includes('--baseline-stdin')) {
    const source = JSON.parse(fs.readFileSync(0, 'utf8'));
    assert.equal(typeof source, 'string');
    baseline = baselineFactory(source);
    console.log('Baseline normalized block SHA-256: ' + createHash('sha256').update(source).digest('hex'));
  }
  for (const [name, fn] of cases) {
    const actual = run(createPromptContext, fn);
    if (baseline) assert.deepEqual(actual, run(baseline, fn), name + ': pre-extraction output/I-O trace');
    console.log('  ✓ ' + name);
  }
  if (process.argv.includes('--facade')) {
    const facade = await import('../src/claude.js');
    const c = await import('../src/config.js');
    assert.deepEqual(facade.briefStampPaths(), [
      path.join(c.DATA_ROOT, '.git', 'refs', 'heads', 'main'),
      path.join(c.DATA_ROOT, '.git', 'packed-refs'),
      path.join(c.ARCHIVE_DIR, 'index.md'),
      c.DOCS_DIR ? path.join(c.DOCS_DIR, 'index.md') : null,
      path.join(c.ARCHIVE_DIR, '.sync-state.json'),
    ]);
    for (const name of ['qa', 'daily', 'weekly', 'summary-check']) {
      const rendered = facade.renderPrompt(name);
      assert.ok(rendered.length); assert.doesNotMatch(rendered, /\{\{(?:EX_[A-C]|ORG|OWNER)\}\}/);
    }
    const before = facade.lastSyncedAt();
    let expected;
    try { expected = JSON.parse(fs.readFileSync(path.join(c.ARCHIVE_DIR, '.sync-state.json'), 'utf8')).last_sync || '알 수 없음'; }
    catch { expected = '알 수 없음'; }
    assert.equal(before, expected);
    console.log('  ✓ real compatibility exports: prompt rendering, sync state, stamp paths (read only)');
  }
  console.log(cases.length + ' prompt/cache contracts passed; live model calls 0; disk writes 0.');
} finally {
  globalThis.fetch = savedFetch;
}
