#!/usr/bin/env node
/** R3j: offline period contracts, memory-only digest state and frozen clock.
 * --baseline-stdin accepts independently retained slack-live.js as a JSON string.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createSlackLiveWindow } from '../src/slack/live-window.js';
import { createSlackLiveRender } from '../src/slack/live-render.js';
import { createSlackLiveApi } from '../src/slack/live-api.js';
const source = fs.readFileSync(new URL('../src/slack-live.js', import.meta.url), 'utf8');
const names = ['dailyWindow', 'weeklyWindow', 'recentWindow'];
const ROOT = '/fixture';
const stateFile = path.join(ROOT, 'logs', 'digest-state.json');
const fixedMs = Date.parse('2024-01-03T12:00:00.789Z');
const latest = Math.floor(fixedMs / 1000);
const H = 3600;
function facade(text, deps) {
  const injected = { ...deps, ROOT, path, FULL_ACCESS: {},
    listAllChannels: () => [], isPrivateChannel: () => false, canSeePrivateChannel: () => false,
    normalizeChannel: x => x, redactPrivateMentions: x => x, BOT_ANSWER_MARK: '(bot omitted)',
    createSlackLiveApi: opts => createSlackLiveApi({ console: deps.console, ...opts }),
    createSlackLiveRender: opts => createSlackLiveRender({ console: deps.console, ...opts }),
    createSlackLiveWindow: opts => createSlackLiveWindow({ console: deps.console, ...opts }) };
  const code = text.replace(/^import\s[\s\S]*?;\r?\n/gm, '')
    .replace(/^export \{[^}]*\};?\r?$/gm, '').replace(/^export /gm, '');
  return compileFunction(code + '\nreturn {' + [...names, 'markDailyDigestSent'].join(',') + '};',
    Object.keys(injected))(...Object.values(injected));
}
function direct(deps) {
  const stamp = createSlackLiveRender({ listAllChannels: () => [] }).stamp;
  return createSlackLiveWindow({ ...deps, stamp, DEFAULT_LIVE_FETCH_MAX_DAYS: 14,
    readDailySentThrough() {
      try {
        const v = Number(JSON.parse(deps.fs.readFileSync(stateFile, 'utf8')).dailySentThrough);
        return Number.isFinite(v) && v > 0 ? v : null;
      } catch { return null; }
    } });
}
function run(make) {
  const results = [];
  function fixture() {
    const trace = [], files = new Map();
    const state = { fail: null };
    const config = { timezone: 'UTC', limits: {}, digest: { daily: {} } };
    function record(op, ...args) {
      trace.push([op, ...args]);
      if (state.fail === op) throw new Error('fixture ' + op);
    }
    const fakeFs = {
      readFileSync(file, encoding) {
        record('read', file, encoding);
        if (!files.has(file)) throw new Error('missing');
        return files.get(file);
      },
      mkdirSync(...args) { record('mkdir', ...args); },
      writeFileSync(file, value, encoding) { record('write', file, value, encoding); files.set(file, value); },
      renameSync(from, to) { record('rename', from, to); files.set(to, files.get(from)); files.delete(from); },
    };
    const console = {
      warn: (...args) => trace.push(['warn', ...args]),
      error: (...args) => trace.push(['error', ...args]),
    };
    const api = make({ config, fs: fakeFs, console });
    assert.deepEqual(trace, [], 'no state reads at creation');
    return { api, trace, files, state, config };
  }
  function check(name, fn) {
    const f = fixture();
    const value = fn(f);
    results.push({ name, value: structuredClone(value), trace: structuredClone(f.trace),
      files: [...f.files] });
  }
  for (const [label, sent, expectedHours, capped] of [
    ['no prior send', null, 24, null],
    ['normal send', latest - 24 * H, 24, null],
    ['failed cycle', latest - 48 * H, 48, null],
    ['over cap', latest - 72 * H, 48, latest - 72 * H],
    ['future state', latest + H, 24, null],
    ['state at now', latest, 24, null],
    ['one second before now', latest - 1, 1 / H, null],
    ['zero state', 0, 24, null],
  ]) {
    check('daily ' + label, f => {
      const w = f.api.dailyWindow(new Date(fixedMs), 'UTC', 24, { sentThrough: sent, maxHours: 48 });
      assert.equal(w.latest, latest);
      assert.equal(w.oldest, latest - expectedHours * H);
      assert.equal(w.sentThrough, sent);
      assert.equal(w.cappedFrom, capped);
      assert.equal(f.trace.filter(x => x[0] === 'read').length, 0);
      assert.equal(f.trace.filter(x => x[0] === 'warn').length, capped === null ? 0 : 1);
      return w;
    });
  }
  check('daily warning exact', f => {
    const w = f.api.dailyWindow(new Date(fixedMs), 'UTC', 24, { sentThrough: latest - 72 * H });
    assert.equal(w.label, '2024-01-01 12:00 ~ 2024-01-03 12:00');
    assert.deepEqual(f.trace, [['warn',
      '[digest] 못 보낸 구간이 상한(48시간)을 넘어 2023-12-31 12:00 ~ 2024-01-01 12:00 는 이번 요약에 안 실립니다 (digest.daily.maxHours).']]);
    return w;
  });
  check('configured cap and mutation stay live', f => {
    f.config.digest.daily.maxHours = 72;
    const first = f.api.dailyWindow(undefined, undefined, undefined, { sentThrough: latest - 100 * H });
    assert.equal(first.oldest, latest - 72 * H);
    f.config.digest.daily.maxHours = 36;
    const second = f.api.dailyWindow(undefined, undefined, undefined, { sentThrough: latest - 100 * H });
    assert.equal(second.oldest, latest - 36 * H);
    return [first, second];
  });
  check('hours exceed maxHours', f => {
    const w = f.api.dailyWindow(undefined, 'UTC', 60, { sentThrough: latest - 72 * H, maxHours: 48 });
    assert.equal(w.oldest, latest - 60 * H);
    return w;
  });
  for (const [label, raw, sent] of [
    ['missing', undefined, null], ['malformed', '{bad', null],
    ['null json', 'null', null], ['empty object', '{}', null],
    ['zero', '{"dailySentThrough":0}', null],
    ['negative', '{"dailySentThrough":-1}', null],
    ['nonnumeric', '{"dailySentThrough":"bad"}', null],
    ['numeric string', JSON.stringify({ dailySentThrough: String(latest - 30 * H) }), latest - 30 * H],
    ['valid', JSON.stringify({ dailySentThrough: latest - 30 * H }), latest - 30 * H],
  ]) {
    check('default state ' + label, f => {
      if (raw !== undefined) f.files.set(stateFile, raw);
      const w = f.api.dailyWindow();
      assert.equal(w.sentThrough, sent);
      assert.equal(w.oldest, sent ?? latest - 24 * H);
      assert.deepEqual(f.trace, [['read', stateFile, 'utf8']]);
      return w;
    });
  }
  check('read failure and explicit null bypass', f => {
    f.state.fail = 'read';
    const a = f.api.dailyWindow();
    const b = f.api.dailyWindow(undefined, undefined, undefined, { sentThrough: null });
    assert.deepEqual(a, b);
    assert.equal(f.trace.length, 1);
    return a;
  });
  for (const [date, tz, start] of [
    ['2024-01-01T00:00:00.999Z', 'UTC', '2024-01-01T00:00:00Z'],
    ['2024-01-07T23:59:59.999Z', 'UTC', '2024-01-01T00:00:00Z'],
    ['2024-01-07T15:00:00.001Z', 'Asia/Seoul', '2024-01-07T15:00:00Z'],
    ['2024-02-29T12:34:56.789Z', 'UTC', '2024-02-26T00:00:00Z'],
    ['2023-01-01T12:00:00Z', 'UTC', '2022-12-26T00:00:00Z'],
  ]) {
    check('weekly ' + date + ' ' + tz, f => {
      const w = f.api.weeklyWindow(new Date(date), tz);
      assert.equal(w.oldest, Date.parse(start) / 1000);
      assert.equal(w.latest, Math.floor(Date.parse(date) / 1000));
      assert.equal(f.trace.length, 0);
      return w;
    });
  }
  for (const days of [0, -2, 1, 2, 14, 999]) {
    check('recent clamp ' + days, f => {
      const w = f.api.recentWindow(days);
      const capped = Math.min(14, Math.max(1, days));
      assert.equal(w.days, capped);
      assert.equal(w.oldest, Date.parse('2024-01-03T00:00:00Z') / 1000 - (capped - 1) * 86400);
      assert.equal(w.latest, latest);
      return w;
    });
  }
  check('recent timezone midnight and milliseconds', f => {
    const a = f.api.recentWindow(1, new Date('2024-01-02T15:00:00.999Z'), 'Asia/Seoul');
    const b = f.api.recentWindow(1, new Date('2024-01-02T15:00:00Z'), 'Asia/Seoul');
    assert.deepEqual(a, b);
    assert.equal(a.oldest, Date.parse('2024-01-02T15:00:00Z') / 1000);
    return a;
  });
  check('config timezone and live fetch cap remain live', f => {
    f.config.timezone = 'Asia/Seoul';
    f.config.limits.liveFetchMaxDays = 3;
    const a = f.api.recentWindow(999);
    assert.equal(a.days, 3);
    f.config.limits.liveFetchMaxDays = 7;
    const b = f.api.recentWindow(999);
    assert.equal(b.days, 7);
    return [a, b, f.api.weeklyWindow()];
  });
  check('absent optional config uses defaults', f => {
    delete f.config.digest;
    delete f.config.limits;
    const a = f.api.dailyWindow(undefined, undefined, undefined, { sentThrough: latest - 72 * H });
    assert.equal(a.oldest, latest - 48 * H);
    assert.equal(f.api.recentWindow(999).days, 14);
    return a;
  });
  check('invalid timezone errors preserved', f => {
    for (const call of [
      () => f.api.dailyWindow(undefined, 'Fixture/Invalid', 24, { sentThrough: null }),
      () => f.api.weeklyWindow(undefined, 'Fixture/Invalid'),
      () => f.api.recentWindow(1, undefined, 'Fixture/Invalid'),
    ]) assert.throws(call, RangeError);
  });
  if (make.integration) {
    check('successful state recording feeds next window', f => {
      assert.equal(f.api.markDailyDigestSent(latest - 30 * H), true);
      assert.equal(f.api.dailyWindow().oldest, latest - 30 * H);
      assert.deepEqual(f.trace.map(x => x[0]), ['mkdir', 'write', 'rename', 'read']);
      assert.equal(f.files.get(stateFile), JSON.stringify({ dailySentThrough: latest - 30 * H }, null, 2) + '\n');
      assert.equal(f.files.has(stateFile + '.tmp'), false);
      return f.api.dailyWindow();
    });
    for (const op of ['mkdir', 'write', 'rename']) {
      check('record failure preserves previous sent state ' + op, f => {
        f.files.set(stateFile, JSON.stringify({ dailySentThrough: latest - 40 * H }));
        f.state.fail = op;
        assert.equal(f.api.markDailyDigestSent(latest - H), false);
        assert.equal(f.api.dailyWindow().oldest, latest - 40 * H);
        assert.equal(f.trace.filter(x => x[0] === 'error').length, 1);
        return f.api.dailyWindow();
      });
    }
    check('invalid sent values do not write', f => {
      for (const value of [null, 0, -1, NaN, Infinity, 'bad']) {
        assert.equal(f.api.markDailyDigestSent(value), false);
      }
      assert.deepEqual(f.trace, []);
    });
  }
  return results;
}
const NativeDate = globalThis.Date;
globalThis.Date = class extends NativeDate {
  constructor(...args) { super(...(args.length ? args : [fixedMs])); }
  static now() { return fixedMs; }
};
try {
  const directResult = run(direct);
  const via = deps => facade(source, deps);
  via.integration = true;
  const actual = run(via);
  assert.deepEqual(actual.slice(0, directResult.length), directResult, 'module vs full facade');
  if (process.argv.includes('--baseline-stdin')) {
    const old = JSON.parse(fs.readFileSync(0, 'utf8'));
    assert.equal(typeof old, 'string');
    const baseline = deps => facade(old, deps);
    baseline.integration = true;
    assert.deepEqual(run(baseline), actual, 'independent baseline vs candidate');
    console.log('Baseline SHA256: ' + createHash('sha256').update(old).digest('hex'));
  }
  console.log('Slack live windows: ' + directResult.length + ' contracts + ' +
    (actual.length - directResult.length) + ' state integration cases passed.');
} finally { globalThis.Date = NativeDate; }
