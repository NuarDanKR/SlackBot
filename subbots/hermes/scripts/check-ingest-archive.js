#!/usr/bin/env node
/** R3p: offline archive contracts. Operational imports are never evaluated.
 * --baseline-stdin compares independent pre-extraction source (JSON string).
 * --skip-python runs only JS contracts; default also checks Python in memory.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { compileFunction } from 'node:vm';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import { createArchiveApi } from '../src/ingest/archive-api.js';
import { createArchiveFormat } from '../src/ingest/archive-format.js';
import { createArchiveChanges } from '../src/ingest/archive-changes.js';
import { createSlackLiveApi } from '../src/slack/live-api.js';
import { createSlackLiveRender } from '../src/slack/live-render.js';

const source=fs.readFileSync(new URL('../src/ingest/slack-archive.js',import.meta.url),'utf8');
const live=fs.readFileSync(new URL('../src/slack-live.js',import.meta.url),'utf8');
const configSource=fs.readFileSync(new URL('../src/config.js',import.meta.url),'utf8');
function declaration(text,name){
 const m=text.match(new RegExp('(?:export )?function '+name+'\\([^]*?\\n\\}'));assert.ok(m,name);
 return m[0].replace(/^export /,'');
}
const names=['SKIP_SUBTYPES','CORRECTION_RE','BOT_AUTHOR_LABEL','BOT_BLOCK_BODY'];
const constants=names.map(n=>{
 const m=live.match(new RegExp('export const '+n+' = [^]*?;'));assert.ok(m,n);return m[0].replace(/^export /,'');
}).join('\n');
const mark=configSource.match(/export const BOT_ANSWER_MARK = [^]*?;/);assert.ok(mark);
const rules=compileFunction(constants+'\n'+mark[0].replace(/^export /,'')+'\n'+
 ['isBotMessage','isThreadBroadcast','keepMessage'].map(n=>declaration(live,n)).join('\n')+
 '\nreturn { SKIP_SUBTYPES,CORRECTION_RE,BOT_AUTHOR_LABEL,BOT_BLOCK_BODY,BOT_ANSWER_MARK,isBotMessage,keepMessage };')();
const quiet={log(){},warn(){},error(){}};
const rendered=createSlackLiveRender({listAllChannels:()=>[],console:quiet});
const common={...rules,renderText:rendered.renderText,stamp:rendered.stamp,
 permalink:(ch,ts)=>'https://example.invalid/'+ch+'/'+ts};
const format=createArchiveFormat(common);
const apiDeps={fetchAllReplies:createSlackLiveApi({console:quiet}).fetchAllReplies};
const NOW=Date.parse('2024-01-10T12:00:00Z');
class FixedDate extends Date {constructor(...a){super(...(a.length?a:[NOW]));}static now(){return NOW;}}
const userMap=new Map([['U1','Writer'],['U2','Reader']]),tz='UTC',selfId='SELF';
const ts=n=>String(Date.parse('2024-01-10T00:00:00Z')/1000+n*60);
const message=(minute,text='body',extra={})=>({ts:ts(minute),user:'U1',text,...extra});
const reply=(minute,text='reply',extra={})=>message(minute,text,{user:'U2',...extra});
function facade(text,overrides={}){
 const deps={...common,...apiDeps,fs:{},path,config:{},ARCHIVE_DIR:'/fixture/archive',
 CHANNELS_DIR:'/fixture/archive/channels',isPrivateChannel:()=>false,normalizeChannel:x=>x.replace(/^#/,''),
 channelRefsIn:()=>[],listBotChannels:async()=>[],getUserMap:async()=>userMap,getSelfId:async()=>selfId,
 SKILL_SCRIPTS:{insertMessages:'insert-fixture'},runScript:()=>{throw new Error('unexpected script');},
 writeTemp:()=>{throw new Error('unexpected temp');},readJson:()=>null,writeJson:()=>{throw new Error('unexpected write');},
 activeDeferred:()=>[],Date:FixedDate,createArchiveApi,createArchiveFormat,createArchiveChanges,...overrides};
 // 실물은 slack-live.js 의 dropSkippedChannels(개명 되짚기 포함) — 여기서는 지도 없는
 // 판(normalizeChannel 대조)으로 채운다. 되짚기 자체는 check-skip-channels.js ①-b 가 잰다.
 if(!deps.dropSkippedChannels)deps.dropSkippedChannels=(chs,skip=deps.config?.digest?.skipChannels||[])=>{
  const s=new Set((skip||[]).map(x=>String(x||'').replace(/^#/,'').trim()));
  return (chs||[]).filter(c=>!s.has(String(c?.name||'').replace(/^#/,'').trim()));
 };
 const code=text.replace(/^import\s[\s\S]*?;\r?\n/gm,'').replace(/^export \{[^}]*\};?\r?$/gm,'').replace(/^export /gm,'');
 return compileFunction(code+'\nreturn {fetchNewMessages,fetchReplies,fetchWindow,grownThreads,'+
 'renderMessage,renderReply,renderBotPlaceholder,monthOf,parseBlocks,BODY_TAIL,UNREAD_REPLIES_MARK,'+
 'writtenFrom,detectChanges,buildPending,ingestChannel,ingestConversations};',Object.keys(deps))(...Object.values(deps));
}
const pure=()=>({...createArchiveApi(apiDeps),...format,
 ...createArchiveChanges({...common,parseBlocks:format.parseBlocks,renderReply:format.renderReply})});
async function contracts(make){
 const results=[];
 async function test(name,run){
  const value=await run(make());results.push({name,value:structuredClone(value)});
 }
 await test('history pages, boundary duplicate, ascending order',async a=>{
  const trace=[],pages=[{messages:[message(5),message(3)],response_metadata:{next_cursor:'next'}},{messages:[message(4),message(2)]}];
  const client={conversations:{history:async args=>{trace.push(args);return pages.shift();}}};
  const out=await a.fetchNewMessages(client,'C1',ts(3));
  assert.deepEqual(out.map(m=>m.ts),[ts(2),ts(4),ts(5)]); // only equal boundary is removed, as before
  assert.equal(trace[0].inclusive,false);assert.equal(trace[1].cursor,'next');assert.equal(trace[0].limit,200);
  return {out,trace};
 });
 await test('lookback six decimal oldest and inclusive bounds',async a=>{
  const trace=[],pages=[{messages:[message(7)],response_metadata:{next_cursor:'n'}},{messages:[message(4)]}];
  const out=await a.fetchWindow({conversations:{history:async args=>{trace.push(args);return pages.shift();}}},'C1',ts(9)+'.123456',123.1);
  assert.match(out.oldest,/^\d+\.\d{6}$/);assert.equal(trace[1].latest,ts(9)+'.123456');assert.equal(trace[0].inclusive,true);
  assert.deepEqual(out.messages.map(m=>m.ts),[ts(4),ts(7)]);return {out,trace};
 });
 for(const fn of ['fetchNewMessages','fetchWindow']){
  await test(fn+' rejects after partial page',async a=>{
   let calls=0;await assert.rejects(()=>a[fn]({conversations:{history:async()=>{
    if(calls++)throw new Error('page failed');return {messages:[message(4)],response_metadata:{next_cursor:'next'}};
   }}},'C1',ts(3),120),/page failed/);assert.equal(calls,2);return calls;
  });
 }
 await test('replies deduplicate all pages and exclude parent',async a=>{
  const trace=[],pages=[{messages:[message(3),reply(4)],response_metadata:{next_cursor:'n'}},{messages:[reply(4),reply(5)]}];
  const out=await a.fetchReplies({conversations:{replies:async args=>{trace.push(args);return pages.shift();}}},'C1',ts(3));
  assert.deepEqual(out.map(m=>m.ts),[ts(4),ts(5)]);return {out,trace};
 });
 await test('grown thread threshold and missing latest_reply',a=>{
  const out=a.grownThreads([message(1,'x',{reply_count:1,latest_reply:ts(9)}),message(2,'x',{reply_count:1}),
   message(3,'x',{reply_count:1,latest_reply:ts(8)}),message(4,'x',{reply_count:0,latest_reply:ts(9)})],ts(8));
  assert.deepEqual(out.map(m=>m.ts),[ts(1)]);return out;
 });
 for(const newline of ['\n','\r\n']){
  await test('render/parse multiline, attachments, bots '+JSON.stringify(newline),a=>{
   const m=message(5,'hello <@U2>\n> quote',{files:[{name:'file.pdf'}],reactions:[{name:'ignore'}]});
   const rs=[reply(6,'line1\nline2',{files:[{name:'r.xlsx'}]}),reply(7,'bot secret',{bot_id:'B'})];
   const md=a.renderMessage(m,rs,userMap,tz,selfId,{unreadReplies:2});
   assert.ok(md.includes('@Reader'));assert.ok(md.includes('\n> line2'));assert.ok(!md.includes('bot secret'));
   assert.ok(!md.includes('ignore'));assert.ok(md.includes(a.UNREAD_REPLIES_MARK));
   const parsed=a.parseBlocks(md.replace(/\n/g,newline));
   assert.equal(parsed.length,1);assert.equal(parsed[0].body,'hello @Reader\n> quote');
   assert.equal(parsed[0].replies.length,1);assert.ok(parsed[0].replies[0].line.includes('\n> line2'));
   return {md,parsed};
  });
 }
 await test('legacy reply and compressed block boundaries',a=>{
  const md='**2024-01-10 00:05 · Writer** old compressed\n> **└ 2024-01-10 00:06 · Reader** — a\n> next\n> **└ Old** — legacy\n## 2023-12\nunrelated';
  const out=a.parseBlocks(md);assert.equal(out[0].compressed,true);assert.equal(out[0].replies.length,1);
  assert.ok(!out[0].replies[0].line.includes('legacy'));return out;
 });
 await test('placeholder hides bot body, month follows timezone',a=>{
  const md=a.renderBotPlaceholder('C1',ts(1),['reply'],tz);
  assert.ok(md.includes(rules.BOT_BLOCK_BODY));assert.ok(md.includes('example.invalid'));
  assert.equal(a.monthOf('1706742000','Asia/Seoul'),'2024-02');return md;
 });
 await test('written identity and last report',a=>{
  const out=a.writtenFrom('REPORT {"written":["old"]}\nnoise\nREPORT {"written":["prefix\\nnew\\nsuffix"]}', ['old','new','absent']);
  assert.deepEqual(out,['new']);return out;
 });
 for(const report of ['', 'REPORT nope', 'REPORT {}', 'REPORT {"written":null}']){
  await test('invalid report '+report,a=>{assert.throws(()=>a.writtenFrom(report,['x']));return true;});
 }
 const old=message(5,'old'),r=reply(6,'old reply');
 const input=()=>({md:format.renderMessage(old,[r],userMap,tz,selfId),windowMsgs:[old],
 repliesByTs:new Map(),userMap,tz,selfId,oldest:ts(0),lastTs:ts(10),editedSince:Number(ts(8)),pendingKeys:new Set()});
 await test('recent edit and renamed author',a=>{
  const x=input();x.windowMsgs=[message(5,'new',{user:'U2',edited:{ts:ts(9)},reply_count:1})];
  const out=a.detectChanges(x);assert.equal(out.edited.length,1);assert.equal(out.deleted.length,0);return out;
 });
 await test('old edit stays pending until matching',a=>{
  const x=input();x.windowMsgs=[message(5,'new',{edited:{ts:ts(7)},reply_count:1})];
  assert.equal(a.detectChanges(x).edited.length,0);
  x.pendingKeys.add('edited|message|2024-01-10 00:05 · Writer');
  const out=a.detectChanges(x);assert.equal(out.edited.length,1);
  x.windowMsgs[0].text='old';assert.equal(a.detectChanges(x).edited.length,0);return out;
 });
 await test('same-minute ambiguity warns without guesses',a=>{
  const x=input();x.md+='\n\n'+format.renderMessage(message(5,'second',{user:'U2'}),[],userMap,tz,selfId);
  x.windowMsgs=[];const out=a.detectChanges(x);assert.equal(out.deleted.length,0);assert.equal(out.notes.length,1);return out;
 });
 await test('bots and system records count as existing',a=>{
  const x=input();x.md=format.renderMessage(old,[],userMap,tz,selfId);
  x.windowMsgs=[message(5,'bot',{bot_id:'B',edited:{ts:ts(9)}}),message(5,'join',{subtype:'channel_join'})];
  // 여기서 새 필드(incomplete·retry)를 보지 않는다 — 이 케이스는 --baseline-stdin 에서
  // 리팩토링 전 소스로도 그대로 돌고, 그쪽은 그 필드를 낼 수 없다. 새 필드의 계약은
  // retryRegressions 의 'detectChanges splits …' 가 따로 못 박는다.
  const out=a.detectChanges(x);assert.deepEqual([out.edited,out.deleted,out.notes],[[],[],[]]);return out;
 });
 await test('both minute boundaries excluded',a=>{
  const x=input();x.md=[message(0),message(10)].map(m=>format.renderMessage(m,[],userMap,tz,selfId)).join('\n\n');
  x.windowMsgs=[];const out=a.detectChanges(x);assert.equal(out.deleted.length,0);return out;
 });
 await test('failed reply read is not deletion, absent thread is',a=>{
  const x=input();x.windowMsgs=[{...old,reply_count:1}];assert.equal(a.detectChanges(x).deleted.length,0);
  x.windowMsgs=[old];const out=a.detectChanges(x);assert.equal(out.deleted.length,1);assert.equal(out.deleted[0].scope,'reply');return out;
 });
 await test('multiline reply edits and preserved bot presence',a=>{
  const x=input();x.windowMsgs=[{...old,reply_count:1}];
  x.repliesByTs.set(old.ts,[reply(6,'new\nsecond',{edited:{ts:ts(9)}})]);
  const out=a.detectChanges(x);assert.equal(out.edited.length,1);assert.ok(out.edited[0].after.includes('\n> second'));
  x.repliesByTs.set(old.ts,[reply(6,'bot',{bot_id:'B'})]);assert.equal(a.detectChanges(x).deleted.length,0);return out;
 });
 await test('compressed edit manual and overflow warning',a=>{
  const x=input();x.md='**2024-01-10 00:05 · Writer** compressed';
  x.windowMsgs=[message(5,'new',{edited:{ts:ts(9)}})];
  const out=a.detectChanges(x);assert.equal(out.edited[0].manual,true);
  x.maxFindings=0;const capped=a.detectChanges(x);assert.equal(capped.edited.length,0);assert.ok(capped.notes.length);
  return {out,capped};
 });
 await test('pending identity preserves firstSeen and kind-specific fields',a=>{
  const edited={scope:'message',key:'key',header:'h',replyKey:null,before:'old',after:'new',manual:true};
  const result={id:'C1',channel:'fixture',file:'old-name',edited:[edited],deleted:[{...edited,key:'deleted'}]};
  const id='C1|edited|message|key';
  const out=a.buildPending([result],{items:[{id,firstSeen:'2024-01-01'}]},'2024-01-10');
  assert.equal(out.items[0].firstSeen,'2024-01-01');assert.equal(out.items[0].manual,true);
  assert.ok(!('after' in out.items[1]));assert.equal(out.items[1].firstSeen,'2024-01-10');return out;
 });
 return results;
}
function fixture(text){
 const trace=[],files=new Map(),temp=new Map();
 const dir=path.join('/fixture/archive','channels'),mdPath=path.join(dir,'fixture.md');
 const f={trace,files,temp,config:{timezone:tz,limits:{threadLookbackDays:30},digest:{skipChannels:[],ingest:{}}},
  state:{channels:{C1:{name:'fixture',file:'fixture',last_ts:ts(10),threads:{}}}},channels:[{id:'C1',name:'fixture'}],
  raw:[],window:[],replyMap:new Map(),replyFail:new Set(),scriptFail:false,writes:[],scriptWritten:null};
 const fakeFs={existsSync:p=>files.has(p),readFileSync:p=>{if(!files.has(p))throw new Error('missing');return files.get(p);},
  unlinkSync:p=>{trace.push(['unlink',p]);files.delete(p);}};
 const readJson=p=>{trace.push(['readJson',p]);return p.endsWith('.sync-state.json')?f.state:
  files.has(p)?JSON.parse(files.get(p)):null;};
 const deps={fs:fakeFs,config:f.config,readJson,writeJson:(p,v)=>{trace.push(['writeJson',p,structuredClone(v)]);files.set(p,JSON.stringify(v));},
  activeDeferred:()=>f.held||[],listBotChannels:async()=>f.channels,
  getUserMap:async(_,{force})=>{assert.equal(force,true);return userMap;},
  writeTemp:(name,body)=>{const p='/fixture/temp/'+name;temp.set(p,body);trace.push(['temp',p,body]);return p;},
  runScript:async(script,args)=>{
   trace.push(['script',script,args]);if(f.scriptFail)return {ok:false,stdout:'',stderr:'fixture failed'};
   // 파이썬이 **성공하면서** 남기는 `NOTE:` 줄 — 사람에게 전할 것이 성공 회차에도 나온다
   if(f.scriptNote)return {ok:true,stderr:'',stdout:'NOTE: '+f.scriptNote+'\nOK 스레드 덧붙임 (이미 반영됨) — x.md'};
   const content=args.includes('--content-file')?temp.get(args[args.indexOf('--content-file')+1]):'';
   const written=f.scriptWritten??(content?[content.trimEnd()]:[]);
   files.set(mdPath,files.get(mdPath)+'\n'+written.join('\n'));
   return {ok:true,stderr:'',stdout:f.badReport?'no report':'REPORT '+JSON.stringify({written})};
  }};
 f.api=facade(text,deps);f.mdPath=mdPath;files.set(mdPath,format.renderMessage(message(5,'parent'),[],userMap,tz,selfId));
 f.client={conversations:{
  history:async args=>{trace.push(['history',args]);if(f.historyFail || f.historyFailChannels?.has(args.channel))throw new Error('history failed');
   return {messages:args.limit===1?[message(20)]:structuredClone(args.latest?f.window:f.raw)};},
  replies:async args=>{trace.push(['replies',args]);if(f.replyFail.has(args.ts))throw new Error('reply failed');
   return {messages:[{ts:args.ts},...(f.replyMap.get(args.ts)||[])]};}}};
 f.run=(opts={})=>f.api.ingestChannel(f.client,f.channels[0],f.state,{
 userMap,selfId,tz,dry:false,editedSince:Number(ts(8)),pendingKeys:new Set(),scanEdits:false,...opts});
 f.snapshot=()=>({state:structuredClone(f.state),files:[...files]});
 return f;
}
async function integration(text,{legacy=false}={}){
 const results=[];
 async function test(name,run){
  const f=fixture(text);const value=await run(f);
  results.push({name,value:structuredClone(value),snapshot:f.snapshot(),trace:structuredClone(f.trace)});
 }
 await test('dry reports without temp files or state writes',async f=>{
  f.raw=[message(12)];const before=f.snapshot();const out=await f.run({dry:true});
  assert.equal(out.added,1);assert.deepEqual(f.snapshot(),before);
  assert.ok(!f.trace.some(x=>['script','temp','writeJson'].includes(x[0])));return out;
 });
 await test('undeclared private fails before IO',async f=>{
  f.channels[0].isPrivate=true;const before=f.snapshot();const out=await f.run();
  assert.equal(out.undeclaredPrivate,true);assert.deepEqual(f.trace,[]);assert.deepEqual(f.snapshot(),before);return out;
 });
 await test('new channel baseline only',async f=>{
  delete f.state.channels.C1;const out=await f.run();assert.equal(out.isNew,true);
  assert.equal(f.state.channels.C1.last_ts,ts(20));assert.equal(f.trace.length,1);return out;
 });
 await test('rename reuses persisted filename',async f=>{
  f.channels[0].name='renamed';const out=await f.run();
  assert.equal(out.file,'fixture');assert.deepEqual(out.renamed,{from:'fixture',to:'renamed',inConfig:[]});
  assert.equal(f.state.channels.C1.file,'fixture');return out;
 });
 await test('failed new-thread read retries after cursor advances',async f=>{
  f.raw=[message(12,'parent',{reply_count:1})];f.replyFail.add(ts(12));
  const first=await f.run();assert.equal(f.state.channels.C1.last_ts,ts(12));assert.equal(f.state.channels.C1.threads[ts(12)].unread,true);
  f.raw=[];f.window=[message(12,'parent',{reply_count:1,latest_reply:ts(13)})];
  // latest_reply equals last_ts on retry: unread, rather than growth, must select it.
  f.state.channels.C1.last_ts=ts(13);f.replyFail.clear();f.replyMap.set(ts(12),[reply(13)]);
  const second=await f.run();assert.equal(second.threadReplies,1);assert.equal(f.state.channels.C1.threads[ts(12)].unread,undefined);
  return {first,second};
 });
 /* 성공한 회차가 남긴 `NOTE:` 줄이 보고까지 온다 (2026-09-22).
  *
  * `insert_messages.py` 는 부모가 모호해도 붙일 답글이 전부 이미 있으면 정상 종료하는데,
  * 그때 **봇 답변 건수만은 못 올린다**. 멈추지 않는 것이 맞지만 조용히 넘기면 그 건수가
  * 영영 안 맞는다. 계약은 **문구가 아니라 `NOTE:` 접두**다 — 특정 문장을 JS 에 베껴 두면
  * 파이썬을 고칠 때 에러 없이 낡는다. 이 줄을 안 옮기면 md 는 멀쩡하고 사람만 못 본다. */
 await test('a NOTE line from a successful script run reaches the report',async f=>{
  f.raw=[];f.window=[message(12,'parent',{reply_count:1,latest_reply:ts(13)})];
  f.state.channels.C1.last_ts=ts(12);f.replyMap.set(ts(12),[reply(13)]);
  f.scriptNote='봇 답변 1건을 어느 블록에 더할지 못 골랐습니다';
  const out=await f.run();
  assert.ok(out.notes.some(n=>n.includes('못 골랐습니다')),JSON.stringify(out.notes));
  assert.ok(!out.notes.some(n=>n.startsWith('NOTE:')),'접두는 떼고 담는다');
  return out;
 });
 await test('out-of-lookback unread is reported',async f=>{
  f.state.channels.C1.threads[ts(1)]={reply_count:0,unread:true};
  const out=await f.run();assert.ok(out.notes.some(n=>n.includes('구간')));return out;
 });
 await test('reply partial write counted from actual lines; replay writes zero',async f=>{
  const rs=[reply(11,'old'),reply(12,'new')];
  f.window=[message(5,'parent',{reply_count:2,latest_reply:ts(12)})];f.replyMap.set(ts(5),rs);
  f.files.set(f.mdPath,format.renderMessage(message(5,'parent'),[rs[0]],userMap,tz,selfId));
  f.scriptWritten=[format.renderReply(rs[1],userMap,tz)];
  const first=await f.run();assert.equal(first.threadReplies,1);assert.equal(first.replyProbes.length,1);
  // Simulate stale state on replay: script reports no new lines.
  f.state.channels.C1.threads={};f.scriptWritten=[];
  const second=await f.run();assert.equal(second.threadReplies,0);assert.equal(second.replyProbes,undefined);
  return {first,second};
 });
 for(const self of [false,true]){
  await test('bot-parent identity '+self,async f=>{
   const rs=[reply(12,'hello'),reply(13,'[정정] corrected')];
   f.raw=[message(11,'secret bot body',{bot_id:'B',user:self?selfId:'OTHER',reply_count:2})];
   f.replyMap.set(ts(11),rs);
   const out=await f.run();assert.equal(out.selfCorrections||0,self?1:0);assert.equal(out.botReplies||0,self?0:2);
   assert.ok(!f.files.get(f.mdPath).includes('secret bot body'));return out;
  });
 }
 for(const kind of ['month','reply','bot','report']){
  await test('failed '+kind+' leaves channel cursor unchanged',async f=>{
   const before=structuredClone(f.state);
   if(kind==='month')f.raw=[message(12)];
   else if(kind==='reply'){f.window=[message(5,'parent',{reply_count:1,latest_reply:ts(12)})];f.replyMap.set(ts(5),[reply(12)]);}
   else {f.raw=[message(12,'bot',{bot_id:'B',reply_count:1})];f.replyMap.set(ts(12),[reply(13)]);}
   f.scriptFail=kind!=='report';f.badReport=kind==='report';
   const out=await f.run();assert.ok(out.error);assert.deepEqual(f.state,before);return out;
  });
 }
 await test('missing md fails only when new content arrives',async f=>{
  f.files.delete(f.mdPath);assert.equal((await f.run()).error,undefined);
  f.raw=[message(12)];const before=structuredClone(f.state);const out=await f.run();
  assert.ok(out.error);assert.equal(out.added,0);assert.deepEqual(f.state,before);return out;
 });
 await test('edit detection failure does not block new content',async f=>{
  f.files.set(f.mdPath,null);f.raw=[message(12)];const out=await f.run({scanEdits:true});
  assert.ok(out.notes.some(n=>n.includes('감지 실패')));assert.equal(f.state.channels.C1.last_ts,ts(12));return out;
 });
 for(const dry of [false,true]){
  await test('scan disabled preserves pending; dry='+dry,async f=>{
   const pending=path.join('/fixture/archive','.pending-edits.json');
   f.files.set(pending,'{"items":[{"id":"C1|edited|message|key","kind":"edited","scope":"message","key":"key"}]}');
   f.config.digest.skipChannels=['#skip'];f.channels.push({id:'C2',name:'skip'});
   const before=f.snapshot();const out=await f.api.ingestConversations(f.client,{dry,scanEdits:false});
   assert.ok(f.files.has(pending));assert.equal(out.totalEdited,0);
   assert.ok(!f.trace.some(x=>x[0]==='history'&&x[1].channel==='C2'));
   if(dry)assert.deepEqual(f.snapshot(),before);return out;
  });
 }
 await test('pending firstSeen retained; deferred remains stored but not counted',async f=>{
  const key='2024-01-10 00:05 · Writer',id='C1|edited|message|'+key;
  f.files.set(path.join('/fixture/archive','.pending-edits.json'),JSON.stringify({items:[{
   id,kind:'edited',scope:'message',key,firstSeen:'2024-01-01'}]}));
  f.held=[[id,{}]];f.window=[message(5,'changed',{edited:{ts:ts(9)}})];
  const out=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(out.totalEdited,0);assert.equal(out.pending.deferred,1);
  const saved=JSON.parse(f.files.get(path.join('/fixture/archive','.pending-edits.json')));
  assert.equal(saved.items[0].firstSeen,'2024-01-01');return out;
 });
 await test('failed channel pending persistence',async f=>{
  const pending=path.join('/fixture/archive','.pending-edits.json');
  f.files.set(pending,JSON.stringify({items:[{id:'C1|edited|message|key',kind:'edited',scope:'message',key:'key'}]}));
  f.historyFail=true;const out=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(out.errors.length,1);assert.equal(f.files.has(pending),!legacy);
  if(!legacy){assert.equal(f.state.edit_scan_retry.C1,0);assert.equal(JSON.parse(f.files.get(pending)).items.length,1);}
  assert.equal(f.state.last_sync,new FixedDate().toISOString());return out;
 });
 return results;
}

async function retryRegressions(){
 let count=0;
 const pending=path.join('/fixture/archive','.pending-edits.json');
 const key='2024-01-10 00:05 · Writer';
 const saved=(channel='C1')=>({id:channel+'|edited|message|'+key,kind:'edited',scope:'message',key,
  channel:'fixture',file:'fixture',before:'parent',after:'changed',firstSeen:'2024-01-01',manual:true});
 const seed=f=>{f.state.last_sync=new Date(Number(ts(8))*1000+900000).toISOString();
  f.files.set(pending,JSON.stringify({items:[saved()]}));};
 async function test(name,run){await run();count++;console.log('PASS retry: '+name);}
 await test('failed and partial scans preserve records, fresh observations replace by ID',()=>{
  const a=pure(),prev={items:[saved(),saved('C2')]},before=structuredClone(prev);
  const fresh={scope:'message',key,header:'h',replyKey:null,before:'parent',after:'newest'};
  const out=a.buildPending([{id:'C1',error:'failed'},{id:'C2',editScanIncomplete:true,edited:[fresh]}],prev,'2024-01-10');
  assert.equal(out.items.length,2);assert.deepEqual(out.items.find(x=>x.id===saved().id),saved());
  assert.equal(out.items.find(x=>x.id===saved('C2').id).after,'newest');
  assert.equal(out.items.find(x=>x.id===saved('C2').id).firstSeen,'2024-01-01');
  assert.deepEqual(prev,before);
  assert.equal(a.buildPending([{id:'C1',edited:[],deleted:[]}],{items:[saved()]},'2024-01-10').items.length,0);
 });
 for(const dry of [false,true]){
  await test('failed channel + healthy channel; dry='+dry,async()=>{
   const f=fixture(source);seed(f);f.historyFailChannels=new Set(['C1']);
   f.channels.push({id:'C2',name:'second'});
   f.state.channels.C2={name:'second',file:'second',last_ts:ts(10),threads:{}};
   f.files.set(path.join('/fixture/archive/channels','second.md'),format.renderMessage(message(5,'parent'),[],userMap,tz,selfId));
   f.files.set(pending,JSON.stringify({items:[saved(),saved('C2')]}));f.window=[message(5,'parent')];
   const before=f.snapshot();const out=await f.api.ingestConversations(f.client,{scanEdits:true,dry});
   assert.equal(out.errors.length,1);assert.equal(out.pending.carried,1);
   if(dry)assert.deepEqual(f.snapshot(),before);
   else{
    assert.deepEqual(JSON.parse(f.files.get(pending)).items,[saved()]);
    assert.equal(f.state.edit_scan_retry.C1,Number(ts(8)));
    assert.equal(f.state.edit_scan_retry.C2,undefined);
    assert.equal(f.state.last_sync,new FixedDate().toISOString());
   }
  });
 }
 await test('failure -> skipped scan -> successful retry finds previously unseen edit',async()=>{
  const f=fixture(source);seed(f);f.files.delete(pending);f.historyFail=true;
  await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(f.state.edit_scan_retry.C1,Number(ts(8)));
  f.historyFail=false;f.window=[message(5,'changed',{edited:{ts:ts(9)}})];
  await f.api.ingestConversations(f.client,{scanEdits:false});
  assert.equal(f.state.edit_scan_retry.C1,Number(ts(8)));assert.ok(!f.files.has(pending));
  const out=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(out.totalEdited,1);assert.equal(out.pending.fresh.length,1);
  assert.equal(f.state.edit_scan_retry,undefined);
  assert.equal(JSON.parse(f.files.get(pending)).items[0].after,'changed');
 });
 await test('repeated failures retain earliest watermark including zero',async()=>{
  const f=fixture(source);seed(f);f.state.edit_scan_retry={C1:0,C9:123};f.historyFail=true;
  await f.api.ingestConversations(f.client,{scanEdits:false});
  await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.deepEqual(f.state.edit_scan_retry,{C1:0,C9:123});
  assert.deepEqual(JSON.parse(f.files.get(pending)).items,[saved()]);
 });
 await test('detection exception preserves pending and new-content ingestion',async()=>{
  const f=fixture(source);seed(f);f.files.set(f.mdPath,null);f.raw=[message(12)];
  const out=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(out.totalAdded,1);assert.equal(f.state.channels.C1.last_ts,ts(12));
  assert.equal(f.state.edit_scan_retry.C1,Number(ts(8)));
  assert.deepEqual(JSON.parse(f.files.get(pending)).items,[saved()]);
 });
 await test('reply API failure and thread cap preserve pending until complete scan',async()=>{
  const f=fixture(source);seed(f);
  f.window=[message(5,'parent',{reply_count:1,latest_reply:ts(6)})];f.replyFail.add(ts(5));
  await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.deepEqual(JSON.parse(f.files.get(pending)).items,[saved()]);
  assert.equal(f.state.edit_scan_retry.C1,Number(ts(8)));
  f.replyFail.clear();f.config.limits.editScanMaxThreads=0;
  await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.deepEqual(JSON.parse(f.files.get(pending)).items,[saved()]);
  f.config.limits.editScanMaxThreads=60;
  await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.ok(!f.files.has(pending));assert.equal(f.state.edit_scan_retry,undefined);
 });
 await test('missing archive and out-of-window pending are not resolution',async()=>{
  const f=fixture(source);seed(f);f.files.delete(f.mdPath);
  await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.deepEqual(JSON.parse(f.files.get(pending)).items,[saved()]);
  f.files.set(f.mdPath,format.renderMessage(message(5,'parent'),[],userMap,tz,selfId));
  const outside={...saved(),key:'2020-01-01 00:05 · Writer',id:'C1|edited|message|2020-01-01 00:05 · Writer'};
  f.files.set(pending,JSON.stringify({items:[outside]}));f.window=[message(5,'parent')];
  const out=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.deepEqual(JSON.parse(f.files.get(pending)).items,[outside]);
  assert.ok(out.channels[0].notes.some(n=>n.includes('수동 확인')));
  assert.ok(f.state.edit_scan_retry);
 });
 await test('successful retry resolves existing finding; deferred stays hidden on failure',async()=>{
  const f=fixture(source);seed(f);f.held=[[saved().id,{}]];f.historyFail=true;
  const failed=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(failed.totalEdited,0);assert.equal(failed.pending.deferred,1);
  assert.deepEqual(JSON.parse(f.files.get(pending)).items,[saved()]);
  f.historyFail=false;f.window=[message(5,'parent',{edited:{ts:ts(9)}})];
  await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.ok(!f.files.has(pending));assert.equal(f.state.edit_scan_retry,undefined);
 });

 for(const scope of ['message','reply']){
  await test('unseen '+scope+' edit survives failure, ambiguous retry, then unique match',async()=>{
   const f=fixture(source);seed(f);f.files.delete(pending);f.historyFail=true;
   await f.api.ingestConversations(f.client,{scanEdits:true});f.historyFail=false;
   if(scope==='message'){
    f.window=[message(5,'changed',{edited:{ts:ts(9)}}),{...message(5,'second'),ts:String(Number(ts(5))+1)}];
   }else{
    f.files.set(f.mdPath,format.renderMessage(message(5,'parent'),[reply(6,'old')],userMap,tz,selfId));
    f.window=[message(5,'parent',{reply_count:2,latest_reply:ts(7)})];
    f.replyMap.set(ts(5),[reply(6,'changed',{edited:{ts:ts(9)}}),{...reply(6,'second'),ts:String(Number(ts(6))+1)}]);
   }
   await f.api.ingestConversations(f.client,{scanEdits:true});
   // 갈래 ③ (2026-09-22): 얼린 기준 대신 ambiguous 항목이 남는다 — 다음 회차의
   // 기준-무관 재대조는 이 항목의 pendingKeys 가 맡는다.
   assert.equal(f.state.edit_scan_retry,undefined);
   assert.equal(JSON.parse(f.files.get(pending)).items[0].kind,'ambiguous');
   if(scope==='message')f.window.pop();
   else f.replyMap.get(ts(5)).pop();
   const out=await f.api.ingestConversations(f.client,{scanEdits:true});
   assert.equal(out.totalEdited,1);assert.equal(f.state.edit_scan_retry,undefined);
   assert.equal(JSON.parse(f.files.get(pending)).items[0].scope,scope);
  });
 }
 for(const scope of ['message','reply']){
  await test('ambiguous '+scope+' timestamp keeps pending, adds an ambiguous item, no watermark',async()=>{
   /* 2026-09-22 갈래 ③ 으로 계약이 바뀌었다 — 옛 판은 기존 후보만 보존하며 기준을
    * 얼렸는데(edit_scan_retry), 이제 그 분의 ambiguous 항목이 **더해지고** 기준은 안
    * 붙잡는다. 항목의 pendingKeys 가 동결이 하던 일(기준 무관 재대조)을 대신한다. */
   const f=fixture(source);seed(f);
   if(scope==='message'){
    f.window=[message(5,'parent'),message(5,'changed',{edited:{ts:ts(9)}})];
   }else{
    const replyKey='2024-01-10 00:06 · Reader';
    const item={...saved(),scope:'reply',key:replyKey,id:'C1|edited|reply|'+replyKey,replyKey};
    f.files.set(pending,JSON.stringify({items:[item]}));
    f.files.set(f.mdPath,format.renderMessage(message(5,'parent'),[reply(6,'old')],userMap,tz,selfId));
    f.window=[message(5,'parent',{reply_count:2,latest_reply:ts(7)})];
    f.replyMap.set(ts(5),[reply(6,'old'),{...reply(6,'new',{edited:{ts:ts(9)}}),ts:String(Number(ts(6))+1)}]);
   }
   const before=JSON.parse(f.files.get(pending)).items;
   const out=await f.api.ingestConversations(f.client,{scanEdits:true});
   const now=JSON.parse(f.files.get(pending)).items;
   for(const it of before)assert.deepEqual(now.find(x=>x.id===it.id),it);
   const amb=now.find(x=>x.kind==='ambiguous');
   assert.ok(amb);assert.equal(amb.scope,scope);assert.equal(amb.manual,true);
   assert.equal(f.state.edit_scan_retry,undefined);
   assert.ok(out.channels[0].notes.some(n=>n.includes('중복')));
  });
 }
 /* 아래는 전부 **래치가 스스로 재생산되는 것**을 막는다 (2026-09-15). 하나라도 풀리면
  * 한 번 걸린 채널이 영영 `retryEdits` 로 돌고, 그 사실은 에러 없이 조용하다.
  * 앞의 회차에서 `historyFail` 로 **래치를 실제로 걸어 두고** 다음 회차가 푸는지를 본다 —
  * 안 걸고 재면 「애초에 안 걸렸다」와 「걸렸다 풀렸다」가 같은 글자가 된다. */
 /* **대조를 끝낸 뒤에 난 실패는 기준을 안 붙잡는다** (2026-09-22).
  *
  * 이 래치의 뜻은 「수정·삭제 대조를 못 하고 왔으니 다음 회차가 그 구간을 다시 보게 하라」다.
  * 그런데 조건이 `result.error` 여서, 대조(②-2)가 끝까지 돌고 난 **뒤** 단계(④ 삽입·④-b
  * 답글 덧붙이기)에서 난 실패도 기준을 얼렸다. 그러면 대조를 멀쩡히 마친 채널이 실패가
  * 이어지는 동안 **되돌아보기 구간을 넓힌 채 매일 같은 자리를 다시 훑는다.** 실제로 어느
  * 채널의 `edit_scan_retry` 가 그렇게 박혀 있었고, 같은 날 배포된 갈래 ③ 이 약속한
  * 「배포 뒤 래치 소멸」도 그 채널에서는 안 일어났다 — 덧붙이기 실패가 매일 다시 걸었다.
  *
  * 두 방향을 함께 재야 한다. 하나만 재면 「안 걸린다」와 「아무 때도 안 건다」가 같은 글자다. */
 await test('a failure after a completed scan does not latch the edit baseline',async()=>{
  const f=fixture(source);seed(f);f.files.delete(pending);
  f.raw=[message(12)];f.scriptFail=true;   // 조회는 되고 md 쓰는 단계에서 깨진다
  const out=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(out.errors.length,1);
  assert.equal(f.state.edit_scan_retry,undefined);
 });
 await test('a failure before the scan still latches',async()=>{
  const f=fixture(source);seed(f);f.files.delete(pending);
  f.historyFail=true;                       // 대조 자체를 못 하고 왔다
  const out=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(out.errors.length,1);
  assert.equal(f.state.edit_scan_retry.C1,Number(ts(8)));
 });

 const armed=async(f)=>{ // 실패 회차 하나로 래치를 걸어 둔다
  f.historyFail=true;await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(f.state.edit_scan_retry.C1,Number(ts(8)));f.historyFail=false;
 };
 for(const scope of ['message','reply']){
  await test('same-minute '+scope+' pair alone does not re-latch after a failed scan',async()=>{
   const f=fixture(source);seed(f);f.files.delete(pending);await armed(f);
   // 아카이브에도 슬랙에도 같은 1분에 둘 — 고쳐지지도 지워지지도 않았다.
   if(scope==='message'){
    const second={...message(5,'second',{user:'U2'}),ts:String(Number(ts(5))+1)};
    f.files.set(f.mdPath,[message(5,'parent'),second].map(m=>format.renderMessage(m,[],userMap,tz,selfId)).join('\n\n'));
    f.window=[message(5,'parent'),second];
   }else{
    const twin={...reply(6,'second'),ts:String(Number(ts(6))+1)};
    f.files.set(f.mdPath,format.renderMessage(message(5,'parent'),[reply(6,'old'),twin],userMap,tz,selfId));
    f.window=[message(5,'parent',{reply_count:2,latest_reply:ts(7)})];
    f.replyMap.set(ts(5),[reply(6,'old'),twin]);
   }
   const out=await f.api.ingestConversations(f.client,{scanEdits:true});
   assert.equal(f.state.edit_scan_retry,undefined);
   assert.ok(!out.channels[0]?.notes?.some(n=>n.includes('중복')),JSON.stringify(out.channels));
  });
 }
 for(const scope of ['message','reply']){
  await test('unpinpointable '+scope+' deletion records an ambiguous item; dedup surfaces the edit',async()=>{
   /* 그 1분은 삭제도 못 짚고 **수정 대조에서도 빠진다.** 옛 판은 기준을 얼려 두는
    * 방식이었고(2026-09-15), 갈래 ③ 부터는 ambiguous 항목의 pendingKeys 가 그 일을
    * 한다 — 사람이 md 를 고쳐 중복이 풀린 회차에 그 수정이 「최근 것」이 아니어도 짚인다. */
   const f=fixture(source);seed(f);f.files.delete(pending);
   const changed=message(5,'changed',{edited:{ts:ts(9)}});
   if(scope==='message'){
    const twin={...message(5,'second',{user:'U2'}),ts:String(Number(ts(5))+1)};
    f.files.set(f.mdPath,[message(5,'parent'),twin].map(m=>format.renderMessage(m,[],userMap,tz,selfId)).join('\n\n'));
    f.window=[changed];
   }else{
    const twin={...reply(6,'second'),ts:String(Number(ts(6))+1)};
    f.files.set(f.mdPath,format.renderMessage(message(5,'parent'),[reply(6,'old'),twin],userMap,tz,selfId));
    f.window=[message(5,'parent',{reply_count:1,latest_reply:ts(7)})];
    f.replyMap.set(ts(5),[reply(6,'changed',{edited:{ts:ts(9)}})]);
   }
   const first=await f.api.ingestConversations(f.client,{scanEdits:true});
   assert.equal(first.totalEdited,0);
   assert.ok(first.channels[0].notes.some(n=>n.includes('판정하지 못했습니다')));
   assert.equal(f.state.edit_scan_retry,undefined);   // 기준 동결이 더는 없다
   const amb=JSON.parse(f.files.get(pending)).items.find(x=>x.kind==='ambiguous');
   assert.ok(amb);assert.equal(amb.scope,scope);
   // 사람이 md 의 중복을 풀었다 — 이제 짚인다
   if(scope==='message')f.files.set(f.mdPath,format.renderMessage(message(5,'parent'),[],userMap,tz,selfId));
   else f.files.set(f.mdPath,format.renderMessage(message(5,'parent'),[reply(6,'old')],userMap,tz,selfId));
   const out=await f.api.ingestConversations(f.client,{scanEdits:true});
   assert.equal(out.totalEdited,1);
   assert.equal(JSON.parse(f.files.get(pending)).items[0].scope,scope);
  });
 }
 await test('compressed-edit notice does not re-latch when already armed',async()=>{
  const f=fixture(source);seed(f);f.files.delete(pending);await armed(f);
  f.files.set(f.mdPath,'**2024-01-10 00:05 · Writer** compressed');
  f.window=[message(5,'changed',{edited:{ts:ts(9)}})];
  const out=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.ok(out.channels[0].notes.some(n=>n.includes('스크립트로 못 고칩니다')));
  assert.equal(JSON.parse(f.files.get(pending)).items[0].manual,true);
  assert.equal(f.state.edit_scan_retry,undefined);
 });
 await test('a reply edit under an ambiguous parent is captured and resolves after dedup',async()=>{
  /* 부모 시각이 중복이면 답글 루프가 통째로 건너뛴다 — 부모만 보면 이 수정이 조용히
   * 사라진다. 옛 판은 기준을 얼렸고, 갈래 ③ 은 **부모 분의 message-scope 항목**으로
   * 남긴 뒤 중복이 풀린 회차에 그 열쇠로 답글 수정을 짚는다. */
  const f=fixture(source);seed(f);f.files.delete(pending);await armed(f);
  const twin={...message(5,'second',{user:'U2'}),ts:String(Number(ts(5))+1)};
  f.files.set(f.mdPath,[format.renderMessage(message(5,'parent'),[reply(6,'old')],userMap,tz,selfId),
   format.renderMessage(twin,[],userMap,tz,selfId)].join('\n\n'));
  f.window=[message(5,'parent',{reply_count:1,latest_reply:ts(7)}),twin];
  f.replyMap.set(ts(5),[reply(6,'changed',{edited:{ts:ts(9)}})]);
  await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(f.state.edit_scan_retry,undefined);
  const amb=JSON.parse(f.files.get(pending)).items.find(x=>x.kind==='ambiguous');
  assert.ok(amb);assert.equal(amb.scope,'message');assert.equal(amb.key,'2024-01-10 00:05');
  // 사람이 부모 중복을 풀었다 — 부모 분의 열쇠가 그 답글 수정을 짚는다
  f.files.set(f.mdPath,format.renderMessage(message(5,'parent'),[reply(6,'old')],userMap,tz,selfId));
  f.window=[message(5,'parent',{reply_count:1,latest_reply:ts(7)})];
  const out=await f.api.ingestConversations(f.client,{scanEdits:true});
  assert.equal(out.totalEdited,1);
  assert.equal(JSON.parse(f.files.get(pending)).items[0].scope,'reply');
 });
 /* ── 갈래 ③: 같은 1분에 숨은 수정 → ambiguous 항목 (2026-09-22) ──
  * 설계: 워크스페이스 10-projects/260922_수집래치-갈래③_설계.md. 열쇠는 블록이 아니라
  * 그 1분이고, 항목이 pendingKeys 로 기준 시각과 무관한 재대조를 부르므로 기준-동결
  * 래치가 필요 없어진다. */
 const min5='2024-01-10 00:05';
 const twin5=()=>({...message(5,'b',{user:'U2'}),ts:String(Number(ts(5))+10)});
 const dupMd3=()=>[message(5,'a'),twin5()].map(m=>format.renderMessage(m,[],userMap,tz,selfId)).join('\n\n');
 const base3=()=>({md:dupMd3(),windowMsgs:[message(5,'a'),twin5()],repliesByTs:new Map(),
  userMap,tz,selfId,oldest:ts(0),lastTs:ts(10),editedSince:Number(ts(8)),pendingKeys:new Set()});
 await test('hidden edit in duplicated minute becomes an ambiguous item without a latch',()=>{
  const a=pure();const x=base3();
  x.windowMsgs[1]={...x.windowMsgs[1],edited:{ts:ts(9)}};
  const out=a.detectChanges(x);
  assert.equal(out.ambiguous.length,1);
  assert.equal(out.ambiguous[0].scope,'message');
  assert.equal(out.ambiguous[0].key,min5);
  assert.equal(out.ambiguous[0].manual,true);
  assert.equal(out.retry,false);            // 기준 동결이 더는 없다
  assert.equal(out.edited.length,0);
 });
 await test('ambiguous item carries while the duplicate persists without a recent edit',()=>{
  const a=pure();const x=base3();
  x.pendingKeys=new Set(['ambiguous|message|'+min5]);
  const out=a.detectChanges(x);
  assert.equal(out.ambiguous.length,1);assert.equal(out.retry,false);
 });
 await test('resolved duplicate surfaces the stale edit and drops the ambiguous item',()=>{
  const a=pure();const x=base3();
  x.md=format.renderMessage(message(5,'old'),[],userMap,tz,selfId);
  x.windowMsgs=[message(5,'new',{edited:{ts:ts(7)}})];   // 기준(ts(8))보다 옛 수정
  x.pendingKeys=new Set(['ambiguous|message|'+min5]);
  const out=a.detectChanges(x);
  assert.equal(out.edited.length,1);assert.equal(out.ambiguous.length,0);
 });
 await test('hidden reply edit in a duplicated reply minute becomes a reply-scope item',()=>{
  const a=pure();
  const twinR={...reply(6,'second'),ts:String(Number(ts(6))+10)};
  const md=format.renderMessage(message(5,'parent'),[reply(6,'old'),twinR],userMap,tz,selfId);
  const x={md,windowMsgs:[message(5,'parent',{reply_count:2})],
   repliesByTs:new Map([[ts(5),[reply(6,'old'),{...twinR,edited:{ts:ts(9)}}]]]),
   userMap,tz,selfId,oldest:ts(0),lastTs:ts(10),editedSince:Number(ts(8)),pendingKeys:new Set()};
  const out=a.detectChanges(x);
  assert.equal(out.ambiguous.length,1);assert.equal(out.ambiguous[0].scope,'reply');
  assert.equal(out.ambiguous[0].key,'2024-01-10 00:06');assert.equal(out.retry,false);
 });
 await test('out-of-window ambiguous follows the md side: kept while duplicated, noted when resolved',()=>{
  const a=pure();const min0='2024-01-10 00:00';
  const oldDup=[message(0,'a'),{...message(0,'b',{user:'U2'}),ts:String(Number(ts(0))+10)}]
   .map(m=>format.renderMessage(m,[],userMap,tz,selfId)).join('\n\n');
  const kept=a.detectChanges({...base3(),md:oldDup,windowMsgs:[],
   pendingKeys:new Set(['ambiguous|message|'+min0])});
  assert.equal(kept.ambiguous.length,1);assert.equal(kept.ambiguous[0].key,min0);
  const dropped=a.detectChanges({...base3(),md:format.renderMessage(message(0,'a'),[],userMap,tz,selfId),
   windowMsgs:[],pendingKeys:new Set(['ambiguous|message|'+min0])});
  assert.equal(dropped.ambiguous.length,0);
  assert.ok(dropped.notes.some(n=>n.includes('자동으로 못 좇습니다')));
 });
 await test('buildPending carries the ambiguous kind with firstSeen and manual',()=>{
  const a=pure();
  const amb={scope:'message',key:min5,header:'h',replyKey:null,before:'h1\nh2',manual:true};
  const out=a.buildPending([{id:'C1',channel:'fixture',file:'fixture',edited:[],deleted:[],ambiguous:[amb]}],
   {items:[{id:'C1|ambiguous|message|'+min5,firstSeen:'2024-01-01'}]},'2024-01-10');
  assert.equal(out.items.length,1);
  assert.equal(out.items[0].kind,'ambiguous');
  assert.equal(out.items[0].manual,true);
  assert.equal(out.items[0].firstSeen,'2024-01-01');
  assert.ok(!('after' in out.items[0]));
 });
 await test('pendingOutOfWindow ignores ambiguous keys and keeps edited semantics',()=>{
  const a=pure();
  assert.equal(a.pendingOutOfWindow(new Set(['ambiguous|message|2020-01-01 00:00']),'2024-01-01 00:00','2024-01-31 23:59'),false);
  assert.equal(a.pendingOutOfWindow(new Set(['edited|message|2020-01-01 00:00 · Writer']),'2024-01-01 00:00','2024-01-31 23:59'),true);
  assert.equal(a.pendingOutOfWindow(new Set(['edited|message|2024-01-05 00:00 · Writer']),'2024-01-01 00:00','2024-01-31 23:59'),false);
 });
 await test('reply carry survives a clean sibling thread sharing the same minute',()=>{
  /* 회의적 검증(2026-09-22)이 재현으로 잡은 결함의 회귀 방지 — 검증 완료를 분 단위로
   * 표시하면, 같은 분(00:06)에 답글을 가진 깨끗한 스레드 A 가 대조되는 순간 모호한
   * 부모 B(중복 분) 밑의 이월 항목이 「검증됐다」로 읽혀 무음 증발했다. */
  const a=pure();const min6='2024-01-10 00:06';
  const parentA=message(5,'A'),dupB=message(2,'B'),dupB2={...message(2,'B2',{user:'U2'}),ts:String(Number(ts(2))+10)};
  const md=[format.renderMessage(parentA,[reply(6,'ok')],userMap,tz,selfId),
   format.renderMessage(dupB,[reply(6,'hidden')],userMap,tz,selfId),
   format.renderMessage(dupB2,[],userMap,tz,selfId)].join('\n\n');
  const out=a.detectChanges({md,windowMsgs:[{...parentA,reply_count:1},dupB,dupB2],
   repliesByTs:new Map([[parentA.ts,[reply(6,'ok')]]]),
   userMap,tz,selfId,oldest:ts(0),lastTs:ts(10),editedSince:Number(ts(8)),
   pendingKeys:new Set(['ambiguous|reply|'+min6])});
  assert.equal(out.ambiguous.length,1);
  assert.equal(out.ambiguous[0].scope,'reply');
  assert.equal(out.ambiguous[0].key,min6);
 });
 await test('a completed scan ends ambiguous for good despite channel incompleteness',()=>{
  /* 회의적 검증(2026-09-22)의 둘째 결함 회귀 방지 — 창-밖 해소로 종결 note 를 낸 항목을
   * 같은 회차의 editScanIncomplete 이월이 되살려, note 가 반복되고 항목이 영영 안 닫혔다. */
  const a=pure();
  const prev={items:[{id:'C1|ambiguous|message|2024-01-10 00:00',kind:'ambiguous',firstSeen:'2024-01-01'}]};
  const done=a.buildPending([{id:'C1',editScanIncomplete:true,editScanComplete:true,
   edited:[],deleted:[],ambiguous:[]}],prev,'2024-01-10');
  assert.equal(done.items.length,0);
  // 스캔 자체가 못 돈 채널(error·md 부재)은 종전대로 보존한다
  const kept=a.buildPending([{id:'C1',error:'failed'}],prev,'2024-01-10');
  assert.equal(kept.items.length,1);
 });
 await test('detectChanges splits unadjudicated from informational and deletion from edit',()=>{
  const a=pure();
  const base=()=>({md:format.renderMessage(message(5,'parent'),[],userMap,tz,selfId),
   windowMsgs:[message(5,'parent')],repliesByTs:new Map(),userMap,tz,selfId,
   oldest:ts(0),lastTs:ts(10),editedSince:Number(ts(8)),pendingKeys:new Set()});
  // 깨끗한 회차 — 둘 다 꺼져 있다
  assert.deepEqual(pick(a.detectChanges(base())),{incomplete:false,retry:false});
  // 압축 1줄 수정 — 판정은 끝났다. 알리기만 한다
  const compressed={...base(),md:'**2024-01-10 00:05 · Writer** compressed',
   windowMsgs:[message(5,'new',{edited:{ts:ts(9)}})]};
  const c=a.detectChanges(compressed);
  assert.equal(c.edited[0].manual,true);assert.deepEqual(pick(c),{incomplete:false,retry:false});
  // 같은 1분에 md 블록 둘 + 슬랙 0 — 삭제를 못 짚는다. 목록은 보존하되 기준은 안 붙잡는다
  const dup={...base(),md:base().md+'\n\n'+format.renderMessage(message(5,'second',{user:'U2'}),[],userMap,tz,selfId),
   windowMsgs:[]};
  assert.deepEqual(pick(a.detectChanges(dup)),{incomplete:true,retry:false});
  // 수정이 한꺼번에 많이 잡힌 회차 — 버린 수정은 기준을 놓으면 영영 안 잡힌다
  const many={...compressed,maxFindings:0};
  assert.deepEqual(pick(a.detectChanges(many)),{incomplete:true,retry:true});
  // 삭제가 한꺼번에 많이 잡힌 회차는 **기준을 안 붙잡는다** — 삭제는 매번 개수로 다시
  // 판정하므로 기준과 무관하게 그대로 다시 잡힌다. 여기 붙잡으면 그 채널이 영영 재시도로 돈다.
  const gone={...base(),md:[message(5,'a'),message(6,'b',{user:'U2'})].map(m=>format.renderMessage(m,[],userMap,tz,selfId)).join('\n\n'),
   windowMsgs:[],maxFindings:1};
  assert.deepEqual(pick(a.detectChanges(gone)),{incomplete:true,retry:false});
 });
 return count;
}
const pick=(r)=>({incomplete:r.incomplete,retry:r.retry});

function pythonContracts(){
 // Only AST-selected function/constant definitions are evaluated. Path below is
 // an in-memory protocol double: even Python's atomic replacement never hits disk.
 const py=[
 'import ast, json, re, sys, io, contextlib',
 'from pathlib import Path',
 'data=json.loads(sys.stdin.buffer.read().decode("utf-8"))',
 'tree=ast.parse(Path(data["source"]).read_text(encoding="utf-8"))',
 'selected=[n for n in tree.body if (isinstance(n,ast.FunctionDef) and n.name!="main") or isinstance(n,ast.Assign)]',
 'env={"re":re,"Path":Path}',
 'exec(compile(ast.Module(body=selected,type_ignores=[]),data["source"],"exec"),env)',
 'class MemoryPath:',
 ' def __init__(self,name,files): self.name=name; self.files=files; self.suffix=".md"',
 ' def read_bytes(self): return self.files[self.name]',
 ' def write_bytes(self,b): self.files[self.name]=b',
 ' def with_suffix(self,s): return MemoryPath(self.name+s,self.files)',
 ' def replace(self,target): self.files[target.name]=self.files.pop(self.name)',
 ' def __str__(self): return self.name',
 'out=[]',
 'for fixture in data["fixtures"]:',
 ' files={"fixture":fixture["md"].encode("utf-8")}; p=MemoryPath("fixture",files); report={}',
 ' with contextlib.redirect_stdout(io.StringIO()):',
 '  env["append_thread"](p,fixture["header"],fixture["reply"],report=report)',
 ' first=p.read_bytes().decode("utf-8"); repeated={}',
 ' with contextlib.redirect_stdout(io.StringIO()):',
 '  env["append_thread"](p,fixture["header"],fixture["reply"],report=repeated)',
 ' assert p.read_bytes().decode("utf-8")==first',
 ' out.append({"md":first,"report":report,"repeat":repeated,"tails":[bool(env["BODY_TAIL_RE"].match(s)) for s in data["tails"]]})',
 'print(json.dumps(out,ensure_ascii=True))'
 ].join('\n');
 const m=message(5,'parent'),old=reply(6,'old'),added=reply(7,'new\nsecond');
 const fixtures=['\n','\r\n'].map(nl=>({
  md:('# fixture\n\n---\n\n## 2024-01\n\n'+format.renderMessage(m,[old],userMap,tz,selfId)+'\n').replace(/\n/g,nl),
  header:'**2024-01-10 00:05 · Writer**',
  reply:[old,added].map(r=>format.renderReply(r,userMap,tz)).join('\n')}));
 const tails=['plain','> quote','📎 첨부: file','> 💬 **스레드 (1)**','> **└ old**','💬 스레드 1건 '+rules.BOT_ANSWER_MARK,'> 💬 스레드 2건 '+format.UNREAD_REPLIES_MARK];
 const input={source:fileURLToPath(new URL('../.claude/skills/slack-sync/scripts/insert_messages.py',import.meta.url)),fixtures,tails};
 const r=spawnSync('python',['-B','-c',py],{input:JSON.stringify(input),encoding:'utf8',maxBuffer:1024*1024});
 if(r.error)throw r.error;assert.equal(r.status,0,r.stderr||r.stdout);
 const out=JSON.parse(r.stdout);
 for(const x of out){
  const lines=[old,added].map(r=>format.renderReply(r,userMap,tz));
  assert.deepEqual(format.writtenFrom('REPORT '+JSON.stringify(x.report),lines),[lines[1]]);
  assert.deepEqual(format.writtenFrom('REPORT '+JSON.stringify(x.repeat),lines),[]);
  const parsed=format.parseBlocks(x.md);assert.equal(parsed[0].replies.length,2);
  assert.equal(parsed[0].replies[1].line,lines[1]);
  assert.deepEqual(x.tails,tails.map(s=>format.BODY_TAIL.test(s)));
 }
 console.log('PASS Python: LF/CRLF partial append, replay, multiline identity and '+tails.length+' shared body boundaries (memory only)');
}
const direct=await contracts(pure);
const wired=await contracts(()=>facade(source));
assert.deepEqual(wired,direct,'facade dependency binding differs from factories');
const integrated=await integration(source);
if(process.argv.includes('--baseline-stdin')){
 const oldSource=JSON.parse(fs.readFileSync(0,'utf8'));
 /* 2026-09-15 에 래치를 둘로 가르면서 필드 셋이 새로 생겼다 — detectChanges 의
  * `incomplete`·`retry`, 채널 결과의 `editScanRetry`. 리팩토링 전 소스는 이 셋을 낼 수
  * 없으므로 양쪽에서 **그 키만** 떼고 댄다. 셋의 계약은 retryRegressions 가 따로 본다.
  * JSON 왕복으로 떼지 않는다 — Map·Set·undefined 가 함께 뭉개져 진짜 어긋남을 숨긴다
  * (buildPending 의 `before` 가 Map 이다). */
 const DROP=new Set(['incomplete','retry','editScanRetry']);
 const strip=(v)=>{
  if(Array.isArray(v))return v.map(strip);
  if(v instanceof Map)return new Map([...v].map(([k,x])=>[k,strip(x)]));
  if(v instanceof Set)return new Set([...v].map(strip));
  if(v&&typeof v==='object'&&Object.getPrototypeOf(v)===Object.prototype){
   const o={};for(const [k,x] of Object.entries(v))if(!DROP.has(k))o[k]=strip(x);return o;
  }
  return v;
 };
 /* note 문구를 일부러 고친 케이스는 이름으로 뺀다 — 사람이 무엇을 해야 하는지 몰라서
  * 매일 같은 줄만 받던 것을 고쳤다 (WHK 결정 2026-09-16). 그 케이스의 **동작**은 여전히
  * 양쪽에서 대진다 — `contracts` 안의 `deleted.length`·`notes.length` 단언이 리팩토링 전
  * 소스로도 그대로 돌기 때문이다. 여기서 빼는 것은 문구 대조뿐이다. */
 const reworded=new Set(['same-minute ambiguity warns without guesses']);
 const keep=(rs)=>strip(rs.filter(x=>!reworded.has(x.name)));
 assert.deepEqual(keep(await contracts(()=>facade(oldSource))),keep(wired),'pre-extraction pure contracts');
 // Two intentional behavior changes have separate old/new assertions and retry regressions.
 const intentional=new Set(['failed channel pending persistence','edit detection failure does not block new content']);
 assert.deepEqual(strip((await integration(oldSource,{legacy:true})).filter(x=>!intentional.has(x.name))),
  strip(integrated.filter(x=>!intentional.has(x.name))),'unchanged pre-extraction state/files/call order');
 console.log('PASS independent baseline SHA256 '+createHash('sha256').update(oldSource).digest('hex'));
}
const retryCount=await retryRegressions();
console.log('PASS retry regressions: '+retryCount);
/* ── 개명 사슬(aka) — `.sync-state.json` 계약 (2026-09-16) ──────────────────
 * `file` 은 맨 처음 이름 하나만 들어서 x → y → z 두 번 개명이면 가운데 y 가 어디에도
 * 안 남았다. 규칙은 renameAka 주석이 정본이고, 여기서는 그 판정과 「실제로 상태에
 * 실리는지」를 잰다. integration() 에 안 넣는 이유: --baseline-stdin 이 리팩토링 전
 * 소스로도 같은 케이스를 돌리는데, 그 소스는 aka 를 낼 수 없다. */
{
 const renameAka=compileFunction(declaration(source,'renameAka')+'\nreturn renameAka;')();
 // 첫 개명 x → y: 첫 이름 x 는 이미 file 에 있으니 사슬은 빈다
 assert.deepEqual(renameAka({name:'x',file:'x'},'y'),[]);
 // 두 번째 개명 y → z: 가운데 y 가 남는다 — 이 계약이 이 필드가 있는 이유다
 assert.deepEqual(renameAka({name:'y',file:'x'},'z'),['y']);
 // 원래 이름으로 복귀 y → x: y 는 그래도 남는다 (y 시절 본문이 있다)
 assert.deepEqual(renameAka({name:'y',file:'x'},'x'),['y']);
 // 같은 이름 재통지: 안 는다
 assert.deepEqual(renameAka({name:'y',file:'x',aka:['y']},'y'),['y']);
 // 왕복해도 중복은 안 는다
 assert.deepEqual(renameAka({name:'y',file:'x',aka:['y']},'x'),['y']);
 // 세 번째 개명: 기존 사슬 보존 + 뒤에 시간순으로 덧붙인다
 assert.deepEqual(renameAka({name:'z',file:'x',aka:['y']},'w'),['y','z']);
 // file 칸이 없는 옛 상태: name 이 곧 첫 이름이다
 assert.deepEqual(renameAka({name:'x'},'y'),[]);
 // 함수만 맞고 부르는 쪽이 안 부르면 전부 통과한 채 아무것도 안 남는다 (⑧ 절과 같은 수법)
 assert.ok(/renameAka\(prev, ch\.name\)/.test(source),'ingestChannel must record aka via renameAka');

 // 상태에 실제로 실리나 — 두 번 개명 시나리오를 fixture 로 돈다
 const f=fixture(source);
 f.state.channels.C1={name:'midname',file:'fixture',last_ts:ts(10),threads:{}};
 f.channels[0].name='newname';
 await f.run();
 assert.deepEqual(f.state.channels.C1.aka,['midname']);
 assert.equal(f.state.channels.C1.name,'newname');
 assert.equal(f.state.channels.C1.file,'fixture');
 f.channels[0].name='thirdname';
 await f.run();
 assert.deepEqual(f.state.channels.C1.aka,['midname','newname']);
 // dry 회차는 상태 객체에도 안 남긴다
 const d=fixture(source);
 d.state.channels.C1={name:'midname',file:'fixture',last_ts:ts(10),threads:{}};
 d.channels[0].name='newname';
 const before=d.snapshot();
 await d.run({dry:true});
 assert.deepEqual(d.snapshot(),before);
 console.log('PASS rename aka chain: 7 pure cases + state carry + dry no-op');
}
if(!process.argv.includes('--skip-python'))pythonContracts();
console.log('PASS ingest archive: '+direct.length+' contracts (factories + facade), '+integrated.length+' integration cases');
