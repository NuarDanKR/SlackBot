#!/usr/bin/env node
/**
 * 이름 풀기가 회차 헤더의 **원본 첨부 파일명**도 후보로 보나.
 *
 *   node scripts/check-doc-resolve-filename.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 ──
 *
 * 봇은 검색 발췌 첫 줄(회차 헤더)에 보이는 원본 파일명을 read_document 에 그대로
 * 넣는다. 그런데 풀기 후보는 슬러그·H1 제목뿐이라 그 이름만 안 풀렸고, 성공한 재생
 * 4회가 4회 모두 첫 호출을 에러로 버렸다 (2026-09-11 실측). 후보 목록 회복은 우연이다
 * — 문서 15건 초과 사업장 10곳에서는 후보 15개 상한에 정답이 안 실린다(17.4%).
 *
 * NFC 를 함께 재는 이유: 맥에서 온 파일명은 조합형(NFD)이라 눈으로 같아도 문자열
 * 비교가 어긋난다 — archivedAttachmentNames 가 2026-09-03 에 실제로 겪은 모양이다.
 */
import { resolveDocument } from '../src/documents.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

const doc = (slug, title, files = []) => ({
  slug,
  title,
  entries: files.map((f) => ({ text: `**2026-07-31 · ${f}**\n본문` })),
});

const DOCS = [
  doc('20260731-보고-60개-현안-및-진행', '분야별 현안 및 진행 (60개)', ['보고(60개) 현안 및 진행_송부용_260731.pdf']),
  doc('20260805-일일-운영보고', '[보고서] 일일 운영보고', ['일일 운영보고_v2.xlsx']),
  // 점이 여럿인 파일명 — 확장자만 떼고 판(`v1.2`)은 남아야 한다. 슬랙 실물에 흔한 모양이다.
  doc('20260810-사업계획', '[계획] 사업계획', ['사업계획_v1.2_최종.pdf']),
];

// [1] 파일명(확장자 떼고) 입력이 풀린다 — 실제 사고의 입력 모양(괄호 포함)
{
  const r = resolveDocument('현장가', '보고(60개) 현안 및 진행_송부용_260731', DOCS);
  if (!r.ok || r.doc.slug !== '20260731-보고-60개-현안-및-진행') fail(`[1] ${JSON.stringify(r.ok ? r.doc.slug : r.candidates)}`);
  else pass('[1] 파일명 입력이 풀린다');
}

// [2] 파일명의 앞부분만 넣어도 부분 일치로 풀린다 (봇이 꼬리를 떼고 넣는 실측 모양)
{
  const r = resolveDocument('현장가', '보고(60개) 현안 및 진행', DOCS);
  if (!r.ok) fail(`[2] 후보: ${r.candidates}`);
  else pass('[2] 파일명 앞부분 부분 일치');
}

// [3] NFC — 입력이 조합형(NFD)이어도 풀린다
{
  const r = resolveDocument('현장가', '일일 운영보고_v2'.normalize('NFD'), DOCS);
  if (!r.ok || r.doc.slug !== '20260805-일일-운영보고') fail(`[3] ${JSON.stringify(r.ok ? r.doc.slug : r.candidates)}`);
  else pass('[3] NFD 입력도 풀린다');
}

// [4] 기존 동작 무변화 — 슬러그·H1 제목 입력
{
  const a = resolveDocument('현장가', '20260805-일일-운영보고', DOCS);
  const b = resolveDocument('현장가', '[보고서] 일일 운영보고', DOCS);
  if (!a.ok || !b.ok) fail('[4] 슬러그/제목 입력이 깨졌다');
  else pass('[4] 슬러그·제목 기존 동작 유지');
}

// [5] 같은 파일명이 두 문서에 있으면 하나로 단정하지 않는다
{
  const dup = [...DOCS, doc('20260901-재송부', '재송부본', ['보고(60개) 현안 및 진행_송부용_260731.pdf'])];
  const r = resolveDocument('현장가', '보고(60개) 현안 및 진행_송부용_260731', dup);
  if (r.ok) fail(`[5] 두 문서에 걸리는데 ${r.doc.slug} 로 단정했다`);
  else pass('[5] 중복 파일명은 후보 목록으로');
}

// [6] 헤더가 계약을 안 지키는 회차(entryFileName undefined)는 조용히 건너뛴다
{
  const broken = [doc('x', 'X제목', []), { slug: 'y', title: 'Y제목', entries: [{ text: '헤더 없는 본문' }] }];
  const r = resolveDocument('현장가', 'Y제목', broken);
  if (!r.ok || r.doc.slug !== 'y') fail(`[6] ${JSON.stringify(r.ok ? r.doc.slug : r.candidates)}`);
  else pass('[6] 파일명 없는 회차 무해');
}

// [7] 점이 여럿인 파일명 — **확장자만** 떼고 판 번호는 남긴다 (2026-09-11 이월 Minor).
//     확장자 정규식이 `$` 앵커를 잃고 탐욕적으로 바뀌면(`\..*` 류) `사업계획` 만 남아
//     **다른 문서에 걸리거나 안 풀린다.** 정적으로만 안전하던 자리에 실측을 붙인다.
{
  const r = resolveDocument('현장가', '사업계획_v1.2_최종', DOCS);
  if (!r.ok || r.doc.slug !== '20260810-사업계획') fail(`[7] ${JSON.stringify(r.ok ? r.doc.slug : r.candidates)}`);
  else pass('[7] 다중 점 파일명도 확장자만 떨어진다');
}

process.exit(ok ? 0 : 1);
