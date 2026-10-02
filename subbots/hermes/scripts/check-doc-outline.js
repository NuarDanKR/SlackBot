#!/usr/bin/env node
/**
 * 큰 문서를 목차로 돌려주는 갈래 점검 — 임시 fixture 로 돈다
 * (실제 아카이브에 그 문서가 없어도 돌아야 한다. check-excel-sheets.js 와 같은 규칙).
 *
 * 왜 있나: read_document 는 세 갈래다.
 *   1) 작은 문서        → 전문
 *   2) 크고 고를 수 있음 → **머리말 전문** + 목차 (sheet 2개 이상 또는 월 2개 이상)
 *   3) 크지만 고를 수 없음 → 앞부분 + 검색 안내
 * 갈래가 새면 에러가 아니라 **오답**으로 드러난다 — 봇이 목차를 「자료 없음」으로
 * 읽거나, 막다른 안내를 받고 뒤쪽을 영영 안 본다.
 *
 * 갈래 2 에 머리말이 붙은 것은 2026-09-03 부터다 (`src/documents/read.js` 의 `outlineWithPreamble`).
 * 그전에는 목차만 왔고, 머리말은 첫 회차 헤더보다 위라 조각 어디에도 안 들어가서
 * **큰 문서에서는 여는 길이 아예 없었다.** [16]~[19] 가 그 자리를 지킨다.
 *
 * 실행: node scripts/check-doc-outline.js
 */
import fs from 'node:fs';
import { outlineOf, sheetsOf, SHEET_RE } from '../src/documents.js';
import { splitMessages } from '../src/archive.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

const SRC = '260805_시험_산정내역.xlsx';
const sheetBlock = (i, n, name, body) =>
  `**2026-08-05 · ${SRC} — 시트 ${i}/${n}: ${name}**\n\n${body}`;

/**
 * 돌려준 글에서 **목차 본체만** 떼어 낸다. 앞에 머리말이 붙어 있으면 빼고 잰다.
 *
 * **길이를 통째로 재면 안 되는 이유가 이것이다** — 2026-09-03 부터 목차 앞에 머리말
 * 전문이 붙어서, 「전문이 왔나 목차가 왔나」를 총 길이로 가르면 머리말이 긴 문서에서
 * 거짓으로 빨개진다. 재야 하는 것은 **목차 쪽이 본문으로 부풀지 않았나** 이다.
 *
 * 목차는 늘 이 문장으로 시작하고 글의 끝까지 간다 (`documents.js` 의 renderOutline ·
 * outlineWithPreamble). 없으면 null 을 내서 부르는 쪽이 실패로 적게 한다.
 */
const outlineBody = (text) => {
  const lines = String(text).split('\n');
  const i = lines.findIndex((l) => l.includes('커서 목차만 실었습니다'));
  return i === -1 ? null : lines.slice(i).join('\n');
};

/* 머리말이 있는 판과 없는 판을 **같은 틀로** 만든다. 제목·메타까지 같아야 갈린 것이
 * 머리말 하나임이 확실해진다 (제목이 다르면 목차 첫 줄의 문서 이름이 함께 달라진다). */
const PRE_NUMBER = '919실';
const PREAMBLE = ['## 회차 요약', '',
  '| 작성일 | 수금 누계 |', '|---|---|', `| 2026-08-02 | **${PRE_NUMBER}** |`].join('\n');
const docWrap = (pre, body) => [
  '# [업무보고] 시험 문서', '',
  '> **사업장**: 시험 · **종류**: 업무보고',
  '> **열람**: 공개', '',
  '---', '',
  ...(pre ? [pre, '', '---', ''] : []),
  body,
].join('\n');

/** 월 2개짜리 본문. 한 달치 크기를 인자로 받아 상한을 넘기거나 안 넘기게 만든다. */
const twoMonths = (per) => ['## 2026-07', '', '**2026-07-05 · 보고.hwp**', '', '가'.repeat(per), '',
  '## 2026-08', '', '**2026-08-05 · 보고.hwp**', '', '나'.repeat(per), ''].join('\n');

const SHEETS_3 = [
  '# [산정내역] 시험 엑셀', '', '> **사업장**: 시험 · **종류**: 산정내역',
  '> **열람**: 공개', '', '---', '', '## 2026-08', '',
  sheetBlock(1, 3, '요약', '가나다'),
  '', sheetBlock(2, 3, '민감도', '라마바'),
  '', sheetBlock(3, 3, '상환스케줄', '사아자'), '',
].join('\n');

console.log('[1] 시트 2개 이상이면 목차를 낼 수 있다');
{
  const o = outlineOf(SHEETS_3);
  if (!o) bad('시트 3개짜리에서 outlineOf 가 null 을 냈습니다');
  else if (o.kind !== 'sheet') bad(`kind 가 '${o.kind}' 입니다 — 'sheet' 여야 합니다`);
  else if (o.pieces.length !== 3) bad(`조각이 ${o.pieces.length}개입니다 — 3개여야 합니다`);
  else ok(`시트 ${o.pieces.length}개: ${o.pieces.map((p) => p.name).join(' · ')}`);
}

console.log('[2] outlineOf 의 시트 이름이 sheetsOf 와 정확히 같다');
{
  const o = outlineOf(SHEETS_3);
  const a = JSON.stringify(o ? o.pieces.map((p) => p.name) : null);
  const b = JSON.stringify(sheetsOf(SHEETS_3));
  if (a === b) ok('두 판정이 같은 이름을 낸다');
  else bad(`갈렸습니다 — outlineOf ${a} vs sheetsOf ${b}`);
}

console.log('[3] 시트가 1개뿐이면 목차를 못 낸다 (고를 것이 하나라 왕복만 는다)');
{
  const one = ['## 2026-08', '', sheetBlock(1, 1, '요약', '가나다'), ''].join('\n');
  if (outlineOf(one) === null) ok('시트 1개 → null');
  else bad('시트 1개인데 목차를 냈습니다');
}

console.log('[4] 월 2개 이상이면 월로 목차를 낸다');
{
  const m = ['## 2026-07', '', '**2026-07-05 · 보고.hwp**', '', '가나다', '',
             '## 2026-08', '', '**2026-08-05 · 보고.hwp**', '', '라마바', ''].join('\n');
  const o = outlineOf(m);
  if (!o) bad('월 2개짜리에서 null 이 났습니다');
  else if (o.kind !== 'month') bad(`kind 가 '${o.kind}' 입니다 — 'month' 여야 합니다`);
  else if (o.pieces.map((p) => p.name).join(',') !== '2026-07,2026-08') {
    bad(`월 이름이 ${o.pieces.map((p) => p.name).join(',')} 입니다`);
  } else ok(`월 ${o.pieces.length}개: ${o.pieces.map((p) => p.name).join(' · ')}`);
}

console.log('[5] 회차 블록으로만 나뉜 문서는 목차를 못 낸다 (지목할 인자가 없다)');
{
  const blocks = ['## 2026-08', '',
    '**2026-08-05 · 제안서.pdf**', '', '가나다', '',
    '**2026-08-06 · 제안서.pdf**', '', '라마바', '',
    '**2026-08-07 · 제안서.pdf**', '', '사아자', ''].join('\n');
  if (splitMessages(blocks).length !== 3) bad('fixture 가 3블록이 아닙니다 — 검사가 헛돕니다');
  else if (outlineOf(blocks) === null) ok('블록 3개·시트 0·월 1 → null');
  else bad('블록만 나뉜 문서에서 목차를 냈습니다 — 봇이 고를 인자가 없습니다');
}

console.log('[6] 크고 시트로 나뉜 문서는 전문 대신 목차가 온다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  const filler = '가'.repeat(30000);
  const big = ['## 2026-08', '',
    sheetBlock(1, 3, '요약', '작은 시트'), '',
    sheetBlock(2, 3, '민감도', filler), '',
    sheetBlock(3, 3, '상환스케줄', filler), ''].join('\n');
  const r = await readDocumentInTmp(big, { project: '시험', document: '산정내역' });
  /* **본문으로 부풀었나는 목차 쪽만 재서 가른다** (2026-09-03). 예전에는 `r.text.length`
   * 를 통째로 쟀는데, 그때는 목차 앞에 아무것도 안 붙었기 때문에 그것이 곧 목차 길이였다.
   * 지금은 머리말 전문이 앞에 붙으므로 총 길이로 가르면 **머리말이 긴 문서에서 거짓으로
   * 빨개진다** — 이 fixture 에는 머리말이 없어서 안 걸리지만, 안 걸린다고 맞는 잣대는
   * 아니다. 머리말 쪽은 [16]~[19] 가 따로 잰다. */
  const body = r.error ? null : outlineBody(r.text);
  if (r.error) bad(`목차 갈래에서 오류: ${r.error}`);
  else if (!body) bad('「목차만 실었다」 표시가 없습니다 — 봇이 자료 없음으로 읽습니다');
  else if (body.length > 2000) bad(`목차가 아니라 본문이 왔습니다 (목차 ${body.length}자)`);
  else if (r.outline !== 'sheet') bad(`outline 이 '${r.outline}' 입니다 — 'sheet' 여야 합니다`);
  else if (!['요약', '민감도', '상환스케줄'].every((n) => r.text.includes(n))) bad('시트 이름이 빠졌습니다');
  else if (!/민감도 \(3[0-9],[0-9]{3}자\)/.test(r.text)) bad('조각 크기가 안 적혔습니다 — 봇이 큰 시트를 미리 못 피합니다');
  else if ((r.text.match(/^→ /gm) || []).length !== 2) bad('다음 행동 두 줄이 없습니다');
  // 목차의 둘째 줄도 search 로 보낸다 — hint 와 같은 이유로 where 를 함께 적어야 한다.
  // scanDocuments 는 project 가 있을 때만 document 로 좁히고, document 만 주면
  // 조용히 전 문서를 훑는다 (2026-08-28 실측: 12건이 딴 문서에서 나왔다).
  else if (!r.text.includes('where')) bad('목차가 search 로 보내면서 where 를 안 알려줍니다 — document 만 주면 전 문서를 훑습니다');
  else ok(`목차 ${body.length}자 (본문이면 6만 자였다)`);
}

console.log('[7] 작은 문서는 전문 그대로 온다 (갈래가 새지 않는다)');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  const r = await readDocumentInTmp(SHEETS_3, { project: '시험', document: '산정내역' });
  if (r.error) bad(`작은 문서에서 오류: ${r.error}`);
  else if (r.outline) bad('작은 문서가 목차로 빠졌습니다 — 왕복만 늘고 얻는 것이 없습니다');
  else if (!r.text.includes('가나다')) bad('본문이 안 왔습니다');
  else ok('작은 문서 → 전문');
}

console.log('[8] readDocument 가 목차를 만들기 전에 가리기를 끝낸다');
{
  const src = fs.readFileSync(new URL('../src/documents/read.js', import.meta.url), 'utf8');
  const body = (src.match(/  function readDocument\([\s\S]*?\n  }/) || [''])[0];
  const redact = body.indexOf('redactPrivateMentions');
  const outline = body.indexOf('outlineOf(');
  if (redact === -1) bad('readDocument 안에 redactPrivateMentions 가 없습니다');
  else if (outline === -1) bad('readDocument 안에 outlineOf 호출이 없습니다');
  else if (redact < outline) ok('가리기 → 목차 순서');
  else bad('목차를 먼저 만들고 가립니다 — 조각 크기가 실제로 보이는 양과 어긋나고 가릴 이름이 목차에 실립니다');
}

/* [9]~[11] 은 「잘렸을 때 어디로 가라고 하나」를 본다. 갈림길은 sheet·month 를
 * 줬느냐가 아니라 **그 조각 안에 회차가 여럿이냐**다.
 *
 * search 의 발췌는 회차 블록의 앞 4,000자를 자를 뿐이라(`documents.js` 의 clip),
 * 회차가 하나뿐인 조각에서는 read_document 가 이미 준 6만 자의 **부분집합**이 온다.
 * 그런 자리에 search 를 권하면 봇이 왕복을 한 번 더 쓰고 같은 앞부분을 다시 본다
 * (2026-08-28 실측: 갈래 3 문서 13건 중 12건이 회차 1개, search 가 더 닿는 것은
 *  1건·4,000자뿐이었다). 회차가 여럿이면 블록마다 발췌가 따로 와서 실제로 닿는다. */
console.log('[9] 회차가 하나뿐인 큰 문서는 search 대신 원본을 권한다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  const plain = ['## 2026-08', '', '**2026-08-05 · 약정서.pdf**', '', '가'.repeat(70000), ''].join('\n');
  const r = await readDocumentInTmp(plain, { project: '시험', document: '산정내역' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.outline) bad('나눌 축이 없는데 목차를 냈습니다');
  else if (!r.truncated) bad('7만 자인데 truncated 가 아닙니다');
  else if (r.hint.includes('지정해 좁혀')) bad('막다른 안내가 남아 있습니다 — 이 문서엔 sheet 도 month 도 없습니다');
  else if (/search 에 이 문서를 지정/.test(r.hint)) {
    bad('회차 1개인데 search 를 권합니다 — 발췌는 앞 4,000자뿐이라 이미 받은 것의 부분집합입니다');
  } else if (!r.hint.includes('원본')) bad('원본을 열라는 말이 없습니다 — 봇이 갈 곳을 잃습니다');
  else ok(`안내: ${r.hint.slice(0, 46)}…`);
}

console.log('[10] 회차가 하나뿐인 시트도 마찬가지다 (좁혀 불렀어도)');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  const big = ['## 2026-08', '',
    sheetBlock(1, 2, '요약', '작은 시트'), '',
    sheetBlock(2, 2, '민감도', '나'.repeat(70000)), ''].join('\n');
  const r = await readDocumentInTmp(big, { project: '시험', document: '산정내역', sheet: '민감도' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (!r.truncated) bad('7만 자 시트인데 truncated 가 아닙니다');
  else if (r.hint.includes('지정해 좁혀')) bad('이미 좁혀 부른 호출에 또 좁히라고 합니다');
  else if (/search 에 이 문서를 지정/.test(r.hint)) bad('시트 하나는 블록 하나인데 search 를 권합니다');
  else if (!r.hint.includes('원본')) bad('원본을 열라는 말이 없습니다');
  else ok(`안내: ${r.hint.slice(0, 46)}…`);
}

console.log('[11] 회차가 여럿인 조각은 search 를 권한다 (거기선 실제로 닿는다)');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  const day = (d, body) => `**2026-08-${d} · 일보.pdf**\n\n${body}`;
  /* **회차 셋을 같은 날짜로 둔다** (2026-09-20). 날짜가 갈리면 이제 주·날짜 축으로
   * 다시 나뉘어 잘림 갈래에 안 온다(`check-doc-week.js`). 여기서 재려는 것은
   * 「더 나눌 축이 없는데 회차는 여럿인 조각」의 안내라, 그 모양을 그대로 만든다. */
  const doc = ['## 2026-07', '', day('05', '칠월'), '',
    '## 2026-08', '',
    day('10', '가'.repeat(25000)), '',
    day('10', '나'.repeat(25000)), '',
    day('10', '다'.repeat(25000)), ''].join('\n');
  const r = await readDocumentInTmp(doc, { project: '시험', document: '산정내역', month: '2026-08' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (!r.truncated) bad('7만 자 넘는 달인데 truncated 가 아닙니다 — fixture 가 안 큽니다');
  else if (!/search 에 이 문서를 지정/.test(r.hint)) {
    bad('회차 3개인데 search 를 안 권합니다 — 블록마다 발췌가 따로 와서 실제로 닿는 자리입니다');
  } else if (r.hint.includes('원본')) bad('닿을 수 있는데 원본을 열라고 합니다');
  else if (!r.hint.includes('where')) bad('where 를 함께 주라는 말이 없습니다 — document 만 주면 조용히 전 문서를 훑습니다');
  else ok(`안내: ${r.hint.slice(0, 46)}…`);
}

console.log('[12] 6만 자 밖에 있는 회차도 세어서 안내를 정한다 (자르기 전에 센다)');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  // 첫 회차만으로 이미 6만 자를 넘고, 둘째 회차는 그 **밖**에 있다.
  // 자른 뒤에 세면 회차가 1개로 보여 「원본을 열라」고 잘못 안내한다 —
  // 실제로는 둘째 회차의 앞부분이 search 로만 닿는 자리다.
  const doc = ['## 2026-08', '',
    '**2026-08-05 · 일보.pdf**', '', '가'.repeat(65000), '',
    '**2026-08-06 · 일보.pdf**', '', '나'.repeat(10000), ''].join('\n');
  const r = await readDocumentInTmp(doc, { project: '시험', document: '산정내역' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (!r.truncated) bad('7.5만 자인데 truncated 가 아닙니다');
  else if (r.hint.includes('원본')) {
    bad('6만 자 밖의 회차를 못 봤습니다 — 자른 뒤에 세고 있습니다');
  } else if (!/search 에 이 문서를 지정/.test(r.hint)) bad('search 를 안 권합니다');
  else ok('밖에 있는 회차까지 세고 search 를 권한다');
}

console.log('[13] month 로 좁혀 불러도 엑셀은 시트 목록 머리줄이 붙는다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  // 그 달 조각을 6만 자 넘게 만드는 것이 요점이다. 작은 fixture 로는
  // 「month 일 때 목차를 안 낸다」 쪽이 안 걸려 검사가 반만 지킨다.
  const doc = ['## 2026-07', '', sheetBlock(1, 1, '칠월', '가나다'), '',
    '## 2026-08', '',
    sheetBlock(1, 2, '요약', '작은 시트'), '',
    sheetBlock(2, 2, '민감도', '나'.repeat(70000)), ''].join('\n');
  const r = await readDocumentInTmp(doc, { project: '시험', document: '산정내역', month: '2026-08' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.outline) bad('month 로 좁혀 불렀는데 목차가 왔습니다 — 이미 고른 조각에 또 고르라는 말입니다');
  else if (!r.text.startsWith('시트 2개: 요약 · 민감도')) {
    bad(`시트 목록 머리줄이 없습니다 — 첫 줄이 ${JSON.stringify(r.text.split('\n')[0])} 입니다`);
  } else if (!r.truncated) bad('7만 자짜리 달인데 truncated 가 아닙니다 — fixture 가 안 큽니다');
  else ok('month 지정 + 시트 목록 머리줄 + 목차 없음');
}

console.log('[14] 도구 설명이 목차 동작을 미리 알려준다');
{
  const src = fs.readFileSync(new URL('../src/llm/tools.js', import.meta.url), 'utf8');
  const desc = (src.match(/name: 'read_document'[\s\S]{0,900}?inputSchema/) || [''])[0];
  if (!desc) bad('read_document 도구 정의를 못 찾았습니다');
  else if (!desc.includes('목차')) bad('도구 설명에 목차 얘기가 없습니다 — 봇은 설명을 보고 계획을 세웁니다');
  else ok('도구 설명에 목차 동작이 적혀 있다');
}

console.log('[15] llm/tools.js 가 옛 막다른 꼬리말을 안 쓴다');
{
  const src = fs.readFileSync(new URL('../src/llm/tools.js', import.meta.url), 'utf8');
  if (src.includes('month 나 sheet 를 지정해 좁혀 보세요')) {
    bad('llm/tools.js 에 옛 꼬리말이 남아 있습니다 — documents.js 의 hint 를 써야 합니다');
  } else if (!src.includes('r.hint')) bad('llm/tools.js 가 hint 를 안 씁니다');
  else ok('꼬리말이 hint 로 넘어갔다');
}

/* [16]~[19] — 목차 앞에 붙는 머리말 (2026-09-03, `src/documents/read.js` 의 outlineWithPreamble).
 *
 * 넷을 함께 둔다. 「붙나」만 보면 아무 데나 아무거나 붙여도 통과하고, 「안 붙나」만 보면
 * 통째로 안 붙여도 통과한다. 그래서 붙는 쪽·안 붙는 쪽·목차가 안 상하는 쪽·넘칠 때를
 * 나란히 잰다. */

console.log('[16] 큰 문서에 머리말이 있으면 목차 **앞**에 전문이 붙는다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  const r = await readDocumentInTmp(docWrap(PREAMBLE, twoMonths(35000)), { project: '시험', document: '산정내역' });
  const body = r.error ? null : outlineBody(r.text);
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.outline !== 'month') bad(`목차 갈래가 아닙니다 (outline=${r.outline}) — fixture 가 안 큽니다`);
  else if (!body) bad('목차 본체가 없습니다');
  else if (!r.text.includes(PRE_NUMBER)) {
    bad(`머리말의 숫자('${PRE_NUMBER}')가 안 왔습니다 — 큰 문서는 이 길 말고 여는 길이 없습니다`);
  } else if (r.text.indexOf(PRE_NUMBER) > r.text.indexOf(body)) {
    bad('머리말이 목차 **뒤**에 붙었습니다 — 앞에 붙여야 합니다 (WHK 결정 2026-09-03)');
  } else if (!r.text.startsWith('**문서 md 상단 정리')) {
    bad(`머리말임을 알리는 표시가 맨 앞에 없습니다 — 봇이 어느 회차의 원문으로 읽습니다`
      + ` (첫 줄: ${JSON.stringify(r.text.split('\n')[0].slice(0, 40))})`);
  } else ok(`머리말 전문 + 목차 (합계 ${r.text.length}자 · 그중 목차 ${body.length}자)`);
}

console.log('[17] 대조군 — 머리말이 없는 문서에는 아무것도 안 붙는다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  // 위와 **같은 틀·같은 본문**이고 머리말만 뺐다. 그래야 갈린 것이 머리말 하나임이 확실하다.
  const r = await readDocumentInTmp(docWrap(null, twoMonths(35000)), { project: '시험', document: '산정내역' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.outline !== 'month') bad(`목차 갈래가 아닙니다 (outline=${r.outline})`);
  else if (outlineBody(r.text) !== r.text) {
    bad(`머리말이 없는데 목차 앞에 뭔가 붙었습니다 — 첫 줄: ${JSON.stringify(r.text.split('\n')[0].slice(0, 40))}`);
  } else if (r.text.includes(PRE_NUMBER)) bad('없는 머리말의 글자가 왔습니다');
  else ok(`목차만 ${r.text.length}자`);
}

console.log('[18] 머리말을 붙여도 목차의 조각 줄과 전체 자수가 안 어긋난다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  const doc = docWrap(PREAMBLE, twoMonths(35000));
  const [a, b] = await Promise.all([
    readDocumentInTmp(doc, { project: '시험', document: '산정내역' }),
    readDocumentInTmp(docWrap(null, twoMonths(35000)), { project: '시험', document: '산정내역' }),
  ]);
  const pieceLine = (t) => (outlineBody(t) || '').split('\n').find((l) => /^월 \d+개:/.test(l));
  if (a.error || b.error) bad(`오류: ${a.error || b.error}`);
  else if (!outlineBody(a.text)) bad('목차 본체를 못 찾았습니다');
  /* ① **조각 줄은 머리말이 있으나 없으나 글자까지 같아야 한다.** 머리말은 첫 월 헤딩보다
   *    위라 어느 달에도 안 속한다 — 조각 크기가 달라졌다면 머리말이 본문 안으로 끼어든
   *    것이고, 그러면 봇이 큰 조각을 미리 못 피한다. */
  else if (pieceLine(a.text) !== pieceLine(b.text)) {
    bad('머리말이 조각 크기를 바꿨습니다 — 머리말은 어느 달에도 안 속합니다\n'
      + `      머리말 있음: ${JSON.stringify(pieceLine(a.text))}\n`
      + `      머리말 없음: ${JSON.stringify(pieceLine(b.text))}`);
  }
  /* ② **전체 자수는 머리말까지 센 파일 길이여야 한다.** 여기가 머리말을 뺀 수로 적히면
   *    봇이 「6만 자 안쪽인데 왜 목차지」로 읽는다. */
  else if (!outlineBody(a.text).includes(`전체 ${doc.length.toLocaleString('en-US')}자`)) {
    bad(`전체 자수가 파일 길이(${doc.length.toLocaleString('en-US')}자)와 다릅니다`
      + ` — 목차 첫 줄: ${JSON.stringify(outlineBody(a.text).split('\n')[0])}`);
  } else if ((outlineBody(a.text).match(/^→ /gm) || []).length !== 2) bad('다음 행동 두 줄이 없습니다');
  else ok(`조각 줄이 두 경우에 같고, 전체 자수가 파일 길이(${doc.length.toLocaleString('en-US')}자)와 맞다`);
}

console.log('[19] 머리말이 예산을 넘치면 **머리말 쪽**이 잘리고, 잘린 사실이 글에 적힌다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  /* 상한을 fixture 로 낮춰 잰다. 실물 넷은 목차 207~242자 · 머리말 581~2,623자라
   * 6만 자 상한에 한참 못 미쳐 **이 갈래가 실물로는 한 번도 안 걸린다** — 그래도 재는
   * 것은 머리말이 사람 손으로 자라는 자리이기 때문이다. 자르기가 조용하면 봇이
   * 「이게 전부」로 읽는다. */
  const MAX = 1200;
  const long = ['## 회차 요약', '', `| 수금 | **${PRE_NUMBER}** |`, '', '라'.repeat(1500), ''].join('\n');
  const fixture = [
    '# [업무보고] 시험 문서', '', '> **사업장**: 시험 · **종류**: 업무보고', '> **열람**: 공개', '',
    '---', '', long, '---', '', twoMonths(900),
  ].join('\n');
  const r = await readDocumentInTmp(fixture, { project: '시험', document: '산정내역', maxChars: MAX });
  const body = r.error ? null : outlineBody(r.text);
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.outline !== 'month') bad(`목차 갈래가 아닙니다 (outline=${r.outline})`);
  else if (r.text.length > MAX) bad(`상한 ${MAX}자를 넘겼습니다 (${r.text.length}자)`);
  else if (!body) bad('목차 본체가 없습니다 — 목차 쪽을 자르면 안 됩니다');
  else if (!/^→ /m.test(body) || (body.match(/^→ /gm) || []).length !== 2) {
    bad('목차 본체가 잘렸습니다 — 자를 것은 머리말 쪽입니다');
  } else if (!r.text.includes('앞부분만 실었습니다')) {
    bad('머리말을 잘라 놓고 그 사실을 안 적었습니다 — 봇이 「이게 전부」로 읽습니다');
  } else if (!r.text.includes(PRE_NUMBER)) bad('머리말 앞부분조차 안 왔습니다');
  else ok(`머리말만 잘림 · 총 ${r.text.length}자 ≤ 상한 ${MAX}자 · 목차 ${body.length}자 그대로`);
}

/* ── [20]~[26] 절 갈래 (2026-09-10 · [24]~[26] 은 2026-09-11 전체 검증) ───────
 * sheet 도 month 도 없는 큰 문서가 번호 붙은 절로 나뉘어 있으면 목차를 준다.
 * 그전에는 이런 문서가 「앞부분 + 막다른 안내」로 갔다 — 40만 자 문서에서
 * 뒤쪽을 영영 못 봤다.
 *
 * 여섯 자리다 — [20][21][22][22b][22c][23] ([22] 안에서 상한을 키운 대조군까지 함께
 * 잰다). [24]~[26] 은 `outlineOf` 의 절 갈래와 `readDocument` 의 잘림 안내가
 * `sectionOutline` 하나로 같은 판정을 보는지를 본다 — 예전에는 안내 쪽이 관문을 안 보고
 * `sectionsOf` 만 봐서, **관문이 일부러 목차를 끈 문서**에 「section 으로 열라」는 틀린
 * 안내가 나갔다(실측 34건 중 2건). */
{
  const sec = (n, name) => `### ${n}. ${name}\n\n${`${name} 상세 `.repeat(400)}\n\n`;
  const bodyText = [1, 2, 3, 4, 5].map((i) => sec(i, `항목${i}`)).join('');
  const doc = docWrap(null, bodyText);

  // [20] outlineOf 가 section 갈래를 낸다
  const o = outlineOf(doc);
  if (!o || o.kind !== 'section') bad(`[20] section 갈래를 기대했는데 ${o ? o.kind : 'null'}`);
  else if (o.pieces.length !== 5) bad(`[20] 조각 5개를 기대했는데 ${o.pieces.length}`);
  else ok('[20] 절로 나뉜 문서가 section 갈래로 온다');

  // [21] 조각 이름이 봇이 그대로 인자로 쓸 수 있는 꼴인가
  if (o && o.kind === 'section' && !/^1\.\s*항목1$/.test(o.pieces[0].name)) {
    bad(`[21] 조각 이름이 '${o.pieces[0].name}' — "N. 제목" 꼴이어야 인자로 쓴다`);
  } else if (o && o.kind === 'section') ok('[21] 조각 이름이 "N. 제목" 꼴');

  // [22] **가장 큰 절이 상한을 넘으면 목차를 내지 않는다.**
  //      이름만 절이고 내용이 한 덩어리인 문서가 실물에 둘 있다 — 목차를 줘도 그 절이
  //      또 잘려서 왕복만 늘고 얻는 것이 없다 (2026-09-10 실측: 최대 절 93,746자 · 454,159자).
  {
    const lopsided = docWrap(null, [
      '### 1. 짧은 머리', '\n짧다.\n',
      '### 2. 몸통', `\n${'덩어리 '.repeat(30000)}\n`,
    ].join('\n'));
    const o3 = outlineOf(lopsided, 60000);
    if (o3 !== null) bad(`[22] null 을 기대했는데 ${o3.kind} — 가장 큰 절이 상한을 넘으면 목차를 내면 안 됩니다`);
    else ok('[22] 절이 상한 안에 안 들어오면 목차를 안 낸다');

    // 같은 문서라도 상한을 크게 주면 목차가 나온다 — 판정 기준이 상한이라는 증거
    const o4 = outlineOf(lopsided, 10_000_000);
    if (!o4 || o4.kind !== 'section') bad(`[22] 상한을 키우면 section 갈래가 나와야 하는데 ${o4 ? o4.kind : 'null'}`);
    else ok('[22] 상한을 키우면 같은 문서가 목차로 온다');
  }

  // [22b] **덮음률이 낮으면 목차를 안 낸다.** 앞 번호 절들이 다른 모양이라 안 잡히면
  //       뒤쪽 두어 개만 절로 뽑히고 앞부분이 통째로 어느 절에도 안 든다. 그런 문서에
  //       목차를 내면 봇이 문서의 일부를 전부로 읽는다 — 이번에 고치는 회귀와 같은 모양이다
  //       (실측 2026-09-10: 덮음 8.1% · 15.5% 인 문서가 실물에 둘 있다).
  {
    const tail = [3, 4, 5].map((i) => sec(i, `뒤항목${i}`)).join('');
    const lopsidedHead = docWrap(null, `${'앞부분 '.repeat(20000)}\n\n${tail}`);
    const o5 = outlineOf(lopsidedHead, 60000);
    if (o5 !== null) bad(`[22b] null 을 기대했는데 ${o5.kind} — 절이 문서를 조금만 덮으면 목차를 내면 안 됩니다`);
    else ok('[22b] 덮음률이 낮으면 목차를 안 낸다');
  }

  // [22c] 첫 절 앞에 남는 양을 목차가 밝히나
  {
    const withHead = docWrap(null, `${'머리 '.repeat(50)}\n\n${bodyText}`);
    const o6 = outlineOf(withHead, 60000);
    if (!o6 || o6.kind !== 'section') bad(`[22c] section 갈래를 기대했는데 ${o6 ? o6.kind : 'null'}`);
    else if (!(o6.before > 0)) bad(`[22c] before 가 ${o6.before} — 첫 절 앞 구간이 있는데 0 입니다`);
    else ok(`[22c] 첫 절 앞 ${o6.before}자를 목차가 셉니다`);
  }

  // [23] 절이 하나도 안 잡히는 큰 문서는 예전 그대로 null (막다른 안내 갈래 유지)
  const flat = docWrap(null, `${'평평한 본문 '.repeat(9000)}`);
  const o2 = outlineOf(flat);
  if (o2 !== null) bad(`[23] null 을 기대했는데 ${o2.kind} — 절 없는 문서의 동작이 바뀌었습니다`);
  else ok('[23] 절 없는 큰 문서는 예전 갈래 그대로');
}

console.log('[24] 절이 있지만 관문(상한)에 막힌 문서는 잘림 안내가 section 을 권하지 않는다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  // [22] 의 lopsided 와 같은 모양 — 짧은 절 하나 + 상한을 훌쩍 넘는 절 하나.
  const blocked = docWrap(null, [
    '### 1. 짧은 머리', '\n짧다.\n',
    '### 2. 몸통', `\n${'덩어리 '.repeat(30000)}\n`,
  ].join('\n'));
  const r = await readDocumentInTmp(blocked, { project: '시험', document: '산정내역' });
  if (r.error) bad(`[24] 오류: ${r.error}`);
  else if (r.outline) bad(`[24] 관문에 막혀야 하는데 목차(${r.outline})가 왔습니다 — fixture 를 다시 보세요`);
  else if (!r.truncated) bad('[24] 잘렸어야 하는데 truncated 가 아닙니다');
  else if (r.hint.includes('section 으로 한 절만 여세요')) {
    bad(`[24] 관문에 막힌 문서인데 section 으로 좁히라고 권합니다 — 그 절도 상한을 넘어 또 잘립니다: ${r.hint}`);
  } else ok(`[24] 관문에 막힌 문서는 section 을 권하지 않는다: ${r.hint.slice(0, 60)}…`);
}

console.log('[24b] 절이 있지만 관문(덮음률)에 막힌 문서는 잘림 안내가 크기를 이유로 대지 않는다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  // [22b] 와 같은 모양 — 앞부분이 길어서 절이 문서를 조금만 덮는다. 절 자체는 작다
  // (가장 큰 절이 상한을 훌쩍 못 미친다) — 그래야 막힌 이유가 **오직 덮음률**임이 분명하다.
  const secSmall = (n, name) => `### ${n}. ${name}\n\n${`${name} 상세 `.repeat(400)}\n\n`;
  const tail = [3, 4, 5].map((i) => secSmall(i, `뒤항목${i}`)).join('');
  const lowCover = docWrap(null, `${'앞부분 '.repeat(20000)}\n\n${tail}`);
  const r = await readDocumentInTmp(lowCover, { project: '시험', document: '산정내역' });
  if (r.error) bad(`[24b] 오류: ${r.error}`);
  else if (r.outline) bad(`[24b] 관문에 막혀야 하는데 목차(${r.outline})가 왔습니다 — fixture 를 다시 보세요`);
  else if (!r.truncated) bad('[24b] 잘렸어야 하는데 truncated 가 아닙니다');
  else if (r.hint.includes('가장 큰 절이')) {
    bad(`[24b] 덮음률로 막혔는데 크기가 이유라고 말합니다(거짓): ${r.hint}`);
  } else if (r.hint.includes('section 으로 한 절만 여세요') || r.hint.includes('section 으로 좁혀도')) {
    bad(`[24b] 덮음률로 막힌 문서인데 section 을 권합니다: ${r.hint}`);
  } else ok(`[24b] 덮음률로 막힌 문서는 크기를 이유로 대지 않는다: ${r.hint.slice(0, 60)}…`);
}

console.log('[25] 관문을 통과하는 문서(목차가 오는 문서)는 잘림-안내 갈래에 안 온다');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  const bigSec = (n, name) => `### ${n}. ${name}\n\n${`${name} `.repeat(5000)}\n\n`;
  const passable = docWrap(null, [1, 2, 3, 4, 5].map((i) => bigSec(i, `절${i}`)).join(''));
  const r = await readDocumentInTmp(passable, { project: '시험', document: '산정내역' });
  if (r.error) bad(`[25] 오류: ${r.error}`);
  else if (r.outline !== 'section') bad(`[25] 관문을 통과해야 하는데 outline='${r.outline}' 입니다 — fixture 를 다시 보세요`);
  else if (r.truncated) bad('[25] 목차 갈래로 갔는데 truncated 가 true 입니다 — 목차 갈래는 자르지 않고 돌려줍니다');
  else if (r.hint) bad(`[25] 목차 갈래인데 hint 가 비어 있지 않습니다: ${r.hint}`);
  else ok('[25] 관문 통과 문서는 목차로 먼저 빠져 잘림-안내 갈래를 안 탄다');
}

console.log('[26] 절 자체가 없는 큰 문서는 예전 문구 그대로 (sheet·month 로도 안 나뉜다)');
{
  const { readDocumentInTmp } = await import('./_outline-probe.js');
  const flatBig = ['## 2026-08', '', '**2026-08-05 · 약정서.pdf**', '', '다'.repeat(70000), ''].join('\n');
  const r = await readDocumentInTmp(flatBig, { project: '시험', document: '산정내역' });
  if (r.error) bad(`[26] 오류: ${r.error}`);
  else if (r.outline) bad(`[26] 절이 없는데 목차(${r.outline})가 왔습니다`);
  else if (!r.truncated) bad('[26] 7만 자인데 truncated 가 아닙니다');
  else if (!r.hint.includes('sheet·month 로 나뉘어 있지 않아 좁힐 수 없습니다')) {
    bad(`[26] 절 없는 문서의 예전 문구가 사라졌습니다: ${r.hint}`);
  } else ok('[26] 절 없는 큰 문서는 예전 문구 그대로');
}

process.exit(failed ? 1 : 0);
