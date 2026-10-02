#!/usr/bin/env node
/** Codex R2f: memory-only read/attachment contracts. No config, files written or API.
 * --baseline-stdin accepts JSON string of independently retained read blocks.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createDocumentRead } from '../src/documents/read.js';
const names = ["fs","path","DOCS_DIR","DOC_READ_MAX_CHARS","DOC_HIT_MAX_CHARS","SECTION_OUTLINE_MIN_COVER","DOC_PREAMBLE_MARK","DIGEST_DOC_MAX_CHARS","DIGEST_DOC_PER_DOC_CHARS","DIGEST_DOC_MIN_CHARS","hasDocuments","resolveProjectFor","resolveDocumentFor","canSeeDoc","BLOCKED_NOTE","readCached","redactPrivateMentions","preambleOf","splitMessages","SHEET_RE","sectionsOf","fold","loadDocument","maskProject","clip"];
function fixture(factory) {
  const trace = [], docs = [], texts = new Map();
  let state = { slack_files: {} }, enabled = true;
  const log = (n, ...a) => trace.push([n, ...structuredClone(a)]);
  const add = (slug, text, extra = {}) => {
    const d = { slug, project: 'local', abs: slug, title: slug, meta: {},
      entries: [{ date: '2026-01-01', text }], ...extra };
    docs.push(d); texts.set(slug, text); return d;
  };
  const sections = text => {
    const matches = [...text.matchAll(/^## (\d+)\. (.+)$/gm)];
    return matches.length < 2 ? null : matches.map((m, i) => ({
      n: +m[1], name: m[2], start: m.index, end: matches[i + 1]?.index ?? text.length }));
  };
  const dep = {
    fs: { readFileSync: p => { log('state', p); if (state instanceof Error) throw state;
      return typeof state === 'string' ? state : JSON.stringify(state); } },
    path, DOCS_DIR: 'synthetic', DOC_READ_MAX_CHARS: 1000, DOC_HIT_MAX_CHARS: 100,
    SECTION_OUTLINE_MIN_COVER: .7, DOC_PREAMBLE_MARK: 'PREAMBLE',
    DIGEST_DOC_MAX_CHARS: 15000, DIGEST_DOC_PER_DOC_CHARS: 3000, DIGEST_DOC_MIN_CHARS: 400,
    hasDocuments: () => { log('has'); return enabled; },
    resolveProjectFor: p => { log('project', p); return p === 'blocked' ?
      { ok: false, error: 'BLOCKED' } : { ok: true, name: p }; },
    resolveDocumentFor: (p, slug) => { log('document', p, slug); const doc = docs.find(d => d.slug === slug);
      return doc ? { ok: true, doc } : { ok: false, error: 'MISSING' }; },
    canSeeDoc: (a, d) => { log('access', a, d.slug); return !d.private || !!a?.full; },
    BLOCKED_NOTE: 'BLOCKED',
    readCached: p => { log('read', p); if (!texts.has(p)) throw Error('read failed'); return texts.get(p); },
    redactPrivateMentions: (s, a) => { log('redact', s, a);
      return a?.full ? s : s.split('\n').filter(l => !l.includes('SECRET')).join('\n'); },
    preambleOf: s => s.startsWith('PRE:') ? s.split('\n')[0].slice(4) : '',
    splitMessages: s => s.split(/(?=^SHEET:)/m).filter(Boolean).map(text => ({ text })),
    SHEET_RE: /^(SHEET)(:)(.+)$/m, sectionsOf: sections,
    fold: s => String(s).trim().toLowerCase().replace(/\s/g, ''),
    loadDocument: (p, file) => { log('load', p, file);
      return docs.find(d => d.slug === file && d.project === p) || { broken: true }; },
    maskProject: p => p,
    clip: (s, max) => s.length > max ? s.slice(0, max) + '\nTRUNC' : s,
  };
  const api = factory(dep);
  assert.deepEqual(trace, [], 'constructor must be lazy');
  for (const name of ['outlineOf', 'readDocument', 'documentsUploadedIn']) {
    const call = api[name]; api[name] = (...args) => {
      const before = structuredClone(docs);
      try { return call(...args); } finally { assert.deepEqual(docs, before); }
    };
  }
  return { api, dep, docs, texts, trace, add, setState: s => { state = s; },
    disable: () => { enabled = false; },
    read: opts => api.readDocument({ project: 'local', document: 'doc', ...opts }),
    uploads: opts => api.documentsUploadedIn({ oldest: 10, latest: 20, ...opts }) };
}
const cases = [
  ['missing archive, lookup and private rejection before read', f => {
    f.add('doc', 'SECRET', { private: true });
    assert.equal(f.read({}).error, 'BLOCKED'); assert.ok(!f.trace.some(t => t[0] === 'read'));
    assert.equal(f.read({ project: 'blocked' }).error, 'BLOCKED');
    assert.equal(f.read({ document: 'absent' }).error, 'MISSING');
    f.disable(); assert.ok(f.read({}).error); assert.deepEqual(f.uploads(), { docs: [], hidden: 0, gistOnly: 0 });
  }],
  ['whole text redaction and read failure propagation', f => {
    f.add('doc', 'visible\nSECRET private');
    const r = f.read({}); assert.equal(r.text, 'visible'); assert.equal(r.truncated, false);
    f.texts.delete('doc'); assert.throws(() => f.read({}), /read failed/); return r;
  }],
  ['month whitespace and missing month', f => {
    f.add('doc', 'PRE:intro\n##  2026-01\nJAN\n##\t2026-02\nFEB');
    const r = f.read({ month: '2026-02' }); assert.match(r.text, /FEB/); assert.ok(!r.text.includes('JAN'));
    assert.match(r.text, /intro/); assert.ok(f.read({ month: 'absent' }).error); return r;
  }],
  ['sheet name, numeric selection and incompatible arguments', f => {
    f.add('doc', 'SHEET:First\nONE\nSHEET:Second\nTWO');
    const r = f.read({ sheet: '2' }); assert.match(r.text, /TWO/); assert.ok(!r.text.includes('ONE'));
    assert.ok(f.read({ sheet: 'missing' }).error); assert.ok(f.read({ sheet: '1', section: '1' }).error); return r;
  }],
  ['section number beats digits in titles and rejects missing numbers', f => {
    f.add('doc', '## 2. A107\nTWO\n## 7. Seven\nSEVEN\n## 10. Other\nTEN');
    const r = f.read({ section: '7' }); assert.equal(r.section, '7. Seven');
    assert.match(r.text, /SEVEN/); assert.ok(!r.text.includes('TWO'));
    assert.ok(f.read({ section: '107' }).error); return r;
  }],
  ['outline choice and blocked size versus cover hints', f => {
    assert.equal(f.api.outlineOf('SHEET:A\none\nSHEET:B\ntwo').kind, 'sheet');
    assert.equal(f.api.outlineOf('## 2026-01\none\n## 2026-02\ntwo').kind, 'month');
    f.add('doc', '## 1. First\n' + 'a'.repeat(200) + '\n## 2. Second\nsmall');
    const size = f.read({ maxChars: 80 }); assert.equal(size.outline, null); assert.match(size.hint, /가장 큰 절/);
    f.texts.set('doc', 'x'.repeat(300) + '\n## 1. First\nsmall\n## 2. Second\nsmall');
    const cover = f.read({ maxChars: 80 }); assert.match(cover.hint, /충분히 덮지 못해/); return [size, cover];
  }],
  ['large document returns full navigable outline', f => {
    f.add('doc', 'PRE:intro\n## 2026-01\n' + 'a'.repeat(600) + '\n## 2026-02\n' + 'b'.repeat(600));
    const r = f.read({ maxChars: 500 }); assert.equal(r.outline, 'month');
    assert.match(r.text, /intro/); assert.match(r.text, /2026-02/); assert.equal(r.truncated, false); return r;
  }],
  ['state missing or malformed yields empty result', f => {
    for (const value of [new Error('missing'), '{bad', {}]) {
      f.setState(value); assert.deepEqual(f.uploads(), { docs: [], hidden: 0, gistOnly: 0 });
    }
  }],
  ['upload window, prefix normalization, duplicate and newest order', f => {
    f.add('doc', 'BODY'); f.add('older', 'OLDER');
    f.setState({ slack_files: {
      a: { doc: 'projects/local/doc', ts: '19', date: '2026-01-01', name: 'first' },
      b: { doc: 'local/doc', ts: 18, date: '2026-01-01', name: 'duplicate' },
      c: { doc: 'local/older', ts: 10 }, d: { doc: 'local/doc', ts: 20 },
      e: { doc: 'local/doc', ts: 9 }, f: { doc: 'invalid', ts: 15 },
    } });
    const r = f.uploads(); assert.deepEqual(r.docs.map(d => d.title), ['doc', 'older']);
    assert.equal(r.docs[0].file, 'first'); return r;
  }],
  ['broken private empty entries and exhausted budget', f => {
    f.add('doc', 'a'.repeat(500)); f.add('private', 'SECRET', { private: true });
    f.add('empty', '', { entries: [] }); f.add('later', 'body');
    f.setState({ slack_files: Object.fromEntries(['doc','private','missing','empty','later'].map((d,i) =>
      [d, { doc: 'local/' + d, ts: 19-i }])) });
    const r = f.uploads({ maxChars: 400 });
    assert.equal(r.hidden, 1); assert.equal(r.gistOnly, 1);
    assert.deepEqual(r.docs.map(d => d.title), ['doc','empty','later']);
    assert.equal(r.docs.at(-1).text, ''); return r;
  }],
];
function run(factory, label) {
  return cases.map(([name, check]) => {
    const f = fixture(factory), result = check(f);
    console.log('PASS ' + label + ': ' + name);
    return { result, trace: f.trace, docs: f.docs };
  });
}
const actual = run(createDocumentRead, 'read');
const a = fixture(createDocumentRead), b = fixture(createDocumentRead);
a.disable(); b.add('doc', 'independent'); assert.equal(b.read({}).text, 'independent');
console.log('PASS independent factories');
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8')); assert.equal(typeof source, 'string');
  const factory = dep => compileFunction(source + '\nreturn {outlineOf, readDocument, documentsUploadedIn};', names)(
    ...names.map(n => dep[n]));
  assert.deepEqual(actual, run(factory, 'baseline'));
  console.log('PASS output/trace equality; baseline SHA256=' + createHash('sha256').update(source).digest('hex'));
}
