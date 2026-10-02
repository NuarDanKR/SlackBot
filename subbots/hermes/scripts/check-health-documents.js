#!/usr/bin/env node
/** R3o: document health contracts using memory files and fake collection.
 * --baseline-stdin accepts independent archive-health.js source as JSON.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createHealthDocuments } from '../src/archive-health/documents.js';
import { createHealthAttachments } from '../src/archive-health/attachments.js';
import { createHealthDocFilters } from '../src/archive-health/doc-filters.js';
import { createHealthStatus } from '../src/archive-health/status.js';
import { compose } from '../src/archive-health/report.js';
const source=fs.readFileSync(new URL('../src/archive-health.js',import.meta.url),'utf8');
const util=fs.readFileSync(new URL('../src/ingest/util.js',import.meta.url),'utf8');
const NOW=Date.parse('2024-01-10T00:00:00Z');
function decl(text,name){
 const m=text.match(new RegExp('(?:export )?function '+name+'\\([^]*?\\n\\}'));assert.ok(m,name);
 return m[0].replace(/^export /,'');
}
const extOf=compileFunction(decl(source,'extOf')+'\nreturn extOf;')();
function facade(text,f){
 const forbidden=()=>{throw new Error('unexpected dependency');};
 const deps={...f.deps,ARCHIVE_DIR:'/fixture/archive',
  fetchAllReplies:forbidden,isBotMessage:()=>false,DEFAULT_LIVE_FETCH_MAX_DAYS:14,
  chunkForSlack:forbidden,logConversation:forbidden,SKILL_SCRIPTS:{},runScript:forbidden,
  suppress:forbidden,syncForRead:forbidden,compose,createHealthStatus,createHealthDocFilters,
  createHealthAttachments:()=>({channelAttachments:f.deps.channelAttachments}),createHealthDocuments,
  archiveChannelNames:()=>new Map(),archiveChannelOf:(n)=>n};
 for(const key of ['readJson','settings','DOC_EXTS','PREFER','DECIDED_OUT','extOf','nameKey','docFilters','classifyDoc','channelAttachments'])delete deps[key];
 const code=text.replace(/^import\s[\s\S]*?;\r?\n/gm,'').replace(/^export \{[^}]*\};?\r?$/gm,'').replace(/^export /gm,'');
 return compileFunction(code+'\nreturn {pendingDocuments,unconvertedAmong};',Object.keys(deps))(...Object.values(deps));
}
async function suite(make){
 const results=[];
 function fixture(){
  const files=new Map(),trace=[],records=new Map(),fail=new Set();
  const config={timezone:'UTC',limits:{threadLookbackDays:30},digest:{skipChannels:['skip'],health:{}}};
  // 개명 지도(아카이브=개명 전 이름 → 슬랙의 현재 이름). 기본은 비어 있다 = 개명 없음.
  const renames=new Map();
  const f={files,trace,records,fail,channels:[{id:'C1',name:'fixture'}],config,renames};
  const fakeFs={
   existsSync(p){trace.push(['exists',p]);return files.has(p);},
   readFileSync(p,enc){trace.push(['read',p,enc]);if(!files.has(p))throw new Error('missing');return files.get(p);}
  };
  const readJson=compileFunction(decl(source,'readJson')+'\nreturn readJson;',['fs'])(fakeFs);
  const activeDeferred=compileFunction(decl(util,'activeDeferred')+'\nreturn activeDeferred;',['config'])(config);
  f.deps={fs:fakeFs,path,config,DOCS_DIR:'/fixture/docs',readJson,activeDeferred,extOf,
   settings:()=>({scanDays:config.digest.health.scanDays??60}),
   isPrivateChannel:x=>x==='private',
   DOC_EXTS:new Set(['pdf','hwp','hwpx','docx','doc','pptx','xlsx','xlsm']),PREFER:['hwpx','hwp','docx','pdf'],
   DECIDED_OUT:new Set(['excluded','deferred','superseded']),...createHealthDocFilters({activeDeferred}),
   listBotChannels:async()=>{trace.push(['channels']);return structuredClone(f.channels);},
   /* 실물은 slack-live.js 의 `dropSkippedChannels` — 설정 철자와 슬랙의 현재 이름을
    * 개명 지도로 한쪽에 모아 대고 거른다. 여기서는 실물 `.sync-state.json` 없이
    * 그 규칙만 가짜 지도(`f.renames`)로 흉내 낸다 (check-skip-channels.js ①-b 와 같은 수법). */
   dropSkippedChannels:(chs)=>{
    const canon=x=>{const n=String(x||'').replace(/^#/,'').trim();return f.renames.get(n)??n;};
    const skip=new Set((config.digest.skipChannels||[]).map(canon));
    return (chs||[]).filter(c=>!skip.has(canon(c?.name)));
   },
   channelAttachments:async(client,ch,oldest,parentOldest)=>{
    trace.push(['collect',ch.id,oldest,parentOldest]);
    if(fail.has(ch.id))throw Object.assign(new Error('failure'),{data:{error:'fixture_error'}});
    return structuredClone(records.get(ch.id)||[]);
   }};
  f.put=state=>files.set(path.join('/fixture/docs','.doc-state.json'),JSON.stringify(state));
  f.file=(name,text)=>files.set(path.join('/fixture/docs','projects',name),text);
  f.record=(id,name,extra={})=>({id,name,ext:extOf({name}),channel:f.channels[0].name,ts:'1704844800',publicTag:false,hasUrl:true,...extra});
  return f;
 }
 async function check(name,fn){
  const f=fixture();
  const value=await fn(f,()=>make(f));
  results.push({name,value:structuredClone(value),trace:structuredClone(f.trace),files:[...f.files]});
 }
 await check('disabled returns before all reads',async(f,make)=>{
  f.deps.DOCS_DIR='';const api=make();
  const value=await api.pendingDocuments({}, {now:NOW});
  assert.deepEqual(value,{total:0,byChannel:[],scanDays:60,failed:[],approvals:[],deferred:[],restricted:[],disabled:true});
  assert.equal(api.unconvertedAmong([{name:'a.pdf'}]),0);assert.deepEqual(f.trace,[]);return value;
 });
 for(const kind of ['missing','malformed','empty']){
  await check('state '+kind,async(f,make)=>{
   if(kind==='malformed')f.files.set(path.join('/fixture/docs','.doc-state.json'),'{bad');
   if(kind==='empty')f.put({});
   f.records.set('C1',[f.record('F','new.pdf')]);
   const api=make();const value=await api.pendingDocuments({}, {scanDays:2,now:NOW});
   assert.equal(value.total,1);assert.equal(value.scanDays,2);
   assert.equal(api.unconvertedAmong([{channel:'fixture',name:'new.pdf'}]),kind==='empty'?1:0);
   return value;
  });
 }
 await check('formats deduplicate by rank and preserve channel counts',async(f,make)=>{
  f.put({});f.records.set('C1',[f.record('P','Same.PDF'),f.record('H','same.hwp'),f.record('X','same.hwpx'),
   f.record('D','same.docx'),f.record('E','other.xlsx'),f.record('T','ignore.txt')]);
  const value=await make().pendingDocuments({}, {now:NOW});
  assert.equal(value.total,2);assert.deepEqual(value.byChannel,[{channel:'fixture',count:2,newest:'same.hwpx'}]);return value;
 });
 await check('restriction distinct from name-only count',async(f,make)=>{
  f.put({});f.records.set('C1',[f.record('R','locked.pdf',{hasUrl:false})]);
  const api=make();const value=await api.pendingDocuments({}, {now:NOW});
  assert.equal(value.total,0);assert.deepEqual(value.restricted,[{channel:'fixture',name:'locked.pdf'}]);
  assert.equal(api.unconvertedAmong([{channel:'fixture',name:'locked.pdf'}]),1);return value;
 });
 for(const branch of ['known','excluded','deferred','superseded']){
  await check('filter '+branch,async(f,make)=>{
   const key={known:'slack_files',excluded:'excluded',deferred:'deferred',superseded:'superseded'}[branch];
   f.put({[key]:{F:{channel:'fixture',name:'a.pdf',until:'2024-01-11'}}});
   f.records.set('C1',[f.record('F','a.pdf'),f.record('NEW','a.hwpx')]);
   const api=make();const value=await api.pendingDocuments({}, {now:NOW});
   assert.equal(value.total,0);assert.equal(api.unconvertedAmong([{channel:'fixture',name:'a.pdf'}]),0);return value;
  });
 }
 for(const [label,doc,text,want] of [
  ['pending',null,null,'pending'],['missing','absent.md',null,'missing'],
  ['closed','closed.md','**열람**: 비공개\n**공개승인**: yes','meta'],
  ['no approval','closed.md','**열람**: 공개','meta'],
  ['open','open.md','**열람**: 공개\n**공개승인**: yes',null],
  ['prefixed','projects/open.md','**열람**: 공개 · note\n**공개승인**: yes',null],
  ['backslash prefix','projects\\open.md','**열람**: 공개\n**공개승인**: yes',null],
  ['late marker','long.md','**열람**: 공개\n'+'x'.repeat(4000)+'**공개승인**: yes','meta'],
 ]){
  await check('approval '+label,async(f,make)=>{
   f.channels=[{id:'C1',name:'private'}];
   f.put(doc?{slack_files:{F:{channel:'private',name:'a.pdf',doc}}}:{});
   if(text!==null)f.file(doc.replace(/^projects[\\/]/,''),text);
   f.records.set('C1',[f.record('F','a.pdf',{publicTag:true}),f.record('DUP','a.hwpx',{publicTag:true})]);
   const value=await make().pendingDocuments({}, {now:NOW});
   assert.equal(value.approvals.length,want===null?0:1);
   if(want)assert.equal(value.approvals[0].state,want);return value;
  });
 }
 await check('public channel approval ignored',async(f,make)=>{
  f.put({});f.records.set('C1',[f.record('F','a.pdf',{publicTag:true})]);
  const value=await make().pendingDocuments({}, {now:NOW});assert.deepEqual(value.approvals,[]);return value;
 });
 await check('skip channel and failed channel',async(f,make)=>{
  f.channels=[{id:'C1',name:'fixture'},{id:'C2',name:'other'},{id:'S',name:'skip'}];
  f.put({});f.fail.add('C2');f.records.set('C1',[f.record('F','a.pdf')]);
  const value=await make().pendingDocuments({}, {now:NOW});
  assert.equal(value.total,1);assert.deepEqual(value.failed,['#other(fixture_error)']);
  assert.ok(!f.trace.some(x=>x[0]==='collect'&&x[1]==='S'));return value;
 });
 /* 개명돼도 여전히 안 센다 (2026-09-16). config 에는 개명 **전** 철자가 남아 있고 슬랙은
  * 새 이름을 준다 — 글자 그대로 대면 그 채널의 미변환이 개명된 날부터 다시 세어져,
  * 안 다루기로 한 채널이 「미변환 N건」을 영영 부풀린다. 에러는 안 난다. */
 await check('renamed skip channel is still not counted',async(f,make)=>{
  f.channels=[{id:'C1',name:'fixture'},{id:'S',name:'skip-renamed'}];
  f.renames.set('skip','skip-renamed');
  f.put({});f.records.set('C1',[f.record('F','a.pdf')]);
  f.records.set('S',[{...f.record('X','skipped.pdf'),channel:'skip-renamed'}]);
  const value=await make().pendingDocuments({}, {now:NOW});
  assert.equal(value.total,1);
  assert.deepEqual(value.byChannel,[{channel:'fixture',count:1,newest:'a.pdf'}]);
  assert.ok(!f.trace.some(x=>x[0]==='collect'&&x[1]==='S'));return value;
 });
 await check('scan window max and live settings',async(f,make)=>{
  f.put({});const api=make();
  f.config.digest.health.scanDays=2;
  const first=await api.pendingDocuments({}, {now:NOW});
  assert.equal(first.scanDays,2);
  assert.deepEqual(f.trace.find(x=>x[0]==='collect').slice(2),[NOW/1000-2*86400,NOW/1000-30*86400]);
  f.trace.length=0;
  await api.pendingDocuments({}, {scanDays:40,now:NOW});
  assert.deepEqual(f.trace.find(x=>x[0]==='collect').slice(2),[NOW/1000-40*86400,NOW/1000-40*86400]);
 });
 await check('name-only inputs and mutation',async(f,make)=>{
  f.put({});const api=make();
  const items=[null,{}, {channel:'fixture',name:'a.pdf'},{channel:'fixture',name:'A.hwpx'},
   {channel:'other',name:'a.pdf'},{channel:'fixture',name:'ignore.txt'}];
  const before=structuredClone(items);
  assert.equal(api.unconvertedAmong(items),2);assert.deepEqual(items,before);
 });
 return results;
}
const nativeNow=Date.now;Date.now=()=>NOW;
try{
 const direct=await suite(f=>createHealthDocuments(f.deps));
 const actual=await suite(f=>facade(source,f));
 assert.deepEqual(actual,direct,'documents module vs facade');
 if(process.argv.includes('--baseline-stdin')){
  const old=JSON.parse(fs.readFileSync(0,'utf8'));assert.equal(typeof old,'string');
  assert.deepEqual(await suite(f=>facade(old,f)),actual,'independent baseline');
  console.log('Baseline SHA256: '+createHash('sha256').update(old).digest('hex'));
 }
 console.log('Health documents: '+actual.length+' contracts and facade comparisons passed.');
}finally{Date.now=nativeNow;}
