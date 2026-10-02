#!/usr/bin/env node
/**
 * 월 헤딩(`## 2026-08`) 정규식이 실물에서 **관대한 쪽**(`^##\s+`, 공백 몇 칸이든)으로
 * 도나 — `archive.js` 의 `MONTH_HEADING`·`extractMonthSection` 이 그 정본이다.
 *
 *   node scripts/check-month-heading.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 (2026-09-02 전수조사) ──
 *
 * `archive.js`·`documents.js` 만 공백 **한 칸** 고정(`## `)이었고, 나머지 여섯
 * (`summary.js`·`ingest/verify.js`·`insert_entry.py`·`verify_format.py`·`insert_messages.py`·
 * `review_work.py`)은 처음부터 `^##\s+` 였다. `##  2026-08`(두 칸)이면 그 여섯은 통과시키는데
 * `archive.js` 의 `readChannel` 만 그 달을 못 찾아 "섹션이 없습니다"로 조용히 실패했다.
 * 정본은 관대한 쪽으로 정해졌다(WHK 결정 2026-09-03) — 여섯이 이미 그 모양이었고, 통과시킨
 * 것을 봇이 못 찾는 쪽보다 못 찾는 쪽을 넓히는 것이 맞다.
 *
 * 여기서는 `archive.js` 가 내보내는 순수 함수(`extractMonthSection`)에 **합성 입력**을
 * 먹여 본다. 디스크의 실물 아카이브가 없어도(이 PC 처럼) 돌아간다 — 이 저장소는 팀끼리
 * 나눠 써서 검사 코드에 실물 채널 내용을 못 적는다(`check-business-names.js`).
 */
import { extractMonthSection } from '../src/archive.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

const ONE_SPACE = [
  '## 2026-08', '', '**2026-08-01 09:00 · 김**', '본문 한 칸', '',
  '## 2026-09', '', '**2026-09-01 09:00 · 김**', '본문 다음달', '',
].join('\n');

const TWO_SPACE = ONE_SPACE.replace('## 2026-08', '##  2026-08');   // 공백 두 칸
const TAB = ONE_SPACE.replace('## 2026-08', '##\t2026-08');          // 탭

console.log('[1/3] 공백 한 칸 — 원래도 되던 것 (대조군)');
{
  const r = extractMonthSection(ONE_SPACE, '2026-08');
  if (r.found && r.text.includes('본문 한 칸') && !r.text.includes('본문 다음달')) {
    pass('한 칸짜리 월 헤딩을 찾고, 다음 달 경계에서 자른다');
  } else {
    fail(`한 칸짜리도 못 찾습니다 — ${JSON.stringify(r)}`);
  }
}

console.log('\n[2/3] 공백 두 칸 — 전에는 못 찾던 것 (이 검사의 핵심)');
{
  const r = extractMonthSection(TWO_SPACE, '2026-08');
  if (r.found && r.text.includes('본문 한 칸') && !r.text.includes('본문 다음달')) {
    pass('두 칸짜리 월 헤딩도 찾고, 다음 달(한 칸)에서 정확히 자른다');
  } else {
    fail(`두 칸짜리 월 헤딩을 못 찾습니다 (관대한 정본이 안 지켜짐) — ${JSON.stringify(r)}`);
  }
}

console.log('\n[3/3] 탭 — 관대한 쪽이 공백만이 아니라 \\s 전부를 보나');
{
  const r = extractMonthSection(TAB, '2026-08');
  if (r.found && r.text.includes('본문 한 칸')) {
    pass('탭으로 띈 월 헤딩도 찾는다');
  } else {
    fail(`탭으로 띈 월 헤딩을 못 찾습니다 — ${JSON.stringify(r)}`);
  }
}

// 이빨 확인 — 아예 없는 달을 물으면 진짜로 못 찾아야 한다 (위 통과가 헛것이 아님을 보인다).
{
  const r = extractMonthSection(ONE_SPACE, '2099-01');
  if (r.found) fail('없는 달인데 찾았다고 합니다 — 이 검사가 아무것도 안 재고 있습니다');
  else if (!r.months.includes('2026-08') || !r.months.includes('2026-09')) {
    fail(`있는 달 목록이 안 돌아옵니다 — ${JSON.stringify(r.months)}`);
  } else {
    pass('없는 달은 못 찾고, 있는 달 목록(2026-08 · 2026-09)은 정확히 돌려준다');
  }
}

if (ok) {
  console.log('\n[check-month-heading] OK — archive.js 의 월 헤딩 찾기가 관대한 정본(^##\\s+)을 씁니다.');
} else {
  console.error('\n고칠 곳: src/archive.js 의 MONTH_HEADING · extractMonthSection');
  process.exitCode = 1;
}
