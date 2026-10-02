#!/usr/bin/env node
/**
 * 절 인식 sectionsOf() — 두 패턴을 모으고, 오름차순 관문으로 하위 항목을 걸러내나.
 *
 *   node scripts/check-doc-sections.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 ──
 *
 * 40만 자짜리 종합보고는 sheet 도 month 도 없어 좁힐 축이 없었다. 그래서 read_document
 * 는 앞 6만 자만 주고 「뒤쪽은 도구로 볼 수 없다」로 닫혔다. 그런데 이런 문서는 번호
 * 붙은 절로 나뉘어 있고, 한 절은 6천 자 남짓이다 — 90% 절감.
 *
 * 절 제목은 한 문서 안에서도 두 모양으로 나온다. PDF 변환기가 헤딩을 **글꼴 크기 비율**
 * 로만 판정하고 자유 문단에만 적용하기 때문에, 레이아웃이 표 위주로 바뀌는 구간부터는
 * 같은 절 제목이 표 렌더러를 타고 볼드로 나온다 (kordoc 코드로 확인, 2026-09-10).
 *
 * ── 오름차순 관문이 본체다 ──
 *
 * 다른 종합보고(44만 자)에서는 `###` 가 절이 아니라 **절마다 반복되는 하위 항목**(1~4)
 * 이다. 두 패턴을 그냥 합치면 절이 269개로 잡혀 목차가 쓰레기가 된다. 그래서 각 패턴을
 * 따로 모아 「번호가 엄격히 오름차순인 집합만 쓴다」로 거른다.
 *
 * 이 검사는 **합성 입력만** 쓴다 — 실물 문서 없이도 돌아야 한다
 * (`check-doc-outline.js` 와 같은 규칙).
 */
import { sectionsOf } from '../src/documents.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);
const body = (s) => `\n\n${s} 관련 내용이 여기에 들어간다.\n\n`;

// [1] h3 만: 오름차순이면 절로 잡힌다
{
  const t = `### 1. 첫째 항목${body('첫째')}### 2. 둘째 항목${body('둘째')}### 3. 셋째 항목${body('셋째')}`;
  const s = sectionsOf(t);
  if (!s || s.length !== 3) fail(`[1] h3 3개를 기대했는데 ${s ? s.length : 'null'}`);
  else if (s[0].name !== '첫째 항목') fail(`[1] 제목이 '${s[0].name}'`);
  else pass('[1] h3 오름차순 3개');
}

// [2] 볼드만: 오름차순이면 절로 잡힌다
{
  const t = `**1. 첫째 항목**${body('첫째')}**2. 둘째 항목**${body('둘째')}`;
  const s = sectionsOf(t);
  if (!s || s.length !== 2) fail(`[2] 볼드 2개를 기대했는데 ${s ? s.length : 'null'}`);
  else pass('[2] 볼드 오름차순 2개');
}

// [3] 섞임: h3 가 앞 번호, 볼드가 뒤 번호 — 합집합도 오름차순이면 합친다 (40만 자 문서의 모양)
{
  const t = `### 1. 가${body('가')}### 2. 나${body('나')}**3. 다**${body('다')}**4. 라**${body('라')}`;
  const s = sectionsOf(t);
  if (!s || s.length !== 4) fail(`[3] 합집합 4개를 기대했는데 ${s ? s.length : 'null'}`);
  else if (s.map((x) => x.n).join(',') !== '1,2,3,4') fail(`[3] 번호열 ${s.map((x) => x.n).join(',')}`);
  else pass('[3] 두 패턴 합집합 4개');
}

// [4] **오름차순 관문** — h3 가 절마다 반복되는 하위 항목이면 버리고 볼드만 쓴다 (44만 자 문서의 모양)
{
  const t = [
    '**1. 첫째 사업**', body('첫째'),
    '### 1. 개요', body('개요'), '### 2. 현황', body('현황'),
    '**2. 둘째 사업**', body('둘째'),
    '### 1. 개요', body('개요'), '### 2. 현황', body('현황'),
    '**3. 셋째 사업**', body('셋째'),
  ].join('\n');
  const s = sectionsOf(t);
  if (!s) fail('[4] null — 볼드 3개가 오름차순이라 절이 잡혀야 합니다');
  else if (s.length !== 3) fail(`[4] 볼드 3개만 남아야 하는데 ${s.length}개 (하위 항목이 섞였습니다)`);
  else if (s.map((x) => x.n).join(',') !== '1,2,3') fail(`[4] 번호열 ${s.map((x) => x.n).join(',')}`);
  else pass('[4] 오름차순 관문이 반복되는 하위 항목을 걸러냅니다');
}

// [5] 둘 다 비오름차순이면 null — 현행 동작을 그대로 둔다
{
  const t = `**1. 가**${body('가')}**2. 나**${body('나')}**1. 다**${body('다')}**2. 라**${body('라')}`;
  const s = sectionsOf(t);
  if (s !== null) fail(`[5] null 을 기대했는데 ${s.length}개 — 번호가 되돌아가는 문서는 절로 나누면 안 됩니다`);
  else pass('[5] 비오름차순은 null');
}

// [6] 절이 하나뿐이면 null — 나눌 것이 없다
{
  const s = sectionsOf(`### 1. 하나뿐${body('하나')}`);
  if (s !== null) fail(`[6] null 을 기대했는데 ${s.length}개`);
  else pass('[6] 절 1개는 null');
}

/* [7] 볼드가 **줄 전체**일 때만 절이다.
 *
 * 세 갈래를 함께 본다. 표 행만 보면 **줄머리 `^\*\*` 앵커**가 혼자 막고 있어서,
 * 문자클래스의 `|` 를 지워도 검사가 초록으로 남는다 (회의적 검증이 돌연변이로 확인,
 * 2026-09-10). 실제로 무엇이 막고 있는지를 갈래마다 따로 재야 한다. */
{
  const cases = [
    ['표 행(줄이 `|` 로 시작)', '| **1. 표 안** | 값 |\n| **2. 표 안** | 값 |\n\n본문'],
    ['제목 안에 `|` (표 조각이 볼드 한 줄로 뭉친 것)', `**1. 가 | 나**${body('가')}**2. 다 | 라**${body('나')}`],
    ['볼드 뒤에 딴 글자가 붙은 줄', `**1. 가** 그리고 더${body('가')}**2. 나** 그리고 더${body('나')}`],
  ];
  let bad = 0;
  for (const [what, t] of cases) {
    const s = sectionsOf(t);
    if (s !== null) { fail(`[7] ${what} — null 을 기대했는데 ${s.length}개`); bad += 1; }
  }
  if (!bad) pass(`[7] 줄 전체가 볼드일 때만 절 (${cases.length}갈래)`);
}

// [8] 날짜형 볼드는 절이 아니다
{
  const t = `**26. 06. 09.**${body('가')}**27. 07. 10.**${body('나')}`;
  const s = sectionsOf(t);
  if (s !== null) fail(`[8] null 을 기대했는데 ${s.length}개 — 날짜 줄은 절 경계가 아닙니다`);
  else pass('[8] 날짜형 볼드 배제');
}

// [9] start·end 가 맞물리고 텍스트를 빠짐없이 덮나
{
  const t = `머리\n\n### 1. 가${body('가')}### 2. 나${body('나')}`;
  const s = sectionsOf(t);
  if (!s) fail('[9] null');
  else {
    if (t.slice(s[0].start, s[0].end).indexOf('### 1. 가') !== 0) fail('[9] 첫 절이 경계 줄에서 시작하지 않습니다');
    if (s[0].end !== s[1].start) fail(`[9] 절이 맞물리지 않습니다 ${s[0].end} !== ${s[1].start}`);
    if (s[s.length - 1].end !== t.length) fail(`[9] 마지막 절이 끝까지 가지 않습니다 ${s[s.length - 1].end} !== ${t.length}`);
    if (!ok) { /* 위에서 이미 보고됨 */ } else pass('[9] start·end 가 맞물립니다');
  }
}

// [10] CRLF 에서도 같게 동작하나 — 실물 md 에 섞여 있다
{
  const lf = `### 1. 가${body('가')}### 2. 나${body('나')}`;
  const a = sectionsOf(lf);
  const b = sectionsOf(lf.replace(/\n/g, '\r\n'));
  if (!a || !b || a.length !== b.length) fail(`[10] LF ${a ? a.length : 'null'} vs CRLF ${b ? b.length : 'null'}`);
  else if (a.map((x) => x.name).join('|') !== b.map((x) => x.name).join('|')) fail('[10] CRLF 에서 제목이 달라집니다');
  else pass('[10] CRLF 무관');
}

/* [11] **엄격히** 오름차순인가 — 같은 번호가 두 번 나오면 안 나눈다.
 *
 * 구현은 `x.n > a[i-1].n` 인데, 이것을 `>=` 로 바꿔도 [1]~[10] 이 전부 초록이었다
 * (회의적 검증 돌연변이, 2026-09-10). 주석은 「엄격히」라고 적고 검사는 안 지키던 자리다. */
{
  for (const [what, t] of [
    ['h3', `### 1. 가${body('가')}### 2. 나${body('나')}### 2. 다${body('다')}### 3. 라${body('라')}`],
    ['볼드', `**1. 가**${body('가')}**2. 나**${body('나')}**2. 다**${body('다')}**3. 라**${body('라')}`],
  ]) {
    const s = sectionsOf(t);
    if (s !== null) fail(`[11] ${what} 1,2,2,3 — null 을 기대했는데 ${s.length}개 (같은 번호가 두 번이면 절이 아니다)`);
  }
  if (ok) pass('[11] 번호가 겹치면 null');
}

/* [12] **두 패턴이 겹쳐 합집합이 어긋나면 안 나눈다.**
 *
 * 여기가 이 함수에서 가장 틀리기 쉬운 자리다. 처음에는 「개수가 많은 쪽」을 골랐는데,
 * 하위 항목에 문서 전체 연번을 매긴 보고서에서는 **안쪽이 더 많아서** 하위 항목이
 * 절로 뽑혔다 — 오름차순 관문이 막으려던 실패가 그 자리에서 되살아난 것이다
 * (회의적 검증이 만든 반례, 2026-09-10). 실물 461건에서 이 갈래는 0건이라 안 나눠도
 * 잃는 것이 없다.
 *
 * 아래 입력은 **두 패턴이 각각은 오름차순**(볼드 1,2 · h3 1,2,3,4)인데 문서 순서로
 * 합치면 1,1,2,2,3,4 라 어긋난다. 「개수 많은 쪽」이었다면 h3 넷(하위 항목)을 냈다. */
{
  const t = [
    '**1. 첫째 사업**', body('첫째'),
    '### 1. 개요', body('개요'), '### 2. 현황', body('현황'),
    '**2. 둘째 사업**', body('둘째'),
    '### 3. 개요', body('개요'), '### 4. 현황', body('현황'),
  ].join('\n');
  const s = sectionsOf(t);
  if (s !== null) {
    fail(`[12] null 을 기대했는데 ${s.length}개 — ${s.map((x) => `${x.n}. ${x.name}`).join(' / ')}`);
  } else pass('[12] 두 패턴이 겹쳐 합집합이 어긋나면 null');
}

process.exit(ok ? 0 : 1);
