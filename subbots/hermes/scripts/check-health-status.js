#!/usr/bin/env node
/** R3l: status contracts using memory files, frozen time and real decision helpers.
 * summaryHash is a fake boundary; no real archive or subprocess is accessed.
 * --baseline-stdin accepts an independent archive-health.js JSON string.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createHealthStatus } from '../src/archive-health/status.js';
import { createHealthDocFilters } from '../src/archive-health/doc-filters.js';
import { createHealthAttachments } from '../src/archive-health/attachments.js';
import { createHealthDocuments } from '../src/archive-health/documents.js';
import { compose } from '../src/archive-health/report.js';
const source = fs.readFileSync(new URL('../src/archive-health.js', import.meta.url), 'utf8');
const utilSource = fs.readFileSync(new URL('../src/ingest/util.js', import.meta.url), 'utf8');
const workSource = fs.readFileSync(new URL('../src/ingest/pending-work.js', import.meta.url), 'utf8');
function declaration(text, name) {
  const found = text.match(new RegExp('(?:export )?function ' + name + '\\([^]*?\\n\\}'));
  assert.ok(found, name);
  return found[0].replace(/^export /, '');
}
function decisions(config, trace, hashes) {
  const active = compileFunction(declaration(utilSource, 'activeDeferred') + '\nreturn activeDeferred;', ['config'])(config);
  const applied = workSource.match(/const appliedAfter = [^]*?\n\};/);
  assert.ok(applied);
  const suppress = compileFunction(applied[0] + '\n' + declaration(workSource, 'suppress') + '\nreturn suppress;',
    ['activeDeferred','summaryHash'])(active, file => { trace.push(['hash',file]); return hashes[file] ?? ''; });
  return { activeDeferred: active, suppress };
}
function facade(text, deps) {
  const forbidden = () => { throw new Error('unexpected dependency'); };
  const injected = { ...deps, DOCS_DIR: '/fixture/docs',
    // 채널 목록을 안 훑는 검사라 항등으로 채운다 — 거르기는 check-health-documents.js 가 잰다.
    dropSkippedChannels: chs => chs,
    isPrivateChannel: () => false, listBotChannels: forbidden, fetchAllReplies: forbidden,
    isBotMessage: () => false, DEFAULT_LIVE_FETCH_MAX_DAYS: 14, chunkForSlack: forbidden,
    logConversation: forbidden, SKILL_SCRIPTS: {}, runScript: forbidden, syncForRead: forbidden,
    compose, createHealthStatus, createHealthDocFilters, createHealthAttachments, createHealthDocuments };
  delete injected.DEFAULTS; // The facade owns its actual defaults.
  delete injected.readJson; // Compile the real facade reader against fake fs.
  const code = text.replace(/^import\s[\s\S]*?;\r?\n/gm, '')
    .replace(/^export \{[^}]*\};?\r?$/gm, '').replace(/^export /gm, '');
  return compileFunction(code + '\nreturn {syncLag,stalePendingEdits,stalePendingWork,compose};',
    Object.keys(injected))(...Object.values(injected));
}
const ARCHIVE_DIR = '/fixture/archive';
const NOW = Date.parse('2024-01-10T00:00:00Z');
const copy = x => structuredClone(x);
function suite(make) {
  const results = [];
  function fixture() {
    const files = new Map(), trace = [], hashes = {};
    const config = { timezone: 'Asia/Seoul' };
    const fakeFs = { readFileSync(file, encoding) {
      trace.push(['read',file,encoding]);
      if (!files.has(file)) throw new Error('missing');
      return files.get(file);
    } };
    const readJson = compileFunction(declaration(source,'readJson') + '\nreturn readJson;', ['fs'])(fakeFs);
    const deps = { config, path, ARCHIVE_DIR, readJson, fs: fakeFs,
      DEFAULTS: { pendingEditDays: 3 }, ...decisions(config, trace, hashes) };
    const api = make(deps);
    assert.deepEqual(trace, []);
    const put = (name, value) => files.set(path.join(ARCHIVE_DIR,name), JSON.stringify(value));
    return { api, put, files, trace, hashes, config };
  }
  function check(name, fn) {
    const f = fixture();
    const value = fn(f);
    results.push({ name, value: copy(value), trace: copy(f.trace), files: [...f.files] });
  }
  for (const [label, raw, expected] of [
    ['missing', undefined, { missing: true }],
    ['malformed', '{broken', { missing: true }],
    ['null', 'null', { missing: true }],
    ['empty', '{}', { missing: true }],
    ['bad date', '{"last_sync":"bad"}', { missing: true }],
    ['today', '{"last_sync":"2024-01-10T00:00:00Z"}', { days: 0, lastSync: '2024-01-10' }],
    ['floor', '{"last_sync":"2024-01-08T12:00:00Z"}', { days: 1, lastSync: '2024-01-08' }],
    ['future', '{"last_sync":"2024-01-10T00:00:01Z"}', { days: -1, lastSync: '2024-01-10' }],
  ]) {
    check('sync ' + label, f => {
      if (raw !== undefined) f.files.set(path.join(ARCHIVE_DIR,'.sync-state.json'),raw);
      const value = f.api.syncLag();
      assert.deepEqual(value,expected);
      return value;
    });
  }
  for (const [method, file] of [['stalePendingEdits','.pending-edits.json'], ['stalePendingWork','.pending-work.json']]) {
    for (const [label, raw] of [['missing',undefined],['malformed','{bad'],['null','null'],['no items','{}'],['empty','{"items":[]}']]) {
      check(method + ' ' + label, f => {
        if (raw !== undefined) f.files.set(path.join(ARCHIVE_DIR,file),raw);
        assert.deepEqual(f.api[method](),[]);
        assert.deepEqual(f.trace,[['read',path.join(ARCHIVE_DIR,file),'utf8']]);
      });
    }
    check(method + ' age threshold and stable sort', f => {
      const items = [
        { id: 'tie-a', firstSeen: '2024-01-07' }, { id: 'young', firstSeen: '2024-01-08' },
        { id: 'old', firstSeen: '2024-01-01' }, { id: 'tie-b', firstSeen: '2024-01-07' },
        { id: 'bad', firstSeen: 'bad' }, { id: 'missing' }, { id: 'future', firstSeen: '2024-01-11' },
      ];
      f.put(file,{items});
      const before = [...f.files];
      const value = f.api[method]();
      assert.deepEqual(value.map(x => [x.id,x.days]),[['old',9],['tie-a',3],['tie-b',3]]);
      assert.deepEqual([...f.files],before);
      return value;
    });
    check(method + ' threshold override and timezone rollover', f => {
      f.put(file,{items:[{id:'a',firstSeen:'2024-01-07'}]});
      const at = Date.parse('2024-01-09T15:00:00Z');
      assert.equal(f.api[method](at,3).length,1);
      f.config.timezone='UTC';
      assert.equal(f.api[method](at,3).length,0);
      assert.equal(f.api[method](at,2).length,1);
      assert.equal(f.api[method](at,0).length,1);
    });
    check(method + ' deferred expiry inclusive', f => {
      f.put(file,{items:['held','expired','invalid','forever'].map(id => ({id,firstSeen:'2024-01-01'}))});
      f.put('.sync-state.json',{deferred:{
        held:{until:'2024-01-10'}, expired:{until:'2024-01-09'},
        invalid:{until:'bad'}, forever:{until:'2099-01-01'},
      }});
      const value=f.api[method]();
      assert.deepEqual(value.map(x=>x.id),['expired','invalid']);
      assert.deepEqual(f.api[method](Date.parse('2024-01-11T00:00:00Z')).map(x=>x.id),['held','expired','invalid']);
      return value;
    });
    check(method + ' malformed state keeps items', f => {
      f.put(file,{items:[{id:'a',firstSeen:'2024-01-01'}]});
      f.files.set(path.join(ARCHIVE_DIR,'.sync-state.json'),'{bad');
      assert.equal(f.api[method]().length,1);
    });
  }
  check('work dismissed and changed summary', f => {
    const items = ['plain','same','changed','keep'].map(id=>({id,firstSeen:'2024-01-01',file:'f-'+id}));
    f.put('.pending-work.json',{items});
    f.put('.sync-state.json',{dismissed:{plain:{summaryHash:null},same:{summaryHash:'old'},changed:{summaryHash:'old'}}});
    f.hashes['f-same']='old'; f.hashes['f-changed']='new';
    const value=f.api.stalePendingWork();
    assert.deepEqual(value.map(x=>x.id),['changed','keep']);
    assert.deepEqual(f.trace.filter(x=>x[0]==='hash'),[['hash','f-same'],['hash','f-changed']]);
    return value;
  });
  check('work applied strictly after last seen only', f => {
    const lastSeen='2024-01-09T00:00:00Z';
    f.put('.pending-work.json',{items:['after','equal','before','invalid','no-last'].map(id=>({
      id,firstSeen:'2024-01-01',...(id==='no-last'?{}:{lastSeen}) }))});
    f.put('.sync-state.json',{applied:{
      after:{at:'2024-01-09T00:00:01Z'},equal:{at:lastSeen},before:{at:'2024-01-08'},
      invalid:{at:'bad'},'no-last':{at:'2024-01-10'},
    }});
    const value=f.api.stalePendingWork();
    assert.deepEqual(value.map(x=>x.id),['equal','before','invalid','no-last']);
    return value;
  });
  check('edits use deferred only', f => {
    f.put('.pending-edits.json',{items:[{id:'a',firstSeen:'2024-01-01',lastSeen:'2024-01-02'}]});
    f.put('.sync-state.json',{applied:{a:{at:'2024-01-03'}},dismissed:{a:{summaryHash:null}}});
    assert.equal(f.api.stalePendingEdits().length,1);
  });
  check('returned arrays do not alter files', f => {
    f.put('.pending-work.json',{items:[{id:'a',firstSeen:'2024-01-01'}]});
    const before=[...f.files];
    const result=f.api.stalePendingWork();
    result[0].firstSeen='changed'; result.push({});
    assert.deepEqual([...f.files],before);
  });
  if(make.integration) {
    check('status feeds report with real facade bindings', f => {
      f.put('.sync-state.json',{last_sync:'2024-01-01'});
      f.put('.pending-edits.json',{items:[{id:'e',key:'edit',channel:'fixture',firstSeen:'2024-01-01'}]});
      f.put('.pending-work.json',{items:[{id:'w',kind:'note',where:'fixture',firstSeen:'2024-01-02'}]});
      const value=f.api.compose(f.api.syncLag(),{total:0,scanDays:60,byChannel:[],failed:[]},
        {syncWarnDays:7,syncCriticalDays:12,liveFetchMaxDays:14,docWarnCount:1},
        {stale:f.api.stalePendingEdits(),work:f.api.stalePendingWork()});
      for(const text of ['9일 밀렸습니다','가장 오래된 것 9일째','가장 오래된 것 8일째']) assert.ok(value.includes(text));
      return value;
    });
  }
  return results;
}
const savedNow=Date.now;
Date.now=()=>NOW;
try {
  const direct=suite(createHealthStatus);
  const via=deps=>facade(source,deps);
  via.integration=true;
  const actual=suite(via);
  assert.deepEqual(actual.slice(0,direct.length),direct,'module vs facade');
  if(process.argv.includes('--baseline-stdin')){
    const old=JSON.parse(fs.readFileSync(0,'utf8'));
    assert.equal(typeof old,'string');
    const baseline=deps=>facade(old,deps); baseline.integration=true;
    assert.deepEqual(suite(baseline),actual,'independent baseline');
    console.log('Baseline SHA256: '+createHash('sha256').update(old).digest('hex'));
  }
  console.log('Health status: '+direct.length+' contracts + '+(actual.length-direct.length)+' report integration passed.');
} finally {Date.now=savedNow;}
