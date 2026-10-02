#!/usr/bin/env node
/** Codex R3c: memory-only rendering contracts.
 * --baseline-stdin accepts a JSON string of the independently retained full
 * pre-extraction facade. --facade also tests actual ESM wiring with fake I/O.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createLogStore } from '../src/convo-log/store.js';
import { createLogRenderer } from '../src/convo-log/render.js';
import { createLogCodec } from '../src/convo-log/codec.js';
import { createLogStats } from '../src/convo-log/stats.js';

const facadeURL = new URL('../src/convo-log.js', import.meta.url);
const facadeSource = fs.readFileSync(facadeURL, 'utf8');
const rawDir = path.resolve('/fixture/raw');
const renderedDir = path.resolve('/fixture/rendered');
const evaluate = (source, deps) => compileFunction(
  source.replace(/^import[\s\S]*?;\r?\n/gm, '').replace(/^export /gm, '') +
  '\nreturn {renderEntry, renderMonth, renderAll, sourceGone, append, KIND_LABEL, BROADCAST};',
  Object.keys(deps),
)(...Object.values(deps));
const at = '2026-01-01T15:00:00Z';
const entries = [
  { at, kind: 'qa', asker: 'A', origin: '#one', question: 'question', answer: 'answer\n\n\ncode',
    channels: ['one', '📄 doc'], usage: { input_tokens: 1000, output_tokens: 3 },
    costUsd: 1, model: 'fixture', elapsedMs: 0, privateAccess: 'public',
    toolCalls: [{ name: 'search', input: { query: 'a' }, chars: 0 }], cacheBaseline: false },
  { at, kind: 'daily', ok: false, target: '#target', origin: 'period', error: 'first\nsecond',
    context: 'generated', accounting: { complete: false, knownCostUsd: 2, unknownAttempts: 1 },
    attempts: [{ errorType: 'overload', waitMs: 60000 }, {}], model: 'fixture' },
  { at: '2026-01-01T14:59:00Z', kind: 'qa', asker: 'B', ok: false, error: 'single',
    costUsd: 0, allowPrivate: false, refused: true, truncated: true, toolLimit: true,
    threadContext: 2, requestId: 'fake-request', permalink: 'https://example.invalid/log',
    cacheMiss: [{ type: 'system_changed', count: 1, tokens: 1234 }] },
  { at: '2026-01-03T00:00:00Z', kind: 'ingest', accountingOnly: true, ok: false,
    accounting: { complete: false, knownCostUsd: 3, unknownAttempts: 2 } },
  { at: '2026-01-01T14:58:00Z', kind: 'empty', answer: 'empty', stats: 'stats' },
  { at: '2026-01-01T14:57:00Z', kind: 'qa', usage: { input_tokens: 1 }, cacheBaseline: true },
];
function memory(initial = {}) {
  const files = new Map(Object.entries(initial));
  const trace = [];
  let failRename = false;
  const dirExists = p => [...files.keys()].some(f => path.dirname(f) === p);
  const io = {
    existsSync(p) { trace.push(['exists', p]); return files.has(p) || dirExists(p); },
    readFileSync(p, encoding) {
      trace.push(['read', p, encoding]); assert.ok(files.has(p), 'unexpected read: ' + p);
      return files.get(p);
    },
    readdirSync(p) {
      trace.push(['list', p]);
      return [...files.keys()].filter(f => path.dirname(f) === p).map(f => path.basename(f));
    },
    mkdirSync(p, opts) { trace.push(['mkdir', p, opts]); },
    writeFileSync(p, text, encoding) { trace.push(['write', p, text, encoding]); files.set(p, text); },
    renameSync(from, to) {
      trace.push(['rename', from, to]);
      if (failRename) throw new Error('synthetic rename failure');
      files.set(to, files.get(from)); files.delete(from);
    },
    appendFileSync(p, text, encoding) {
      trace.push(['append', p, text, encoding]); files.set(p, (files.get(p) || '') + text);
    },
  };
  return { files, trace, io, failRename() { failRename = true; } };
}
const writes = trace => trace.filter(t => ['mkdir', 'write', 'rename', 'append'].includes(t[0]));
const rawFile = path.join(rawDir, 'qa-2026-01.jsonl');
const mdFile = path.join(renderedDir, '2026-01.md');
const indexFile = path.join(renderedDir, 'index.md');
const jsonl = entries.map(e => JSON.stringify(e)).join('\n') + '\n{broken\n\n';
const compiled = source => (m, enabled = true) => evaluate(source, {
  createLogRenderer, createLogCodec, createLogStats, createLogStore, fs: m.io, path,
  config: { timezone: 'Asia/Seoul', workspace: 'fixture' },
  LOG_ENABLED: enabled, LOG_DIR: renderedDir, LOG_RAW_DIR: rawDir,
  console: { error(...args) { m.trace.push(['error', ...args]); } },
});

function run(make, label) {
  const records = [];
  const capture = (m, fn) => {
    m.trace.length = 0;
    const result = fn();
    records.push({ result: structuredClone(result), trace: structuredClone(m.trace),
      files: [...m.files] });
    return result;
  };
  const m = memory({ [rawFile]: jsonl, [path.join(rawDir, 'ignore.txt')]: 'ignored' });
  const api = make(m);
  assert.equal(writes(m.trace).length, 0, 'initialization has no writes');
  for (const entry of entries) {
    const before = structuredClone(entry);
    const text = capture(m, () => api.renderEntry(entry));
    assert.deepEqual(entry, before, 'entry is not mutated, including first invocation');
    assert.equal(api.renderEntry(entry), text);
  }
  assert.match(api.renderEntry(entries[0]), /### 00:00/);
  assert.ok(api.renderEntry(entries[0]).includes('answer\n\n\ncode'));
  assert.ok(!/^### /m.test(api.renderEntry(entries[3])));
  const before = [...m.files];
  const preview = capture(m, () => api.renderAll({ write: false }));
  assert.deepEqual([...m.files], before, 'preview preserves files');
  assert.equal(writes(m.trace).length, 0);
  assert.equal(preview.qa, 4); assert.equal(preview.broadcast, 1); assert.equal(preview.broken, 1);
  const month = preview.months[0];
  assert.equal(month.failed, 2); assert.equal(month.usd, 6); assert.equal(month.unknownCost, 3);
  assert.ok(month.text.indexOf('question') < month.text.indexOf('first\nsecond'), 'stable ties');
  assert.match(month.text, /문답 4건\*\* \(실패 1건\)/);
  assert.match(month.text, /정기 발송 1건\*\* \(실패 1건\)/);
  assert.match(month.text, /읽지 못한 줄 1개/);
  assert.match(month.text, /## 2026-01-02/);
  const saved = capture(m, () => api.renderAll());
  assert.equal(saved.changed, 2);
  assert.deepEqual(writes(m.trace).map(t => t[0]), ['mkdir', 'write', 'rename', 'mkdir', 'write', 'rename']);
  assert.equal(m.files.get(mdFile), month.text);
  const again = capture(m, () => api.renderAll());
  assert.equal(again.changed, 0); assert.equal(writes(m.trace).length, 0);
  assert.equal(again.index.text, saved.index.text);
  for (const initial of [
    { [mdFile]: 'preserve month', [indexFile]: 'preserve index' },
    { [path.join(rawDir, 'ignore.txt')]: 'not a month', [mdFile]: 'preserve month' },
  ]) {
    const missing = memory(initial), guarded = make(missing);
    const result = capture(missing, () => guarded.renderAll());
    assert.equal(result.noSource, true); assert.equal(result.changed, 0);
    assert.equal(writes(missing.trace).length, 0);
    assert.deepEqual([...missing.files], Object.entries(initial));
  }
  const empty = memory(), first = make(empty);
  const firstResult = capture(empty, () => first.renderAll({ write: false }));
  assert.equal(firstResult.noSource, undefined); assert.equal(firstResult.qa, 0);
  assert.equal(writes(empty.trace).length, 0);
  const absentMonth = capture(empty, () => first.renderMonth('2026-02', { write: false }));
  assert.equal(absentMonth.qa, 0); assert.match(absentMonth.text, /기록이 없습니다/);
  const failure = memory({ [rawFile]: jsonl, [mdFile]: 'old month', [indexFile]: 'old index' });
  const failing = make(failure); failure.failRename();
  assert.throws(() => failing.renderAll(), /synthetic rename failure/);
  assert.equal(failure.files.get(mdFile), 'old month');
  assert.equal(failure.files.get(indexFile), 'old index', 'index not written after month failure');
  records.push({ trace: failure.trace, files: [...failure.files] });
  console.log('PASS ' + label + ': entry/month/index outputs, preview, idempotency, guards and failure order');
  return records;
}

const actual = run(compiled(facadeSource), 'facade contracts');
const disabled = memory();
assert.equal(compiled(facadeSource)(disabled, false).renderAll(), null);
assert.deepEqual(disabled.trace, [], 'disabled logging does not touch storage');

// Factory creation must not call injected boundaries or read operational config.
const reject = () => { throw new Error('unexpected boundary call'); };
const isolated = createLogRenderer({
  localParts: reject, BROADCAST: new Set(), KIND_LABEL: {}, num: reject, toolLine: reject,
  readRaw: reject, path: { join: reject }, LOG_DIR: '/other', writeIfChanged: reject,
  months: reject, cacheStats: reject, failureSectionLines: reject, NO_BASELINE: 'other',
});
assert.deepEqual(Object.keys(isolated).sort(), ['renderEntry', 'renderIndex', 'renderMonth']);
assert.match(isolated.renderEntry({ accountingOnly: true, at: 'fixture' }), /계측 금액/);
const labels = { qa: 'initial' }, broadcast = new Set();
const deps = { localParts: () => ({ time: '12:34' }), BROADCAST: broadcast, KIND_LABEL: labels,
  num: () => '0', toolLine: reject };
const live = createLogRenderer(deps);
labels.qa = 'changed'; broadcast.add('qa');
assert.match(live.renderEntry({ kind: 'qa', origin: 'period', answer: 'text' }), /changed/);
assert.match(live.renderEntry({ kind: 'qa', answer: 'text' }), /보낸 내용/);
const other = createLogRenderer({ ...deps, KIND_LABEL: { qa: 'separate' }, BROADCAST: new Set() });
assert.match(other.renderEntry({ kind: 'qa', answer: 'text' }), /답변/);
assert.match(live.renderEntry({ kind: 'qa' }), /changed/);

if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.equal(typeof source, 'string');
  assert.ok(!source.includes('createLogStore'), 'baseline must precede storage extraction');
  assert.deepEqual(actual, run(compiled(source), 'pre-extraction baseline'));
  console.log('PASS exact return values, bytes and I/O traces; baseline SHA256=' +
    createHash('sha256').update(source).digest('hex'));
}
if (process.argv.includes('--facade')) {
  // The default contracts support Node >=20. Synchronous loader hooks are
  // optional and only available in Node 22.15+/23.5+ and later releases.
  const { registerHooks } = await import('node:module');
  assert.equal(typeof registerHooks, 'function',
    '--facade requires Node with module.registerHooks (22.15+/23.5+); run without --facade for the default contracts');
  // Only this facade's config/fs imports are replaced. Production module and
  // its codec/stats/renderer dependencies are loaded by the real ESM loader.
  const key = Symbol.for('hermes.r3c.fixture');
  assert.equal(globalThis[key], undefined);
  const bridge = { io: null };
  globalThis[key] = bridge;
  const hook = registerHooks({
    resolve(specifier, context, next) {
      if (context.parentURL?.startsWith(facadeURL.href)) {
        let source;
        if (specifier === 'node:fs') source =
          'export default new Proxy({}, {get: (_, k) => (...args) => globalThis[Symbol.for("hermes.r3c.fixture")].io[k](...args)});';
        if (specifier === './config.js') source =
          'export const config = {timezone:"Asia/Seoul",workspace:"fixture"};' +
          'export const LOG_ENABLED=true, LOG_DIR=' + JSON.stringify(renderedDir) +
          ', LOG_RAW_DIR=' + JSON.stringify(rawDir) + ';';
        if (source) return { url: 'data:text/javascript,' + encodeURIComponent(source), shortCircuit: true };
      }
      return next(specifier, context);
    },
  });
  try {
    const api = await import(facadeURL.href + '?r3c-fixture');
    assert.deepEqual(actual, run(m => { bridge.io = m.io; return api; }, 'actual ESM facade'));
  } finally { hook.deregister(); delete globalThis[key]; }
}
console.log('PASS factory isolation and policy references; disk writes/model/Slack calls 0.');
