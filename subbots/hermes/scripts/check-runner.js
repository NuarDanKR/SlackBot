/** Check selection and offline process execution; no application imports. */
import fs from 'node:fs';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
export function parseMode(args) {
  let mode='all', list=false, selected=false;
  for (const arg of args) {
    if (arg==='--list' && !list) { list=true; continue; }
    if (['--offline','--archive','--live'].includes(arg) && !selected) {
      mode=arg.slice(2); selected=true; continue;
    }
    throw new Error('Unknown or conflicting check option: '+arg);
  }
  return {mode,list};
}
export function selectChecks(checks,mode) {
  if (!['all','offline','archive','live'].includes(mode)) throw new Error('Unknown check mode: '+mode);
  return checks.filter(c=>mode==='all'||c.mode===mode);
}
export function discoverTests(root,io=fs) {
  const out=[];
  const skip=new Set(['node_modules','venv','.venv','__pycache__','.git']);
  function walk(dir) {
    if(!io.existsSync(dir))return;
    for(const e of io.readdirSync(dir,{withFileTypes:true})) {
      if(e.isSymbolicLink())throw new Error('Test discovery refuses symbolic links: '+e.name);
      const p=path.join(dir,e.name);
      if(e.isDirectory()&&!skip.has(e.name))walk(p);
      else if(e.isFile()&&/^test_.*\.py$/.test(e.name))out.push(path.relative(root,p).split(path.sep).join('/'));
    }
  }
  walk(path.join(root,'.claude','skills'));
  const deploy=path.join(root,'deploy');
  if(io.existsSync(deploy))for(const e of io.readdirSync(deploy,{withFileTypes:true})) {
    if(e.isSymbolicLink())throw new Error('Test discovery refuses symbolic links: '+e.name);
    if(e.isFile()&&/^test-.*\.sh$/.test(e.name))out.push('deploy/'+e.name);
  }
  return out.sort();
}
/** 카탈로그 등록 대상이 아닌 node 파일. 앞 셋은 검사를 고르고 돌리는 기반 파일이고,
 * 뒤 넷은 잊은 게 아니라 정한 것이다 — deployed·prereqs 는 사람이 따로 부르는 도구
 * (`npm run deployed`·`npm run doctor`), live-llm 은 유료 호출이라 사람 승인 플래그가 있어야
 * 돌고, sync-for-read 는 임시 git 저장소까지 만들어서 뺐다(check-setup.js 끝 주석).
 * 여기 이름을 넣으면 그 파일은 자동 발견에서 빠진다. 카탈로그에 이미 등록된 파일은 넣을 수
 * 없고(validateNodeChecks 가 막는다), 목록을 비우면 기반 파일들이 미등록으로 걸려 빨개진다. */
export const NODE_CHECK_EXEMPT=[
 'scripts/check-runner.js','scripts/check-catalog.js','scripts/check-setup.js',
 'scripts/check-deployed.js','scripts/check-prereqs.js','scripts/check-live-llm.js',
 'scripts/check-sync-for-read.js',
];
export function discoverNodeChecks(root,io=fs) {
  const out=[];
  for(const e of io.readdirSync(path.join(root,'scripts'),{withFileTypes:true})) {
    if(e.isSymbolicLink())throw new Error('Check discovery refuses symbolic links: '+e.name);
    if(e.isFile()&&/^check-.*\.js$/.test(e.name)&&!NODE_CHECK_EXEMPT.includes('scripts/'+e.name))out.push('scripts/'+e.name);
  }
  // 자기 방어: 발견 규칙이 좁아지거나 scripts/ 를 못 읽으면 0건이 되어 조용히 초록이 된다.
  // 하한은 실측(2026-09-16 기준 102개)보다 낮게만 잡고, 내리지 않는다.
  if(out.length<90)throw new Error('Node check discovery returned only '+out.length+' files; the discovery rule or root is broken');
  return out.sort();
}
/** scripts/ 의 check-*.js 가 전부 카탈로그에 실려 있나. 등록 블록 하나가 지워지면
 * 그 검사는 어디서도 안 돌면서 아무것도 안 빨개진다 — 그 구멍을 여기서 잡는다. */
export function validateNodeChecks(checks,discoveredNode) {
  const registered=new Set(checks.filter(c=>c.runtime==='node').map(c=>c.file));
  for(const f of NODE_CHECK_EXEMPT)if(registered.has(f))throw new Error('Exempt file is registered in the catalog: '+f);
  for(const f of discoveredNode)if(!registered.has(f))throw new Error('Unregistered node check: '+f);
}
export function validateCatalog(checks,discovered,{exists=()=>true}={}) {
  const seen=new Set();
  for(const c of checks) {
    if(!c.file||!['node','python','bash'].includes(c.runtime)||!['offline','archive'].includes(c.mode)||!c.what||!c.reason)
      throw new Error('Incomplete check classification: '+c.file);
    if(c.file.includes('\\')||c.file.startsWith('/')||c.file.split('/').some(p=>!p||p==='.'||p==='..'))
      throw new Error('Invalid check path: '+c.file);
    if(seen.has(c.file))throw new Error('Duplicate check: '+c.file);
    seen.add(c.file);
    if(!exists(c.file))throw new Error('Missing check: '+c.file);
  }
  const expected=new Set(checks.filter(c=>c.runtime!=='node').map(c=>c.file));
  if(new Set(discovered).size!==discovered.length)throw new Error('Duplicate discovered test');
  for(const f of discovered)if(!expected.has(f))throw new Error('Unclassified discovered test: '+f);
  for(const f of expected)if(!discovered.includes(f))throw new Error('Registered test not discovered: '+f);
}
export function offlineEnvironment(env) {
  const out={...env};
  for(const k of Object.keys(out)) {
    if(/^(NODE_OPTIONS|NODE_PATH|PYTHONPATH|PYTHONHOME|PYTHONSTARTUP|BASH_ENV|ENV)$/i.test(k)||
       /^(HERMES_|SLACK_|ANTHROPIC_)/i.test(k))delete out[k];
  }
  out.PYTHONDONTWRITEBYTECODE='1';
  out.PYTHONIOENCODING='utf-8';
  return out;
}
export function runOffline(checks,{root,spawn=spawnSync,exists=fs.existsSync,log=console.log,env=process.env,node=process.execPath}={}) {
  if(!checks.length)throw new Error('No offline checks selected');
  let passed=0,failed=0,unmeasured=0;
  for(const c of checks) {
    if(c.mode!=='offline'||c.runtime!=='node')throw new Error('Not an approved offline Node check: '+c.file);
    const script=path.join(root,c.file);
    let r;
    if(!exists(script))r={status:1,stderr:'Missing check: '+c.file};
    else {
      try {r=spawn(node,[script],{cwd:root,encoding:'utf8',env:offlineEnvironment(env),timeout:120000,maxBuffer:8*1024*1024});}
      catch(error){r={error,status:null};}
    }
    const text=String(r.stdout||'')+'\n'+String(r.stderr||'');
    const notes=text.split(/\r?\n/).filter(l=>l.trim().startsWith('[못잼]'));
    if(r.status===0&&!r.error) {
      if(notes.length){unmeasured++;log('[못잼] '+c.file);notes.forEach(l=>log(l));}
      else {passed++;log('PASS '+c.file);}
      text.split(/\r?\n/).filter(l=>l.trim().startsWith('[보임]')).forEach(l=>log(l));
    } else if(r.status===2&&!r.error) {
      unmeasured++;log('[못잼] '+c.file);log(text.trim());
    } else {
      failed++;log('FAIL '+c.file);
      log(r.error?.message||text.trim()||'Process terminated without a result');
    }
  }
  const exitCode=failed?1:unmeasured?2:0;
  log('offline: '+passed+' passed, '+failed+' failed, '+unmeasured+' unmeasured');
  return {passed,failed,unmeasured,exitCode};
}
