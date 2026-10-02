#!/usr/bin/env node
/**
 * node-cron 이 회차를 건너뛰면(`execution:missed`) `scheduler.js` 가 조용히 넘어가지
 * 않고 실제로 알리나(`onFailure` → 로그 + DM).
 *
 *   node scripts/check-missed-execution.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 (2026-09-02 전수조사) ──
 *
 * node-cron 의 기본 `missedExecutionTolerance` 는 1초다 — 예정 시각을 1초 넘겨 다음
 * 검사가 돌면 그 회차는 **실행되지 않고** `execution:missed` 로만 알려진다. 리스너를
 * 안 달면 node-cron 자체 로거의 `console.warn` 한 줄이 전부다(이 파일의 `onFailure`
 * 머리말이 이미 적어 둔 2026-08-06 사고와 같은 모양 — "콘솔 로그 한 줄이 전부라 밖에서는
 * 아무 표시가 없었다"). 게다가 **실행 자체가 안 됐으므로** 그 잡 자신의 실패 보고
 * (`SELF_REPORTS`)도 못 돈다 — 여기서 놓치면 그 회차는 로그에도 DM 에도 안 남는다.
 *
 * **네트워크·슬랙을 안 쓴다.** `client` 를 흉내만 내고 실제 호출을 가로챈다.
 * **가짜 config 로 새 프로세스를 하나 띄운다** — `config` 는 모듈 로드 시 한 번만
 * 읽히는 상수라 같은 프로세스 안에서 cron 식을 바꿔칠 수 없다
 * (`check-live-fetch-max-days.js`·`check-git-rebase-hash.js` 와 같은 이유).
 * **이벤트 루프를 실제로 동기 코드로 막아서** 진짜 missed 를 일으킨다 — 흉내가 아니라
 * node-cron 이 스스로 판단하게 둔다(위 smoke 확인: 2.5초 막으면 1회 missed).
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

console.log('[1/2] 소스 모양 — schedule() 이 execution:missed 를 실제로 듣고 onFailure 로 잇나');
{
  const text = fs.readFileSync(path.join(ROOT, 'src/scheduler.js'), 'utf-8');
  const code = text.split('\n').filter((l) => !/^\s*(\/\/|\*|\/\*)/.test(l)).join('\n');
  if (!/task\.on\(\s*['"]execution:missed['"]/.test(code)) {
    fail("scheduler.js 가 task.on('execution:missed', …) 를 안 답니다");
  } else if (!/execution:missed[\s\S]{0,400}onFailure\(/.test(code)) {
    fail("execution:missed 리스너 안에서 onFailure 를 안 부릅니다 (콘솔 로그만 남길 수 있습니다)");
  } else {
    pass('execution:missed 리스너가 있고 onFailure 로 잇는다');
  }
}

console.log('\n[2/2] 실제 동작 — 이벤트 루프를 막아 진짜 missed 를 내고, DM·기록까지 가나');
{
  // **이 가짜 아카이브에는 개명 지도(`slack-export/.sync-state.json`)가 없다.** 지금은
  // 아카이브 자료를 안 읽어서 괜찮지만, 읽게 되면 `src/config.js` 가 지도를 「죽음」으로
  // 보고 **전체 권한이 아닌 접근을 전부 닫는다**(fail-closed) — 검사는 초록인데 그 초록은
  // **닫힘이 만든 것**이다. 자료를 읽게 고칠 때는 지도도 함께 쓴다
  // (`check-doc-card-kill-switch.js` 의 주석이 원본).
  const tmp = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'hermes-missed-'));
  try {
    const cfg = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf-8'));
    cfg.log = { enabled: false };                          // 진짜 저장소 밖으로 안 쓴다
    cfg.owner = { name: '시험', slackUserId: 'U_TEST' };    // 없으면 notifyFailure 가 조용히 나간다
    cfg.digest = cfg.digest || {};
    cfg.digest.health = { enabled: true, cron: '* * * * * *' };  // 매초 — 6번째 자리가 초
    fs.writeFileSync(path.join(tmp, 'config.json'), JSON.stringify(cfg));
    fs.mkdirSync(path.join(tmp, 'slack-export', 'channels'), { recursive: true });
    fs.mkdirSync(path.join(tmp, 'documents', 'projects'), { recursive: true });

    const probe = [
      `import { schedule, JOBS } from ${JSON.stringify(pathToFileURL(path.join(ROOT, 'src/scheduler.js')).href)};`,
      // 실제 위생 점검(runHealth)은 슬랙·아카이브가 필요하다 — 놓친 실행 판정과 무관하니
      // 빠르게 끝나는 가짜로 바꿔친다. schedule() 호출 **전에** 바꿔야 그 값을 집어간다.
      "JOBS.health.run = async () => ({ sent: false, reason: 'no-activity' });",
      'const calls = [];',
      'const client = {',
      '  conversations: { open: async () => ({ channel: { id: "C_TEST" } }) },',
      '  chat: { postMessage: async (args) => { calls.push(args.text); return {}; } },',
      '};',
      "const task = schedule(client, 'health');",
      'if (!task) { console.log(JSON.stringify({ error: "schedule() 이 task 를 안 돌려줍니다" })); process.exit(1); }',
      '',
      '// 500ms 뒤부터 2.5초 동안 이벤트 루프를 동기적으로 막는다 — 그 구간에 걸린 매초',
      '// 틱 하나 이상이 missed 로 잡힌다 (smoke 확인: 1초 tolerance, 2.5초면 안정적으로 잡힘).',
      'await new Promise((r) => setTimeout(r, 500));',
      'const blockUntil = Date.now() + 2500;',
      'while (Date.now() < blockUntil) { /* 일부러 막는다 */ }',
      '',
      '// 막은 뒤 node-cron 이 missed 를 emit 하고 onFailure(비동기)가 끝날 시간을 준다.',
      'await new Promise((r) => setTimeout(r, 800));',
      'task.stop?.();',
      'console.log(JSON.stringify({ dmCount: calls.length, dm: calls[0] || null }));',
    ].join('\n');
    const probeFile = path.join(tmp, 'probe.mjs');
    fs.writeFileSync(probeFile, probe);

    const r = spawnSync(process.execPath, [probeFile], {
      cwd: tmp,
      encoding: 'utf-8',
      timeout: 15000,
      env: { ...process.env, HERMES_DATA_ROOT: tmp },
    });
    if (r.error || r.status !== 0) {
      fail(`놓친 실행 시나리오를 못 돌렸습니다 — ${(r.stderr || r.error?.message || '').split('\n').slice(0, 8).join(' / ')}`);
    } else {
      let out;
      try { out = JSON.parse(r.stdout.trim().split('\n').pop()); } catch (e) {
        fail(`출력을 못 읽었습니다: ${e.message} — ${r.stdout}`);
      }
      if (out?.error) fail(out.error);
      else if (out) {
        if (!out.dmCount) {
          fail('이벤트 루프를 막았는데 DM 이 한 통도 안 갔습니다 — execution:missed 가 조용히 넘어갑니다');
        } else if (!/건너뛰었습니다/.test(out.dm || '')) {
          fail(`DM 은 갔는데 놓친 실행 문구가 없습니다 — ${JSON.stringify(out.dm)}`);
        } else {
          pass(`이벤트 루프를 막으니 실제로 DM 이 갔다 — "${out.dm.split('\n')[0]}"`);
        }
      }
    }
  } finally {
    fs.rmSync(tmp, { recursive: true, force: true });
  }
}

if (ok) {
  console.log('\n[check-missed-execution] OK — 놓친 예약 실행이 조용히 안 넘어가고 알립니다.');
} else {
  console.error('\n고칠 곳: src/scheduler.js 의 schedule() — task.on("execution:missed", …) 에서 onFailure 를 불러야 합니다.');
  process.exitCode = 1;
}
