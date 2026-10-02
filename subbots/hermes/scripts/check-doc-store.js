#!/usr/bin/env node
/**
 * Codex R2b: memory-only loading/cache contracts. No archive/config imports,
 * file writes, API calls or Git operations. --baseline-stdin accepts a JSON
 * string of independently retained pre-extraction documents.js or loader block.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createDocumentStore } from '../src/documents/store.js';

const dependencyNames = ['fs', 'path', 'projectDir', 'projectPrivateChannel', 'projectIsPrivate',
  'readCached', 'metaBlock', 'splitMessages', 'preambleOf'];
function oldFactory(source) {
  const a = source.indexOf('function parseMeta(');
  const b = source.indexOf('/** 사업장(생략 시 전체)의 문서 목록 */', a);
  assert.ok(a >= 0, 'baseline loader start exists');
  const body = source.slice(a, b < 0 ? undefined : b);
  return deps => compileFunction(body + '\nreturn { loadDocument };', dependencyNames)(
    ...dependencyNames.map(n => deps[n]));
}
function fixture(factory) {
  const trace = [], files = new Map();
  // map: channelMap 이 돌려주는 「지도의 지금 판」. 같은 객체 = 지도 그대로, 바꿔 끼우면
  // 지도가 다시 만들어진 것이다 (config.js 의 currentChannelNames 가 그렇게 동작한다).
  const state = { statError: false, readError: false, metaError: false, splitError: false, preError: false, map: {} };
  const record = (name, ...args) => trace.push([name, ...args]);
  const key = (p, f = 'doc.md') => path.join('/fixture', p, f);
  const put = (project = 'public', changes = {}, file = 'doc.md') => {
    const value = { mtime: 1, text: '# Synthetic title\n**2026-01-01 · file.pdf**\nBody',
      meta: '**열람**: 공개 · **종류**: 보고', ...changes };
    files.set(key(project, file), value); return value;
  };
  const deps = {
    path,
    fs: { statSync: abs => {
      record('stat', abs);
      if (state.statError || !files.has(abs)) throw new Error('stat failure');
      return { mtimeMs: files.get(abs).mtime };
    } },
    projectDir: p => { record('projectDir', p); return path.join('/fixture', p); },
    projectPrivateChannel: p => { record('privateChannel', p); return p.split('/')[0] === 'secret' ? 'secret' : null; },
    projectIsPrivate: p => { record('isPrivate', p); return p.split('/')[0] === 'secret'; },
    readCached: abs => {
      record('read', abs); if (state.readError) throw new Error('read failure');
      return files.get(abs).text.replace(/\r\n/g, '\n');
    },
    metaBlock: abs => {
      record('meta', abs); if (state.metaError) throw new Error('meta failure');
      return files.get(abs).meta;
    },
    splitMessages: text => {
      record('split', text); if (state.splitError) throw new Error('split failure');
      return [{ date: '2026-01-01', text }];
    },
    preambleOf: text => {
      record('preamble', text); if (state.preError) throw new Error('preamble failure');
      return 'synthetic preamble';
    },
    // path 처럼 trace 에 안 적는다 — 매 load 마다 도는 결정적 getter 라 순서 단언만 시끄러워진다.
    channelMap: () => state.map,
  };
  const store = factory(deps);
  assert.deepEqual(trace, [], 'factory must not invoke dependencies');
  const load = (p = 'public', f = 'doc.md') => store.loadDocument(p, f);
  return { ...store, load, put, files, key, state, trace, deps };
}
const cases = [
  ['cold fields and warm shared identity', f => {
    f.put(); const a = f.load();
    assert.equal(a.title, 'Synthetic title'); assert.equal(a.slug, 'doc');
    assert.equal(a.private, false); assert.equal(a.privateChannel, null);
    assert.equal(a.broken, false); assert.equal(a.preamble, 'synthetic preamble');
    assert.equal(a.entries.length, 1); assert.equal(a.meta['종류'], '보고');
    assert.deepEqual(f.trace.map(x => x[0]), ['projectDir', 'stat', 'read', 'meta', 'split', 'preamble', 'isPrivate', 'privateChannel']);
    const n = f.trace.length;
    assert.equal(f.load(), a);
    assert.deepEqual(f.trace.slice(n).map(x => x[0]), ['projectDir', 'stat']);
    return a;
  }],
  ['mtime refresh and unchanged-mtime reuse', f => {
    const file = f.put(); const a = f.load();
    file.text = '# Changed'; assert.equal(f.load(), a);
    file.mtime++; const b = f.load(); assert.notEqual(b, a); assert.equal(b.title, 'Changed');
    assert.equal(f.load(), b); return b;
  }],
  // 지도(.sync-state.json)가 죽거나 바뀌면 문서 mtime 이 그대로여도 다시 읽어야 한다 —
  // private·privateChannel 은 읽는 순간의 지도로 굳힌 값이라, 지도만 바뀐 뒤 캐시에서
  // 나오면 옛 권한 판정이 계속 산다 (2026-09-17. --baseline-stdin 의 추출 전 원본은
  // 이 동작이 없어 이 케이스에서 일부러 실패한다 — 기준선이 낡았다는 신호가 맞다).
  ['map change invalidates entries even with unchanged mtime', f => {
    f.put('secret/sub', { meta: '**열람**: 공개\n**공개승인**: approved' });
    const a = f.load('secret/sub'); assert.equal(a.private, false);
    assert.equal(f.load('secret/sub'), a, 'same map and mtime must reuse the cache');
    const n = f.trace.length;
    f.state.map = {}; // 지도가 다시 만들어졌다 — 파일 쪽 신호는 아무것도 안 움직였다
    const b = f.load('secret/sub');
    assert.notEqual(b, a, 'a rebuilt map must force a fresh privacy verdict');
    assert.ok(f.trace.slice(n).some(x => x[0] === 'isPrivate'), 'verdict is recomputed, not copied');
    assert.equal(f.load('secret/sub'), b, 'the new map is cached in turn');
    return [a, b];
  }],
  ['separate absolute paths', f => {
    f.put(); f.put('second'); f.put('public', {}, 'other.md');
    const a = f.load(), b = f.load('second'), c = f.load('public', 'other.md');
    assert.notEqual(a, b); assert.notEqual(a, c); assert.equal(f.load(), a);
    return [a, b, c];
  }],
  ['stat failure yields uncached broken objects', f => {
    f.put('secret/sub'); f.state.statError = true;
    const a = f.load('secret/sub'), b = f.load('secret/sub');
    assert.notEqual(a, b); assert.equal(a.private, true); assert.equal(a.broken, true);
    assert.equal(a.privateChannel, 'secret'); assert.deepEqual(a.entries, []);
    assert.ok(!f.trace.some(x => x[0] === 'read'));
    f.state.statError = false; assert.equal(f.load('secret/sub').broken, false); return a;
  }],
  ['read failure and recovery', f => {
    f.put(); f.state.readError = true; const a = f.load();
    assert.equal(a.broken, true); assert.equal(a.private, true);
    assert.equal(a.title, 'doc'); assert.equal(a.preamble, '');
    assert.ok(!f.trace.some(x => x[0] === 'meta'));
    f.state.readError = false; assert.equal(f.load().broken, false); return a;
  }],
  ['later dependency failures propagate without caching', f => {
    f.put(); const out = [];
    for (const [flag, message] of [['metaError', 'meta failure'], ['splitError', 'split failure'], ['preError', 'preamble failure']]) {
      f.state[flag] = true; assert.throws(() => f.load(), { message }); out.push(message);
      f.state[flag] = false;
    }
    assert.equal(f.load().broken, false); return out;
  }],
  ['private folder requires both public access and approval', f => {
    const out = [];
    for (const [meta, hidden] of [
      ['**열람**: 공개', true],
      ['**열람**: 공개\n**공개승인**: approved', false],
      ['**열람**: 비공개\n**공개승인**: approved', true],
      ['**공개승인**: approved', true],
      ['**열람**: 공개\n**공개승인**:', true],
    ]) {
      f.put('secret/sub', { meta, mtime: out.length + 1 });
      const d = f.load('secret/sub'); assert.equal(d.private, hidden);
      assert.equal(d.privateChannel, 'secret'); out.push(d);
    }
    return out;
  }],
  ['missing malformed and multi-key metadata', f => {
    const out = [];
    for (const [meta, broken, hidden] of [
      ['', true, true], ['not metadata', true, true],
      ['**종류**: 보고', false, true],
      ['**열람**: public', false, true],
      ['**열람**: 공개 · **종류**: 보고\n**주요 항목**: A · B', false, false],
    ]) {
      f.put('public', { meta, mtime: out.length + 1 });
      const d = f.load(); assert.equal(d.broken, broken); assert.equal(d.private, hidden); out.push(d);
    }
    assert.equal(out.at(-1).meta['주요 항목'], 'A · B'); return out;
  }],
  ['delete then recreate with same and different mtime', f => {
    f.put(); const a = f.load(); f.files.delete(f.key('public'));
    assert.equal(f.load().broken, true);
    f.put('public', { text: '# Recreated', mtime: 1 });
    assert.equal(f.load(), a, 'preserve existing mtime-only cache policy, not endorse it');
    f.files.get(f.key('public')).mtime = 2;
    const b = f.load(); assert.notEqual(b, a); assert.equal(b.title, 'Recreated'); return b;
  }],
  ['failed refresh preserves old cache entry', f => {
    const file = f.put(); const a = f.load(); file.mtime = 2; f.state.readError = true;
    assert.equal(f.load().broken, true);
    file.mtime = 1; assert.equal(f.load(), a, 'warm cache avoids reads even when read boundary fails');
    file.mtime = 2; f.state.readError = false; assert.notEqual(f.load(), a); return a;
  }],
  ['title fallback and normalized reader input', f => {
    f.put('public', { text: 'no title\r\nline' }); const a = f.load();
    assert.equal(a.title, 'doc'); assert.ok(!a.entries[0].text.includes('\r'));
    return a;
  }],
  ['metadata policy remains cached until mtime changes', f => {
    const file = f.put('secret', { meta: '**열람**: 공개\n**공개승인**: yes' });
    const a = f.load('secret'); file.meta = '**열람**: 비공개';
    assert.equal(f.load('secret'), a); file.mtime++;
    assert.equal(f.load('secret').private, true); return a;
  }],
];
function run(factory, label) {
  const results = [];
  for (const [name, check] of cases) {
    const f = fixture(factory);
    results.push({ name, value: check(f), trace: f.trace });
    console.log('PASS ' + label + ': ' + name);
  }
  const one = fixture(factory), two = fixture(factory); one.put(); two.put();
  assert.notEqual(one.load(), two.load(), 'factory caches are isolated');
  console.log('PASS ' + label + ': independent factory caches');
  return results;
}
const actual = run(createDocumentStore, 'store');
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8')); assert.equal(typeof source, 'string');
  assert.deepEqual(actual, run(oldFactory(source), 'baseline'));
  console.log('PASS exact result and dependency trace comparison; baseline SHA256=' +
    createHash('sha256').update(source).digest('hex'));
}
