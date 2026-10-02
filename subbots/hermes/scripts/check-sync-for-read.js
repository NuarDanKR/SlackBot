#!/usr/bin/env node
/**
 * `syncForRead()` 가 네 상황에서 무엇을 돌려주나.
 *
 *   node scripts/check-sync-for-read.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * 이 함수는 **실패했을 때 무엇을 돌려주느냐가 전부**다. 성공 경로만 맞으면
 * 아무 의미가 없다 — 실패했는데 ok:true 를 돌려주면 위생 점검이 낡은 값을
 * 정상이라고 말하고, 그게 애초에 이 작업의 원인이었다 (2026-08-20).
 *
 * **네트워크를 안 쓴다.** 로컬 bare 저장소를 origin 으로 삼는다.
 * **WHK 저장소를 안 건드린다.** os.tmpdir() 아래에서만 돈다.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { syncForRead, parseCount } from '../src/ingest/git.js';

let ok = true;
const fail = (msg) => { ok = false; console.error(`✗ ${msg}`); };
const pass = (msg) => console.log(`✓ ${msg}`);

const run = (cwd, args) =>
  execFileSync('git', args, { cwd, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });

const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-syncread-'));
const origin = path.join(root, 'origin.git');
const work = path.join(root, 'work');
const other = path.join(root, 'other');

// origin (bare) + 첫 커밋을 넣을 클론 하나
fs.mkdirSync(origin);
run(origin, ['init', '--bare', '--initial-branch=main', '--quiet']);
run(root, ['clone', '--quiet', origin, 'work']);
run(work, ['config', 'user.email', 't@e.st']);
run(work, ['config', 'user.name', 'T']);
fs.writeFileSync(path.join(work, 'a.txt'), 'one\n');
run(work, ['add', 'a.txt']);
run(work, ['commit', '--quiet', '-m', 'one']);
run(work, ['push', '--quiet', '-u', 'origin', 'main']);

// origin 에 커밋 하나를 더 얹는다 → work 는 1 뒤처진다
run(root, ['clone', '--quiet', origin, 'other']);
run(other, ['config', 'user.email', 't@e.st']);
run(other, ['config', 'user.name', 'T']);
fs.writeFileSync(path.join(other, 'b.txt'), 'two\n');
run(other, ['add', 'b.txt']);
run(other, ['commit', '--quiet', '-m', 'two']);
run(other, ['push', '--quiet']);

try {
  // A. 정상 — 1 뒤처진 상태에서 pull 하면 따라잡는다
  {
    const r = await syncForRead({ cwd: work });
    if (r.ok !== true) fail(`A 정상: ok 가 ${r.ok} (true 여야 한다) · reason=${r.reason}`);
    else if (r.behind !== 0) fail(`A 정상: behind 가 ${r.behind} (0 이어야 한다)`);
    else if (!/^[0-9a-f]{7,}$/.test(r.head || '')) fail(`A 정상: head 가 '${r.head}'`);
    else if (!/^\d{2}:\d{2}:\d{2}$/.test(r.checkedAt || '')) fail(`A 정상: checkedAt 이 '${r.checkedAt}'`);
    else pass('A 정상 — pull 로 따라잡고 ok:true · behind:0');
    if (!fs.existsSync(path.join(work, 'b.txt'))) fail('A 정상: pull 이 실제로 안 됐다 (b.txt 없음)');
  }

  // B. 더러운 작업 트리 + 뒤처짐 → pull 은 거부, 그래도 fetch 는 되므로 behind 는 알아낸다
  //
  // ⚠️ **더럽히는 파일이 들어오는 커밋이 건드리는 파일과 같아야 한다.** 겹치지 않으면
  //    `pull --ff-only` 는 로컬 수정을 덮어쓸 일이 없으므로 **그냥 성공한다** — 그러면
  //    이 시나리오는 아무것도 검사하지 않는다 (2026-08-20 에 실제로 이렇게 틀려 있었다).
  {
    fs.writeFileSync(path.join(other, 'b.txt'), 'two — remote edit\n');  // ← 같은 파일
    run(other, ['commit', '--quiet', '-am', 'edit b']);
    run(other, ['push', '--quiet']);
    fs.writeFileSync(path.join(work, 'b.txt'), 'LOCAL EDIT\n');          // ← 같은 파일을 더럽힌다

    const r = await syncForRead({ cwd: work });
    if (r.ok !== false) fail(`B 더러운 트리: ok 가 ${r.ok} (false 여야 한다)`);
    else if (r.behind !== 1) fail(`B 더러운 트리: behind 가 ${r.behind} (1 이어야 한다)`);
    else if (!r.reason) fail('B 더러운 트리: reason 이 비었다 — 사유 없이 실패를 알리면 손쓸 수가 없다');
    else pass('B 더러운 트리 — ok:false · behind:1 · 사유 있음');
    run(work, ['checkout', '--', 'b.txt']);
  }

  // C. pull 은 실패하는데 이미 최신 → ok:true (「pull 성공」이 아니라 「안 뒤처짐」이 판정)
  {
    run(work, ['pull', '--ff-only', '--quiet']);              // 먼저 따라잡아 둔다
    fs.writeFileSync(path.join(work, '.git', 'index.lock'), ''); // pull 을 막는다
    const r = await syncForRead({ cwd: work });
    fs.rmSync(path.join(work, '.git', 'index.lock'), { force: true });
    if (r.behind !== 0) fail(`C 잠금+최신: behind 가 ${r.behind} (0 이어야 한다)`);
    else if (r.ok !== true) fail(`C 잠금+최신: ok 가 ${r.ok} — pull 실패해도 안 뒤처졌으면 정상이다`);
    else pass('C 잠금+최신 — pull 은 실패했지만 안 뒤처졌으므로 ok:true');
  }

  // D. 원격에 못 닿으면 behind 는 «모름» 이다 — 0 으로 적으면 안 된다
  //
  // ⚠️ **이 시나리오가 이 함수 전체에서 가장 중요하다.** `origin/main` 은 로컬에 캐시된
  //    참조라 원격이 사라져도 남아 있다. 캐시로 재면 «안 뒤처졌다»(behind 0) 가 나오고,
  //    그러면 **닿지도 못했는데 ok:true** 가 된다 — 낡은 상태를 «최신» 이라고 보고하는
  //    것이고 그게 이 작업이 없애려는 고장 그 자체다. 그래서 여기서 work 는 **이미
  //    최신인 상태**(C 를 지나온)로 들어온다. 캐시를 보면 반드시 통과해 버리는 조건에서
  //    `reached` 가 실제로 막아 주는지를 본다.
  {
    run(work, ['remote', 'set-url', 'origin', path.join(root, 'nope.git')]);
    const r = await syncForRead({ cwd: work });
    if (r.ok !== false) fail(`D 원격 없음: ok 가 ${r.ok} — 원격에 못 닿았으면 «최신» 이라고 말할 근거가 없다`);
    else if (r.behind !== null) fail(`D 원격 없음: behind 가 ${r.behind} — 캐시된 origin/main 으로 잰 값이다. 모르는 것은 null 이다`);
    else pass('D 원격 없음 — ok:false · behind:null (모름)');
  }
  // E. rev-list 가 내놓은 글자를 수로 읽는 규칙.
  //
  // ⚠️ **이건 밖에서 입력을 만들 수 없어서 따로 뗀 것이다.** `git rev-list --count` 는
  //    늘 숫자를 내므로 A~D 어느 시나리오로도 «빈 출력» 이나 «숫자가 아닌 출력» 을
  //    만들 수 없다. 그런데 거기가 이 함수에서 가장 위험한 자리다 — `Number('')` 은
  //    **0** 이고 `Number.isInteger(0)` 은 **true** 라, 빈 출력이 오면 「안 뒤처졌다」로
  //    읽혀 **원격에 닿지도 못한 상태가 ok:true 가 된다.** 이 함수가 없애려는 고장 그 자체다.
  {
    const cases = [
      ['3', 3, '보통'],
      ['0', 0, '안 뒤처짐'],
      [' 12\n', 12, '공백·줄바꿈이 섞여도'],
      ['', null, '빈 출력 — 0 이 아니라 «모름»'],
      ['abc', null, '숫자가 아니면 «모름»'],
      ['3.5', null, '정수가 아니면 «모름»'],
      ['-1', null, '음수는 있을 수 없다'],
    ];
    for (const [raw, want, why] of cases) {
      const got = parseCount(raw);
      if (got !== want) fail(`E ${why}: parseCount(${JSON.stringify(raw)}) 가 ${JSON.stringify(got)} — ${JSON.stringify(want)} 여야 한다`);
    }
    if (cases.every(([raw, want]) => parseCount(raw) === want)) pass('E 수 읽기 — 빈 출력·비정수는 0 이 아니라 null');
  }
} finally {
  fs.rmSync(root, { recursive: true, force: true });
}

console.log(ok ? '\n통과' : '\n어긋남');
process.exit(ok ? 0 : 1);
