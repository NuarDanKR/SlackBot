#!/usr/bin/env node
/**
 * Codex R2c: memory-only document access contracts. No config/archive imports,
 * writes, API or Git calls. --baseline-stdin accepts a JSON string containing
 * independently retained pre-extraction access blocks, not candidate source.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createDocumentAccess } from '../src/documents/access.js';

const deps = ['isPrivateChannel', 'normalizeChannel', 'canSeePrivateChannel', 'listProjects',
  'listDocuments', 'resolveProject', 'resolveDocument', 'matchesHiddenPrivate', 'BLOCKED_NOTE'];
const names = ['projectIsPrivate', 'projectPrivateChannel', 'maskProject', 'realProjects',
  'documentsFor', 'canSeeDoc', 'visibleProjects', 'resolveProjectFor', 'resolveDocumentFor'];
const PUB = { channels: [] }, MEMBER = { channels: ['secret'] }, OTHER = { channels: ['other'] }, FULL = { full: true };
const ALIAS = '_승인자료';
const clone = v => structuredClone(v);
function fixture(factory) {
  const trace = [], projects = ['public', 'secret/sub', 'other', 'empty'];
  const docs = [
    { project: 'public', slug: 'plain', private: false },
    { project: 'public', slug: 'broken', private: true, privateChannel: null },
    { project: 'secret/sub', slug: 'approved', private: false, privateChannel: 'secret' },
    { project: 'secret/sub', slug: 'hidden', private: true, privateChannel: 'secret' },
    { project: 'other', slug: 'other-hidden', private: true, privateChannel: 'other' },
  ];
  const log = (name, ...args) => trace.push([name, ...clone(args)]);
  const privateNames = new Set(['secret', 'other', 'empty']);
  const dependencies = {
    BLOCKED_NOTE: 'BLOCKED',
    normalizeChannel: s => { log('normalize', s); return String(s).replace(/^#/, ''); },
    isPrivateChannel: s => { log('private', s); return privateNames.has(s); },
    canSeePrivateChannel: (a, s) => { log('canSee', a, s); return !!(a?.full || a?.channels?.includes(s)); },
    listProjects: () => { log('projects'); return projects.slice(); },
    listDocuments: p => { log('documents', p); return docs.filter(d => p === undefined || d.project === p); },
    resolveProject: (input, all = projects) => {
      log('resolveProject', input, all);
      return all.includes(input) ? { ok: true, name: input } : { ok: false, candidates: all.slice() };
    },
    resolveDocument: (project, input, all) => {
      log('resolveDocument', project, input, all);
      const d = all.find(d => d.slug === input);
      return d ? { ok: true, doc: d } : { ok: false, candidates: all.map(d => d.slug) };
    },
    matchesHiddenPrivate: (input, a) => {
      log('hiddenName', input, a);
      return privateNames.has(input) && !(a?.full || a?.channels?.includes(input));
    },
  };
  const api = factory(dependencies);
  assert.deepEqual(trace, [], 'construction must not invoke facade callbacks');
  return { api, trace, docs, projects, privateNames, dependencies };
}
const cases = [
  ['private top-level and original normalization difference', f => {
    const a = f.api;
    assert.equal(a.projectIsPrivate('secret/sub'), true);
    assert.equal(a.projectPrivateChannel('secret/sub'), 'secret');
    assert.equal(a.projectIsPrivate('#secret/sub'), false, 'do not add normalization during extraction');
    assert.equal(a.projectPrivateChannel('#secret/sub'), 'secret');
    assert.equal(a.projectPrivateChannel('public'), null);
  }],
  ['public member other full missing permissions', f => {
    for (const [access, expected] of [[PUB, [true, false, true, false, false]],
      [MEMBER, [true, false, true, true, false]], [OTHER, [true, false, true, false, true]],
      [FULL, [true, true, true, true, true]], [undefined, [true, false, true, false, false]]]) {
      assert.deepEqual(f.docs.map(d => f.api.canSeeDoc(access, d)), expected);
    }
  }],
  ['masking and alias expansion', f => {
    for (const access of [PUB, OTHER, undefined]) assert.equal(f.api.maskProject('secret/sub', access), ALIAS);
    for (const access of [MEMBER, FULL]) assert.equal(f.api.maskProject('secret/sub', access), 'secret/sub');
    assert.equal(f.api.maskProject('public', PUB), 'public');
    assert.deepEqual(f.api.realProjects(ALIAS), ['secret/sub', 'other', 'empty']);
    assert.deepEqual(f.api.realProjects('public'), ['public']);
  }],
  ['alias document filtering keeps references and order', f => {
    const p = f.api.documentsFor(ALIAS, PUB);
    assert.deepEqual(p.map(d => d.slug), ['approved']); assert.equal(p[0], f.docs[2]);
    assert.deepEqual(f.api.documentsFor(ALIAS, MEMBER).map(d => d.slug), ['approved', 'hidden']);
    assert.deepEqual(f.api.documentsFor(ALIAS, OTHER).map(d => d.slug), ['approved', 'other-hidden']);
  }],
  ['visible names stable across alternating permissions', f => {
    for (const [access, expected] of [[PUB, ['public', ALIAS]], [MEMBER, ['public', 'secret/sub', ALIAS]],
      [OTHER, ['public', ALIAS, 'other']], [FULL, ['public', 'secret/sub', 'other', ALIAS]], [PUB, ['public', ALIAS]]]) {
      assert.deepEqual(f.api.visibleProjects(access), expected);
    }
  }],
  ['alias disappears when nothing is visible', f => {
    f.docs.splice(2, 1);
    assert.deepEqual(f.api.visibleProjects(PUB), ['public']);
    assert.deepEqual(f.api.documentsFor(ALIAS, PUB), []);
  }],
  ['project blocked vs absent vs visible alias', f => {
    assert.deepEqual(f.api.resolveProjectFor(ALIAS, MEMBER), { ok: true, name: ALIAS });
    assert.deepEqual(f.api.resolveProjectFor('secret/sub', PUB), { ok: false, error: 'BLOCKED' });
    assert.deepEqual(f.api.resolveProjectFor('empty', PUB), { ok: false, error: 'BLOCKED' });
    const r = f.api.resolveProjectFor('missing', PUB);
    assert.equal(r.error, "'missing' 사업장을 찾지 못했습니다. 후보: public, " + ALIAS);
  }],
  ['virtual document miss never scans hidden documents as fallback', f => {
    const r = f.api.resolveDocumentFor(ALIAS, 'hidden', PUB);
    assert.equal(r.error, ALIAS + " 에서 'hidden' 문서를 찾지 못했습니다. 후보: approved");
    const calls = f.trace.filter(x => x[0] === 'resolveDocument');
    assert.equal(calls.length, 1); assert.deepEqual(calls[0][3].map(d => d.slug), ['approved']);
    assert.equal(f.trace.filter(x => x[0] === 'documents').length, 3, 'one listing per real private project, no fallback scan');
    assert.ok(!r.error.includes('secret')); assert.ok(!r.error.includes('BLOCKED'));
  }],
  ['real document miss distinguishes blocked and absent', f => {
    assert.deepEqual(f.api.resolveDocumentFor('secret/sub', 'hidden', PUB), { ok: false, error: 'BLOCKED' });
    assert.equal(f.api.resolveDocumentFor('secret/sub', 'absent', PUB).error,
      "secret/sub 에서 'absent' 문서를 찾지 못했습니다. 후보: approved");
    const r = f.api.resolveDocumentFor('secret/sub', 'hidden', MEMBER);
    assert.equal(r.doc, f.docs[3], 'resolver result retains document reference');
  }],
  ['candidate limits and original ordering', f => {
    f.projects.splice(0, f.projects.length); f.docs.splice(0, f.docs.length);
    for (let i = 0; i < 18; i++) {
      const project = 'p' + i; f.projects.push(project);
      f.docs.push({ project, slug: 'd' + i, private: false });
    }
    const r = f.api.resolveProjectFor('absent', PUB);
    assert.equal(r.error.split('후보: ')[1], f.projects.slice(0, 15).join(', '));
    f.docs.forEach(d => { d.project = 'p0'; });
    const d = f.api.resolveDocumentFor('p0', 'absent', PUB);
    assert.equal(d.error.split('후보: ')[1], f.docs.slice(0, 15).map(x => x.slug).join(', '));
  }],
  ['dynamic lists and channel classification', f => {
    assert.equal(f.api.projectIsPrivate('later/sub'), false); f.privateNames.add('later');
    assert.equal(f.api.projectIsPrivate('later/sub'), true);
    f.projects.push('later/sub'); f.docs.push({ project: 'later/sub', slug: 'new', private: false });
    assert.ok(f.api.documentsFor(ALIAS, PUB).some(d => d.slug === 'new'));
  }],
  ['lookup exceptions are not swallowed', f => {
    // The factory captured the function, not a later replacement of the dependency object.
    const errorDeps = { ...f.dependencies, listProjects: () => { throw new Error('lookup failed'); } };
    const a = f.factory(errorDeps);
    assert.throws(() => a.visibleProjects(PUB), /lookup failed/);
  }],
];
function run(factory, label) {
  const results = [];
  for (const [name, check] of cases) {
    const f = fixture(factory); f.factory = factory; const original = clone(f.docs);
    check(f);
    results.push({ name, trace: f.trace, docs: clone(f.docs) });
    if (!['alias disappears when nothing is visible', 'candidate limits and original ordering',
      'dynamic lists and channel classification'].includes(name)) assert.deepEqual(f.docs, original);
    console.log('PASS ' + label + ': ' + name);
  }
  const a = fixture(factory), b = fixture(factory);
  a.docs.splice(0); assert.deepEqual(a.api.visibleProjects(PUB), []);
  assert.deepEqual(b.api.visibleProjects(PUB), ['public', ALIAS]);
  console.log('PASS ' + label + ': independent factory state');
  return results;
}
const actual = run(createDocumentAccess, 'access');
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8')); assert.equal(typeof source, 'string');
  const oldFactory = dependencies => compileFunction(source + '\nreturn { ' + names.join(', ') + ' };', deps)(
    ...deps.map(n => dependencies[n]));
  assert.deepEqual(actual, run(oldFactory, 'baseline'));
  console.log('PASS result assertions and exact lookup trace comparison; baseline SHA256=' +
    createHash('sha256').update(source).digest('hex'));
}
