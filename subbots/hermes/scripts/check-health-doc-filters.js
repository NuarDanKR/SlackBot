#!/usr/bin/env node
/** R3m: document filtering with frozen time, memory state and Python AST fixtures.
 * --baseline-stdin accepts an independently retained archive-health.js JSON string.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { spawnSync } from 'node:child_process';
import { createHealthDocFilters } from '../src/archive-health/doc-filters.js';
import { createHealthAttachments } from '../src/archive-health/attachments.js';
import { createHealthDocuments } from '../src/archive-health/documents.js';
import { createHealthStatus } from '../src/archive-health/status.js';
import { compose } from '../src/archive-health/report.js';
const source=fs.readFileSync(new URL('../src/archive-health.js',import.meta.url),'utf8');
const util=fs.readFileSync(new URL('../src/ingest/util.js',import.meta.url),'utf8');
const activeSource=util.match(/export function activeDeferred\([^]*?\n\}/);
assert.ok(activeSource);
const NOW=Date.parse('2024-01-10T00:00:00Z');
const activeDeferred=compileFunction(activeSource[0].replace(/^export /,'')+'\nreturn activeDeferred;',
 ['config'])({timezone:'Asia/Seoul'});
function facade(text, state=null){
 const forbidden=()=>{throw new Error('unexpected dependency');};
 const reads=[];
 const deps={path,config:{timezone:'Asia/Seoul'},ARCHIVE_DIR:'/fixture/archive',DOCS_DIR:'/fixture/docs',
  fs:{readFileSync(file,encoding){reads.push([file,encoding]);assert.equal(file,path.join('/fixture/docs','.doc-state.json'));return JSON.stringify(state);}},
  // 채널 목록을 안 훑는 검사라 항등 — skip 거르기는 check-health-documents.js 가 잰다.
  dropSkippedChannels:chs=>chs,
  isPrivateChannel:()=>false,listBotChannels:forbidden,fetchAllReplies:forbidden,isBotMessage:()=>false,
  DEFAULT_LIVE_FETCH_MAX_DAYS:14,chunkForSlack:forbidden,logConversation:forbidden,SKILL_SCRIPTS:{},
  runScript:forbidden,activeDeferred,suppress:forbidden,syncForRead:forbidden,
  compose,createHealthStatus,createHealthDocFilters,createHealthAttachments,createHealthDocuments,
  archiveChannelNames:()=>new Map(),archiveChannelOf:(n)=>n};
 const code=text.replace(/^import\s[\s\S]*?;\r?\n/gm,'').replace(/^export \{[^}]*\};?\r?$/gm,'').replace(/^export /gm,'');
 return {...compileFunction(code+'\nreturn {nameKey,docFilters,classifyDoc,unconvertedAmong};',
 Object.keys(deps))(...Object.values(deps)),reads};
}
const shared=[];
function suite(api){
 const out=[];
 for(const name of ['Report.PDF','report.hwpx','.hidden','a.b.C','no-extension','space name.docx','C:file.pdf','한글.HWP']){
  const actual=api.nameKey('fixture',name);
  const dot=name.lastIndexOf('.');
  assert.equal(actual,'fixture|'+(dot>0?name.slice(0,dot):name).toLowerCase());
  out.push({name,actual});
 }
 for(let mask=0;mask<32;mask++){
  const rec={channel:'fixture',name:'report.pdf'};
  const state={slack_files:{},excluded:{},deferred:{},superseded:{}};
  if(mask&1)state.slack_files.F={channel:'other',name:'different.pdf'};
  if(mask&2)state.excluded.EX={...rec};
  if(mask&4)state.deferred.DF={...rec,until:'2024-01-10'};
  if(mask&8)state.superseded.SUP={...rec};
  if(mask&16)state.slack_files.OTHER={...rec,name:'report.hwpx'};
  const before=structuredClone(state);
  const filters=api.docFilters(state);
  for(const id of ['F','']){
   const candidate={id,...rec};
   const want=id&&(mask&1)?'known':mask&2?'excluded':mask&4?'deferred':mask&8?'superseded':mask&16?'other_format':null;
   const actual=api.classifyDoc(candidate,filters);
   assert.equal(actual,want,'precedence mask '+mask+' id '+id);
   assert.deepEqual(state,before);
   out.push({mask,id,actual});
   shared.push({state,rec:candidate,want});
  }
 }
 for(const [section,want] of [['slack_files','known'],['excluded','excluded'],['deferred','deferred'],['superseded','superseded']]){
  const state={[section]:{F:{channel:'fixture',name:'report.pdf',until:'2024-01-11'}}};
  const rec={id:'F',channel:'different',name:'new.pdf'};
  const actual=api.classifyDoc(rec,api.docFilters(state));
  assert.equal(actual,want);
  out.push({section,actual});shared.push({state,rec,want});
 }
 for(const [until,want] of [['2024-01-09',null],['2024-01-10','deferred'],['2024-01-11','deferred'],['bad',null],['',null]]){
  const state={deferred:{DF:{channel:'fixture',name:'held.pdf',until}}};
  const rec={id:'new',channel:'fixture',name:'held.hwpx'};
  const actual=api.classifyDoc(rec,api.docFilters(state));
  assert.equal(actual,want);out.push({until,actual});shared.push({state,rec,want});
 }
 const unusual={slack_files:{a:null,b:[],c:1,d:'text',e:{channel:'fixture',name:'valid.pdf'}}};
 const f=api.docFilters(unusual);
 assert.deepEqual([...f.known],['a','b','c','d','e']);
 assert.deepEqual([...f.knownNames],['fixture|valid']);
 out.push({unusual:Object.fromEntries(Object.entries(f).map(([k,v])=>[k,[...v]]))});
 assert.equal(api.classifyDoc(null,api.docFilters(null)),null);
 assert.equal(api.classifyDoc({},api.docFilters({})),null);
 const first=api.docFilters({});first.known.add('LOCAL');
 assert.equal(api.docFilters({}).known.has('LOCAL'),false);
 return out;
}
function integration(text){
 const state={slack_files:{K:{channel:'fixture',name:'known.hwpx'}},
  excluded:{X:{channel:'fixture',name:'excluded.pdf'}},
  deferred:{D:{channel:'fixture',name:'held.pdf',until:'2024-01-10'}},
  superseded:{S:{channel:'fixture',name:'old.pdf'}}};
 const api=facade(text,state);
 const items=['known.pdf','excluded.hwpx','held.pdf','old.pdf','new.pdf','new.hwpx','other.xlsx','ignore.txt']
  .map(name=>({channel:'fixture',name}));
 items.push(null,{channel:'fixture'});
 const before=structuredClone(items);
 assert.equal(api.unconvertedAmong(items),2);
 assert.deepEqual(items,before);
 assert.equal(api.reads.length,1);
 const empty=facade(text,null);
 assert.equal(empty.unconvertedAmong(items),0);
 return {count:api.unconvertedAmong(items),reads:api.reads};
}
const nativeNow=Date.now;
Date.now=()=>NOW;
try{
 const direct=suite(createHealthDocFilters({activeDeferred}));
 const fixtures=shared.splice(0);
 assert.deepEqual(suite(facade(source)),direct);
 shared.length=0;
 const integrated=integration(source);
 // Compile only selected Python function AST nodes, never importing the operational script.
 const pythonSource=fs.readFileSync(new URL('../.claude/skills/doc-archive/scripts/fetch_slack_files.py',import.meta.url),'utf8');
 const py=`
import ast,json,sys
from datetime import date
data=json.loads(sys.stdin.buffer.read().decode('utf-8'))
names={'stem','name_key','deferred_is_active','active_deferred','load_filters','classify'}
tree=ast.parse(data['source'])
selected=[n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name in names]
assert {n.name for n in selected}==names
scope={'date':date,'today_kst':lambda:date(2024,1,10)}
exec(compile(ast.Module(body=selected,type_ignores=[]),'<fixture-functions>','exec'),scope)
values=[scope['classify'](f['rec'],scope['load_filters'](f['state'])) for f in data['fixtures']]
sys.stdout.write(json.dumps(values))
`;
 const result=spawnSync('python',['-B','-c',py],{input:JSON.stringify({source:pythonSource,fixtures}),encoding:'utf8'});
 assert.equal(result.status,0,result.stderr||result.error?.message);
 assert.deepEqual(JSON.parse(result.stdout),fixtures.map(f=>f.want),'Python shared decisions');
 if(process.argv.includes('--baseline-stdin')){
  const old=JSON.parse(fs.readFileSync(0,'utf8'));assert.equal(typeof old,'string');
  assert.deepEqual(suite(facade(old)),direct);
  assert.deepEqual(integration(old),integrated);
  console.log('Baseline SHA256: '+createHash('sha256').update(old).digest('hex'));
 }
 console.log('Health doc filters: '+direct.length+' contracts, '+fixtures.length+' Python comparisons, unconverted integration passed.');
}finally{Date.now=nativeNow;}
