#!/usr/bin/env node
/** Codex R3a: memory-only codec contracts; --baseline-stdin takes a JSON string
 * of independently retained pre-extraction toolLine/parse blocks.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createLogCodec } from '../src/convo-log/codec.js';

function run(factory, label) {
  const KIND_LABEL = { qa: '문답', daily: '일일 요약', empty: '빈 질문' };
  const trace = [];
  const api = factory({ KIND_LABEL, num: v => {
    trace.push(v); return Number(v || 0).toLocaleString('en-US');
  } });
  assert.deepEqual(trace, [], 'no formatting at construction');
  assert.equal(api.TOOL_LINE_PREFIX, '> **도구** ');
  const prefix = api.TOOL_LINE_PREFIX;
  assert.equal(api.parseToolLine(null), null);
  assert.equal(api.parseToolLine('not a tool line'), null);
  assert.deepEqual(api.parseToolLine(prefix), []);
  assert.equal(api.parseToolLine(prefix + 'bad fragment')[0].name, null);
  const zero = api.parseToolLine(prefix + api.toolLine({ name: 'search', input: { q: 'test' }, chars: 0 }))[0];
  assert.equal(zero.chars, 0);
  const unknown = api.parseToolLine(prefix + api.toolLine({ name: 'search', input: { q: 'test' } }))[0];
  assert.equal(unknown.chars, null);
  const cleaned = api.toolLine({ name: 'search', narrowedTo: '([])', widenedFrom: 'A(B)[C]' });
  assert.equal(cleaned, 'search() [넓힘: ABC]');
  const long = api.parseToolLine(prefix + api.toolLine({ name: 'read_document', input: { q: 'x'.repeat(81) } }))[0];
  assert.equal(long.truncated, true); assert.equal(long.args.length, 81);
  assert.equal(api.parseCostLine('> **소요** 0.0초 · 총액 미상').costUsd, null);
  assert.equal(api.parseCostLine('> **비용** model · 1.2초 · 약 $0.000').costUsd, 0);
  assert.deepEqual(api.parseEntryHeader('### 12:34 · user · channel · 문답 · ⚠️ 실패'),
    { time: '12:34', who: 'user', where: 'channel', kind: '문답', ok: false });
  KIND_LABEL.added = 'new · kind';
  assert.equal(api.parseEntryHeader('### 12:34 · user · channel · new · kind').kind, 'new · kind',
    'label object remains live, not copied');

  const results = [];
  for (const chars of [undefined, null, 0, 1, 12345]) {
    for (const value of ['', 'a(b), c → d', 'x'.repeat(80), 'x'.repeat(81), 'x → search(y)', 'line\nbreak', 0, false, { a: 1 }]) {
      for (const tail of [undefined, '([])', 'local(name)[a]', 'local']) {
        const tool = { name: 'search', input: { value }, chars, narrowedTo: tail, widenedFrom: tail };
        const before = structuredClone(tool);
        const line = api.toolLine(tool);
        assert.deepEqual(tool, before, 'renderer must not mutate tool');
        const parsed = api.parseToolLine(prefix + line);
        const multi = api.parseToolLine(prefix + line + ' → read_document(doc) 0자');
        assert.equal(api.toolLine(tool), line, 'deterministic render');
        results.push({ line, parsed, multi });
      }
    }
  }
  for (const line of [null, undefined, '', '### bad', '### 00:00 · user · 문답',
    '### 23:59 · user · with · dot · unknown', '### 12:34 · user · channel · 빈 질문',
    '### 12:34 · user · channel · 일일 요약 · ⚠️ 실패',
    '> **비용** model · 1.23초 · 약 $0.125', '> **소요** 0초', '> **소요** 총액 미상',
    '> **비용** old', '> **비용** model · 약 $1.2.3', '> **비용** model · 약 $0.0   ']) {
    results.push({ header: api.parseEntryHeader(line), cost: api.parseCostLine(line), tool: api.parseToolLine(line) });
  }
  // Separate factories must not borrow each other's labels or formatting policy.
  const other = factory({ KIND_LABEL: { other: 'other · kind' }, num: () => '99' });
  assert.equal(other.toolLine({ name: 'x', chars: 1 }), 'x() 99자');
  assert.equal(api.toolLine({ name: 'x', chars: 1 }), 'x() 1자');
  console.log('PASS ' + label + ': explicit contracts, ' + results.length + ' comparisons, factory isolation');
  return { results, trace };
}
const actual = run(createLogCodec, 'codec');
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8')); assert.equal(typeof source, 'string');
  const factory = deps => compileFunction(source + '\nreturn {toolLine, TOOL_LINE_PREFIX, parseToolLine, parseEntryHeader, parseCostLine};',
    ['KIND_LABEL', 'num'])(deps.KIND_LABEL, deps.num);
  assert.deepEqual(actual, run(factory, 'baseline'));
  console.log('PASS exact outputs and format calls; baseline SHA256=' + createHash('sha256').update(source).digest('hex'));
}
