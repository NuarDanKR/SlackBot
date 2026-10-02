#!/usr/bin/env node
/**
 * Codex R2d: memory-only index/folding contracts. No config/archive imports,
 * file writes, model/Slack/Git calls. --baseline-stdin takes a JSON string of
 * independently retained pre-extraction brief blocks (not candidate source).
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createDocumentBrief } from '../src/documents/brief.js';

const dependencyNames = ['path', 'DOCS_DIR', 'readCached', 'hasDocuments', 'listProjects', 'listDocuments',
  'canSeeDoc', 'maskProject', 'projectPrivateChannel', 'canSeePrivateChannel', 'redactPrivateMentions',
  'PUBLIC_ACCESS', 'preambleOutline', 'latestDate', 'isSheetDoc', 'isSeriesDoc',
  'DOC_BRIEF_MAX_CHARS', 'DOC_BRIEF_PRIVATE_MAX_CHARS', 'DOC_BRIEF_RECENT_MONTHS', 'FOLD_KINDS_MAX', 'Date'];
const exports = ['foldedLine', 'floorParts', 'indexGauge', 'buildDocumentsBrief', 'buildDocumentsBriefSplit'];
const PUB = { channels: [] }, MEMBER = { channels: ['secret'] }, FULL = { full: true };
const copy = v => structuredClone(v);
function fixture(factory) {
  const trace = [];
  const state = { present: true, readError: false, time: '2026-09-30T23:30:00Z',
    missing: '# index\n## 변환하지 못한 것\nPUBLIC_MISSING\nSECRET_MISSING\n---\nFOOTER' };
  const doc = (title, kind, dates, project = 'public', extra = {}) => ({
    project, title, slug: title, meta: { '종류': kind, '주요 항목': 'DO_NOT_RENDER_NUMBER_9999' },
    entries: dates.map(date => ({ date, text: 'body' })), private: false, ...extra,
  });
  const docs = [
    doc('old-a', '보고', ['2025-01-01']),
    doc('old-b', '보고', ['2025-01-01']),
    doc('recent', '현황', ['2026-09-20'], 'public', { preamble: 'Heading\nPRIVATE_NUMBER_123' }),
    doc('contract', '계약', ['2025-01-01']),
    doc('series', '정기', ['2026-08-01', '2026-09-01']),
    doc('sheet', '엑셀', ['2026-01-01', '2026-01-01'], 'public', { meta: { '시트': '2', '종류': '엑셀' } }),
    doc('approved', '보고', ['2026-09-01'], 'secret'),
    doc('hidden', '보고', ['2026-09-01'], 'secret', { private: true }),
  ];
  const projects = ['public', 'secret'];
  const log = (name, ...args) => trace.push([name, ...copy(args)]);
  const see = a => !!(a?.full || a?.channels?.includes('secret'));
  class FixedDate extends Date {
    constructor(...args) { super(...(args.length ? args : [state.time])); if (!args.length) log('clock', state.time); }
  }
  const deps = {
    path, DOCS_DIR: '/fixture', PUBLIC_ACCESS: PUB,
    Date: FixedDate, DOC_BRIEF_MAX_CHARS: 6000, DOC_BRIEF_PRIVATE_MAX_CHARS: 1500,
    DOC_BRIEF_RECENT_MONTHS: 3, FOLD_KINDS_MAX: 3,
    hasDocuments: () => { log('has'); return state.present; },
    listProjects: () => { log('projects'); return projects.slice(); },
    listDocuments: p => { log('documents', p); return docs.filter(d => p === undefined || d.project === p); },
    canSeeDoc: (a, d) => { log('canSee', a, d.title); return !d.private || see(a); },
    maskProject: (p, a) => { log('mask', p, a); return p === 'secret' && !see(a) ? '_승인자료' : p; },
    projectPrivateChannel: p => { log('privateChannel', p); return p === 'secret' ? 'secret' : null; },
    canSeePrivateChannel: (a, ch) => { log('channelAccess', a, ch); return see(a); },
    redactPrivateMentions: (text, a) => {
      log('redact', text, a);
      return text.split('\n').filter(line => !line.includes('SECRET_MISSING') || see(a)).join('\n');
    },
    readCached: file => { log('read', file); if (state.readError) throw new Error('read error'); return state.missing; },
    preambleOutline: text => { log('outline', text); return text.split('\n')[0]; },
    latestDate: entries => entries.reduce((m, e) => e.date > m ? e.date : m, ''),
    isSheetDoc: d => Boolean(d.meta && d.meta['시트']),
    isSeriesDoc: d => !d.meta?.['시트'] && d.entries.length >= 2,
  };
  const api = factory(deps);
  assert.deepEqual(trace, [], 'constructor must be lazy, including clock');
  return { api, deps, docs, projects, state, trace };
}
const cases = [
  ['empty archive and empty documents', f => {
    f.state.present = false;
    assert.equal(f.api.buildDocumentsBrief({ access: PUB }), '');
    assert.deepEqual(f.api.buildDocumentsBriefSplit({ access: PUB }), { common: '', extra: '' });
    assert.ok(!f.trace.some(x => x[0] === 'read'));
    f.state.present = true; f.docs.splice(0);
    assert.equal(f.api.buildDocumentsBrief({ access: PUB }), '');
    return f.api.indexGauge();
  }],
  ['roomy output: metadata numbers omitted, sheet count and heading only', f => {
    const visible = [], folded = [];
    const text = f.api.buildDocumentsBrief({ access: PUB, maxChars: 100000, visibleOut: visible, foldedOut: folded });
    assert.ok(text.includes('시트 2개')); assert.ok(text.includes('상단정리: Heading'));
    assert.ok(!text.includes('PRIVATE_NUMBER_123')); assert.ok(!text.includes('DO_NOT_RENDER_NUMBER_9999'));
    assert.ok(!text.includes('hidden')); assert.ok(!text.includes('### secret'));
    assert.ok(!text.includes('SECRET_MISSING')); assert.ok(!text.includes('FOOTER'));
    assert.equal(folded.length, 0); assert.equal(visible.length, 7);
    assert.equal(visible[0], f.docs[0]); return { text, visible, folded };
  }],
  ['zero budget folds series last and preserves above-budget floor', f => {
    const folded = [], visible = [];
    const text = f.api.buildDocumentsBrief({ access: PUB, maxChars: 0, foldedOut: folded, visibleOut: visible });
    assert.ok(text.length > 0); assert.ok(text.includes('그 외 7건'));
    assert.deepEqual(folded.slice(0, 2).map(d => d.title), ['old-a', 'old-b']);
    // foldedOut is grouped by project, not the global candidate ordering.
    assert.equal(folded.filter(d => d.project === 'public').at(-1).title, 'series');
    assert.equal(folded.length, 7); assert.deepEqual(visible, folded);
    return { text, folded, visible, gauge: f.api.indexGauge({ budget: 0 }) };
  }],
  ['budget matrix preserves arrays and source document identities', f => {
    const results = [];
    for (const maxChars of [0, 1, 200, 500, 650, 800, 1000, 2000, 100000]) {
      const sentinel = { sentinel: true }, folded = [sentinel], visible = [sentinel];
      const parts = ['HEADER', ''], before = parts.slice();
      const text = f.api.buildDocumentsBrief({ access: PUB, maxChars, foldedOut: folded, visibleOut: visible, parts });
      assert.deepEqual(parts, before); assert.equal(folded[0], sentinel); assert.equal(visible[0], sentinel);
      for (const d of visible.slice(1)) assert.ok(f.docs.includes(d));
      for (const p of ['public', 'secret']) {
        const ordered = visible.slice(1).filter(d => d.project === p);
        assert.deepEqual(ordered, [...f.docs.filter(d => d.project === p && !d.private && !folded.includes(d)),
          ...folded.slice(1).filter(d => d.project === p)]);
      }
      results.push({ text, folded, visible });
    }
    return results;
  }],
  ['missing omitted vs empty and read failure', f => {
    const normal = f.api.buildDocumentsBrief({ access: PUB });
    assert.ok(normal.includes('PUBLIC_MISSING'));
    const n = f.trace.length;
    const empty = f.api.buildDocumentsBrief({ access: PUB, missing: '' });
    assert.ok(!empty.includes('PUBLIC_MISSING')); assert.ok(!f.trace.slice(n).some(x => x[0] === 'read'));
    f.state.readError = true;
    assert.equal(f.api.buildDocumentsBrief({ access: PUB }), empty);
    f.state.readError = false; f.state.missing = '## Other\nnone';
    assert.equal(f.api.buildDocumentsBrief({ access: PUB }), empty);
    return { normal, empty };
  }],
  ['public common bytes and approved duplicate in extra', f => {
    const results = [PUB, MEMBER, FULL, PUB].map(access => f.api.buildDocumentsBriefSplit({ access }));
    for (const r of results) assert.equal(r.common, results[0].common);
    assert.equal(results[0].extra, ''); assert.ok(results[1].extra.includes('### secret'));
    assert.ok(results[1].extra.includes('approved')); assert.ok(results[1].common.includes('approved'));
    assert.ok(results[1].extra.includes('SECRET_MISSING')); assert.ok(!results[1].common.includes('SECRET_MISSING'));
    return results;
  }],
  ['revived missing-only with no private project keeps existing empty-extra behavior', f => {
    f.projects.splice(1); f.docs.splice(6);
    const r = f.api.buildDocumentsBriefSplit({ access: MEMBER });
    assert.equal(r.extra, '', 'no visible documents causes the original builder early return');
    return r;
  }],
  ['stable folded-kind ties and kind limit', f => {
    const docs = ['B', 'A', 'C', 'D'].map(kind => ({ meta: { '종류': kind } }));
    assert.equal(f.api.foldedLine(docs), '- 그 외 4건 (B·A·C 등)');
    assert.equal(f.api.foldedLine([]), '- 그 외 0건');
    assert.equal(f.api.foldedLine(docs.slice(0, 3)), '- 그 외 3건 (B·A·C)');
    return f.api.foldedLine(docs);
  }],
  ['floor parts sum includes exact separators', f => {
    return ['', 'header', 'header\n', 'header\n### A\n- [K] doc\n- 그 외 1건\n## 변환하지 못한 것\nrow\n'].map(text => {
      const result = f.api.floorParts(text);
      const sum = [...result.matchAll(/ (\d+)/g)].reduce((n, m) => n + Number(m[1]), 0);
      assert.equal(sum, text.length); return result;
    });
  }],
  ['month-end and time boundaries with fresh clock objects', f => {
    const outputs = [];
    for (const time of ['2026-05-31T23:30:00Z', '2026-03-31T00:30:00Z', '2024-02-29T23:59:59Z']) {
      f.state.time = time;
      // Independently calculate the old local-month -> UTC date boundary.
      const cutoff = new Date(time); cutoff.setMonth(cutoff.getMonth() - 3);
      const date = cutoff.toISOString().slice(0, 10);
      f.docs.splice(0, f.docs.length,
        { project: 'public', title: 'boundary', meta: {}, entries: [{ date }], private: false },
        { project: 'public', title: 'old', meta: {}, entries: [{ date: '2020-01-01' }], private: false });
      f.projects.splice(1);
      const first = f.api.buildDocumentsBrief({ access: PUB, maxChars: 0, foldedOut: outputs });
      assert.equal(f.api.buildDocumentsBrief({ access: PUB, maxChars: 0 }), first);
      assert.deepEqual(outputs.slice(-2).map(d => d.title), ['old', 'boundary']);
    }
    return outputs;
  }],
];
function run(factory, label) {
  const results = [];
  for (const [name, check] of cases) {
    const f = fixture(factory), before = copy(f.docs);
    const value = check(f);
    if (!name.startsWith('empty ') && !name.startsWith('revived ') && !name.startsWith('month-end ')) {
      assert.deepEqual(f.docs, before, 'shared documents must not be mutated');
    }
    results.push({ name, value, trace: f.trace });
    console.log('PASS ' + label + ': ' + name);
  }
  const a = fixture(factory), b = fixture(factory); a.state.present = false;
  assert.equal(a.api.buildDocumentsBrief({ access: PUB }), '');
  assert.ok(b.api.buildDocumentsBrief({ access: PUB }));
  console.log('PASS ' + label + ': factory isolation');
  return results;
}
const actual = run(createDocumentBrief, 'brief');
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8')); assert.equal(typeof source, 'string');
  const factory = deps => compileFunction(source.replace(/^export /gm, '') +
    '\nreturn { ' + exports.join(', ') + ' };', dependencyNames)(...dependencyNames.map(k => deps[k]));
  assert.deepEqual(actual, run(factory, 'baseline'));
  console.log('PASS exact output, diagnostics and dependency traces; baseline SHA256=' +
    createHash('sha256').update(source).digest('hex'));
}
