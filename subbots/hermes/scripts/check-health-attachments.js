#!/usr/bin/env node
/** R3n: attachment collection contracts with fake Slack and memory doc state.
 * --baseline-stdin accepts an independent archive-health.js JSON string.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { createHash } from 'node:crypto';
import { createHealthAttachments } from '../src/archive-health/attachments.js';
import { createHealthDocuments } from '../src/archive-health/documents.js';
import { createHealthDocFilters } from '../src/archive-health/doc-filters.js';
import { createHealthStatus } from '../src/archive-health/status.js';
import { compose } from '../src/archive-health/report.js';
import { createSlackLiveApi } from '../src/slack/live-api.js';
const source=fs.readFileSync(new URL('../src/archive-health.js',import.meta.url),'utf8');
const slackSource=fs.readFileSync(new URL('../src/slack-live.js',import.meta.url),'utf8');
function declaration(text,name){
 const match=text.match(new RegExp('(?:export )?function '+name+'\\([^]*?\\n\\}'));
 assert.ok(match,name);return match[0].replace(/^export /,'');
}
const isBotMessage=compileFunction(declaration(slackSource,'isBotMessage')+'\nreturn isBotMessage;')();
const extOf=compileFunction(declaration(source,'extOf')+'\nreturn extOf;')();
const PUBLIC_TAG=compileFunction(source.match(/const PUBLIC_TAG = [^;]+;/)[0]+'\nreturn PUBLIC_TAG;')();
const activeSource=fs.readFileSync(new URL('../src/ingest/util.js',import.meta.url),'utf8');
const clone=x=>structuredClone(x);
function facade(text,f){
 const forbidden=()=>{throw new Error('unexpected dependency');};
 const deps={path,config:f.config,ARCHIVE_DIR:'/fixture/archive',DOCS_DIR:'/fixture/docs',
  fs:{readFileSync(p,encoding){f.trace.push(['read',p,encoding]);assert.equal(p,path.join('/fixture/docs','.doc-state.json'));return JSON.stringify(f.state);},existsSync:()=>false},
  isPrivateChannel:x=>x==='private',
  // 실물은 slack-live.js 의 dropSkippedChannels(개명 되짚기 포함) — 여기서는 지도 없는
  // 판으로 채운다. 되짚기 자체는 check-skip-channels.js ①-b·check-health-documents.js 가 잰다.
  dropSkippedChannels:chs=>{
   const s=new Set((f.config.digest?.skipChannels||[]).map(x=>String(x||'').replace(/^#/,'').trim()));
   return (chs||[]).filter(c=>!s.has(String(c?.name||'').replace(/^#/,'').trim()));
  },
  listBotChannels:async()=>{f.trace.push(['channels']);return clone(f.channels);},
  fetchAllReplies:f.fetchAllReplies,isBotMessage,DEFAULT_LIVE_FETCH_MAX_DAYS:14,
  chunkForSlack:forbidden,logConversation:forbidden,SKILL_SCRIPTS:{},runScript:forbidden,
  activeDeferred:f.activeDeferred,suppress:forbidden,syncForRead:forbidden,
  compose,createHealthStatus,createHealthDocFilters,createHealthAttachments,createHealthDocuments,
  // 개명 지도. 하네스는 슬랙도 파일도 안 보므로 **항등**으로 고정한다 —
  // 되짚기가 갈리는지는 check-shared-rules.js ①-d 가 두 언어를 대봐서 본다.
  archiveChannelNames:()=>new Map(),archiveChannelOf:(n)=>n};
 const code=text.replace(/^import\s[\s\S]*?;\r?\n/gm,'').replace(/^export \{[^}]*\};?\r?$/gm,'').replace(/^export /gm,'');
 return compileFunction(code+'\nreturn {channelAttachments,pendingDocuments};',
  Object.keys(deps))(...Object.values(deps));
}
async function suite(make){
 const out=[];
 function fixture(){
  const trace=[],unexpected=[],queues={history:[],replies:[]};
  const config={timezone:'UTC',digest:{skipChannels:['skip'],health:{}},limits:{threadLookbackDays:30}};
  const f={trace,unexpected,queues,config,state:{},channels:[{id:'C1',name:'fixture'}]};
  f.activeDeferred=compileFunction(declaration(activeSource,'activeDeferred')+'\nreturn activeDeferred;',['config'])(config);
  f.fetchAllReplies=createSlackLiveApi().fetchAllReplies;
  const call=name=>async args=>{
   trace.push([name,clone(args)]);
   if(!queues[name].length){unexpected.push([name,args]);throw new Error('unexpected '+name);}
   const value=queues[name].shift();if(value instanceof Error)throw value;return clone(value);
  };
  f.client={conversations:{history:call('history'),replies:call('replies')}};
  f.api=make(f);assert.deepEqual(trace,[]);
  return f;
 }
 async function check(name,fn){
  const f=fixture();const value=await fn(f);
  assert.deepEqual(f.unexpected,[],name+': swallowed unexpected API call');
  for(const q of Object.values(f.queues))assert.equal(q.length,0,name+': unused response');
  out.push({name,value:clone(value),trace:clone(f.trace)});
 }
 const file=(id,name='report.pdf',more={})=>({id,name,url_private_download:'fixture-url',...more});
 const msg=(ts,files=[],more={})=>({ts:String(ts),files,...more});
 const collect=f=>f.api.channelAttachments(f.client,{id:'C1',name:'fixture'},100,50);
 await check('empty pages follow cursor',async f=>{
  f.queues.history=[{response_metadata:{next_cursor:'next'}},{}];
  const value=await collect(f);assert.deepEqual(value,[]);
  assert.deepEqual(f.trace.map(x=>x[1]),[{channel:'C1',oldest:'50',limit:200,cursor:undefined},{channel:'C1',oldest:'50',limit:200,cursor:'next'}]);
  return value;
 });
 await check('file fields and oldest boundary',async f=>{
  f.queues.history=[{messages:[msg(99,[file('old')]),msg(100,[file('F','Report.HWP',{filetype:'binary'}),{id:'N',filetype:'PDF'},{}])]}];
  const value=await collect(f);
  assert.deepEqual(value,[
   // `channel` 은 사업장 이름, `slackChannel` 은 슬랙의 현재 이름이다. 개명 안 한
   // 채널에서는 둘이 같다 — 갈리는 경우는 아래 'renamed channel' 회차가 본다.
   {id:'F',name:'Report.HWP',ext:'hwp',channel:'fixture',slackChannel:'fixture',ts:'100',publicTag:false,hasUrl:true},
   {id:'N',name:'N',ext:'pdf',channel:'fixture',slackChannel:'fixture',ts:'100',publicTag:false,hasUrl:false}]);
  return value;
 });
 await check('default parent window',async f=>{
  f.queues.history=[{}];await f.api.channelAttachments(f.client,{id:'C1',name:'fixture'},100);
  assert.equal(f.trace[0][1].oldest,'100');
 });
 for(const subtype of [{bot_id:'B'},{app_id:'APP'},{subtype:'bot_message'}]){
  await check('bot attachment excluded '+JSON.stringify(subtype),async f=>{
   f.queues.history=[{messages:[msg(100,[file('BOT')],subtype),msg(101,[file('HUMAN')],{user:'U1'})]}];
   const value=await collect(f);assert.deepEqual(value.map(x=>x.id),['HUMAN']);return value;
  });
 }
 for(const [text,want] of [['[공개]',true],['검토 후 [공개] 승인',true],['공개해도 되나요?',false],['[비공개]',false]]){
  await check('thread tag '+text,async f=>{
   const parent=msg(100,[file('P')],{thread_ts:'100',reply_count:2,latest_reply:'102'});
   f.queues.history=[{messages:[parent]}];
   f.queues.replies=[{messages:[parent,msg(101,[file('R')]),msg(102,[],{text})]}];
   const value=await collect(f);assert.deepEqual(value.map(x=>[x.id,x.publicTag]),[['P',want],['R',want]]);return value;
  });
 }
 await check('old parent with new reply and tag',async f=>{
  const parent=msg(60,[file('OLD')],{thread_ts:'60',reply_count:2,latest_reply:'101'});
  f.queues.history=[{messages:[parent]}];
  f.queues.replies=[{messages:[parent,msg(99,[file('OLDREPLY')]),msg(101,[file('NEW')],{text:'[공개]'})]}];
  const value=await collect(f);assert.deepEqual(value.map(x=>x.id),['NEW']);assert.equal(value[0].publicTag,true);return value;
 });
 await check('latest_reply fallback and reply paging',async f=>{
  const parent=msg(100,[file('P')],{thread_ts:'100',reply_count:2});
  f.queues.history=[{messages:[parent]}];
  f.queues.replies=[{messages:[parent,msg(101,[file('R')])],response_metadata:{next_cursor:'next'}},
   {messages:[parent,msg(101,[file('R')]),msg(102,[file('S')])]}];
  const value=await collect(f);assert.deepEqual(value.map(x=>x.id),['P','R','S']);return value;
 });
 for(const fields of [{thread_ts:'60',reply_count:1,latest_reply:'99'},{reply_count:1,latest_reply:'101'},{thread_ts:'100',reply_count:0}]){
  await check('reply gate '+JSON.stringify(fields),async f=>{
   f.queues.history=[{messages:[msg(100,[file('P')],fields)]}];const value=await collect(f);
   assert.equal(f.trace.filter(x=>x[0]==='replies').length,0);return value;
  });
 }
 await check('parent tag alone does not approve',async f=>{
  f.queues.history=[{messages:[msg(100,[file('P')],{text:'[공개]'})]}];
  const value=await collect(f);assert.equal(value[0].publicTag,false);return value;
 });
 await check('bot tag is retained but bot files excluded',async f=>{
  const parent=msg(100,[file('P')],{thread_ts:'100',reply_count:1,latest_reply:'101'});
  f.queues.history=[{messages:[parent]}];f.queues.replies=[{messages:[parent,msg(101,[file('BOT')],{bot_id:'B',text:'[공개]'})]}];
  const value=await collect(f);assert.deepEqual(value.map(x=>[x.id,x.publicTag]),[['P',true]]);return value;
 });
 for(const stage of ['history-first','history-second','replies-first','replies-second']){
  await check('failure rejects '+stage,async f=>{
   const err=Object.assign(new Error('fixture error'),{data:{error:'fixture_error'}});
   const parent=msg(100,[file('P')],{thread_ts:'100',reply_count:1,latest_reply:'101'});
   if(stage==='history-first')f.queues.history=[err];
   if(stage==='history-second')f.queues.history=[{messages:[msg(100,[file('P')])],response_metadata:{next_cursor:'next'}},err];
   if(stage.startsWith('replies')){
    f.queues.history=[{messages:[parent]}];
    f.queues.replies=stage==='replies-first'?[err]:[{messages:[parent],response_metadata:{next_cursor:'next'}},err];
   }
   await assert.rejects(collect(f),e=>e===err);
  });
 }
 if(make.integration){
  await check('pendingDocuments dedup restricted and approval integration',async f=>{
   f.channels=[{id:'C1',name:'private'},{id:'SKIP',name:'skip'}];
   const parent=msg(100,[file('PDF','same.pdf'),file('HWP','same.hwpx'),file('LOCK','locked.pdf',{url_private_download:undefined})],
    {thread_ts:'100',reply_count:1,latest_reply:'101'});
   f.queues.history=[{messages:[parent]}];f.queues.replies=[{messages:[parent,msg(101,[],{text:'[공개]'})]}];
   const value=await f.api.pendingDocuments(f.client,{scanDays:1,now:100000});
   assert.equal(value.total,1);assert.equal(value.restricted.length,1);assert.equal(value.approvals.length,2);
   assert.deepEqual(value.byChannel,[{channel:'private',count:1,newest:'same.hwpx'}]);
   assert.equal(f.trace.filter(x=>x[0]==='history').length,1);
   return value;
  });
  await check('pendingDocuments marks failed channel without partial attachments',async f=>{
   const err=Object.assign(new Error('failure'),{data:{error:'fixture_error'}});
   f.queues.history=[{messages:[msg(100,[file('P')])],response_metadata:{next_cursor:'next'}},err];
   const value=await f.api.pendingDocuments(f.client,{scanDays:1,now:100000});
   assert.equal(value.total,0);assert.deepEqual(value.failed,['#fixture(fixture_error)']);return value;
  });
 }
 return out;
}
const direct=await suite(f=>({channelAttachments:createHealthAttachments({fetchAllReplies:f.fetchAllReplies,isBotMessage,extOf,PUBLIC_TAG}).channelAttachments}));
const via=f=>facade(source,f);via.integration=true;
const actual=await suite(via);
assert.deepEqual(actual.slice(0,direct.length),direct,'module vs facade');
/* 개명된 채널 — `channel` 은 사업장 이름, `slackChannel` 은 슬랙의 현재 이름이라야 한다.
 * 위 회차들은 개명 안 한 채널이라 둘이 같아서, 되짚기를 통째로 지워도 안 빨개진다.
 * **가르지 못하는 입력은 검사가 아니다** (check-shared-rules.js 머리말과 같은 이유). */
{
 const {channelAttachments}=createHealthAttachments({
  fetchAllReplies:createSlackLiveApi().fetchAllReplies,isBotMessage,extOf,PUBLIC_TAG,
  archiveChannel:(n)=>n==='now'?'was':n});
 const pages=[{messages:[{ts:'100',files:[{id:'F',name:'report.pdf',url_private_download:'u'}]}]}];
 const client={conversations:{history:async()=>pages.shift()||{},replies:async()=>({})}};
 const got=await channelAttachments(client,{id:'C1',name:'now'},100,100);
 assert.deepEqual(got,[{id:'F',name:'report.pdf',ext:'pdf',channel:'was',slackChannel:'now',
  ts:'100',publicTag:false,hasUrl:true}],'개명된 채널의 사업장 이름');
}

if(process.argv.includes('--baseline-stdin')){
 const old=JSON.parse(fs.readFileSync(0,'utf8'));assert.equal(typeof old,'string');
 const baseline=f=>facade(old,f);baseline.integration=true;
 assert.deepEqual(await suite(baseline),actual,'independent baseline');
 console.log('Baseline SHA256: '+createHash('sha256').update(old).digest('hex'));
}
console.log('Health attachments: '+direct.length+' contracts + '+(actual.length-direct.length)+' pendingDocuments integrations + 개명 1건 passed.');
