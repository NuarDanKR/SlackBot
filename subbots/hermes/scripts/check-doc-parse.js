#!/usr/bin/env node
/**
 * Codex R2a: deterministic parser contracts. No model/Slack/Git/file writes.
 * Default: compile the actual parser and archive splitter in memory.
 * --baseline-stdin: JSON string containing pre-extraction documents.js (or its
 * parser block), obtained independently, e.g. from the recorded pre-R2a commit.
 * --facade: additionally check actual ESM exports with synthetic config reads.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { compileFunction } from 'node:vm';
import { syncBuiltinESMExports } from 'node:module';
import { createHash } from 'node:crypto';

const root = fileURLToPath(new URL('../', import.meta.url));
const parserSource = fs.readFileSync(new URL('../src/documents/parse.js', import.meta.url), 'utf8');
const archiveSource = fs.readFileSync(new URL('../src/archive.js', import.meta.url), 'utf8');
const names = ['SHEET_RE', 'entryFileName', 'sheetsOf', 'sectionsOf'];
const header = archiveSource.match(/^const MESSAGE_HEADER = .*;$/m)?.[0];
const splitter = archiveSource.match(/^export function splitMessages\(text\) \{[\s\S]*?^\}/m)?.[0];
assert.ok(header && splitter, 'actual archive splitter boundaries must exist');
const splitMessages = compileFunction(header + '\n' + splitter.replace(/^export /, '') + '\nreturn splitMessages;')();
const importLine = "import { splitMessages } from '../archive.js';";
assert.equal(parserSource.split(importLine).length, 2, 'one archive dependency');
const body = parserSource.replace(importLine, '').replace(/^export /gm, '');
const candidate = compileFunction(body + '\nreturn { ' + names.join(', ') + ' };', ['splitMessages'])(splitMessages);

function baseline(source) {
  const a = source.indexOf('const SHEET_TAIL =');
  const b = source.indexOf('/* 절 목차를 내밀 최소 덮음률', a);
  assert.ok(a >= 0, 'baseline start exists');
  const block = source.slice(a, b < 0 ? undefined : b).replace(/^export /gm, '');
  return compileFunction(block + '\nreturn { ' + names.join(', ') + ' };', ['splitMessages'])(splitMessages);
}
const sheet = (n, name) => '**2026-01-02 · fixture.xlsx — 시트 ' + n + '/2: ' + name + '**\ncell';
const sheets = 'preamble\n' + sheet(1, 'First') + '\n' + sheet(2, 'Second');
const section = 'preface\n### 1. First\nbody\n### 3. Last\nend';
const cases = [
  ['sheet capture positions', p => {
    assert.deepEqual(sheet(1, 'First').match(p.SHEET_RE).slice(1), ['1', '2', 'First']);
    assert.equal(p.SHEET_RE.flags, '');
    assert.equal(p.SHEET_RE.test(sheet(1, 'First')), true);
    assert.equal(p.SHEET_RE.test(sheet(1, 'First')), true);
  }],
  ['reject malformed sheet header', p => {
    for (const s of ['**2026-01-02 fixture.xlsx — 시트 1/2: A**',
      '**2026-01-02 · fixture.xlsx - 시트 1/2: A**', '**2026-01-02 · file.pdf**']) {
      assert.equal(p.SHEET_RE.test(s), false);
    }
  }],
  ['attachment names', p => {
    assert.equal(p.entryFileName({ text: '**2026-01-02 · fixture.pdf**\ntext' }), 'fixture.pdf');
    assert.equal(p.entryFileName({ text: sheet(1, 'First') }), 'fixture.xlsx');
    assert.equal(p.entryFileName({ text: '**2026-01-02 ·  spaced name.pdf **' }), 'spaced name.pdf');
    assert.equal(p.entryFileName({ text: 'no header' }), undefined);
  }],
  ['sheet order and duplicates', p => {
    assert.deepEqual(p.sheetsOf(sheets), ['First', 'Second']);
    assert.deepEqual(p.sheetsOf(sheet(1, 'Same') + '\n' + sheet(2, 'Same')), ['Same', 'Same']);
    assert.deepEqual(p.sheetsOf('plain text'), []);
  }],
  ['section offsets with line-ending variants', p => {
    for (const eol of ['\n', '\r\n']) for (const tail of ['', eol]) {
      const text = section.replace(/\n/g, eol) + tail;
      const s = p.sectionsOf(text);
      assert.deepEqual(s, [
        { n: 1, name: 'First', start: text.indexOf('### 1.'), end: text.indexOf('### 3.') },
        { n: 3, name: 'Last', start: text.indexOf('### 3.'), end: text.length },
      ]);
      assert.ok(text.slice(s[1].start, s[1].end).includes('end'));
    }
  }],
  ['bold sections and date-like rejection', p => {
    const text = '**1. Alpha**\nx\n**26. 06. 09.**\nx\n**2. Beta**\ny';
    assert.deepEqual(p.sectionsOf(text).map(s => s.name), ['Alpha', 'Beta']);
  }],
  ['mixed increasing headings', p => {
    assert.deepEqual(p.sectionsOf('### 1. A\nx\n**2. B**\nx\n### 3. C\nx\n**4. D**').map(s => s.n), [1, 2, 3, 4]);
  }],
  ['duplicate and decreasing headings', p => {
    for (const text of ['### 1. A\n### 1. B', '### 2. A\n### 1. B', '### 1. A', 'plain', '']) {
      assert.equal(p.sectionsOf(text), null);
    }
  }],
  ['ambiguous union rejected', p => {
    assert.equal(p.sectionsOf('**1. Outer**\n### 1. Inner\n### 2. Inner\n**2. Outer**\n### 3. Inner\n### 4. Inner'), null);
  }],
  ['usable pattern survives unusable other pattern', p => {
    assert.deepEqual(p.sectionsOf('### 1. A\n**1. inner**\n### 2. B\n**1. inner**').map(s => s.name), ['A', 'B']);
  }],
];
function run(p, label) {
  for (const [name, check] of cases) { check(p); console.log('PASS ' + label + ': ' + name); }
}
run(candidate, 'memory');
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.equal(typeof source, 'string');
  const old = baseline(source);
  run(old, 'baseline');
  const inputs = [sheets, section, '', 'plain', '**1. A**\n**2. B**',
    '### 1. A\n### 1. B', '### 1. A\n**2. B**\n### 3. C\n**4. D**'];
  // Exhaustively vary numbering and heading styles, including malformed unions.
  for (const a of [1, 2, 3]) for (const b of [1, 2, 3]) for (const c of [1, 2, 3])
    for (const style of [0, 1, 2, 3, 4, 5, 6, 7]) {
      inputs.push([a, b, c].map((n, i) => (style & (1 << i))
        ? '**' + n + '. Name' + i + '**\nbody' : '### ' + n + '. Name' + i + '\nbody').join('\n'));
    }
  for (const input of inputs) for (const eol of ['\n', '\r\n']) for (const tail of ['', eol]) {
    const text = input.replace(/\n/g, eol) + tail;
    assert.deepEqual(candidate.sectionsOf(text), old.sectionsOf(text));
    assert.deepEqual(candidate.sheetsOf(text), old.sheetsOf(text));
    assert.equal(candidate.entryFileName({ text }), old.entryFileName({ text }));
    assert.deepEqual(text.match(candidate.SHEET_RE), text.match(old.SHEET_RE));
  }
  console.log('PASS baseline differential: ' + inputs.length * 4 + ' inputs; source SHA256=' +
    createHash('sha256').update(source).digest('hex'));
}
if (process.argv.includes('--facade')) {
  // Intercept only config and .env reads; source files still use the real loader.
  // No fixture directory is created. Never import the facade before this guard.
  const savedRead = fs.readFileSync, savedFetch = globalThis.fetch;
  const keys = ['HERMES_DATA_ROOT', 'HERMES_DOCS_DIR'];
  const savedEnv = Object.fromEntries(keys.map(k => [k, process.env[k]]));
  const fixtureRoot = path.join(root, '__codex_memory_config__');
  const configPath = path.join(fixtureRoot, 'config.json');
  const envPath = path.join(root, '.env');
  let configReads = 0, envReads = 0;
  process.env.HERMES_DATA_ROOT = fixtureRoot;
  process.env.HERMES_DOCS_DIR = path.join(fixtureRoot, 'documents');
  fs.readFileSync = function(file, options) {
    const filename = file instanceof URL ? fileURLToPath(file) : file;
    const resolved = typeof filename === 'string' ? path.resolve(filename) : null;
    let text;
    if (resolved === configPath) {
      configReads++;
      text = JSON.stringify({ archivePath: 'slack-export', documentsPath: 'documents', limits: {} });
    } else if (resolved === envPath) { envReads++; text = ''; }
    if (text !== undefined) {
      const encoding = typeof options === 'string' ? options : options?.encoding;
      return encoding ? text : Buffer.from(text);
    }
    return savedRead.apply(this, arguments);
  };
  globalThis.fetch = () => { throw new Error('Network forbidden in parser test'); };
  syncBuiltinESMExports();
  try {
    const parsed = await import('../src/documents/parse.js');
    const facade = await import('../src/documents.js');
    for (const name of names) assert.equal(facade[name], parsed[name], name + ' same binding');
    assert.equal(configReads, 1); assert.equal(envReads, 1);
    run(facade, 'ESM facade');
    assert.deepEqual(facade.outlineOf(sheets).pieces.map(p => p.name), ['First', 'Second']);
    console.log('PASS actual ESM facade and outline consumer with synthetic config');
  } finally {
    fs.readFileSync = savedRead; globalThis.fetch = savedFetch;
    for (const key of keys) {
      if (savedEnv[key] === undefined) delete process.env[key]; else process.env[key] = savedEnv[key];
    }
    syncBuiltinESMExports();
  }
}
