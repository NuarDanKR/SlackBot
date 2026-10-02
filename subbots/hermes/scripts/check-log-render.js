#!/usr/bin/env node
/**
 * 대화 로그 렌더가 **원본이 사라진 것과 기록이 0건인 것을 가르나.**
 *
 *     node scripts/check-log-render.js
 *
 * 종료코드: 0 통과 / 1 실패 있음
 *
 * ── 왜 남겨 두는가 ──
 *
 * 렌더는 「원본이 유일한 진실」이라 md 를 통째로 다시 쓴다. 그 전제가 깨지는 자리가 하나 있다 —
 * 원본(`LOG_RAW_DIR`)은 git 밖이라 **클론·재배포에 안 딸려온다.** 그때 빈 원본을 「0건」으로
 * 읽으면 렌더본을 빈 표로 덮어쓴다.
 *
 * 2026-08-31 저장소를 가르며 실제로 났고 `hermes-log/2026-08.md` 에서 6,743줄이 지워진 채
 * 커밋·push 됐다. 에러는 안 났다 — 렌더는 정상 종료했고 커밋도 push 도 성공했으며, 남은
 * 머리말의 「문답 0건」은 **그날 대화가 없었다는 뜻으로도 읽힌다.**
 *
 * 그때 `months()` 에만 가드가 들어가 월별 md 는 지켜졌지만 `index.md` 는 계속 덮였다.
 * **절반만 고친 상태가 그대로 남아 있었고** 이 검사가 그 절반을 못 박는다.
 *
 * 두 가지를 본다:
 *
 *   ① 판정(`sourceGone`)이 네 경우를 제대로 가르나 — **실물 함수를 그대로 부른다.**
 *      검사가 같은 조건을 따로 적으면 저쪽이 바뀌어도 둘이 사이좋게 틀린다.
 *   ② 그 판정이 `renderAll` 에 **실제로 배선돼 있나** — ①이 맞아도 안 불리면 소용이 없다.
 *      이 기계의 실제 상태로 확인하므로, 원본이 있는 기계와 없는 기계에서 보는 것이 다르다.
 *      어느 쪽이든 하나는 반드시 확인된다.
 */
import fs from 'node:fs';
import { renderAll, sourceGone } from '../src/convo-log.js';
import { LOG_ENABLED, LOG_RAW_DIR } from '../src/config.js';

let bad = 0;
const fail = (m) => {
  console.log(`  ✗ ${m}`);
  bad += 1;
};
const ok = (m) => console.log(`  ✓ ${m}`);

/* ① 판정 자체. 「원본 0 · 렌더본 있음」만 참이어야 한다. */
const CASES = [
  { raw: [], rendered: ['2026-08'], want: true, why: '원본이 사라졌다 — 멈춰야 한다' },
  { raw: [], rendered: [], want: false, why: '첫 실행 — 둘 다 없는 것은 정상이다' },
  { raw: ['2026-08'], rendered: ['2026-08'], want: false, why: '평상시' },
  { raw: ['2026-08'], rendered: [], want: false, why: '아직 렌더 전 — 써야 한다' },
];
let judged = 0;
for (const c of CASES) {
  const got = sourceGone(c.raw, c.rendered);
  if (got !== c.want) {
    fail(`판정이 틀립니다 — 원본 ${c.raw.length}개 · 렌더본 ${c.rendered.length}개 → ${got} (기대 ${c.want}). ${c.why}`);
  } else {
    judged += 1;
  }
}
if (judged === CASES.length) ok(`원본 없음과 0건을 가릅니다 (${CASES.length}가지)`);

/* ② 배선. renderAll 이 그 판정을 실제로 쓰는가. 쓰지 않는다(write:false). */
if (!LOG_ENABLED) {
  /* `[못잼]` 표시를 단다 — 종료코드 0 이라 `check-setup.js` 는 통과로 적는데, 그 화면에
   * 이 줄이 안 올라가면 「②를 안 쟀다」가 초록 한 줄에 묻힌다. 문구(「안 댔습니다」)에
   * 기대던 시절에는 그쪽 말버릇 목록에 안 물려 실제로 묻혔다. */
  console.log('[못잼]      · 대화 로그가 꺼져 있어 배선은 안 댔습니다 (config.json 의 log.enabled)');
} else {
  const res = renderAll({ write: false });
  const rawExists = fs.existsSync(LOG_RAW_DIR);
  if (!rawExists) {
    /* 이 기계엔 원본이 없다 — 결함이 있으면 여기서 드러난다. */
    if (!res?.noSource) {
      fail('원본이 없는데 renderAll 이 멈추지 않았습니다 — 렌더본을 빈 표로 덮어씁니다.');
      console.log(`      원본 자리: ${LOG_RAW_DIR}`);
      console.log(`      index.md 를 덮으려 하나: ${res?.index ? res.index.changed : '(모름)'}`);
    } else {
      ok(`원본이 없어 renderAll 이 멈춥니다 (렌더본 ${res.rendered.length}개 지켜짐)`);
    }
  } else {
    /* 원본이 있는 기계 — 멈추면 안 된다. 가드가 지나치게 잡는 것도 결함이다. */
    if (res?.noSource) {
      fail('원본이 있는데 renderAll 이 멈췄습니다 — 가드가 지나칩니다.');
    } else {
      ok('원본이 있어 renderAll 이 정상 렌더합니다');
    }
  }
}

if (bad) {
  console.log('\n원본이 사라진 날 렌더본이 지워집니다.');
  process.exit(1);
}
console.log('\n원본 없음을 0건으로 읽지 않습니다.');
