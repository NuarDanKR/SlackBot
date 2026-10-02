#!/usr/bin/env node
/**
 * 실패 로그의 **오류** 절 — 여러 줄 오류 메시지가 md 를 안 깨뜨리나.
 *
 *   node scripts/check-error-block.js
 *
 * 종료코드: 0 통과 / 1 실패
 *
 * ── 왜 필요한가 ──
 *
 * `renderEntry` 의 **오류** 절은 인라인 백틱 하나로 감싼다. 인라인 코드는 줄바꿈을 담을
 * 수 없다 — 여러 줄 오류 메시지(자동 반영 중단의 `err.message`, ingest/index.js 가 그대로
 * `result.fatal` 에 싣는다)가 오면 백틱 밖으로 줄이 새어 md 가 깨진다. 멱등하게 깨지므로
 * 헛커밋은 안 나지만 읽기가 나쁘다(2026-08-28 최종 검토가 미룬 셋 중 하나, 2026-09-03 결정).
 *
 * **파일도 네트워크도 안 쓴다.** renderEntry 에 합성 항목을 직접 먹인다.
 */
import { renderEntry } from '../src/convo-log.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

/* ① 여러 줄 오류 — 펜스로 감싸져야 하고, 원문 줄이 전부 살아 있어야 한다 */
const MULTI = 'git push 실패: fatal: unable to access\nCould not resolve host\n두 번째 원인 줄';
const multi = renderEntry({
  at: '2026-08-28T08:30:00.000Z', kind: 'ingest', ok: false, target: 'WHK DM',
  origin: '아침 회차', error: MULTI,
});
if (!multi.includes('```')) fail(`여러 줄 오류인데 펜스 코드블록이 없습니다.\n${multi}`);
for (const line of MULTI.split('\n')) {
  if (!multi.includes(line)) fail(`오류 원문 줄이 렌더에서 빠졌습니다: "${line}"\n${multi}`);
}
// 인라인 백틱 하나로 감쌌다면 첫 줄 뒤에 짝 없는 백틱이 남아 그 줄 전체가 코드처럼 굳는다 —
// 그 모양(줄이 백틱 하나로 시작해 같은 줄에서 안 닫힘)이 없어야 한다.
if (/^`[^`]*$/m.test(multi.replace(/```[\s\S]*?```/g, ''))) {
  fail(`짝 없는 인라인 백틱이 남아 있습니다 — 여전히 깨진 모양입니다.\n${multi}`);
}

/* ② 한 줄 오류 — 여전히 가볍게(인라인) 나가야 한다. 매 건 펜스를 두르면 로그가 불어난다. */
const single = renderEntry({
  at: '2026-08-28T08:30:00.000Z', kind: 'daily', ok: false, target: '#요약채널',
  origin: '2026-08-28', error: 'overloaded_error — Overloaded',
});
if (!single.includes('`overloaded_error — Overloaded`')) {
  fail(`한 줄 오류가 인라인 백틱으로 안 나옵니다.\n${single}`);
}
if (single.includes('```')) {
  fail(`한 줄 오류인데 펜스가 붙었습니다 — 불필요하게 무겁습니다.\n${single}`);
}

if (!ok) process.exit(1);
console.log('  ✓ 여러 줄 오류는 펜스로, 한 줄 오류는 인라인으로 — md 가 안 깨집니다');
