#!/usr/bin/env node
/**
 * `readDocument({section})` 의 절 고르기 — 회의적 검증이 잡은 Blocker.
 *
 *   node scripts/check-doc-section-read.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 (2026-09-11 회의적 검증) ──
 *
 * 절 고르기는 ① 제목 정확 일치 → ② **제목 부분 일치** → ③ 숫자 일치 순이었다.
 * 절 제목에는 숫자가 흔히 들어간다(`A107BL`·`8구역`·`15블럭`). 그래서 ②가 ③을 앞지른다.
 * 실측: `section="7"` 을 주면 7번 절이 아니라 **10번 절**(제목에 `A107`)이 열렸다 —
 * 에러도 경고도 없이. 58개 절짜리 문서에서 3개, 다른 문서에서는 4개 중 1개가 어긋났다.
 *
 * 그런데 `readDocument({section})` 를 부르는 검사는 이 저장소에 **0건**이었다
 * (`check-doc-outline.js` 의 [20]~[23]은 `outlineOf` 만 재고 `section` 인자로
 * `readDocument` 를 부르지 않는다). 그래서 Blocker 가 `npm run check` 를 초록으로
 * 통과했다. 이 검사가 그 빈자리다.
 *
 * ── 무엇을 재나 ──
 *
 * `readDocument` 는 아카이브를 읽는 함수라, `check-doc-outline.js` 와 같은 방식으로
 * (`_outline-probe.js` 의 `readDocumentInTmp`) 임시 문서 하나를 놓고 실제 함수를 부른다.
 * 합성 문서만 쓴다 — 사업장 이름 대신 `항목N`·`A107동`·`8구역상가` 같은 자리표시자.
 *
 * 고친 뒤의 규칙(계획서 Task 4 Step 4 ③): **숫자만 주면 절 번호로만 찾는다. 제목은
 * 안 본다.** 그래서 제목에 그 숫자가 든 다른 절이 있어도 안 걸린다.
 */
import { readDocumentInTmp } from './_outline-probe.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

/* 절 제목에 숫자가 섞인 합성 문서. 실측 3건(7→10, 8→16, 15→19)을 그대로 재현하도록
 * 번호를 골랐다 — 7·8·15 번 절은 제목에 숫자가 없고, 10·16·19 번 절은 제목에 각각
 * '7'·'8'·'15' 를 부분 문자열로 담고 있다. */
const NAMES = {
  1: '사업개요', 2: '자금현황', 3: '공사현황', 4: '분양현황', 5: '인허가현황',
  6: '소송현황', 7: '일곱째현황', 8: '여덟째현황', 9: '아홉째현황', 10: 'A107동현황',
  11: '준공현황', 12: '정산현황', 13: '대출현황', 14: '담보현황', 15: '열다섯째현황',
  16: '8구역상가현황', 17: '미수금현황', 18: '유보금현황', 19: '15블럭현황', 20: '종합의견',
};
const marker = (n) => `${NAMES[n]} 상세내용`;
const sec = (n) => `### ${n}. ${NAMES[n]}\n\n${marker(n)} ${'내용 '.repeat(20)}\n\n`;
const BODY = Object.keys(NAMES).map((n) => sec(Number(n))).join('');
/* 실물 종합보고와 같은 틀 — 회차(`**날짜 · 파일**`) 하나 안에 절이 전부 든다.
 * 이 헤더가 없으면 `preambleOf` 가 `**날짜**` 로 시작하는 회차 경계를 못 찾아 문서
 * 전체를 「머리말」로 읽는다(`readDocument` 가 section 에도 머리말을 붙이므로,
 * 2026-09-10 Task 4 Step 4 ④) — 그러면 이 검사가 재려는 것과 무관하게 글자 수가 부푼다. */
const DOC = [
  '# [업무보고] 시험 문서', '',
  '> **사업장**: 시험 · **종류**: 업무보고',
  '> **열람**: 공개', '',
  '---', '',
  '## 2026-08', '',
  '**2026-08-05 · 260805_종합보고.pdf**', '',
  BODY,
].join('\n');

const open = (section) => readDocumentInTmp(DOC, { project: '시험', document: '산정내역', section });

console.log('[1/8] 번호로 열기 — 제목에 그 숫자가 든 다른 절이 있어도 번호 그대로 연다 (Blocker 재현 자리)');
{
  const r = await open('7');
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.section !== '7. 일곱째현황') bad(`'7' 을 줬는데 '${r.section}' 이 열렸습니다 — 제목에 '7'이 든 10번 절(A107동현황)이 아니라 7번이 열려야 합니다`);
  else if (!r.text.includes(marker(7))) bad('7번 절 본문이 없습니다');
  else if (r.text.includes(marker(10))) bad('10번 절(A107동현황) 본문이 섞여 들어왔습니다');
  else ok(`'7' → ${r.section}`);
}

console.log('[2/8] 같은 모양 — section="8" 은 16번(8구역상가현황)이 아니라 8번을 연다');
{
  const r = await open('8');
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.section !== '8. 여덟째현황') bad(`'8' 을 줬는데 '${r.section}' 이 열렸습니다`);
  else ok(`'8' → ${r.section}`);
}

console.log('[3/8] 같은 모양 — section="15" 는 19번(15블럭현황)이 아니라 15번을 연다');
{
  const r = await open('15');
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.section !== '15. 열다섯째현황') bad(`'15' 를 줬는데 '${r.section}' 이 열렸습니다`);
  else ok(`'15' → ${r.section}`);
}

console.log('[4/8] 제목 일부로 열기 — 숫자가 아닌 제목 조각은 여전히 제목으로 찾는다');
{
  const r = await open('A107동');
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.section !== '10. A107동현황') bad(`'A107동' 을 줬는데 '${r.section}' 이 열렸습니다`);
  else ok(`'A107동' → ${r.section}`);
}

console.log('[5/8] "N. 제목" 통째로 줘도 같은 절이 열린다');
{
  const r = await open('7. 일곱째현황');
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.section !== '7. 일곱째현황') bad(`'7. 일곱째현황' 을 줬는데 '${r.section}' 이 열렸습니다`);
  else ok(`'7. 일곱째현황' → ${r.section}`);
}

console.log('[6/8] 번호 · 제목 일부 · "N. 제목" 통째로 — 세 길이 같은 절을 낸다');
{
  const [byNumber, byTitle, byFull] = await Promise.all([
    open('7'), open('일곱째현황'), open('7. 일곱째현황'),
  ]);
  if (byNumber.error || byTitle.error || byFull.error) {
    bad(`오류: ${byNumber.error || byTitle.error || byFull.error}`);
  } else if (byNumber.text !== byTitle.text || byTitle.text !== byFull.text) {
    bad('세 길이 다른 절을 냈습니다 — 번호·제목 일부·통째 지정이 같은 절로 모여야 합니다');
  } else ok('세 길이 같은 절(7. 일곱째현황)로 모인다');
}

console.log('[7/8] 없는 번호(결번)면 아무것도 안 열고 있는 절 목록을 낸다');
{
  const r = await open('21');
  if (!r.error) bad(`'21' 은 없는 번호인데 '${r.section}' 이 열렸습니다`);
  else if (!/절을 특정하지 못했습니다/.test(r.error)) bad(`에러 문구가 다릅니다: ${r.error}`);
  else if (!r.error.includes('7. 일곱째현황')) bad('있는 절 목록이 에러에 안 실렸습니다');
  else ok(`'21' → 에러: ${r.error.slice(0, 40)}…`);
}

console.log('[8/8] 제목 일부가 여럿에 걸리면 아무것도 안 연다 · 마지막 절은 끝까지 온다');
{
  const ambiguous = await open('현황');
  if (!ambiguous.error) bad(`'현황' 은 여러 절 제목에 걸리는데 '${ambiguous.section}' 이 열렸습니다`);
  else ok(`'현황' → 여럿에 걸려 에러: ${ambiguous.error.slice(0, 30)}…`);

  const last = await open('20');
  // 마지막 절의 end 는 text.length 다 — 우리가 만든 DOC 에서 그 절 머리글부터
  // 끝까지를 그대로 대본다 (redactPrivateMentions 는 이 합성 문서엔 지울 것이 없다).
  const expected = DOC.slice(DOC.indexOf('### 20. 종합의견'));
  if (last.error) bad(`오류: ${last.error}`);
  else if (last.section !== '20. 종합의견') bad(`마지막 절이 '${last.section}' 로 열렸습니다`);
  else if (last.text !== expected) {
    bad(`마지막 절이 문서 끝까지 안 옵니다 — 기대 ${expected.length}자, 받은 ${last.text.length}자`);
  } else ok(`마지막 절은 문서 끝까지 온다 (${last.text.length}자)`);
}

process.exit(failed ? 1 : 0);
