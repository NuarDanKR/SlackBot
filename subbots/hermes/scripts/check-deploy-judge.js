#!/usr/bin/env node
/**
 * 배포 판정이 「받았는데 재시작 안 함」을 실제로 잡나.
 *
 *   node scripts/check-deploy-judge.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 ──
 *
 * 2026-08-13 에 06:33 에 push 한 소스 고침이 07:00 회차에 안 붙었다. VM 에서
 * `git log` 는 새 커밋을 보여줬고 pull 도 성공했는데, **재시작을 안 해서** 옛 코드가
 * 계속 돌았다. 에러도 경고도 안 났다 — 「배포됐다」로 읽기 딱 좋은 화면이었다.
 *
 * 그 자리를 잡으려면 **해시 대조로는 모자란다.** 해시는 같은데(받았으니까) 도는 코드가
 * 옛것이기 때문이다. 시각을 봐야 한다 — VM 이 그 커밋으로 옮겨간 시각이 봇 기동 시각보다
 * 뒤면 아직 안 붙은 것이다. **두 시각을 다 VM 자기 시계에서 읽는다.** 커밋 시각을 쓰면
 * 내 PC 시계와 섞이고, 오래된 커밋을 늦게 push 한 경우에 조용히 통과한다.
 *
 * **파일도 네트워크도 안 쓴다.** 순수 함수 하나에 합성 입력을 먹인다.
 * 그래서 판정이 `src/deploy-judge.js` 에 따로 있다 — `scripts/check-deployed.js` 를
 * 그대로 import 하면 그 안의 VM 조회가 함께 돌아, 네트워크 없이 도는 성질이 깨진다.
 */
import { judgeDeploy } from '../src/deploy-judge.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

const has = (rs, level, needle) =>
  rs.some((r) => r.level === level && r.text.includes(needle));

/** 다 붙은 상태. 시각은 epoch 초. */
const base = () => ({
  codeUnpushed: 0,
  dataUnpushed: 0,
  codeRemote: 'aaa111',
  dataRemote: 'bbb222',
  vmWhy: '',
  vm: {
    codeHead: 'aaa111',
    dataHead: 'bbb222',
    codeMovedAt: 1000,
    botStartedAt: 2000,   // 받은 뒤에 떴다 = 붙었다
    configMtime: 1500,
    active: 'active',
  },
});

// ── ① 다 붙었으면 나쁜 것이 하나도 없다 ──
{
  const rs = judgeDeploy(base());
  if (rs.some((r) => r.level === 'bad')) fail(`다 붙었는데 bad 가 났습니다 — ${JSON.stringify(rs)}`);
  if (!has(rs, 'ok', '붙었습니다')) fail('다 붙었을 때 확인 문구가 없습니다.');
}

// ── ② 받았는데 재시작 안 함 = 해시는 같은데 시각이 뒤집힘 ──
{
  const o = base();
  o.vm.codeMovedAt = 3000;      // 기동(2000) 뒤에 받았다
  const rs = judgeDeploy(o);
  if (!has(rs, 'bad', '재시작')) fail('받았는데 재시작 안 한 것을 못 잡았습니다 — 이게 2026-08-13 의 그 자리입니다.');
}

// ── ③ 아예 안 받음 = 해시가 다르다 ──
{
  const o = base();
  o.vm.codeHead = 'ccc333';
  const rs = judgeDeploy(o);
  if (!has(rs, 'bad', '옛 코드')) fail('VM 이 옛 코드인 것을 못 잡았습니다.');
}

// ── ④ 설정만 바뀌고 재시작 안 함 ──
{
  const o = base();
  o.vm.configMtime = 2500;      // 기동(2000) 뒤에 바뀌었다
  const rs = judgeDeploy(o);
  if (!has(rs, 'bad', '설정')) fail('설정이 바뀌고 재시작 안 한 것을 못 잡았습니다.');
}

// ── ⑤ 안 나간 커밋 ──
{
  const o = base();
  o.codeUnpushed = 2;
  const rs = judgeDeploy(o);
  if (!has(rs, 'bad', '안 나간 커밋')) fail('안 나간 코드 커밋을 못 잡았습니다.');
}

// ── ⑥ 자료가 아직 안 붙은 것은 나쁜 것이 아니다 (15분 안에 붙는다) ──
{
  const o = base();
  o.vm.dataHead = 'ddd444';
  const rs = judgeDeploy(o);
  if (rs.some((r) => r.level === 'bad' && r.text.includes('자료'))) {
    fail('자료가 아직 안 붙은 것을 bad 로 냈습니다 — 15분 안에 저절로 붙습니다.');
  }
  if (!has(rs, 'info', '자료')) fail('자료가 안 붙은 것을 알려주지도 않았습니다.');
}

// ── ⑦ 봇이 죽어 있으면 bad ──
{
  const o = base();
  o.vm.active = 'failed';
  const rs = judgeDeploy(o);
  if (!has(rs, 'bad', '봇')) fail('봇이 안 도는 것을 못 잡았습니다.');
}

// ── ⑧ VM 을 못 쟀으면 「통과」가 아니라 「못 쟀다」다 ──
{
  const o = base();
  o.vm = null;
  o.vmWhy = 'GCP_VM 이 .env 에 없습니다';
  const rs = judgeDeploy(o);
  if (rs.some((r) => r.level === 'ok' && r.text.includes('붙었습니다'))) {
    fail('VM 을 못 쟀는데 「붙었습니다」를 냈습니다 — 통과와 못 잰 것은 다릅니다.');
  }
  if (!has(rs, 'unknown', 'GCP_VM')) fail('못 쟨 사유를 그대로 싣지 않았습니다.');
}

if (ok) {
  console.log('[check-deploy-judge] OK — 「받았는데 재시작 안 함」·「못 쟀다」를 가려냅니다.');
} else {
  console.error('\n고칠 곳: src/deploy-judge.js 의 judgeDeploy()');
  process.exitCode = 1;
}
