#!/usr/bin/env node
/**
 * Codex R2e: memory-only search contracts. No config, archives, writes or API.
 * --baseline-stdin accepts an independently retained, export-stripped search block.
 * Compare returned JSON, dependency trace and unchanged input documents.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createDocumentSearch } from '../src/documents/search.js';
const depNames = ["limits","DOC_SEARCH_MAX_HITS","DOC_HIT_MAX_CHARS","PARTIAL_HIT_MAX_CHARS","DOC_LIST_MAX","DOC_PREAMBLE_MARK","TRUNC_PHRASE","companyWideDocProjects","hasDocuments","splitTerms","scoreTerms","termHits","PARTIAL_MIN_TERMS","clipPartial","resolveProjectFor","resolveDocumentFor","documentsFor","listDocuments","canSeeDoc","redactPrivateMentions","maskProject","clip","pickSpread","spreadNote","byScoreThenDate","fold","readCached","sectionsOf","isSeriesDoc","latestDate"];
function fixture(factory) {
  const trace = [], docs = [], texts = new Map();
  const log = (name, ...args) => trace.push([name, ...structuredClone(args)]);
  const add = (project, slug, text, extra = {}) => {
    const d = { project, slug, title: slug, abs: project + '/' + slug,
      entries: [{ date: '2026-01-01', text }], meta: {}, ...extra };
    docs.push(d); texts.set(d.abs, text); return d;
  };
  const safe = (text, access) => access?.full ? text : text.split('\n').filter(l => !l.includes('SECRET')).join('\n');
  const visible = (access, d) => !d.private || !!access?.full;
  const list = p => { log('list', p); return docs.filter(d => !p || d.project === p); };
  const dep = {
    limits: { outsideWhenNarrowedMaxHits: 2 }, DOC_SEARCH_MAX_HITS: 12,
    DOC_HIT_MAX_CHARS: 100, PARTIAL_HIT_MAX_CHARS: 30, DOC_LIST_MAX: 2,
    DOC_PREAMBLE_MARK: 'PREAMBLE', TRUNC_PHRASE: 'TRUNC', companyWideDocProjects: ['common'],
    hasDocuments: () => { log('has'); return true; },
    splitTerms: q => String(q || '').toLowerCase().split(/\s+/).filter(Boolean),
    scoreTerms: (s, ts) => ts.filter(t => s.includes(t)).length,
    termHits: (s, ts) => ts.reduce((n, t) => n + s.split(t).length - 1, 0),
    PARTIAL_MIN_TERMS: 2,
    clipPartial: (s, max, ts) => { log('partial', s, max, ts); return s.slice(0, max); },
    resolveProjectFor: (p, a) => { log('resolveProject', p, a);
      return p === 'blocked' ? { ok: false, error: 'BLOCKED' } :
        { ok: true, name: p === 'alias' ? 'local' : p }; },
    resolveDocumentFor: (p, name, a) => { log('resolveDoc', p, name, a);
      const doc = docs.find(d => d.project === p && d.slug === name && visible(a, d));
      return doc ? { ok: true, doc } : { ok: false, error: 'MISSING' }; },
    documentsFor: (p, a) => list(p).filter(d => visible(a, d)),
    listDocuments: list,
    canSeeDoc: (a, d) => { log('access', a, d.slug); return visible(a, d); },
    redactPrivateMentions: (s, a) => { log('redact', s, a); return safe(s, a); },
    maskProject: (p, a) => { log('mask', p, a); return p; },
    clip: (s, max) => s.length > max ? s.slice(0, max) + '\nTRUNC' : s,
    pickSpread: (groups, max, per, compare) => {
      log('spread', [...groups], max, per, !!compare);
      return [...groups.values()].flatMap(rows => rows.slice().sort(compare).slice(0, per)).slice(0, max);
    },
    spreadNote: (groups, total, shown, note) => total > shown ? note : undefined,
    byScoreThenDate: (a, b) => b.score - a.score || b.date.localeCompare(a.date),
    fold: s => s.toLowerCase().replace(/[^a-z0-9]/g, ''),
    readCached: p => { log('read', p); if (!texts.has(p)) throw Error('unreadable'); return texts.get(p); },
    sectionsOf: s => [...s.matchAll(/^## (.+)$/gm)].map(m => ({ name: m[1] })),
    isSeriesDoc: d => !!d.series,
    latestDate: entries => entries.map(e => e.date).sort().at(-1),
  };
  const api = factory(dep);
  assert.deepEqual(trace, [], 'lazy construction');
  // Guard every public call, including the first call made by each case.
  for (const name of ['searchDocuments', 'companyWideCards']) {
    const call = api[name];
    api[name] = (...args) => {
      const before = structuredClone(docs);
      try { return call(...args); }
      finally { assert.deepEqual(docs, before, name + ' must not mutate cached documents'); }
    };
  }
  return { api, dep, docs, texts, trace, add };
}
const cases = [
  ['empty query', f => {
    assert.deepEqual(f.api.searchDocuments({ query: '' }).hits, []);
  }],
  ['exact, partial, preamble, redaction and input order', f => {
    f.add('local', 'exact', 'alpha beta', { preamble: 'alpha beta introduction' });
    f.add('local', 'partial', 'alpha only');
    f.add('local', 'private', 'alpha beta', { private: true });
    f.add('local', 'masked', 'SECRET alpha beta');
    const r = f.api.searchDocuments({ query: 'alpha beta', project: 'local' });
    assert.equal(r.hits.length, 3);
    assert.equal(r.hits[0].text.startsWith('PREAMBLE'), true);
    assert.equal(r.hits.at(-1).score, 1);
    assert.ok(!JSON.stringify(r).includes('SECRET')); return r;
  }],
  ['partial only and inventory limit', f => {
    for (let i = 0; i < 4; i++) f.add('local', 'd' + i, 'alpha');
    const r = f.api.searchDocuments({ project: 'local', query: 'alpha beta' });
    assert.equal(r.partial, true); assert.match(r.note, /그 외 2건/); return r;
  }],
  ['zero hits inventory, outside alias and document scope', f => {
    f.add('local', 'empty', 'nothing'); f.add('remote', 'match', 'alpha beta');
    const r = f.api.searchDocuments({ project: 'alias', query: 'alpha beta' });
    assert.equal(r.outside.length, 1); assert.match(r.note, /0건/);
    const pinned = f.api.searchDocuments({ project: 'local', document: 'empty', query: 'alpha beta' });
    assert.equal(pinned.outside, undefined); assert.equal(pinned.cards, undefined);
    return [r, pinned];
  }],
  ['truncation hint and per-call limits', f => {
    f.add('local', 'long', 'alpha ' + 'x'.repeat(150)); f.add('remote', 'other', 'alpha');
    const r = f.api.searchDocuments({ query: 'alpha', maxHits: 1, perProject: 1 });
    assert.equal(r.hits.length, 1); assert.match(r.note, /일부 히트는 길이 제한/); return r;
  }],
  ['cards are body-free, structural first, title filtering before cap', f => {
    f.add('common', 'SECRET title', '## local\nalpha', { entries: [{ date: '2026-03-01', text: 'alpha' }] });
    f.add('common', 'visible', 'alpha alpha');
    f.add('common', 'other', 'alpha');
    const r = f.api.companyWideCards({ project: 'local', query: 'alpha alpha', max: 2 });
    // Density is 1/5 for 'other', versus 2/11 for 'visible'.
    assert.equal(r.length, 2); assert.deepEqual(r.map(c => c.title), ['other', 'visible']);
    assert.ok(r.every(c => !Object.hasOwn(c, 'text'))); return r;
  }],
  ['card-only result, blocked project, unreadable and private documents', f => {
    f.add('common', 'structural', '## local\nnothing');
    f.add('common', 'hidden', '## local\nSECRET alpha', { private: true });
    const bad = f.add('common', 'unreadable', 'alpha'); f.texts.delete(bad.abs);
    const r = f.api.searchDocuments({ project: 'local', query: 'absent' });
    assert.equal(r.hits.length, 0); assert.equal(r.cards.length, 1);
    const blocked = f.api.searchDocuments({ project: 'blocked', query: 'alpha' });
    assert.equal(blocked.cards, undefined); return [r, blocked];
  }],
  ['kill switch prevents raw reads even for many partial hits', f => {
    f.dep.companyWideDocProjects.splice(0);
    for (let i = 0; i < 5; i++) f.add('local', 'd' + i, 'alpha');
    f.dep.limits.docSearchMaxPerProject = 10;
    const r = f.api.searchDocuments({ project: 'local', query: 'alpha beta' });
    assert.equal(r.cards, undefined); assert.ok(!f.trace.some(t => t[0] === 'read')); return r;
  }],
  ['alias local rescan uses unique terms and suppresses cards', f => {
    f.dep.limits.docSearchMaxPerProject = 10;
    for (let i = 0; i < 5; i++) f.add('local', 'd' + i, i === 4 ? 'beta' : 'alpha');
    f.add('common', 'structural', '## local\nalpha beta');
    const r = f.api.searchDocuments({ project: 'alias', query: 'alpha beta alpha' });
    assert.equal(r.cards, undefined); assert.ok(f.trace.some(t => t[0] === 'read'));
    return r;
  }],
];
function run(factory, label) {
  return cases.map(([name, check]) => {
    const f = fixture(factory);
    const result = check(f), before = structuredClone(f.docs);
    const repeated = f.api.searchDocuments({ query: 'alpha beta', project: 'local', access: { full: true } });
    assert.deepEqual(f.docs, before, 'search must not mutate cached documents');
    console.log('PASS ' + label + ': ' + name);
    return { result, repeated, trace: f.trace, docs: f.docs };
  });
}
const actual = run(createDocumentSearch, 'search');
const a = fixture(createDocumentSearch), b = fixture(createDocumentSearch);
a.add('local', 'only-a', 'alpha');
assert.equal(b.api.searchDocuments({ query: 'alpha' }).hits.length, 0);
console.log('PASS independent factories');
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.equal(typeof source, 'string');
  const factory = dep => compileFunction(source + '\nreturn {searchDocuments, companyWideCards};', depNames)(
    ...depNames.map(n => dep[n]));
  assert.deepEqual(actual, run(factory, 'baseline'));
  console.log('PASS baseline output/trace equality SHA256=' + createHash('sha256').update(source).digest('hex'));
}
