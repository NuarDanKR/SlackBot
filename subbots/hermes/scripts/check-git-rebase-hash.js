#!/usr/bin/env node
/**
 * `commitAndPush()` 가 push 거절 → `pull --rebase` → 재시도를 거친 뒤, DM 에 알리는
 * `sha` 가 **그 시점의 진짜 HEAD** 를 가리키나.
 *
 *   node scripts/check-git-rebase-hash.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 (2026-09-02 전수조사) ──
 *
 * `git pull --rebase` 는 로컬 커밋을 origin 뒤에 다시 얹으며 **해시를 새로 만든다**
 * (replay). 전에는 `commit` 직후에 `sha` 를 한 번만 읽었는데, rebase 가 있었던 회차는
 * 그 순간부터 그 해시가 **어느 브랜치 끝에도 없는 값**이 된다 — DM 은 "커밋 abc1234
 * 를 올렸습니다"라고 말하는데 `git show abc1234` 도 GitHub 검색도 그 해시를 못 찾는다.
 *
 * **네트워크를 안 쓴다.** 로컬 bare 저장소를 origin 으로 삼는다 (`check-sync-for-read.js`
 * 와 같은 방식). **WHK 저장소를 안 건드린다.** os.tmpdir() 아래에서만 돈다.
 *
 * `commitAndPush` 는 `cwd` 인자가 없고 모듈 상수 `DATA_ROOT`(공식은 `HERMES_DATA_ROOT`
 * 환경변수)로만 저장소를 정하므로, 같은 프로세스 안에서 값을 바꿔치기할 수 없다.
 * 그래서 임시 저장소를 가리키는 환경변수로 **새 프로세스를 하나 띄운다**
 * (`check-live-fetch-max-days.js` 와 같은 이유·같은 방식).
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

const run = (cwd, args) => execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-rebasehash-'));
const originDir = path.join(root, 'origin.git');
const work = path.join(root, 'work');
const other = path.join(root, 'other');

try {
  fs.mkdirSync(originDir);
  run(originDir, ['init', '--bare', '--initial-branch=main', '--quiet']);
  run(root, ['clone', '--quiet', originDir, 'work']);
  for (const dir of [work]) {
    run(dir, ['config', 'user.email', 't@e.st']);
    run(dir, ['config', 'user.name', 'T']);
  }
  fs.writeFileSync(path.join(work, 'seed.txt'), 'seed\n');
  run(work, ['add', 'seed.txt']);
  run(work, ['commit', '--quiet', '-m', 'seed']);
  run(work, ['push', '--quiet', '-u', 'origin', 'main']);

  run(root, ['clone', '--quiet', originDir, 'other']);
  run(other, ['config', 'user.email', 't@e.st']);
  run(other, ['config', 'user.name', 'T']);

  // config.js 가 DATA_ROOT/config.json 을 못 읽으면 process.exit(1) 로 죽는다 — 임시
  // 저장소 안에 최소 설정을 하나 둔다 (내용은 config.example.json 그대로 복사).
  fs.copyFileSync(path.join(ROOT, 'config.example.json'), path.join(work, 'config.json'));

  // `other` 가 먼저 origin 에 커밋을 하나 얹는다 → work 의 다음 push 는 거절되고
  // commitAndPush 의 rebase 경로를 반드시 타게 된다.
  fs.writeFileSync(path.join(other, 'race.txt'), 'race\n');
  run(other, ['add', 'race.txt']);
  run(other, ['commit', '--quiet', '-m', 'race']);
  run(other, ['push', '--quiet']);

  // work 쪽에 다른 파일을 고쳐 commitAndPush 로 커밋+push 를 태운다.
  fs.writeFileSync(path.join(work, 'seed.txt'), 'seed — local change\n');

  const probe = [
    `import { commitAndPush } from ${JSON.stringify(pathToFileURL(path.join(ROOT, 'src/ingest/git.js')).href)};`,
    "const r = await commitAndPush('test commit', ['seed.txt']);",
    'console.log(JSON.stringify(r));',
  ].join('\n');
  const probeFile = path.join(root, 'probe.mjs');
  fs.writeFileSync(probeFile, probe);

  const r = spawnSync(process.execPath, [probeFile], {
    cwd: work,
    encoding: 'utf-8',
    env: { ...process.env, HERMES_DATA_ROOT: work },
  });

  console.log('[1/2] commitAndPush 가 rebase 경로를 실제로 탔나 (전제 확인)');
  if (r.status !== 0) {
    fail(`commitAndPush 를 부르지 못했습니다 — ${(r.stderr || '').split('\n').slice(0, 6).join(' / ')}`);
  } else {
    let out;
    try { out = JSON.parse(r.stdout.trim().split('\n').pop()); } catch (e) {
      fail(`출력을 못 읽었습니다: ${e.message} — ${r.stdout}`);
    }
    if (out) {
      if (!out.committed) fail(`커밋이 안 됐습니다 — ${JSON.stringify(out)}`);
      else pass('커밋됐다');

      console.log('\n[2/2] DM 에 알릴 sha 가 진짜 HEAD 이고, origin 에도 실제로 있나');
      const realHead = run(work, ['rev-parse', '--short', 'HEAD']).trim();
      if (out.sha !== realHead) {
        fail(`알린 sha(${out.sha})가 지금 HEAD(${realHead})와 다릅니다 — rebase 전 해시를 그대로 알렸을 가능성`);
      } else {
        pass(`알린 sha(${out.sha})가 지금 HEAD와 같다`);
      }
      if (!out.pushed) {
        fail(`push 가 실패했습니다 — rebase 경로를 못 탔거나(전제가 깨짐) 다른 문제 — ${JSON.stringify(out)}`);
      } else {
        // origin(bare) 에 그 해시가 실제로 있는지 — «없는 커밋 해시」인지의 직접 증거.
        let onOrigin = false;
        try { run(originDir, ['cat-file', '-e', out.sha]); onOrigin = true; } catch { /* 아래에서 실패 처리 */ }
        if (!onOrigin) fail(`origin 에 ${out.sha} 커밋이 없습니다 — 알린 해시가 진짜로 "없는 커밋 해시"입니다`);
        else pass(`origin 에도 ${out.sha} 커밋이 있다 (없는 커밋 해시가 아니다)`);
      }
    }
  }
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}

if (ok) {
  console.log('\n[check-git-rebase-hash] OK — rebase 뒤에도 DM 의 sha 가 실제 HEAD·origin 과 맞습니다.');
} else {
  console.error('\n고칠 곳: src/ingest/git.js 의 commitAndPush — sha 를 push 시도가 다 끝난 뒤에 읽어야 합니다.');
  process.exitCode = 1;
}
