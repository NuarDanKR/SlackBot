#!/usr/bin/env node
/**
 * `index.md` 감사 함수가 **틀린 것을 실제로 잡는지** 본다.
 *
 * 이 검사가 지키는 것은 index.md 가 아니라 **감사 함수 자신**이다. 감사가 조용히
 * 통과하면 index.md 가 낡아도 아무도 모르는 원래 상태로 돌아가는데, 그때는
 * `npm run check` 가 초록이라 「대보고 있다」로 읽힌다.
 *
 * 픽스처는 파일을 안 읽는다 — 실제 아카이브에 기대면 아카이브가 자랄 때마다
 * 전제가 깨져 「통과」와 「못 잼」이 섞인다.
 */
import { auditDocIndex, countDocs } from '../src/doc-index-audit.js';

let failed = false;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { console.log(`  ✗ ${m}`); failed = true; };

/** 회차 n건짜리 가짜 문서 하나 */
const doc = (project, rounds, sheet) => ({
  project,
  entries: Array.from({ length: rounds }, (_, i) => ({ date: `2026-08-0${i + 1}` })),
  meta: sheet ? { 시트: sheet } : {},
});

// 가나 3건(회차 1+2+1=4, 시리즈 1개/2건) · 다라 2건(엑셀 1개/3건, 단발 1건)
const DOCS = [
  doc('가나', 1), doc('가나', 2), doc('가나', 1),
  doc('다라', 3, '갑 · 을 · 병'), doc('다라', 1),
];
const PROJECTS = ['가나', '다라'];

// 위 DOCS 와 정확히 맞는 index.md 본문.
const GOOD = [
  '## 사업장별 문서',
  '',
  '> 문서 5건 · 회차 8건 · 사업장 2개 (시리즈 1개가 회차 2건 · 엑셀 1건이 회차 3건 · 나머지 3건이 단발).',
  '',
  '### 가나 (문서 3 · 회차 4)',
  '',
  '- [보고서] 하나 — 2026-08-01',
  '- [보고서] 둘 — 2026-08-02',
  '- [보고서] 셋 — 2026-08-03',
  '',
  '### 다라 (문서 2 · 회차 4)',
  '',
  '- [보고서] 넷 — 2026-08-04',
  '- [보고서] 다섯 — 2026-08-05',
  '',
  '## 변환하지 못한 것',
  '',
  '- 이 절의 `- ` 줄은 세면 안 된다',
  '- 세면 다라가 4줄로 잡힌다',
].join('\n');

// [1/9] 세는 법 자체
const c = countDocs(DOCS);
const want = { docs: 5, rounds: 8, series: 1, seriesRounds: 2, excel: 1, excelRounds: 3, single: 3 };
if (JSON.stringify(c) === JSON.stringify(want)) ok('countDocs 가 시리즈·엑셀·단발을 정의대로 가른다');
else bad(`countDocs ${JSON.stringify(c)} — ${JSON.stringify(want)} 여야 합니다`);

// [2/9] 맞는 index 는 조용하다
const clean = auditDocIndex({ indexText: GOOD, docs: DOCS, projects: PROJECTS });
if (clean.length === 0) ok('맞는 index.md 에는 아무 말도 안 한다');
else bad(`맞는 index.md 에 ${clean.length}건을 냈습니다: ${clean.join(' / ')}`);

// [3/9] 앞머리 숫자 하나를 어긋내면 잡는다 — **한 칸만** 바꾼다
const t3 = auditDocIndex({ indexText: GOOD.replace('문서 5건', '문서 6건'), docs: DOCS, projects: PROJECTS });
if (t3.length === 1 && t3[0].includes('문서')) ok('앞머리 문서 수가 틀리면 잡는다');
else bad(`앞머리 문서 수 변이 → ${t3.length}건 (${t3.join(' / ')}) — 1건이어야 합니다`);

// [4/9] 앞머리의 다른 칸도 따로 잡힌다 (한 칸만 봐서 통과하는 검사를 막는다)
const t4 = auditDocIndex({ indexText: GOOD.replace('엑셀 1건이 회차 3건', '엑셀 2건이 회차 3건'), docs: DOCS, projects: PROJECTS });
if (t4.length === 1 && t4[0].includes('엑셀')) ok('앞머리 엑셀 수가 틀리면 잡는다');
else bad(`앞머리 엑셀 수 변이 → ${t4.length}건 (${t4.join(' / ')}) — 1건이어야 합니다`);

// [5/9] 사업장 헤딩 숫자
const t5 = auditDocIndex({ indexText: GOOD.replace('### 가나 (문서 3 · 회차 4)', '### 가나 (문서 3 · 회차 9)'), docs: DOCS, projects: PROJECTS });
if (t5.length === 1 && t5[0].includes('가나')) ok('사업장 헤딩의 회차 수가 틀리면 잡는다');
else bad(`헤딩 변이 → ${t5.length}건 (${t5.join(' / ')}) — 1건이어야 합니다`);

// [6/9] **목록 줄이 모자란 것** — 이번에 찾으려는 바로 그 결함이다
const t6 = auditDocIndex({ indexText: GOOD.replace('- [보고서] 셋 — 2026-08-03\n', ''), docs: DOCS, projects: PROJECTS });
if (t6.length === 1 && t6[0].includes('목록')) ok('사업장 절의 목록 줄이 모자라면 잡는다');
else bad(`목록 줄 변이 → ${t6.length}건 (${t6.join(' / ')}) — 1건이어야 합니다`);

// [7/9] 사업장 절이 통째로 없는 경우
const t7 = auditDocIndex({
  indexText: GOOD, docs: [...DOCS, doc('마바', 1)], projects: [...PROJECTS, '마바'],
});
if (t7.some((m) => m.includes('마바'))) ok('문서가 있는데 절이 없는 사업장을 잡는다');
else bad(`절 없는 사업장을 못 잡았습니다: ${t7.join(' / ')}`);

// [8/9] **줄 수는 같은데 문서 줄 하나가 메모 줄로 바뀐 경우** — 옛 감사는 `- ` 줄의
// 개수만 세서 이걸 놓쳤다(2026-08-27 리뷰에서 실물 index.md 로 재현). 문서 줄 하나를
// 모양이 다른 `- ` 줄로 바꿔치기해도 총 줄 **수**는 그대로다.
const MEMO_LINE = '- 이 사업장은 8월에 재구조화 논의 중이다';
const t8 = auditDocIndex({
  indexText: GOOD.replace('- [보고서] 셋 — 2026-08-03', MEMO_LINE),
  docs: DOCS,
  projects: PROJECTS,
});
if (t8.some((m) => m.includes('가나') && m.includes('목록'))) {
  ok('문서 줄 하나가 메모 줄로 바뀌어도 (줄 수는 그대로라도) 잡는다');
} else {
  bad(`메모 줄 치환 변이 → ${t8.length}건 (${t8.join(' / ')}) — 「가나」의 목록 결함이 있어야 합니다`);
}

// [9/9] 그 메모 줄 자체가 「문서 줄이 아니다」로 따로 보고되나 — 조용히 버리면
// 이번에 고치는 병이 형태만 바뀌어 남는다.
if (t8.some((m) => m.includes('가나') && m.includes('문서 줄 모양이 아닌'))) {
  ok('문서 줄 모양이 아닌 - 줄을 「세지 않았다」로 따로 알린다');
} else {
  bad(`메모 줄이 「문서 줄이 아니다」로 안 잡혔습니다: ${t8.join(' / ')}`);
}

process.exit(failed ? 1 : 0);
