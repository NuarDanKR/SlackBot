#!/usr/bin/env node
/** R3k: report contracts and health runner wiring using fake data and clients.
 * --baseline-stdin accepts independently retained archive-health.js as a JSON string.
 * No real archive/config imports, Git commands, filesystem writes or Slack calls.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { compose } from '../src/archive-health/report.js';
import { createHealthStatus } from '../src/archive-health/status.js';
import { createHealthDocFilters } from '../src/archive-health/doc-filters.js';
import { createHealthAttachments } from '../src/archive-health/attachments.js';
import { createHealthDocuments } from '../src/archive-health/documents.js';
const source = fs.readFileSync(new URL('../src/archive-health.js', import.meta.url), 'utf8');
const S = { scanDays: 60, syncWarnDays: 7, syncCriticalDays: 12, docWarnCount: 1, liveFetchMaxDays: 14 };
const lag = { days: 0, lastSync: '2024-01-01' };
const docs = n => ({ total: n, scanDays: 60, byChannel: [], failed: [], approvals: [], restricted: [], deferred: [] });
const fresh = { ok: true, head: 'abc123', headAt: '10:00', checkedAt: '10:01', behind: 0, reason: null };
const clone = x => structuredClone(x);
function facade(text, f = {}) {
  const forbidden = () => { throw new Error('unexpected dependency'); };
  const config = f.config || { limits: {}, digest: {} };
  const deps = { fs: new Proxy({}, { get: () => forbidden }), path, config,
    ARCHIVE_DIR: '/fixture/archive', DOCS_DIR: '/fixture/docs',
    isPrivateChannel: () => false,
    // 여기서는 채널 목록을 훑지 않는다(listBotChannels 가 forbidden) — 항등으로 채운다.
    // skip 채널을 실제로 거르는지는 check-health-documents.js·check-health-attachments.js 가 잰다.
    dropSkippedChannels: chs => chs,
    listBotChannels: forbidden, fetchAllReplies: forbidden, isBotMessage: () => false,
    DEFAULT_LIVE_FETCH_MAX_DAYS: 14, chunkForSlack: f.chunkForSlack || forbidden,
    logConversation: f.logConversation || forbidden, SKILL_SCRIPTS: {},
    runScript: forbidden, activeDeferred: forbidden, suppress: forbidden,
    syncForRead: f.syncForRead || forbidden, compose,
    console: f.console || { log: forbidden }, replacements: f.replacements || null,
    createHealthDocFilters, createHealthAttachments,
    documentsExtracted: text.includes('createHealthDocuments'),
    createHealthDocuments: opts => f.replacements ? { pendingDocuments: f.replacements.pendingDocuments } : createHealthDocuments(opts),
    statusExtracted: text.includes('createHealthStatus'),
    createHealthStatus: opts => f.replacements ? {
      syncLag: f.replacements.syncLag, stalePendingEdits: f.replacements.stalePendingEdits,
      stalePendingWork: f.replacements.stalePendingWork,
    } : createHealthStatus(opts) };
  const code = text.replace(/^import\s[\s\S]*?;\r?\n/gm, '')
    .replace(/^export \{[^}]*\};?\r?$/gm, '').replace(/^export /gm, '');
  return compileFunction(code + `
if (replacements) {
  if (!statusExtracted) syncLag = replacements.syncLag;
  if (!documentsExtracted) pendingDocuments = replacements.pendingDocuments;
  if (!statusExtracted) stalePendingEdits = replacements.stalePendingEdits;
  if (!statusExtracted) stalePendingWork = replacements.stalePendingWork;
  archiveIssues = replacements.archiveIssues;
}
return { compose, runHealth };
`, Object.keys(deps))(...Object.values(deps));
}
function reports(fn) {
  const out = [];
  function check(name, args, verify) {
    const before = clone(args);
    const value = fn(...args);
    assert.deepEqual(args, before, name + ': input mutation');
    verify?.(value);
    out.push({ name, value });
  }
  check('healthy is silent', [lag, docs(0), S], value => assert.equal(value, null));
  check('fresh metadata alone is silent', [lag, docs(0), S, { freshness: fresh }], value => assert.equal(value, null));
  check('deferred alone is silent', [lag, { ...docs(0), deferred: [{ until: '2024-02-01' }] }, S],
    value => assert.equal(value, null));
  check('missing state exact', [{ missing: true }, docs(0), S],
    value => assert.equal(value, '🗂 *아카이브 위생 점검*\n\n⚠️ `.sync-state.json` 을 읽지 못했습니다 — 대화 동기화 상태를 알 수 없습니다.'));
  for (const preDigest of [false, true]) {
    for (const days of [6, 7, 11, 12]) {
      for (const total of [0, 1]) {
        for (const freshness of [null, fresh, { ...fresh, ok: false, reason: 'fixture failure', behind: null }]) {
          check('matrix ' + JSON.stringify({ preDigest, days, total, freshness }),
            [{ ...lag, days }, docs(total), S, { preDigest, freshness }], value => {
              const shouldSend = (!preDigest && days >= 7) || total >= 1 || freshness?.ok === false;
              assert.equal(value !== null, shouldSend);
              if (value && preDigest) assert.ok(!value.includes('대화 동기화가'));
              if (value && freshness?.ok) assert.ok(value.endsWith('기준: HEAD abc123 · 10:01 최신화'));
            });
        }
      }
    }
  }
  for (const behind of [0, 3, null]) {
    check('unsynced is not failure ' + behind,
      [lag, docs(0), S, { preDigest: true, freshness: { ...fresh, ok: false, behind } }],
      value => {
        assert.ok(value.includes('최신화 안 함'));
        assert.ok(!value.includes('최신화 실패'));
        assert.ok(value.includes('*0건*'));
        assert.ok(!value.includes('지금 `doc-archive`'));
      });
  }
  const many = n => Array.from({ length: n }, (_, i) => i);
  const populated = {
    ...docs(10),
    byChannel: many(10).map(i => ({ channel: 'fixture-' + i, count: i + 1, newest: 'file-' + i })),
    approvals: many(10).map(i => ({ channel: 'fixture-' + i, name: 'doc-' + i,
      doc: 'doc-' + i + '.md', state: ['pending', 'missing', 'converted'][i % 3] })),
    failed: ['fixture-failed'],
    restricted: many(8).map(i => ({ channel: 'fixture-' + i, name: 'restricted-' + i })),
    deferred: [{ until: '2024-02-03' }, { until: '2024-03-01' }],
  };
  const extra = {
    stale: many(8).map(i => ({ days: 20 - i, channel: 'fixture-' + i, key: 'key-' + i, scope: i % 2 ? 'reply' : 'parent' })),
    work: many(8).map(i => ({ days: 20 - i, kind: ['summary','dropped','derive','new-channel','renamed','note','undeclared-private','unknown'][i],
      channel: i % 2 ? '' : 'fixture-' + i, where: 'where-' + i })),
    arch: { total: 24, files: many(8).map(i => ({ file: 'archive-file-' + i, problems: ['problem-a','problem-b','problem-c'] })) },
    freshness: fresh,
  };
  check('all sections and truncation', [lag, populated, S, extra], value => {
    for (const text of ['그 밖 2개 채널','그 밖 2건','그 밖 2개 파일','… 그 밖 1건',
      '보류 2건 (가장 이른 만기 02/03)', '요약 불일치', '(답글)', '최신화']) assert.ok(value.includes(text), text);
    assert.ok(!value.includes('restricted-6'));
    assert.ok(!value.includes('problem-c'));
    assert.ok(!value.includes('archive-file-7`'));
    const positions = ['미변환 첨부', '팀이', '읽지 못한 채널', '봇이 받지 못하는', '반영 안 된 슬랙', '며칠째 안 정한', '아카이브 구조 결함', '⏸ 보류'].map(x => value.indexOf(x));
    assert.deepEqual([...positions].sort((a,b) => a-b), positions);
  });
  check('preDigest omits unrelated sections', [lag, populated, S, { ...extra, preDigest: true }], value => {
    for (const text of ['승인했는데', '봇이 받지 못하는', '반영 안 된 슬랙', '며칠째 안 정한', '구조 결함', '보류 2건']) assert.ok(!value.includes(text));
    assert.ok(value.includes('읽지 못한 채널'));
  });
  for (const kind of ['summary','dropped','derive','new-channel','renamed','note','undeclared-private','unknown']) {
    check('work label ' + kind, [lag, docs(0), S, { work: [{ kind, days: 3, where: 'somewhere' }] }],
      value => assert.ok(value.includes(kind === 'unknown' ? '[unknown]' : '[')));
  }
  check('archive failure beats findings', [lag, docs(0), S, { arch: { failed: 'fixture error', total: 2 } }],
    value => { assert.ok(value.includes('검사를 돌리지 못했습니다')); assert.ok(!value.includes('구조 결함')); });
  check('empty archive files omitted', [lag, docs(0), S, { arch: { total: 1, files: [
    { file: 'empty', problems: [] }, { file: 'broken', problems: ['bad'] }] } }],
    value => { assert.ok(value.includes('(1개 파일)')); assert.ok(!value.includes('`empty`')); });
  check('custom threshold', [lag, docs(1), { ...S, docWarnCount: 2 }], value => assert.equal(value, null));
  check('threshold zero preserves zero report', [lag, docs(0), { ...S, docWarnCount: 0 }], value => assert.ok(value.includes('*0건*')));
  return out;
}
async function runners(text) {
  const out = [];
  for (const mode of ['healthy', 'dry', 'sent', 'preDigest', 'partialPost', 'syncFailure']) {
    const trace = [], logs = [];
    const config = { limits: {}, digest: { health: {}, healthPre: { scanDays: 2 } },
      owner: { slackUserId: 'OWNER', name: 'Fixture owner' } };
    const traceCall = (name, value) => (...args) => { trace.push([name, clone(args)]); return clone(value); };
    const fail = new Error('fixture failure');
    const deps = {
      config, console: { log: (...a) => trace.push(['console', ...a]) },
      syncForRead: async args => {
        trace.push(['sync', args]);
        if (mode === 'syncFailure') throw fail;
        return fresh;
      },
      chunkForSlack: body => { trace.push(['chunk', body]); return ['part-1','part-2']; },
      logConversation: entry => { trace.push(['log', entry]); logs.push(entry); },
      replacements: {
        syncLag: traceCall('lag', lag),
        pendingDocuments: async (client, opts) => {
          trace.push(['docs', clone(opts)]);
          return docs(mode === 'healthy' ? 0 : 1);
        },
        stalePendingEdits: traceCall('stale', []),
        stalePendingWork: traceCall('work', []),
        archiveIssues: traceCall('archive', null),
      },
    };
    let posts = 0;
    const client = {
      conversations: { open: async args => { trace.push(['open', args]); return { channel: { id: 'DM' } }; } },
      chat: { postMessage: async args => {
        trace.push(['post', args]); posts++;
        if (mode === 'partialPost' && posts === 2) throw fail;
      } },
    };
    const api = facade(text, deps);
    let result;
    try {
      result = await api.runHealth(client, { dry: mode === 'dry', preDigest: mode === 'preDigest', sync: true });
      assert.ok(!['partialPost','syncFailure'].includes(mode), 'expected failure');
    } catch (err) {
      assert.equal(err, fail);
      assert.ok(['partialPost','syncFailure'].includes(mode));
      if (mode === 'partialPost') assert.ok(err.hermesContext.endsWith('본문 조립 ✓ → 그다음에서 중단'));
      else assert.equal(err.hermesContext, undefined);
      result = { error: err.message, context: err.hermesContext };
    }
    if (mode === 'healthy') { assert.deepEqual(result, { sent: false, reason: 'healthy' }); assert.equal(posts,0); }
    if (mode === 'dry') { assert.equal(result.reason,'dry-run'); assert.equal(posts,0); }
    if (mode === 'sent' || mode === 'preDigest') {
      assert.equal(result.sent,true); assert.equal(posts,2); assert.equal(logs.length,1);
      assert.equal(logs[0].answer, result.text); assert.equal(trace.at(-1)[0],'log');
    } else assert.equal(logs.length,0);
    if (mode === 'preDigest') {
      assert.ok(!trace.some(x => ['stale','work','archive'].includes(x[0])));
      assert.deepEqual(trace.find(x => x[0] === 'docs')[1], { scanDays: 2 });
    }
    out.push({ mode, result, trace });
  }
  return out;
}
const nativeNow = Date.now;
Date.now = () => 1700000000000;
try {
  assert.match(source, /import \{ compose \} from '\.\/archive-health\/report\.js';/);
  assert.match(source, /export \{ compose \};/);
  const direct = reports(compose);
  assert.deepEqual(reports(facade(source).compose), direct);
  const integrated = await runners(source);
  // Run the existing freshness assertions with only their import replaced in memory.
  const existing = fs.readFileSync(new URL('./check-health-freshness.js', import.meta.url), 'utf8');
  let failed = false;
  compileFunction(existing.replace(/^import .*;\r?\n/gm, ''),
    ['compose','console','process'])(compose, { log() {}, error(...args) { failed = true; console.error(...args); } },
      { exit(v) { if (v) failed = true; }, set exitCode(v) { if (v) failed = true; } });
  assert.equal(failed, false, 'existing freshness assertions');
  if (process.argv.includes('--baseline-stdin')) {
    const old = JSON.parse(fs.readFileSync(0,'utf8'));
    assert.equal(typeof old, 'string');
    assert.deepEqual(reports(facade(old).compose), direct, 'report baseline');
    assert.deepEqual(await runners(old), integrated, 'health runner baseline');
    console.log('Baseline SHA256: ' + createHash('sha256').update(old).digest('hex'));
  }
  console.log('Health report: ' + direct.length + ' contracts + ' + integrated.length +
    ' runner cases + existing freshness assertions passed.');
} finally { Date.now = nativeNow; }
