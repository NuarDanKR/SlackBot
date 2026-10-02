#!/usr/bin/env node
/** R3p: real runIngest orchestration, with all effect boundaries replaced.
 * No production imports, Git commands, model calls, disk writes or Slack sends.
 * Recovery, finalization and report delivery failures preserve the primary outcome.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { summarizeUsage, usageFields } from '../src/llm/usage.js';
const source=fs.readFileSync(new URL('../src/ingest/index.js',import.meta.url),'utf8');
const code=source.replace(/^import\s[\s\S]*?;\r?\n/gm,'').replace(/^export /gm,'');
const accounting=summarizeUsage([{model:'fixture-model',messageId:'fixture',usage:{input_tokens:20,output_tokens:5},costUsd:0.123}]);
const sha='a'.repeat(40),paths=['archive','docs','logs'];
function fixture(options={}){
 const trace=[],logs=[];
 const f={trace,logs,fail:null,changes:true,gate:{passed:true},push:{committed:true,pushed:true,sha:'b'.repeat(40)},
  conversations:{totalAdded:1,totalReplies:0,newChannels:[],errors:[],channels:[
   {channel:'fixture',file:'fixture',added:1,transcript:'fixture input',probeHeader:'header'},
   {channel:'late',file:'late',added:0,threadReplies:1,replyProbes:[{file:'late',header:'parent',reply:'late reply'}]}]},
  rendered:{qa:1,broadcast:0,changed:1},findings:[],suppressed:new Set(),report:'report',...options};
 const event=(name,args=[])=>{
  trace.push([name,...args]);
  if(f.fail===name || f.failures?.includes(name))throw new Error(name+' failed');
 };
 const deps={path,DATA_ROOT:path.resolve('/fixture'),ARCHIVE_DIR:path.resolve('/fixture/archive'),
  DOCS_DIR:path.resolve('/fixture/docs'),LOG_DIR:path.resolve('/fixture/logs'),config:{timezone:'UTC'},
  console:{log(){},warn(){},error(){}},usageFields,
  head:async()=>{event('head');return f.currentSha || 'c'.repeat(40);},
  syncBeforeWork:async()=>{event('sync');f.currentSha=sha;return 'synced\nmore';},
  ingestConversations:async(client,opts)=>{event('collect',[opts]);return structuredClone(f.conversations);},
  renderConversationLog:opts=>{event('render',[opts]);return f.rendered;},
  checkSummaries:async(touched,opts)=>{
   event('summary-start',[structuredClone(touched)]);opts.onUsage(accounting);
   event('summary');return {findings:structuredClone(f.findings),failed:f.summaryFailed?['fixture']:[]};
  },
  runDerive:async opts=>{event('derive',[opts]);return {changed:['index'],unresolved:[]};},
  runPendingWork:(result,opts)=>{event('work',[opts]);assert.ok(result.derive);return {total:0,suppressedFindings:f.suppressed};},
  runGate:async opts=>{event('gate',[opts]);return f.gate;},
  hasChanges:async p=>{event('changes',[p]);return f.changes;},
  commitAndPush:async(msg,p)=>{event('commit-push',[msg,p]);return {...f.push};},
  rollback:async(s,p)=>{event('rollback',[s,p]);if(f.rollbackError)throw new Error('rollback failed');},
  logConversation:entry=>{if(f.logAfterWrite)logs.push(entry);event('accounting');if(!f.logAfterWrite)logs.push(entry);},
  clearTemp:()=>event('cleanup'),
  compose:result=>{event('compose');f.reported=structuredClone(result);return f.report;},
  send:async(client,text,opts)=>{event('send',[text,opts]);return {sent:!opts.dry};},
 };
 const exports=compileFunction(code+'\nreturn {runIngest,classifyFailure,stepsReached,buildCommitMessage,targetPaths};',
 Object.keys(deps))(...Object.values(deps));
 f.run=opts=>exports.runIngest({},opts);f.api=exports;
 f.names=()=>trace.map(x=>x[0]);f.calls=name=>trace.filter(x=>x[0]===name);
 return f;
}
let passed=0;
async function test(name,run){await run();passed++;console.log('PASS '+name);}
const stages=['sync','head','collect','render','summary','derive','work','gate','changes','commit-push'];
for(const stage of stages){
 await test(stage+' failure preserves rollback and reporting boundary',async()=>{
  const f=fixture({fail:stage});const out=await f.run();
  assert.equal(out.reason,'fatal');assert.equal(out.errorType,'ingest_fatal');assert.equal(out.fatal,stage+' failed');
  assert.equal(out.sent,true);assert.equal(out.origin,'아침 회차');
  const rollback=f.calls('rollback');
  assert.equal(rollback.length,['head','sync','commit-push'].includes(stage)?0:1);
  if(rollback.length)assert.deepEqual(rollback[0],['rollback',sha,paths]);
  assert.ok(f.names().indexOf('cleanup')<f.names().indexOf('compose'));
  assert.ok(f.names().indexOf('compose')<f.names().indexOf('send'));
  const expectedCost=stages.indexOf(stage)>=stages.indexOf('summary');
  assert.equal(f.logs.length,expectedCost?1:0);
  if(expectedCost){
   assert.equal(f.logs[0].accountingOnly,true);assert.equal(f.logs[0].costUsd,0.123);assert.equal(f.logs[0].ok,false);
   assert.ok(f.names().indexOf('accounting')<f.names().indexOf('cleanup'));
  }
  assert.equal(out.accounting,undefined);assert.equal(out.costUsd,undefined);
  if(stages.indexOf(stage)<stages.indexOf('commit-push'))assert.equal(f.calls('commit-push').length,0);
  if(stage==='sync')assert.match(out.context,/대화 반영 ✗/);
 });
}
await test('success exact stage order, paths, probes, summary selection',async()=>{
 const f=fixture();const out=await f.run({scanEdits:false});
 assert.deepEqual(f.names(),['sync','head','collect','render','summary-start','summary','derive','work','gate','changes','commit-push','accounting','cleanup','compose','send']);
 assert.deepEqual(f.calls('collect')[0][1],{dry:false,scanEdits:false});
 assert.deepEqual(f.calls('render')[0][1],{write:true});
 assert.equal(f.calls('summary-start')[0][1].length,1);
 assert.deepEqual(f.calls('gate')[0][1].probes,{channels:[{file:'fixture',header:'header'}],
  threadReplies:[{file:'late',header:'parent',reply:'late reply'}]});
 assert.deepEqual(f.calls('commit-push')[0][2],paths);
 assert.match(f.calls('commit-push')[0][1],/대화 1건/);
 assert.equal(out.push.stateOnly,false);assert.equal(out.errorType,undefined);assert.equal(f.logs.length,1);
});
await test('gate fail rolls back once without committing',async()=>{
 const f=fixture({gate:{passed:false,failures:[{check:'fixture-gate',detail:['broken']}]}});
 const out=await f.run();assert.equal(out.reason,'gate_failed');assert.equal(out.errorType,'gate_failed');
 assert.equal(f.calls('rollback').length,1);assert.equal(f.calls('changes').length,0);
 assert.equal(f.calls('commit-push').length,0);assert.equal(out.sent,true);
 assert.match(out.context,/관문 ✓/);assert.match(out.context,/커밋·push ✗/);
});
await test('push unsuccessful result preserves commit and never rolls back',async()=>{
 const f=fixture({push:{committed:true,pushed:false,reason:'network unavailable'}});
 const out=await f.run();assert.equal(out.reason,'push_failed');assert.equal(f.calls('rollback').length,0);
 assert.equal(f.calls('commit-push').length,1);assert.match(out.error,/network unavailable/);
});
await test('no changes avoids commit but completes accounting/report',async()=>{
 const f=fixture({changes:false});const out=await f.run();
 assert.equal(f.calls('commit-push').length,0);assert.equal(out.push,undefined);assert.equal(f.logs.length,1);
});
await test('no report still returns failure metadata',async()=>{
 const f=fixture({fail:'collect',report:null});const out=await f.run();
 assert.equal(out.sent,false);assert.equal(out.reason,'fatal');assert.equal(out.errorType,'ingest_fatal');
 assert.equal(f.calls('send').length,0);
});
for(const skipSummary of [false,true]){
 await test('dry zero Git/log writes; skipSummary='+skipSummary,async()=>{
  const f=fixture();const out=await f.run({dry:true,skipSummary,scanEdits:true});
  for(const name of ['head','sync','gate','changes','commit-push','rollback','accounting'])assert.equal(f.calls(name).length,0);
  assert.deepEqual(f.calls('render')[0][1],{write:false});assert.deepEqual(f.calls('derive')[0][1],{dry:true});
  assert.equal(f.calls('work')[0][1].dry,true);assert.equal(f.calls('work')[0][1].summaryChecked,!skipSummary);
  assert.deepEqual(f.calls('send')[0][2],{dry:true});assert.equal(out.sent,false);assert.equal(f.logs.length,0);
  assert.equal(out.summaryAccounting?.costUsd,skipSummary?undefined:0.123);
 });
}
for(const stage of ['collect','summary','derive','work']){
 await test('dry '+stage+' failure never rolls back',async()=>{
  const f=fixture({fail:stage});const out=await f.run({dry:true});
  assert.equal(out.reason,'fatal');assert.equal(f.calls('rollback').length,0);assert.equal(f.logs.length,0);
  assert.equal(out.summaryAccounting?.costUsd,stage==='collect'?undefined:0.123);
 });
}
await test('skipSummary controls failure origin and skips model boundary',async()=>{
 const f=fixture({fail:'derive'});const out=await f.run({skipSummary:true});
 assert.equal(out.origin,'요약 전 회차');assert.equal(f.calls('summary-start').length,0);assert.equal(f.logs.length,0);
});
await test('summary suppression happens after work and before report',async()=>{
 const f=fixture({findings:[{channel:'fixture',type:'amount',where:'row'},{channel:'other',type:'date'}],
 suppressed:new Set(['fixture|summary|amount|row'])});
 const out=await f.run();assert.deepEqual(out.summary.findings,[{channel:'other',type:'date'}]);
 assert.deepEqual(f.reported.summary.findings,out.summary.findings);
});
await test('summary partial failures persist one unsuccessful cost record',async()=>{
 const f=fixture({summaryFailed:true});await f.run();assert.equal(f.logs.length,1);assert.equal(f.logs[0].ok,false);
});
await test('no source log passes preserve signal through orchestration',async()=>{
 const f=fixture({rendered:{noSource:true,rawDir:'/fixture/raw',rendered:['preserved']}});
 const out=await f.run();assert.equal(out.log.noSource,true);assert.deepEqual(out.log.rendered,['preserved']);
});
await test('no fresh conversations still commit derived/log/state changes',async()=>{
 const f=fixture();f.conversations.totalAdded=0;f.conversations.channels=[];
 const out=await f.run();assert.equal(f.calls('summary-start').length,0);assert.equal(f.logs.length,0);
 assert.equal(f.calls('commit-push').length,1);assert.equal(out.push.stateOnly,true);
 assert.match(f.calls('commit-push')[0][1],/색인 1곳/);assert.match(f.calls('commit-push')[0][1],/로그 1개/);
 assert.equal(f.calls('work')[0][1].summaryChecked,false);
});

for(const stage of ['rollback','cleanup','compose','send','accounting']){
 await test(stage+' failure is retained without hiding primary outcome',async()=>{
  const f=fixture({fail:stage==='rollback'?'collect':stage,rollbackError:stage==='rollback'});
  const out=await f.run();
  assert.equal(out.reason,'fatal');
  assert.ok(out.secondaryErrors.some(e=>e.stage===stage));
  assert.equal(f.calls('cleanup').length,1);
  assert.equal(f.calls('rollback').length,stage==='rollback'?1:0);
  assert.equal(f.calls('send').length,stage==='compose'?0:1);
  assert.equal(out.sent,!['compose','send'].includes(stage));
  if(stage==='rollback')assert.equal(out.fatal,'collect failed');
  else assert.equal(out.push.pushed,true);
  if(stage==='accounting'){
   assert.equal(out.accountingLog,'unknown');assert.equal(out.summaryAccounting.costUsd,0.123);
   assert.equal(out.accounting,undefined);assert.equal(f.calls('accounting').length,1);
  }
 });
}
await test('gate rollback + accounting + cleanup + compose failures all survive',async()=>{
 const f=fixture({gate:{passed:false,failures:[{check:'gate',detail:[]}]},rollbackError:true,
  failures:['accounting','cleanup','compose']});
 const out=await f.run();
 assert.equal(out.reason,'gate_failed');assert.equal(out.rollback.ok,false);
 assert.deepEqual(out.secondaryErrors.map(e=>e.stage),['rollback','accounting','cleanup','compose']);
 assert.equal(f.calls('rollback').length,1);assert.equal(f.calls('accounting').length,1);
 assert.equal(f.calls('cleanup').length,1);assert.equal(f.calls('send').length,0);
 assert.equal(out.sent,false);assert.equal(out.reportDelivery.status,'not-sent');
 assert.equal(out.summaryAccounting.costUsd,0.123);
});
await test('post-pull HEAD is the rollback baseline',async()=>{
 const f=fixture({fail:'collect'});const out=await f.run();
 assert.deepEqual(f.names().slice(0,3),['sync','head','collect']);
 assert.equal(out.rollback.sha,sha);assert.equal(out.rollback.ok,true);
});
await test('commit exception preserves unknown outcome without rollback',async()=>{
 const f=fixture({fail:'commit-push'});const out=await f.run();
 assert.equal(out.commitOutcomeUnknown,true);
 assert.deepEqual(out.rollback,{attempted:false,reason:'commit-started'});
 assert.equal(f.calls('rollback').length,0);assert.equal(f.calls('commit-push').length,1);
});
await test('logger may write then throw: preserve usage without retry',async()=>{
 const f=fixture({fail:'accounting',logAfterWrite:true});const out=await f.run();
 assert.equal(f.logs.length,1);assert.equal(f.calls('accounting').length,1);
 assert.equal(out.accounting,undefined);assert.equal(out.accountingLog,'unknown');
 assert.equal(out.summaryAccounting.costUsd,0.123);
});
const gitSource=fs.readFileSync(new URL('../src/ingest/git.js',import.meta.url),'utf8');
for(const failure of [null,'reset','clean']){
 await test('actual rollback wrapper detects '+(failure||'successful commands'),async()=>{
  const calls=[];
  const gitCode=gitSource.replace(/^import\s[\s\S]*?;\r?\n/gm,'').replace(/^export /gm,'');
  const actual=compileFunction(gitCode+'\nreturn rollback;', ['execFile','promisify','DATA_ROOT'])(
   async(command,args)=>{
    assert.equal(command,'git');calls.push(args);
    if(args[0]===failure)throw Object.assign(new Error('command failed'),{stderr:failure+' denied'});
    return {stdout:'',stderr:''};
   },fn=>fn,'/fixture');
  if(failure)await assert.rejects(()=>actual(sha,paths),new RegExp(failure+' denied'));
  else await actual(sha,paths);
  assert.deepEqual(calls.map(a=>a[0]),failure==='reset'?['reset']:['reset','clean']);
  assert.deepEqual(calls[0],['reset','--hard',sha]);
  if(calls.length===2)assert.deepEqual(calls[1],['clean','-fd','--',...paths]);
 });
}
const reportSource=fs.readFileSync(new URL('../src/ingest/report.js',import.meta.url),'utf8');
function reportApi(config={owner:{slackUserId:'OWNER'}}){
 const reportCode=reportSource.replace(/^import\s[\s\S]*?;\r?\n/gm,'').replace(/^export /gm,'');
 return compileFunction(reportCode+'\nreturn {compose,send};',['config','chunkForSlack','console'])(
  config,()=>['part 1','part 2'],{log(){}});
}
await test('actual report distinguishes rollback failed, recovered, committed and unknown',()=>{
 const {compose}=reportApi();
 for(const [r,expected] of [
  [{fatal:'collect',rollback:{attempted:true,ok:false},secondaryErrors:[{stage:'rollback',error:'denied'}]},'되돌리기에 실패'],
  [{fatal:'collect',rollback:{attempted:true,ok:true}},'pull 이후 기준'],
  [{fatal:'commit',commitOutcomeUnknown:true},'커밋·전송 결과를 확인하지 못'],
  // 이 모양은 방어용이다 — commitAndPush 가 돌아온 뒤 try 안에 던질 문장이 남아 있지
  // 않아 지금 코드로는 안 나온다. 그래도 복구 문구는 한 줄씩 다 시험해 둔다.
  [{fatal:'finalize',push:{committed:true,pushed:true,sha:'sent'}},'커밋·push는 완료'],
  [{gate:{passed:false,failures:[]},rollback:{ok:false}},'복구 상태 확인'],
 ]){
  const text=compose(r);assert.ok(text.includes(expected),text);
  assert.ok(!text.includes('이번 실행은 아무것도 반영하지 못했습니다'));
 }
});
// 뒷처리 실패 한 줄이 그날 보고를 통째로 갈아버리던 것을 막는다 (2026-09-15).
await test('post-work failures keep the report body and gate detail',()=>{
 const {compose}=reportApi();
 const body=compose({push:{committed:true,pushed:true,sha:'sent'},
  secondaryErrors:[{stage:'cleanup',error:'failed'}],
  conversations:{newChannels:['새채널'],renamed:[],channels:[{channel:'비공개마',undeclaredPrivate:true}],errors:[]},
  summary:{findings:[{channel:'c',type:'t',where:'w'}]}});
 for(const s of ['[cleanup] failed','새채널','상단 요약과 다른 것','push 완료'])assert.ok(body.includes(s),body);
 // 맨 앞의 설명 줄이 없으면 `[cleanup] failed` 한 줄만 남아 무엇이 됐는지 안 보인다
 assert.ok(body.includes('뒷처리 단계에서 오류'),body);
 // 그리고 뒷처리 실패는 「자동 반영이 죽었다」가 아니다
 assert.ok(!body.includes('🚨'),body);
 // 자료가 새는 쪽(비공개 선언 누락)이 뒷처리 알림보다 **위**여야 한다
 assert.ok(body.indexOf('🔒')>=0&&body.indexOf('🔒')<body.indexOf('⚠️'),body);
 // push 가 안 된 날도 여기로 온다 — 그때 「반영은 끝났다」고 말하면 안 된다
 const half=compose({push:{committed:true,pushed:false,sha:'sent',reason:'denied'},
  secondaryErrors:[{stage:'accounting',error:'x'}]});
 for(const s of ['[accounting] x','push 가 안 됐습니다'])assert.ok(half.includes(s),half);
 assert.ok(!half.includes('🚨'),half);
 assert.ok(!half.includes('반영은 끝났'),half);
 // 알릴 결과가 하나도 없는 날 — 「아래 그대로」라고 해 놓고 아래가 비면 안 된다
 const quiet=compose({secondaryErrors:[{stage:'cleanup',error:'failed'}]});
 assert.ok(quiet.includes('그 밖에 알릴 반영 결과는 없습니다'),quiet);
 // 관문 실패 + 되돌리기 실패가 겹쳐도 어느 검사가 깨졌는지가 남아야 한다
 const gate=compose({gate:{passed:false,failures:[{check:'헤더 대조',detail:['#a 줄 3']}]},
  rollback:{attempted:true,ok:false},secondaryErrors:[{stage:'rollback',error:'denied'}]});
 for(const s of ['헤더 대조','#a 줄 3','[rollback] denied','그리고 뒷처리에서도'])assert.ok(gate.includes(s),gate);
 // fatal 이 겹쳐도 마찬가지 — 뒷처리 오류도 함께 남아야 한다
 const fatal=compose({fatal:'죽음',gate:{passed:false,failures:[{check:'헤더 대조',detail:['#a 줄 3']}]},
  secondaryErrors:[{stage:'rollback',error:'denied'}]});
 for(const s of ['헤더 대조','#a 줄 3','관문 실패','[rollback] denied'])assert.ok(fatal.includes(s),fatal);
 // 관문 결과가 깨져 있어도 DM 은 나가야 한다 — 여기서 던지면 그날 보고가 통째로 사라진다
 for(const broken of [{passed:false},{passed:false,failures:[{check:'x'}]}]){
  assert.ok(compose({fatal:'죽음',gate:broken}).includes('관문 실패'));
  assert.ok(compose({gate:broken}).includes('관문에서 막혔습니다'));
 }
});
for(const failure of [null,'open','first','second']){
 await test('actual report send metadata: '+(failure||'complete'),async()=>{
  const calls=[];let posts=0;
  const client={conversations:{open:async()=>{calls.push('open');if(failure==='open')throw new Error('open');return {channel:{id:'DM'}};}},
   chat:{postMessage:async()=>{posts++;calls.push('post');if((failure==='first'&&posts===1)||(failure==='second'&&posts===2))throw new Error('post');}}};
  if(failure){
   await assert.rejects(()=>reportApi().send(client,'body'),err=>{
    assert.deepEqual(err.hermesReport,{status:failure==='open'?'not-sent':'unknown',
     confirmedParts:failure==='second'?1:0,attemptedParts:failure==='open'?0:failure==='first'?1:2});
    return true;
   });
  }else{
   const out=await reportApi().send(client,'body');assert.equal(out.sent,true);
   assert.deepEqual(out.reportDelivery,{status:'sent',confirmedParts:2,attemptedParts:2});
  }
  assert.equal(posts,failure==='open'?0:failure==='first'?1:2);
 });
}
await test('actual dry report invokes no Slack API',async()=>{
 const out=await reportApi().send({},'body',{dry:true});
 assert.equal(out.sent,false);assert.equal(out.reportDelivery.status,'dry-run');
});

await test('failure classification precedence and error truncation',()=>{
 const {classifyFailure}=fixture().api;
 assert.equal(classifyFailure({fatal:'x'.repeat(700),gate:{passed:false},push:{committed:true,pushed:false}}).error.length,500);
 assert.equal(classifyFailure({fatal:'x',gate:{passed:false}}).reason,'fatal');
 assert.equal(classifyFailure({gate:{passed:false,failures:[]},push:{committed:true,pushed:false}}).reason,'gate_failed');
 assert.equal(classifyFailure({push:{committed:false,pushed:false}}),null);
});
console.log('\n'+passed+' ingest failure/orchestration contracts passed; actual external effects 0.');
