#!/usr/bin/env node
/**
 * `documents.js` 의 월 헤딩 정규식 두 곳(`outlineOf` 의 `heads` · `readDocument` 의
 * `month` 자르기)이 **관대한 정본**(`^##\s+`, 공백 몇 칸이든)으로 도나.
 *
 *   node scripts/check-doc-month-heading.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 있나 ──
 *
 * `documents.js:83,1068`(WHK 지시 당시 줄 번호)이 `^## `(공백 한 칸 고정)이었고, 나머지는
 * `^##\s+` 였다. `##  2026-08`(두 칸)이면 다른 관문은 통과시키는데 이 두 자리만 그 달을
 * 못 찾는다 — `outlineOf` 는 월이 실제로 2개 이상인데 목차 갈래를 못 타 1개로 보이고,
 * `readDocument` 는 `month` 를 지정한 호출에서 "섹션이 없습니다"로 조용히 실패한다.
 * 정본은 관대한 쪽으로 정해졌다(WHK 결정 2026-09-03) — `archive.js` 의
 * `extractMonthSection`(`check-month-heading.js` 가 그쪽을 잰다)과 같은 규칙이다.
 *
 * `check-month-heading.js` 는 `archive.js` 만 잰다(2조 파일) — 이 검사가 `documents.js`
 * 쪽(이 조 파일)을 잰다. 실제 아카이브가 없어도 fixture 로 돈다(`check-doc-outline.js` 와
 * 같은 규칙) — `outlineOf` 는 순수 함수라 바로 부르고, `readDocument` 는 `_outline-probe.js`
 * 로 임시 문서 아카이브를 만들어 별도 프로세스에서 돈다.
 */
import { outlineOf } from '../src/documents.js';
import { readDocumentInTmp } from './_outline-probe.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

const twoSpaceDoc = [
  '## 2026-07', '', '**2026-07-05 · 보고.hwp**', '', '칠월본문', '',
  '##  2026-08', '', '**2026-08-05 · 보고.hwp**', '', '팔월본문', '',   // 공백 두 칸
].join('\n');

console.log('[1/3] outlineOf — 공백 두 칸 헤딩도 월로 센다 (대조군: 한 칸은 원래도 됨)');
{
  const oneSpace = twoSpaceDoc.replace('##  2026-08', '## 2026-08');
  const oOne = outlineOf(oneSpace);
  if (!oOne || oOne.kind !== 'month' || oOne.pieces.length !== 2) {
    bad(`대조군(한 칸)부터 틀렸습니다 — fixture 를 다시 보세요: ${JSON.stringify(oOne)}`);
  } else {
    ok('한 칸짜리는 원래도 월 2개로 잡힌다 (대조군)');
  }

  const oTwo = outlineOf(twoSpaceDoc);
  if (!oTwo) bad('두 칸 헤딩이 섞이니 outlineOf 가 null 을 냅니다 — 월이 1개로만 보입니다');
  else if (oTwo.kind !== 'month') bad(`kind 가 '${oTwo.kind}' 입니다 — 'month' 여야 합니다`);
  else if (oTwo.pieces.length !== 2) bad(`월이 ${oTwo.pieces.length}개로 잡혔습니다 — 2개(2026-07·2026-08)여야 합니다`);
  else if (oTwo.pieces.map((p) => p.name).join(',') !== '2026-07,2026-08') {
    bad(`월 이름이 ${oTwo.pieces.map((p) => p.name).join(',')} 입니다`);
  } else ok(`두 칸 헤딩도 월 2개로 잡힌다: ${oTwo.pieces.map((p) => p.name).join(' · ')}`);
}

console.log('\n[2/3] readDocument({ month }) — 공백 두 칸 헤딩의 달도 찾는다');
{
  const r = await readDocumentInTmp(twoSpaceDoc, { project: '시험', document: '산정내역', month: '2026-08' });
  if (r.error) bad(`month:"2026-08"(두 칸 헤딩) → 오류: ${r.error}`);
  else if (!r.text.includes('팔월본문')) bad(`8월 섹션을 못 찾았습니다: ${JSON.stringify(r.text).slice(0, 200)}`);
  else if (r.text.includes('칠월본문')) bad('7월 내용까지 섞여 왔습니다 — 자르기 경계가 틀렸습니다');
  else ok('두 칸 헤딩이어도 그 달 섹션을 정확히 찾고 다음 달에서 자른다');
}

console.log('\n[3/3] readDocument({ month }) — 탭으로 띈 헤딩도 찾는다 (관대한 쪽이 \\s 전부를 보나)');
{
  const tabDoc = twoSpaceDoc.replace('##  2026-08', '##\t2026-08');
  const r = await readDocumentInTmp(tabDoc, { project: '시험', document: '산정내역', month: '2026-08' });
  if (r.error) bad(`탭 헤딩 → 오류: ${r.error}`);
  else if (!r.text.includes('팔월본문')) bad(`탭 헤딩의 달을 못 찾았습니다: ${JSON.stringify(r.text).slice(0, 200)}`);
  else ok('탭으로 띈 헤딩도 찾는다');
}

if (failed) {
  console.error(`\n[check-doc-month-heading] FAIL — ${failed}건. 고칠 곳: src/documents/read.js 의 outlineOf(heads) · readDocument(month 자르기)`);
} else {
  console.log('\n[check-doc-month-heading] OK — documents/read.js 의 월 헤딩 찾기 두 곳이 관대한 정본(^##\\s+)을 씁니다.');
}
process.exit(failed ? 1 : 0);
