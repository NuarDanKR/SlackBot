#!/usr/bin/env node
/**
 * RPC worker 의 **배치 계약** — 소켓 그룹·enable·설치 helper.
 *
 *   node scripts/check-rpc-unit.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 따로 재는가 ──
 *
 * 이 축의 실패는 **오류를 안 낸다.** 소켓 그룹이 틀리면 worker 는 멀쩡히 뜨고,
 * TYBot 은 연결에 실패하고, 사람은 「전문가가 조용히 답을 안 함」 만 본다.
 * 그 상태에서 볼 로그가 양쪽 어디에도 없다.
 *
 * ## 소켓 그룹 — 무엇이 틀리기 쉬운가
 *
 * 프로세스의 기본 그룹은 `hermes-rpc-<ws>` 이고 TYBot 은 거기 없다. 그래서 소켓은
 * **공유 그룹**(`tybot-specialist-rpc`)을 받아야 하는데, 디렉터리 그룹만 바꾸면
 * 안 된다 — 그 안에 만들어지는 파일은 여전히 프로세스 기본 그룹을 받는다.
 * 그룹을 물려주는 것은 **SGID**(`2750`) 하나뿐이다.
 *
 * 디렉터리 소유자를 `root` 로 돌리는 것도 안 된다. 그러면 `2750` 에서 group 은 `r-x`
 * 라, worker 는 그 안에 **소켓 파일을 만들지 못한다**.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRpcServer } from '../src/rpc/server.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..');
const UNIT = path.join(ROOT, 'deploy', 'hermes-rpc@.service');
const INSTALLER = path.join(ROOT, 'deploy', 'install-rpc.sh');
const SHARED_GROUP = 'tybot-specialist-rpc';

let failed = 0;
const lines = [];
const notes = [];
async function test(name, fn) {
  try { await fn(); lines.push(`  ✓ ${name}`); }
  catch (err) { failed += 1; lines.push(`  ✗ ${name}\n      ${String(err.message).split('\n')[0]}`); }
}

/** 주석을 뺀 지시문만. 설명문에 적힌 값을 설정으로 읽지 않는다. */
function directives(text) {
  return text.split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#'));
}

const unitText = fs.readFileSync(UNIT, 'utf8');
const unit = directives(unitText);
const has = (re) => unit.some((l) => re.test(l));

console.log('\n[1/4] 소켓 그룹 — TYBot 이 들어올 수 있는가');

await test('RuntimeDirectory 가 인스턴스별 자리다', () => {
  assert.ok(has(/^RuntimeDirectory=tybot-subbots\/hermes-%i$/),
    'RuntimeDirectory 가 `tybot-subbots/hermes-%i` 가 아닙니다');
});

await test('디렉터리에 SGID 가 걸려 있다 (2750)', () => {
  const mode = unit.find((l) => l.startsWith('RuntimeDirectoryMode='));
  assert.ok(mode, 'RuntimeDirectoryMode 가 없습니다');
  const value = mode.split('=')[1].trim();
  assert.equal(value, '2750',
    `RuntimeDirectoryMode 가 ${value} 입니다. SGID(2) 가 없으면 소켓이 프로세스 기본 그룹을 받고, TYBot 은 못 붙습니다`);
  // systemd 판본에 따라 SGID 가 떨어지므로 한 번 더 거는 줄이 있어야 한다.
  assert.ok(has(/^ExecStartPre=\+\/usr\/bin\/chmod 2750 \/run\/tybot-subbots\/hermes-%i$/),
    'chmod 2750 을 다시 거는 ExecStartPre 가 없습니다');
});

await test('디렉터리 그룹만 공유 그룹으로 바꾼다', () => {
  assert.ok(has(new RegExp(`^ExecStartPre=\\+/usr/bin/chgrp ${SHARED_GROUP} /run/tybot-subbots/hermes-%i$`)),
    `chgrp ${SHARED_GROUP} 이 없습니다`);
  // **소유자를 root 로 돌리지 않는다.** 돌리면 2750 에서 group 이 `r-x` 라
  // worker 가 그 안에 소켓 파일을 만들지 못한다.
  assert.ok(!has(/^ExecStartPre=.*\bchown\b.*root[: ]/),
    '디렉터리 소유자를 root 로 바꾸고 있습니다 — worker 가 소켓을 만들지 못합니다');
});

await test('계정은 인스턴스별, 보조 그룹은 공유 그룹 하나', () => {
  assert.ok(has(/^User=hermes-rpc-%i$/), 'User 가 인스턴스별이 아닙니다');
  assert.ok(has(/^Group=hermes-rpc-%i$/), 'Group 이 인스턴스별이 아닙니다');
  const extra = unit.filter((l) => l.startsWith('SupplementaryGroups='));
  assert.equal(extra.length, 1, '보조 그룹 줄이 하나가 아닙니다');
  assert.equal(extra[0], `SupplementaryGroups=${SHARED_GROUP}`,
    'TYBot 기본 그룹을 공유하면 도구 소켓과 함께 아카이브 파일 권한까지 따라옵니다');
});

await test('서버가 소켓을 0660 으로 열고 그룹을 맞춘다', () => {
  const src = fs.readFileSync(path.join(ROOT, 'src', 'rpc', 'server.js'), 'utf8');
  assert.match(src, /mode = 0o660/, '소켓 기본 모드가 0660 이 아닙니다');
  assert.match(src, /alignSocketGroup\(socketPath\)/, '소켓 그룹을 맞추는 호출이 없습니다');
  assert.match(src, /export function alignSocketGroup/, 'alignSocketGroup 이 없습니다');
});

console.log('[2/4] 실제 그룹 상속 (리눅스에서만)');

await test('SGID 디렉터리에서 연 소켓이 디렉터리 그룹을 받는다', async () => {
  if (process.platform === 'win32') {
    notes.push('그룹 상속: Windows 에는 gid 가 없다 — **못 쟀다**. 리눅스 smoke 참조');
    return;
  }
  // 기본 그룹과 **다른** 보조 그룹이 있어야 상속을 실제로 잴 수 있다.
  const groups = (process.getgroups?.() || []).filter((g) => g !== process.getgid?.());
  if (!groups.length) {
    notes.push('그룹 상속: 기본 그룹 말고 쓸 수 있는 보조 그룹이 없다 — **못 쟀다**');
    return;
  }
  const target = groups[0];
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-sgid-'));
  const sub = path.join(dir, 'sock');
  fs.mkdirSync(sub);
  fs.chownSync(sub, -1, target);
  fs.chmodSync(sub, 0o2750); // SGID

  const at = path.join(sub, 'http.sock');
  const worker = createRpcServer({
    secret: Buffer.alloc(32, 7), workspace: 'x', version: 'v', answerer: { answer: async () => ({}) },
    console: { error() {}, warn() {} },
  });
  try {
    await worker.listen(at);
    const st = fs.statSync(at);
    assert.equal(st.gid, target,
      `소켓 그룹이 ${st.gid} 입니다(디렉터리 ${target}). SGID 상속이 안 됐습니다`);
    assert.equal(st.mode & 0o777, 0o660, `소켓 모드가 ${(st.mode & 0o777).toString(8)} 입니다`);
  } finally {
    await worker.close().catch(() => {});
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

await test('그룹이 어긋나면 기동을 막는다', async () => {
  if (process.platform === 'win32') {
    notes.push('그룹 어긋남 거부: Windows 에서 **못 쟀다**');
    return;
  }
  const { alignSocketGroup } = await import('../src/rpc/server.js');
  // 소켓 gid 와 디렉터리 gid 가 다르고 chown 도 못 하는 상황.
  assert.throws(
    () => alignSocketGroup('/x/http.sock', {
      stat: (p) => ({ gid: p.endsWith('.sock') ? 1001 : 2002 }),
      chown: () => { const e = new Error('EPERM'); e.code = 'EPERM'; throw e; },
    }),
    /SGID/,
    '그룹을 못 맞췄는데 그냥 지나갑니다',
  );
  // 이미 맞으면 아무 일도 안 한다.
  let touched = false;
  const gid = alignSocketGroup('/x/http.sock', {
    stat: () => ({ gid: 3003 }),
    chown: () => { touched = true; },
  });
  assert.equal(gid, 3003);
  assert.equal(touched, false, '이미 맞는데 chown 을 불렀습니다');
});

console.log('[3/4] enable 할 수 있는가');

await test('[Install] 이 있다', () => {
  assert.ok(unit.includes('[Install]'),
    '[Install] 이 없으면 `systemctl enable` 이 「no installation config」 로 실패합니다');
  assert.ok(has(/^WantedBy=multi-user\.target$/), 'WantedBy 가 없습니다');
});

await test('그래도 기본은 꺼져 있다', () => {
  // unit 에 `[Install]` 이 있는 것과 켜져 있는 것은 다르다. 설치 helper 가
  // enable·start 를 하지 않는 것이 「기본 disabled」 의 실제 내용이다.
  // **실행과 출력을 가른다.** helper 는 켜는 명령을 사람에게 *보여 준다* — 그 줄은
  // `say "  systemctl enable …"` 이라 따옴표 안에 있다. 실행하는 줄은 명령으로 시작한다.
  const run = fs.readFileSync(INSTALLER, 'utf8')
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l && !l.startsWith('#'));
  const executes = run.filter((l) => /^(sudo\s+)?systemctl\s+(enable|start|restart)\b/.test(l));
  assert.deepEqual(executes, [], `설치 helper 가 켭니다: ${executes.join(' / ')}`);
  // 대신 **어떻게 켜는지는 알려 줘야 한다.** 안 알려 주면 설치하고 아무 일도 안 일어난다.
  assert.ok(/systemctl enable/.test(run.join('\n')), '켜는 명령을 안내하지 않습니다');
});

console.log('[4/4] 설치 helper');

const sh = fs.readFileSync(INSTALLER, 'utf8');
const shCode = sh.split(/\r?\n/).filter((l) => !l.trim().startsWith('#')).join('\n');

await test('unit 을 0644 로 설치하고 daemon-reload 한다', () => {
  assert.match(shCode, /install -o root -g root -m 0644/, '0644 로 설치하지 않습니다');
  assert.match(shCode, /systemctl daemon-reload/, 'daemon-reload 가 없습니다');
});

await test('멱등하다 — 같으면 복사도 reload 도 안 한다', () => {
  assert.match(shCode, /cmp -s/, '기존 파일과 비교하지 않습니다');
});

await test('계정·비밀·env 를 만들지 않는다', () => {
  for (const [re, why] of [
    [/^\s*(sudo\s+)?useradd\s/m, '계정을 만듭니다'],
    [/^\s*(sudo\s+)?groupadd\s/m, '그룹을 만듭니다'],
    [/^\s*(sudo\s+)?usermod\s/m, '그룹 멤버십을 바꿉니다'],
    [/openssl\s+rand/, 'HMAC 비밀을 만듭니다 — 한쪽만 새로 만들면 전부 401 입니다'],
    [/install .*\/dev\/null .*rpc\.env/, 'env 파일을 만듭니다'],
  ]) {
    assert.ok(!re.test(shCode), `설치 helper 가 ${why}`);
  }
});

await test('모자란 것을 사람이 칠 수 있게 적어 준다', () => {
  for (const needle of ['useradd', 'usermod', 'groupadd', 'HERMES_DOMAIN']) {
    assert.ok(sh.includes(needle), `안내에 ${needle} 이 없습니다`);
  }
  // 비밀 값을 찍지 않는다.
  assert.ok(/wc -c/.test(shCode), '비밀을 길이로만 보지 않습니다');
  assert.ok(!/cat .*hmac|echo .*\$\(.*hmac/.test(shCode), '비밀 값을 출력합니다');
});

await test('실행 권한 비트가 서 있다', () => {
  if (process.platform === 'win32') {
    notes.push('helper 실행 비트: Windows 에서 **못 쟀다**');
    return;
  }
  assert.ok(fs.statSync(INSTALLER).mode & 0o111, 'install-rpc.sh 에 실행 비트가 없습니다');
});

console.log(`\n${lines.join('\n')}`);
if (notes.length) {
  console.log('\n  못 쟀다:');
  for (const n of notes) console.log(`    · ${n}`);
}
if (failed) {
  console.error(`\n✗ ${failed}건 어긋남`);
  process.exit(1);
}
console.log('\n✓ 배치 계약이 서 있다');
