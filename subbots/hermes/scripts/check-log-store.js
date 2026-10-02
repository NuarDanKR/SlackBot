#!/usr/bin/env node
/** Codex R3d: memory-only storage contracts.
 * --baseline-stdin accepts a JSON string containing the independently retained
 * full facade before storage extraction. No disk fixtures or external calls.
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

const facade = fs.readFileSync(new URL('../src/convo-log.js', import.meta.url), 'utf8');
const rawDir = path.resolve('/fixture/raw');
const logDir = path.resolve('/fixture/rendered');
const rawFile = path.join(rawDir, 'qa-2026-02.jsonl');
const mdFile = path.join(logDir, '2026-02.md');
const at = '2026-01-31T15:00:00.000Z';
const fixedNow = '2026-02-01T00:00:00.000Z';
const apiNames = 'append, readRaw, months, renderedMonths, writeIfChanged';
function fromFacade(source) {
  return deps => compileFunction(
    source.replace(/^import[\s\S]*?;\r?\n/gm, '').replace(/^export /gm, '') +
      '\nreturn {' + apiNames + '};',
    Object.keys(deps),
  )(...Object.values(deps));
}
function memory(initial = {}, dirs = []) {
  const files = new Map(Object.entries(initial)), directories = new Set(dirs), trace = [];
  let fault = null;
  function event(name, ...args) {
    trace.push([name, ...args]);
    if (fault?.name === name) throw fault.error;
  }
  const io = {
    existsSync(p) {
      event('exists', p);
      return files.has(p) || directories.has(p) || [...files.keys()].some(f => path.dirname(f) === p);
    },
    readFileSync(p, encoding) {
      event('read', p, encoding);
      assert.ok(files.has(p), 'unexpected fixture read'); return files.get(p);
    },
    readdirSync(p) {
      event('list', p);
      return [...files.keys(), ...directories].filter(f => path.dirname(f) === p).map(f => path.basename(f));
    },
    mkdirSync(p, opts) { event('mkdir', p, opts); directories.add(p); },
    appendFileSync(p, text, encoding) {
      event('append', p, text, encoding); files.set(p, (files.get(p) || '') + text);
    },
    writeFileSync(p, text, encoding) { event('write', p, text, encoding); files.set(p, text); },
    renameSync(from, to) {
      event('rename', from, to);
      assert.ok(files.has(from)); files.set(to, files.get(from)); files.delete(from);
    },
  };
  return { files, directories, trace, io,
    fault(name) { const error = new Error('synthetic ' + name); fault = { name, error }; return error; },
  };
}
function localParts(at) {
  const s = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Seoul',
    year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(at));
  return { ym: s.slice(0, 7) };
}
function run(factory, label) {
  const results = [];
  function instance(initial = {}, enabled = true, dirs = []) {
    const m = memory(initial, dirs);
    const deps = {
      fs: m.io, path, LOG_ENABLED: enabled, LOG_DIR: logDir, LOG_RAW_DIR: rawDir, localParts,
      console: { error(...args) { m.trace.push(['error', ...args]); } },
      // The full facade defines its own date helper and constructs sibling
      // factories. They use the same policy/timezone as the isolated store.
      config: { timezone: 'Asia/Seoul' }, createLogStore, createLogRenderer, createLogCodec, createLogStats,
    };
    const api = factory(deps);
    assert.deepEqual(m.trace, [], 'factory initialization has no I/O');
    return { m, api };
  }
  function record(m, call) {
    m.trace.length = 0;
    const value = call(), error = undefined;
    results.push({ value: structuredClone(value), error, trace: structuredClone(m.trace),
      files: [...m.files], dirs: [...m.directories] });
    return { value, error };
  }
  // Disabled logging must return before reading even a malformed entry.
  {
    const { m, api } = instance({}, false);
    assert.equal(record(m, () => api.append(new Proxy({}, {
      get() { throw new Error('disabled entry accessed'); },
    }))).error, undefined);
    assert.deepEqual(m.trace, []);
  }
  {
    const { m, api } = instance();
    const entry = { at, kind: 'qa', answer: '한글\nsecond line', nested: { n: 1 } };
    const before = structuredClone(entry);
    assert.equal(record(m, () => api.append(entry)).value, undefined);
    assert.deepEqual(entry, before, 'append does not mutate caller, including first call');
    assert.equal(m.files.get(rawFile), JSON.stringify(entry) + '\n');
    assert.deepEqual(m.trace.map(t => t[0]), ['mkdir', 'append']);
    record(m, () => api.append(entry));
    assert.equal(m.files.get(rawFile), (JSON.stringify(entry) + '\n').repeat(2));
  }
  // Missing/falsy time uses the clock once; an explicit timestamp is preserved.
  const NativeDate = globalThis.Date;
  globalThis.Date = class extends NativeDate {
    constructor(...args) { super(...(args.length ? args : [fixedNow])); }
  };
  try {
    for (const supplied of [undefined, null, '', 0, false]) {
      const { m, api } = instance();
      const entry = { kind: 'qa', at: supplied }, before = structuredClone(entry);
      record(m, () => api.append(entry));
      assert.equal(JSON.parse(m.files.get(rawFile)).at, fixedNow);
      assert.deepEqual(entry, before);
    }
  } finally { globalThis.Date = NativeDate; }
  for (const failure of ['mkdir', 'append']) {
    const { m, api } = instance();
    m.fault(failure);
    assert.equal(record(m, () => api.append({ at })).error, undefined, 'append failure is swallowed');
    assert.equal(m.files.has(rawFile), false);
    assert.deepEqual(m.trace.at(-1), ['error', '[log] 대화 기록 실패 —', 'synthetic ' + failure]);
  }
  for (const entry of [{ at: 'invalid' }, { at, value: 1n }]) {
    const { m, api } = instance();
    assert.equal(record(m, () => api.append(entry)).error, undefined);
    assert.equal(m.files.size, 0); assert.equal(m.trace.at(-1)[0], 'error');
  }
  {
    const { m, api } = instance();
    assert.deepEqual(record(m, () => api.readRaw('2026-02')).value, { entries: [], broken: 0 });
    assert.deepEqual(m.trace.map(t => t[0]), ['exists']);
  }
  {
    const text = '\uFEFF {"at":"a"}\r\n\n malformed\nnull\n0\nfalse\n[]\n"string"\n{"x":2}\n{\n';
    const { m, api } = instance({ [rawFile]: text });
    const before = [...m.files];
    const read = record(m, () => api.readRaw('2026-02')).value;
    assert.deepEqual(read, { entries: [{ at: 'a' }, null, 0, false, [], 'string', { x: 2 }], broken: 2 });
    assert.deepEqual([...m.files], before);
    read.entries[0].at = 'changed';
    assert.equal(record(m, () => api.readRaw('2026-02')).value.entries[0].at, 'a',
      'each read parses fresh entries');
  }
  for (const method of ['months', 'renderedMonths']) {
    const { m, api } = instance();
    assert.deepEqual(record(m, () => api[method]()).value, []);
    assert.deepEqual(m.trace.map(t => t[0]), ['exists']);
  }
  {
    const initial = {};
    for (const name of ['qa-2026-12.jsonl', 'qa-2026-01.jsonl', 'qa-2026-99.jsonl',
      'qa-2026-1.jsonl', 'qa-2026-02.jsonl.tmp', 'other.jsonl']) initial[path.join(rawDir, name)] = '';
    for (const name of ['2026-12.md', '2026-01.md', 'index.md', '2026-1.md', '2026-02.md.tmp'])
      initial[path.join(logDir, name)] = '';
    const { m, api } = instance(initial);
    assert.deepEqual(record(m, () => api.months()).value, ['2026-01', '2026-12', '2026-99']);
    assert.deepEqual(record(m, () => api.renderedMonths()).value, ['2026-01', '2026-12']);
  }
  for (const existing of [undefined, 'same', 'different']) {
    for (const write of [true, false]) {
      const { m, api } = instance(existing === undefined ? {} : { [mdFile]: existing });
      const before = [...m.files];
      const result = record(m, () => api.writeIfChanged(mdFile, 'same', write));
      assert.equal(result.value, existing !== 'same');
      const ops = m.trace.map(t => t[0]);
      if (write && existing !== 'same') {
        assert.deepEqual(ops.slice(-3), ['mkdir', 'write', 'rename']);
        assert.equal(m.files.get(mdFile), 'same');
        assert.equal(m.files.has(mdFile + '.tmp'), false);
      } else {
        assert.ok(!ops.some(op => ['mkdir', 'write', 'rename'].includes(op)));
        assert.deepEqual([...m.files], before);
      }
    }
  }
  // Storage failures propagate outside append. Retain original file and the
  // existing .tmp behavior; this refactor adds no cleanup or retry policy.
  for (const operation of ['exists', 'read', 'mkdir', 'write', 'rename']) {
    const { m, api } = instance({ [mdFile]: 'old' });
    const error = m.fault(operation);
    record(m, () => {
      assert.throws(() => api.writeIfChanged(mdFile, 'new', true), e => e === error);
    });
    assert.equal(m.files.get(mdFile), 'old');
    assert.equal(m.files.has(mdFile + '.tmp'), operation === 'rename');
    assert.equal(m.trace.at(-1)[0], operation);
  }
  for (const [method, operation] of [['readRaw', 'read'], ['months', 'list'], ['renderedMonths', 'list']]) {
    const { m, api } = instance({ [rawFile]: '{}', [mdFile]: '' });
    const error = m.fault(operation);
    record(m, () => { assert.throws(() => api[method]('2026-02'), e => e === error); });
  }
  // Factories retain their own directories and enabled flag.
  {
    const { m, api } = instance();
    const other = factory({ fs: m.io, path, LOG_ENABLED: false, LOG_RAW_DIR: '/other/raw',
      LOG_DIR: '/other/md', localParts, console: { error() {} }, config: { timezone: 'Asia/Seoul' },
      createLogStore, createLogRenderer, createLogCodec, createLogStats });
    record(m, () => other.append({ at }));
    assert.deepEqual(m.trace, []);
    record(m, () => api.append({ at }));
    assert.ok(m.files.has(rawFile));
  }
  console.log('PASS ' + label + ': ' + results.length + ' storage scenarios');
  return results;
}
const actual = run(createLogStore, 'store');
assert.deepEqual(actual, run(fromFacade(facade), 'production facade wiring'));
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.equal(typeof source, 'string');
  assert.ok(!source.includes('createLogStore'), 'baseline must precede storage extraction');
  assert.deepEqual(actual, run(fromFacade(source), 'pre-extraction baseline'));
  console.log('PASS exact returns, failures, file bytes and I/O traces; baseline SHA256=' +
    createHash('sha256').update(source).digest('hex'));
}
console.log('PASS no disk fixture writes, model calls or Slack calls.');
