#!/usr/bin/env node
/** R3g: failure reporting contracts with fake Slack, logging and cron boundaries.
 * --baseline-stdin accepts independently retained pre-extraction scheduler.js.
 * No actual timers, bot starts, disk fixtures or external service calls.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createSchedulerFailure } from '../src/scheduler/failure.js';
import { usageFields } from '../src/llm/usage.js';

const source = fs.readFileSync(new URL('../src/scheduler.js', import.meta.url), 'utf8');
const mapping = { daily: 'daily', weekly: 'weekly', health: 'health', healthPre: 'health',
  ingest: 'ingest', ingestPre: 'ingest' };
const accounting = { usage: { input_tokens: 10, output_tokens: 2 },
  byModel: [{ model: 'fixture-model' }], costUsd: 0.25, knownCostUsd: 0.25,
  complete: true, unknownAttempts: 0 };
function compile(source, deps) {
  return compileFunction(source.replace(/^import[\s\S]*?;\r?\n/gm, '').replace(/^export /gm, '') +
    '\nreturn {notifyFailure, onFailure, schedule, startScheduler, JOBS, LOG_KIND};',
    Object.keys(deps))(...Object.values(deps));
}
const reject = () => { throw new Error('unexpected operational boundary'); };
const direct = deps => {
  const LOG_KIND = { ...mapping };
  return { ...createSchedulerFailure({ ...deps, LOG_KIND }), LOG_KIND };
};
const wiring = source => deps => compile(source, {
  ...deps, createSchedulerFailure, cron: { validate: reject, schedule: reject },
  runDigest: reject, runHealth: reject, runIngest: reject, errLabel: reject,
});
const specs = [
  ...Object.keys(mapping).map(kind => ({ name: kind, kind })),
  { name: 'unknown kind', kind: 'new-kind' },
  { name: 'notification disabled', notify: false },
  { name: 'owner absent', owner: null },
  { name: 'owner ID absent', owner: { name: 'Owner' } },
  { name: 'open fails', failure: 'open' },
  { name: 'post fails', failure: 'post' },
  { name: 'direct open fails', method: 'notifyFailure', failure: 'open' },
  { name: 'direct post fails', method: 'notifyFailure', failure: 'post' },
  { name: 'direct notification', method: 'notifyFailure' },
  { name: 'direct missing owner', method: 'notifyFailure', owner: null },
  { name: 'custom target', target: '#fixture' },
  { name: 'empty target fallback', target: '' },
  { name: 'long reason', reason: 'a'.repeat(550) + '\n한글' },
  { name: 'numeric reason', reason: 0 },
  { name: 'undefined reason', noReason: true },
  { name: 'no accounting', noAccounting: true },
  { name: 'unknown accounting', accounting: { ...accounting, costUsd: null, complete: false, unknownAttempts: 2 } },
  { name: 'no request ID', noRequest: true },
  { name: 'Slack error precedence', failure: 'open', errorData: 'platform_error' },
  { name: 'logger failure does not block notification', failure: 'log' },
];
function snap(v) {
  if (v instanceof Error) return { name: v.name, message: v.message, data: snap(v.data) };
  if (Array.isArray(v)) return v.map(snap);
  if (v && typeof v === 'object') return Object.fromEntries(Object.entries(v).map(([k, x]) => [k, snap(x)]));
  return v;
}
async function run(make, label, { legacy = false } = {}) {
  const results = [];
  const NativeDate = globalThis.Date;
  globalThis.Date = class extends NativeDate {
    constructor(...args) { super(...(args.length ? args : ['2026-01-02T03:04:05Z'])); }
  };
  try {
    for (const spec of specs) {
      const trace = [], logs = [], posts = [];
      const config = { owner: spec.owner === undefined ? { name: 'Owner', slackUserId: 'U_OWNER' } : spec.owner,
        timezone: 'Asia/Seoul' };
      const err = new Error('synthetic failure');
      if (spec.errorData) err.data = { error: spec.errorData };
      const record = (type, ...args) => trace.push([type, ...args.map(snap)]);
      const deps = { config, usageFields: a => { record('usage', a); return usageFields(a); },
        logConversation(entry) { record('log', entry); if (spec.failure === 'log') throw err; logs.push(structuredClone(entry)); },
        console: { error(...args) { record('error', ...args); }, log: reject } };
      const api = make(deps);
      assert.deepEqual(trace, [], 'initialization has no I/O');
      const client = {
        conversations: { async open(args) {
          record('open', args); if (spec.failure === 'open') throw err;
          return { channel: { id: 'D_OWNER' } };
        } },
        chat: { async postMessage(args) {
          record('post', args); posts.push(structuredClone(args));
          if (spec.failure === 'post') throw err;
        } },
      };
      const kind = spec.kind || 'daily', reason = spec.noReason ? undefined : spec.reason ?? 'failure reason';
      const requestId = spec.noRequest ? undefined : 'request-fixture';
      const options = { errorType: 'fixture_error', requestId, context: 'prepared',
        attempts: [{ errorType: 'retry', waitMs: 5 }], elapsedMs: 123,
        origin: 'period', target: spec.target, notify: spec.notify ?? true,
        accounting: spec.noAccounting ? undefined : spec.accounting || accounting };
      const before = structuredClone({ config, options });
      let outcome;
      try {
        outcome = { value: spec.method === 'notifyFailure'
          ? await api.notifyFailure(client, kind, 'label', reason, requestId)
          : await api.onFailure(client, kind, 'label', reason, options) };
      } catch (error) { outcome = { error: snap(error) }; }
      assert.deepEqual({ config, options }, before, 'inputs are not mutated on first invocation');
      const directNotify = spec.method === 'notifyFailure';
      if (!directNotify) {
        assert.deepEqual(trace.slice(0, 2).map(t => t[0]), ['usage', 'log'], 'log before opening DM');
        if (spec.failure === 'log') {
          if (legacy) {
            assert.deepEqual(outcome.error, snap(err));
            assert.ok(!trace.some(t => t[0] === 'open'));
          } else {
            assert.equal(outcome.error, undefined);
            assert.ok(trace.some(t => t[0] === 'open'));
            assert.equal(logs.length, 0);
          }
        } else {
          assert.equal(outcome.error, undefined, 'notification errors are contained');
          assert.equal(logs.length, 1);
          const entry = logs[0];
          assert.equal(entry.kind, mapping[kind] || kind); assert.equal(entry.ok, false);
          assert.equal(entry.target, spec.target || (config.owner?.name + ' DM'));
          assert.equal(entry.error, String(reason).slice(0, 500));
          for (const key of ['errorType', 'requestId', 'context', 'attempts', 'elapsedMs', 'origin'])
            assert.deepEqual(entry[key], options[key]);
          if (options.accounting) {
            assert.equal(entry.costUsd, options.accounting.costUsd);
            assert.deepEqual(entry.accounting, options.accounting);
          } else assert.ok(!Object.hasOwn(entry, 'accounting'));
          if (spec.failure) {
            assert.deepEqual(trace.at(-1), ['error', '[' + kind + '] 실패 알림도 보내지 못했습니다:',
              spec.errorData || err.message]);
          }
        }
      } else {
        assert.equal(logs.length, 0);
        if (spec.failure) assert.deepEqual(outcome.error, snap(err));
        else assert.equal(outcome.error, undefined);
      }
      const suppressed = !config.owner?.slackUserId || (!directNotify && options.notify === false) || (legacy && spec.failure === 'log');
      if (suppressed) assert.ok(!trace.some(t => ['open', 'post'].includes(t[0])));
      else {
        assert.deepEqual(trace.find(t => t[0] === 'open')[1], { users: 'U_OWNER' });
        if (spec.failure !== 'open') {
          assert.equal(posts.length, 1);
          assert.equal(posts[0].channel, 'D_OWNER');
          assert.equal(posts[0].unfurl_links, false); assert.equal(posts[0].unfurl_media, false);
          assert.ok(posts[0].text.includes('사유: ' + reason));
          assert.ok(posts[0].text.includes('2026-01-02 12:04:05 (Asia/Seoul)'));
          assert.equal(posts[0].text.includes('요청 ID:'), !!requestId);
          assert.equal(posts[0].text.includes('npm run digest:'), kind === 'daily' || kind === 'weekly');
        }
      }
      results.push({ name: spec.name, outcome, trace });
    }
    // Existing config and mapping objects stay live, and factories stay separate.
    const trace = [];
    const config = { owner: { name: 'Before' }, timezone: 'UTC' };
    const deps = { config, usageFields, logConversation: e => trace.push(structuredClone(e)),
      console: { error: reject, log: reject } };
    const first = make(deps), second = make(deps);
    first.LOG_KIND.daily = 'changed';
    config.owner.name = 'After';
    await first.onFailure({}, 'daily', 'label', 'reason', { notify: false });
    await second.onFailure({}, 'daily', 'label', 'reason', { notify: false });
    assert.equal(trace[0].kind, 'changed'); assert.equal(trace[1].kind, 'daily');
    assert.ok(trace.every(e => e.target === 'After DM'));
    results.push({ name: 'policy references', trace });
    console.log('PASS ' + label + ': ' + results.length + ' failure-reporting scenarios');
    return results;
  } finally { globalThis.Date = NativeDate; }
}
async function integration(source, { legacy = false } = {}) {
  const results = [];
  const NativeDate = globalThis.Date;
  globalThis.Date = class extends NativeDate {
    constructor(...args) { super(...(args.length ? args : ['2026-01-02T03:04:05Z'])); }
    static now() { return Date.parse('2026-01-02T03:04:05Z'); }
  };
  try {
    for (const [kind, mode] of [['daily', 'returned'], ['daily', 'thrown'], ['ingest', 'returned'],
      ['ingest', 'missed'], ['health', 'healthy'],
      ...(!legacy ? [['ingest', 'sent'], ['ingest', 'thrown'], ['ingest', 'unknown'],
        ['ingest', 'logger-failed'], ['ingest', 'notify-failed'], ['ingestPre', 'returned']] : [])]) {
      const trace = [], handlers = {};
      let callback;
      const task = { on(name, fn) { handlers[name] = fn; }, getNextRun() { return null; } };
      const config = { timezone: 'Asia/Seoul', owner: { name: 'Owner', slackUserId: 'U_OWNER' },
        digest: { [kind]: { enabled: true, cron: '* * * * *' } } };
      const api = compile(source, {
        createSchedulerFailure, config, usageFields,
        cron: { validate: () => true, schedule(_, fn) { callback = fn; return task; } },
        runDigest: reject, runHealth: reject, runIngest: reject,
        errLabel: () => 'fixture_error',
        logConversation: e => {
          trace.push(['log', snap(e)]);
          if (mode === 'logger-failed') throw new Error('log failed');
        },
        console: { log() {}, error(...args) { trace.push(['console', ...args.map(snap)]); } },
      });
      api.JOBS[kind].run = async () => {
        if (mode === 'thrown') throw Object.assign(new Error('failed'), { hermesAccounting: accounting,
          hermesOrigin: 'period', requestID: 'request-fixture' });
        return { sent: mode === 'sent', reason: mode === 'healthy' ? 'healthy' : kind.startsWith('ingest') ? 'fatal' : 'generation-failed',
          error: mode === 'unknown' ? '보고 일부 전달 가능' : 'failed', accounting, origin: 'period',
          reportDelivery: { status: mode === 'sent' ? 'sent' : mode === 'unknown' ? 'unknown' : 'not-sent' } };
      };
      let resolvePosted;
      const posted = new Promise(resolve => { resolvePosted = resolve; });
      const client = { conversations: { async open() { trace.push(['open']); return { channel: { id: 'D' } }; } },
        chat: { async postMessage(args) {
          trace.push(['post', args]); resolvePosted();
          if (mode === 'notify-failed') throw new Error('post failed');
        } } };
      assert.equal(api.schedule(client, kind), task);
      if (mode === 'missed') {
        handlers['execution:missed']({ date: new Date('2026-01-02T00:00:00Z') });
        // No real timers: all fake client promises settle in the microtask queue.
        await posted;
      } else await callback();
      const logs = trace.filter(t => t[0] === 'log'), posts = trace.filter(t => t[0] === 'post');
      assert.equal(logs.length, mode === 'healthy' ? 0 : 1);
      assert.equal(posts.length, mode === 'healthy' || mode === 'sent' || (legacy && kind === 'ingest' && mode !== 'missed') ? 0 : 1);
      if (!legacy && kind.startsWith('ingest') && posts.length) {
        assert.ok(posts[0][1].text.includes('처리·보고 상태'));
        assert.ok(!posts[0][1].text.includes('이번 회차는 나가지 않았습니다'));
      }
      if (mode === 'unknown') assert.ok(posts[0][1].text.includes('일부 전달 가능'));
      if (logs.length && mode !== 'missed') assert.equal(logs[0][1].costUsd, 0.25);
      if (mode === 'missed') {
        assert.equal(logs[0][1].errorType, 'missed-execution'); assert.equal(logs[0][1].elapsedMs, 0);
      }
      if (posts.length) assert.ok(trace.findIndex(t => t[0] === 'log') < trace.findIndex(t => t[0] === 'open'));
      results.push({ kind, mode, trace });
    }
    console.log('PASS scheduler integration: returned/thrown/missed/self-reported/healthy paths');
    return results;
  } finally { globalThis.Date = NativeDate; }
}
const actual = await run(direct, 'failure factory');
assert.deepEqual(actual, await run(wiring(source), 'production facade'));
const integrated = await integration(source);
if (process.argv.includes('--baseline-stdin')) {
  const baseline = JSON.parse(fs.readFileSync(0, 'utf8'));
  assert.equal(typeof baseline, 'string');
  assert.ok(!baseline.includes('createSchedulerFailure'));
  // Intentional changes: ingest alert wording, logger error containment, and
  // fallback notifications when self-reporting did not confirm delivery.
  const changed = new Set(['ingest', 'ingestPre', 'logger failure does not block notification']);
  const previous = await run(wiring(baseline), 'pre-extraction baseline', { legacy: true });
  assert.deepEqual(actual.filter(x => !changed.has(x.name)), previous.filter(x => !changed.has(x.name)));
  const unchanged = x => x.kind !== 'ingest' && x.kind !== 'ingestPre';
  assert.deepEqual(integrated.filter(unchanged), (await integration(baseline, { legacy: true })).filter(unchanged));
  console.log('PASS exact payloads, log fields, errors and order; baseline SHA256=' +
    createHash('sha256').update(baseline).digest('hex'));
}
console.log('PASS actual cron timers, disk writes and external calls 0.');
