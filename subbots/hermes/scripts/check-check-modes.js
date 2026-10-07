#!/usr/bin/env node
/** R4: fake process/files/service boundaries exercise the actual CLI source.
 * --baseline-stdin compares the old full-check body using the same fake inputs.
 * No operational module is evaluated and no child process is launched here.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { CHECKS, LIVE_CHECKS } from './check-catalog.js';
import { parseMode, selectChecks, discoverTests, validateCatalog, discoverNodeChecks, validateNodeChecks, NODE_CHECK_EXEMPT, offlineEnvironment, runOffline } from './check-runner.js';
const root=fileURLToPath(new URL('../',import.meta.url));
const source=fs.readFileSync(new URL('./check-setup.js',import.meta.url),'utf8');
let passed=0;
async function test(name,fn){await fn();passed++;console.log('PASS '+name);}
const item=(name,extra={})=>({file:'scripts/'+name+'.js',runtime:'node',mode:'offline',what:name,reason:'fixture',...extra});
await test('default full mode and explicit mode/list parsing',()=>{
 assert.deepEqual(parseMode([]),{mode:'all',list:false});
 for(const mode of ['offline','archive','live']){
  assert.deepEqual(parseMode(['--'+mode]),{mode,list:false});
  assert.deepEqual(parseMode(['--list','--'+mode]),{mode,list:true});
 }
 for(const args of [['--all'],['--offline','--live'],['--archive','--archive'],['--facade'],['--list','--list']])
  assert.throws(()=>parseMode(args));
});
await test('catalog covers discovery exactly, with no duplicate files',()=>{
 validateCatalog(CHECKS,discoverTests(root),{exists:f=>fs.existsSync(path.join(root,f))});
 validateNodeChecks(CHECKS,discoverNodeChecks(root));
 assert.ok(CHECKS.some(c=>c.runtime==='python'));
 assert.ok(CHECKS.some(c=>c.runtime==='bash'));
 assert.ok(CHECKS.some(c=>c.file==='scripts/check-check-modes.js'&&c.mode==='offline'));
 assert.equal(selectChecks(CHECKS,'live').length,0);assert.equal(LIVE_CHECKS.length,2);
});
await test('unknown, missing and duplicate classifications fail closed',()=>{
 const a=item('a'),py=item('p',{file:'.claude/skills/test_p.py',runtime:'python',mode:'archive'});
 assert.throws(()=>validateCatalog([a,a],[]),/Duplicate/);
 assert.throws(()=>validateCatalog([{...a,reason:''}],[]),/classification/);
 assert.throws(()=>validateCatalog([a],['deploy/test-new.sh']),/Unclassified/);
 assert.throws(()=>validateCatalog([py],[]),/not discovered/);
 assert.throws(()=>validateCatalog([a],[],{exists:()=>false}),/Missing/);
 assert.throws(()=>validateCatalog([{...a,file:'../outside'}],[]),/Invalid/);
 assert.throws(()=>selectChecks([a],'typo'));
});
await test('node checks missing from the catalog or hidden by the exempt list fail closed',()=>{
 const real=discoverNodeChecks(root);
 // 등록 블록 하나를 지운 카탈로그 = 그 검사는 어디서도 안 돌게 된다. 빨개져야 한다.
 assert.throws(()=>validateNodeChecks(CHECKS.filter(c=>c.file!=='scripts/check-llm-tools.js'),real),
  /Unregistered node check: scripts\/check-llm-tools\.js/);
 // 카탈로그에 등록된 파일을 제외 목록에도 넣어 두 자리 판정이 되는 것을 막는다.
 assert.throws(()=>validateNodeChecks([...CHECKS,item('check-runner')],real),/Exempt file is registered/);
 // 발견이 좁아져 빈손이 되면 조용히 초록이 아니라 하한에서 빨개진다.
 const entry=(name,kind='file')=>({name,isDirectory:()=>kind==='dir',isFile:()=>kind==='file',isSymbolicLink:()=>kind==='link'});
 assert.throws(()=>discoverNodeChecks('/fixture',{readdirSync:()=>[entry('check-a.js')]}),/discovery rule or root is broken/);
 assert.throws(()=>discoverNodeChecks('/fixture',{readdirSync:()=>[entry('link','link')]}),/links/);
 assert.ok(NODE_CHECK_EXEMPT.length>=3&&real.length>=90);
});
await test('operational config and mixed legacy checks stay outside offline',()=>{
 for(const file of ['check-search-terms.js','check-log-render.js','check-archive-contract.js','check-business-names.js',
  'check-incident-refs.js','check-gitignore.js','check-bootstrap.js','check-doc-digest-limits.js']){
  assert.equal(CHECKS.find(c=>c.file==='scripts/'+file).mode,'archive',file);
 }
 assert.ok(CHECKS.filter(c=>c.runtime!=='node').every(c=>c.mode==='archive'));
});
await test('discovery ignores dependency directories and rejects links',()=>{
 const entry=(name,kind)=>({name,isDirectory:()=>kind==='dir',isFile:()=>kind==='file',isSymbolicLink:()=>kind==='link'});
 const io={existsSync:()=>true,readdirSync:p=>p.endsWith('deploy')?[entry('test-a.sh','file'),entry('setup.sh','file')]:
  [entry('test_a.py','file'),entry('venv','dir'),entry('ordinary.py','file')]};
 assert.deepEqual(discoverTests('/fixture',io),['.claude/skills/test_a.py','deploy/test-a.sh']);
 assert.throws(()=>discoverTests('/fixture',{...io,readdirSync:()=>[entry('link','link')]}),/links/);
});
await test('offline child environment removes inherited runtime injection and operating tokens',()=>{
 const env={PATH:'path',SystemRoot:'system',node_options:'--require arbitrary',NODE_PATH:'arbitrary',
  PYTHONPATH:'arbitrary',PYTHONHOME:'arbitrary',PYTHONSTARTUP:'arbitrary',BASH_ENV:'arbitrary',ENV:'arbitrary',
  HERMES_DATA_ROOT:'real',SLACK_BOT_TOKEN:'real',ANTHROPIC_API_KEY:'real'};
 const out=offlineEnvironment(env);
 assert.equal(out.PATH,'path');assert.equal(out.SystemRoot,'system');
 assert.equal(out.PYTHONDONTWRITEBYTECODE,'1');assert.equal(out.PYTHONIOENCODING,'utf-8');
 for(const k of Object.keys(env).filter(k=>!['PATH','SystemRoot'].includes(k)))assert.equal(out[k],undefined,k);
 assert.equal(env.HERMES_DATA_ROOT,'real');
});
for(const [name,res,expected] of [
 ['pass',{status:0,stdout:'PASS'},0],
 ['success describing skip handling',{status:0,stdout:'OK 못 쟀다 판정을 가려냅니다'},0],
 ['failure',{status:1,stderr:'broken'},1],
 ['missing interpreter',{status:null,error:new Error('ENOENT')},1],
 ['signal',{status:null,signal:'SIGTERM'},1],
 ['timeout',{status:null,error:new Error('ETIMEDOUT')},1],
 ['optional dependency',{status:2,stdout:'unavailable'},2],
 ['legacy skipped',{status:0,stdout:'[못잼] no fixtures'},2],
]){
 await test('offline result: '+name,()=>{
  const lines=[],calls=[];
  const result=runOffline([item('a')],{root:'/fixture',exists:()=>true,log:l=>lines.push(l),env:{},
   spawn:(exe,args,opts)=>{calls.push({exe,args,opts});return res;}});
  assert.equal(result.exitCode,expected);assert.equal(calls.length,1);assert.equal(calls[0].args.length,1);
  assert.equal(calls[0].opts.cwd,'/fixture');assert.equal(calls[0].opts.timeout,120000);
  if(expected===2)assert.equal(result.passed,0);
 });
}
await test('missing script fails without spawn and failures do not stop later tests',()=>{
 let spawns=0;const result=runOffline([item('a'),item('b')],{root:'/fixture',exists:p=>p.endsWith('b.js'),log(){},
 spawn:()=>{spawns++;return {status:0,stdout:'[보임] count 1'};}});
 assert.equal(result.failed,1);assert.equal(result.passed,1);assert.equal(spawns,1);
 assert.throws(()=>runOffline([]),/No offline/);
 assert.throws(()=>runOffline([item('a',{mode:'archive'})]),/Not an approved/);
});
async function execute(text,args=[],options={}){
 const calls=[],lines=[],loads=[];
 const checks=options.checks || [item('offline'),item('archive',{mode:'archive'})];
 const f={pyStatus:0,docs:false,...options};
 const config={timezone:'UTC',privateChannels:[],digest:{skipChannels:[]},limits:{},models:{qa:{id:'fixture'}}};
 const processStub={argv:['node','check-setup.js',...args],execPath:'node-fixture',env:{},platform:'linux',exitCode:0,
  exit(code){this.exitCode=code;}};
 const entry=name=>({name,isDirectory:()=>false,isFile:()=>true,isSymbolicLink:()=>false});
 const io={existsSync:()=>true,
  readdirSync:p=>String(p).endsWith('skills')?[entry('test_fixture.py')]:
   String(p).endsWith('deploy')?[entry('test-fixture.sh')]:[],
  readFileSync:p=>{calls.push(['read',String(p)]);return 'bot:\n  - channels:history\n';}};
 const spawn=(exe,argv,opts)=>{
  calls.push(['spawn',exe,argv,opts?.env]);
  if(argv.includes('--version'))return {status:0,stdout:'fixture version'};
  if(argv.some(a=>String(a).endsWith('.py')))return {status:f.pyStatus,stdout:'python fixture'};
  return {status:0,stdout:''};
 };
 class Anthropic {
  models={retrieve:async id=>{calls.push(['model',id]);if(f.modelFail)throw new Error('model failed');return {display_name:'fixture'};}};
  messages={create:async request=>{calls.push(['paid',request]);return {};}};
 }
 class WebClient {
  auth={test:async()=>{calls.push(['auth']);return {team:'fixture',user:'fixture',user_id:'BOT'};}};
 }
 const imports={
  '@anthropic-ai/sdk':{default:Anthropic},'@slack/web-api':{WebClient},
  '../src/config.js':{config,ROOT:root,ARCHIVE_DIR:'/fixture/archive',CHANNELS_DIR:'/fixture/channels',DOCS_DIR:f.docs?'/fixture/docs':null,
   DOC_PROJECTS_DIR:null,FULL_ACCESS:{},companyWideDocProjects:[],
   requireEnv:()=>{calls.push(['tokens']);return {SLACK_BOT_TOKEN:'xoxb-'+'f'.repeat(30),SLACK_APP_TOKEN:'xapp-'+'f'.repeat(30),ANTHROPIC_API_KEY:'sk-ant-'+'f'.repeat(30)};}},
  '../src/archive.js':{
   assertArchive:()=>{calls.push(['archive']);},listAllChannels:()=>[],listArchivedChannels:()=>[],
   buildArchiveBriefSplit:()=>({common:'',extra:''}),uninvitedChannels:()=>({actionable:[],ignored:[]}),
   staleChannelRefs:()=>[]},
  '../src/documents.js':{hasDocuments:()=>false},
  '../src/slack-live.js':{listBotChannels:async()=>[],listSlackChannels:async()=>[]},
  '../src/doc-index-audit.js':{},'./_card-warnings.js':{reportCardWarnings:()=>{}},
  '../src/llm/provider.js':{PROVIDERS:{anthropic:{}}},
 };
 const load=async spec=>{loads.push(spec);assert.ok(imports[spec],spec);return imports[spec];};
 let code=text.replace(/^#![^\n]*\n/,'');
 // Evaluate exactly the source statements with inert imports; no application module runs.
 code=code.replace(/^import\s([\s\S]*?)\sfrom\s'([^']+)';\r?\n/gm,(_m,binding,spec)=>{
  /* `./_child-run.js` 도 **지운다**(2026-10-07). 자식 검사 러너는 설정도 아카이브도
   * 안 읽는 순수 모듈이라 `--list` 가 「아무것도 안 불렀다」를 지키는 데 걸림돌이
   * 되면 안 된다 — `check-catalog.js`·`check-runner.js` 와 같은 자리다. 대신 아래
   * `deps` 가 **`spawn` 스텁을 지나는** 가짜를 꽂는다(진짜를 쓰면 자식 71개가 실제로 돈다). */
  if(spec.startsWith('node:')||spec==='./check-catalog.js'||spec==='./check-runner.js'||spec==='./_child-run.js')return '';
  return 'const '+(binding.startsWith('{')?binding:'{ default: '+binding+' }')+' = await load('+JSON.stringify(spec)+');\n';
 });
 code=code.replace(/\bimport\(([^)]+)\)/g,'load($1)').replace(/import\.meta\.url/g,JSON.stringify(new URL('./check-setup.js',import.meta.url).href));
 // Old source's independently saved list stays intact for baseline comparison.
 const deps={fs:io,path,fileURLToPath,URL,spawnSync:spawn,CHECKS:checks,LIVE_CHECKS,parseMode,selectChecks,
  discoverTests:()=>[],validateCatalog(){calls.push(['catalog']);},
  discoverNodeChecks:()=>[],validateNodeChecks(){calls.push(['catalog']);},
  runOffline:(selected,opts)=>runOffline(selected,{...opts,spawn,exists:()=>true,env:{},log:l=>lines.push(l)}),
  runCheck:({file,label,log,ok})=>{
   log?.(`  … ${label}`);
   const r=spawn(process.execPath,[file],undefined);
   if(r.status===0)ok?.(label);
   return {ok:r.status===0,timedOut:false,ms:0,output:r.stdout||'',visible:[]};
  },
  childLimitMs:()=>120000,formatDuration:ms=>`${ms}ms`,
  load,process:processStub,console:{log:(...x)=>lines.push(x.join(' '))},
  fetch:async()=>{calls.push(['scopes']);return {headers:{get:()=> 'channels:history'}};}};
 const AsyncFunction=Object.getPrototypeOf(async function(){}).constructor;
 await new AsyncFunction(...Object.keys(deps),code)(...Object.values(deps));
 return {calls,lines,loads,exit:processStub.exitCode};
}
for(const mode of ['offline','archive','live','all']){
 await test('actual CLI source routes '+mode,async()=>{
  const out=await execute(source,mode==='all'?[]:['--'+mode]);
  assert.equal(out.exit,0,out.lines.join('\n'));
  const names=out.calls.map(c=>c[0]);
  if(mode==='offline'){
   assert.deepEqual(out.loads,[]);assert.ok(!names.includes('read'));assert.equal(names.filter(n=>n==='spawn').length,1);
  } else {
   assert.equal(names.includes('paid'),mode!=='archive');assert.equal(names.includes('auth'),mode!=='archive');
   assert.equal(names.includes('tokens'),mode!=='archive');assert.equal(names.includes('archive'),mode!=='live');
   const phases=out.lines.filter(l=>/^\n\[\d\/6\]/.test(l)).map(l=>Number(l.match(/\[(\d)/)[1]));
   assert.deepEqual(phases,mode==='all'?[1,2,3,4,5,6]:mode==='archive'?[1,3,4]:[2,5,6]);
   if(mode!=='all')assert.ok(!out.lines.some(l=>l.includes('npm start 로 실행하세요')));
  }
 });
 await test('actual '+mode+' list does not load config or run children',async()=>{
  const out=await execute(source,mode==='all'?['--list']:['--'+mode,'--list']);
  assert.deepEqual(out.loads,[]);assert.deepEqual(out.calls,[['catalog'],['catalog']]);
  const shown=JSON.parse(out.lines[0]);assert.equal(shown.mode,mode);
  assert.equal(shown.live.length,['all','live'].includes(mode)?2:0);
 });
}
await test('scoped live failure and archive missing documents propagate failure',async()=>{
 assert.equal((await execute(source,['--live'],{modelFail:true})).exit,1);
 assert.equal((await execute(source,['--archive'],{docs:true})).exit,1);
});
await test('full mode preserves optional Python dependency exit semantics',async()=>{
 const out=await execute(source,[],{pyStatus:2});
 assert.equal(out.exit,0);assert.ok(out.lines.some(l=>l.includes('못 쟀습니다')));
});
await test('public package commands and documentation agree with CLI',()=>{
 const pkg=JSON.parse(fs.readFileSync(new URL('../package.json',import.meta.url),'utf8'));
 assert.equal(pkg.scripts.check,'node scripts/check-setup.js');
 for(const mode of ['offline','archive','live'])assert.equal(pkg.scripts['check:'+mode],'node scripts/check-setup.js --'+mode);
});
if(process.argv.includes('--baseline-stdin')){
 const old=JSON.parse(fs.readFileSync(0,'utf8'));
 for(const opts of [{},{pyStatus:2},{modelFail:true},{docs:true}]){
  const fullOptions={...opts,checks:CHECKS.filter(c=>c.file!=='scripts/check-check-modes.js')};
  const before=await execute(old,[],fullOptions),after=await execute(source,[],fullOptions);
  // The full catalog adds this test. Compare unchanged operational phases and service requests.
  const effect=c=>c[0]!=='catalog';
  assert.deepEqual(after.calls.filter(effect),before.calls.filter(effect));
  assert.equal(after.exit,before.exit);
  const phase=l=>/^\n\[[2-6]\/6\]/.test(l)||l.includes('점검 통과')||l.includes('점검 실패');
  assert.deepEqual(after.lines.filter(phase),before.lines.filter(phase));
 }
 console.log('PASS independent pre-R4 operational baseline (4 scenarios)');
}
console.log(passed+' check-mode contracts passed; real data/service/process effects 0.');
