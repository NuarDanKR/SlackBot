#!/usr/bin/env node
/** Codex R3b: memory-only log statistics contracts.
 * --baseline-stdin accepts a JSON string of independently retained stats bodies.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createLogStats } from '../src/convo-log/stats.js';
function run(factory, label) {
  const BROADCAST = new Set(['daily', 'weekly', 'health', 'ingest']);
  const DIAG_FAILED = new Set(['previous_message_not_found']);
  const NO_BASELINE = '(대조 기준 없음)';
  /* 「집계 분모에 넣나」 판정. **원본은 파사드(`src/convo-log.js`)에 하나뿐**이고, 여기서
   * 따로 만드는 것은 이 검사가 **자기 정책 집합**을 쓰기 때문이다(아래에서 `BROADCAST` 에
   * 값을 더해 참조 공유를 확인한다 — 파사드 것을 가져오면 그걸 못 잰다).
   * 그래서 여기 적힌 것이 맞나는 아래 「사용량 기록은 어느 분모에도 안 든다」가 본다. */
  const isOperational = (e) => !e?.accountingOnly;
  const isQaRound = (e) => isOperational(e) && !BROADCAST.has(e?.kind);
  const isBroadcastRound = (e) => isOperational(e) && BROADCAST.has(e?.kind);
  const api = factory({ BROADCAST, DIAG_FAILED, NO_BASELINE, isQaRound, isBroadcastRound });
  const results = [];
  const probe = (entries, { changed = false } = {}) => {
    const before = structuredClone(entries);
    const r = {
      changed,
      failure: api.failureStats(entries), lines: api.failureSectionLines(entries), cache: api.cacheStats(entries),
    };
    assert.deepEqual(entries, before, 'no mutation of caller entries');
    assert.deepEqual(api.failureStats(entries), r.failure, 'repeat stability');
    results.push(r); return r;
  };
  assert.deepEqual(probe([]).failure, { broadcastTotal: 0, failedTotal: 0, retriedTotal: 0, rows: [] });
  const excluded = probe([{ kind: 'ingest', ok: false, accountingOnly: true }, { kind: 'qa', ok: false }]);
  assert.equal(excluded.failure.broadcastTotal, 0); assert.deepEqual(excluded.lines, []);
  const failures = probe([
    { kind: 'daily', ok: false, errorType: 'second' },
    { kind: 'weekly', ok: false, errorType: 'first' },
    { kind: 'health', ok: false },
    { kind: 'ingest', ok: true, attempts: [{ errorType: 'retry' }, {}] },
  ]);
  assert.deepEqual(failures.failure.rows.map(r => r.type), ['second', 'first', '(기타)', 'retry']);
  assert.equal(failures.failure.retriedTotal, 1);
  assert.match(failures.lines.join('\n'), /전체 회차 수는 기록하지 않습니다/);
  const unknown = probe([
    { kind: 'qa' },
    { kind: 'qa', cacheMiss: [] },
    { kind: 'qa', cacheMiss: [], cacheBaseline: false },
    { kind: 'qa', cacheMiss: [{ type: 'previous_message_not_found', count: 1, tokens: 0 }] },
    { kind: 'qa', cacheMiss: [{ type: 'new_reason', count: 1, tokens: 10 }] },
  ]).cache;
  assert.equal(unknown.qaMeasured, 4); assert.equal(unknown.qaUnmeasured, 1);
  assert.equal(unknown.withMiss, 1); assert.equal(unknown.withUnknown, 2);
  assert.equal(unknown.noBaseline, 1);
  assert.equal(unknown.rows[0].type, 'new_reason');
  const overlap = probe([{ kind: 'qa', cacheBaseline: false, cacheMiss: [
    { type: 'new_reason', count: 2, tokens: 20 },
    { type: 'previous_message_not_found', count: 1, tokens: 0 },
  ] }]).cache;
  assert.equal(overlap.withMiss, 1); assert.equal(overlap.withUnknown, 1);
  const broadcast = probe([{ kind: 'daily', cacheMiss: [{ type: 'new_reason', count: 3, tokens: 30 }] }]).cache;
  assert.equal(broadcast.qaMeasured, 0); assert.equal(broadcast.rows[0].n, 3);
  /* **사용량 기록은 어느 분모에도 안 든다 — 모든 kind 에 대해.**
   *
   * 전에는 이 자리가 `qaMeasured === 1` 을 못 박고 있었다. `accountingOnly` 를 만드는 자리는
   * `src/ingest/index.js` 한 곳이고 거기 `kind` 가 `'ingest'` 라 `BROADCAST` 검사에 먼저
   * 걸렸기 때문인데, 그건 **가려져 있던 것이지 맞게 적힌 것이 아니었다.** broadcast 아닌
   * kind 로 한 번만 쓰면 그 회차가 캐시 분모에 들어가 「쟀다」로 세어지고 에러 없이 비율만
   * 묽어진다. `cacheStats` 한 곳만 `accountingOnly` 를 안 빼고 있었다 (2026-09-16).
   *
   * **한 자리를 못 박는 대신 규칙을 못 박는다** — 자리가 하나 더 생겨도 여기서 잡힌다. */
  for (const kind of ['qa', 'daily', 'weekly', 'health', 'ingest', 'unknown-kind']) {
    const only = probe([{ kind, accountingOnly: true, cacheMiss: [], ok: false, attempts: [{}, {}] }],
      { changed: true }).cache;
    assert.equal(only.qaMeasured, 0, `accountingOnly must not be measured: ${kind}`);
    assert.equal(only.qaUnmeasured, 0, `accountingOnly must not be unmeasured either: ${kind}`);
    const f = probe([{ kind, accountingOnly: true, ok: false }], { changed: true }).failure;
    assert.equal(f.broadcastTotal, 0, `accountingOnly must not be a broadcast round: ${kind}`);
    assert.equal(f.failedTotal, 0, `accountingOnly must not count as a failure: ${kind}`);
  }
  const reasons = [undefined, [], [{ type: 'same', count: 1, tokens: 10 }],
    [{ type: 'previous_message_not_found', count: 1, tokens: 0 }],
    [{ type: 'same', count: 0, tokens: 0 }, { type: 'new', count: 2, tokens: 100 }]];
  for (const kind of ['qa', 'empty', 'daily', 'weekly', 'health', 'ingest', 'future']) {
    for (const ok of [undefined, true, false]) {
      for (const cacheMiss of reasons) {
        for (const cacheBaseline of [undefined, true, false]) {
          probe([{ kind, ok, cacheMiss, cacheBaseline, attempts: [{ errorType: 'retry' }, {}] }]);
        }
      }
    }
  }
  BROADCAST.add('future');
  assert.equal(api.failureStats([{ kind: 'future', ok: false }]).failedTotal, 1, 'same policy set reference');
  DIAG_FAILED.add('new_reason');
  assert.equal(api.cacheStats([{ kind: 'qa', cacheMiss: [{ type: 'new_reason' }] }]).withUnknown, 1);
  const otherCast = new Set();
  const other = factory({
    BROADCAST: otherCast, DIAG_FAILED: new Set(), NO_BASELINE: 'other',
    isQaRound: (e) => isOperational(e) && !otherCast.has(e?.kind),
    isBroadcastRound: (e) => isOperational(e) && otherCast.has(e?.kind),
  });
  assert.equal(other.failureStats([{ kind: 'daily', ok: false }]).failedTotal, 0);
  assert.equal(api.failureStats([{ kind: 'daily', ok: false }]).failedTotal, 1);
  console.log('PASS ' + label + ': ' + results.length + ' result sets, explicit contracts and policy isolation');
  return results;
}
const actual = run(createLogStats, 'stats');
if (process.argv.includes('--baseline-stdin')) {
  const source = JSON.parse(fs.readFileSync(0, 'utf8')); assert.equal(typeof source, 'string');
  const factory = d => compileFunction(source + '\nreturn {failureStats, failureSectionLines, cacheStats};',
    ['BROADCAST','DIAG_FAILED','NO_BASELINE'])(d.BROADCAST,d.DIAG_FAILED,d.NO_BASELINE);
  /* `changed: true` 로 표시한 probe 는 뺀다 — 2026-09-16 에 `cacheStats` 가
   * `accountingOnly` 를 빼도록 **일부러 바꾼** 자리라, 옛 본문은 그 값을 낼 수 없다.
   * 그 자리의 계약은 바로 위 반복문이 따로 못 박는다. 나머지는 그대로 대진다 —
   * 「다르니까 뺀다」가 아니라 **「어디가 왜 다른지 적고 그것만 뺀다」**이다. */
  const keep = (rs) => rs.filter((r) => !r.changed);
  assert.deepEqual(keep(actual), keep(run(factory, 'baseline')));
  console.log('PASS exact result/wording/order equality; baseline SHA256=' + createHash('sha256').update(source).digest('hex'));
}
