#!/usr/bin/env node
/**
 * 엑셀 시트 블록 점검 — 임시 fixture 로 돈다 (실제 아카이브에 엑셀이 없어도 돌아야 한다).
 *
 * 왜 있나: 엑셀은 「시트 = 회차 블록」으로 담긴다. 그러면 read_document 가 시트 하나만
 * 열 수 있어야 하고(안 그러면 60,000자에서 잘린다), 색인은 그것을 시리즈로 세면 안 된다
 * (documents.js 의 isSeriesDoc() 이 시트 문서를 빼지 않으면, 엑셀도 시리즈(rank 0)가 되어
 * 「가장 마지막 순서로만 접힌다」쪽에 섞여 정작 접혀야 할 예산에서도 안 접힌다).
 *
 * 실행: node scripts/check-excel-sheets.js
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { sheetsOf, SHEET_RE } from '../src/documents.js';
import { splitMessages } from '../src/archive.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

const SRC = '260805_시험_산정내역.xlsx';
const sheetBlock = (i, n, name, body) =>
  `**2026-08-05 · ${SRC} — 시트 ${i}/${n}: ${name}**\n\n${body}`;
// 정규식은 `documents.js` 의 SHEET_RE 를 그대로 가져다 쓴다 — 여기 사본을 두면
// 안 된다. 예전에 여기 있던 사본은 `**날짜 · ` 부분을 안 봐서, `· ` 가 빠진 헤더를
// 이 검사만 통과시켰다. 검사가 잡으라고 있는 바로 그 입력을 못 가른 것이다.

const FIXTURE = [
  '# [산정내역] 시험 엑셀',
  '',
  '> **사업장**: 시험 · **종류**: 산정내역',
  '> **열람**: 공개',
  '> **시트**: 산정내역 · 이자계산 · 상환스케줄',
  '',
  '---',
  '',
  '## 2026-08',
  '',
  sheetBlock(1, 3, '산정내역', '<table><tr><th>구분</th><th>금액</th></tr><tr><td>선순위</td><td>1,520</td></tr></table>'),
  '',
  sheetBlock(2, 3, '이자계산', '<table><tr><th>일자</th><th>연체이자</th></tr><tr><td>2026-09-05</td><td>3,120</td></tr></table>'),
  '',
  sheetBlock(3, 3, '상환스케줄', '<table><tr><th>회차</th><th>원금</th></tr><tr><td>1</td><td>500</td></tr></table>'),
  '',
].join('\n');

console.log('[1/9] splitMessages 가 시트마다 블록 하나로 쪼갠다');
{
  const blocks = splitMessages(FIXTURE);
  if (blocks.length === 3) ok('블록 3개');
  else bad(`블록이 ${blocks.length}개입니다 — 3개여야 합니다 (회차 헤더 계약이 깨졌습니다)`);
}

console.log('[2/9] sheetsOf 가 시트 이름을 순서대로 준다');
{
  const names = sheetsOf(FIXTURE);
  const want = ['산정내역', '이자계산', '상환스케줄'];
  if (JSON.stringify(names) === JSON.stringify(want)) ok(names.join(' · '));
  else bad(`sheetsOf = ${JSON.stringify(names)} — ${JSON.stringify(want)} 여야 합니다`);
}

console.log('[3/9] 시트가 아닌 문서에는 sheetsOf 가 빈 배열을 준다');
{
  const plain = '## 2026-08\n\n**2026-08-05 · 260805_보고.hwp**\n\n본문\n';
  const names = sheetsOf(plain);
  if (names.length === 0) ok('일반 문서 → []');
  else bad(`일반 문서에서 ${JSON.stringify(names)} 가 나왔습니다`);
}

/* [4/9]·[5/9] 는 readDocument 를 임시 아카이브에 대고 부른다.
 * 실제 아카이브를 안 건드리려고 DOCS_DIR 을 임시 폴더로 돌려 별도 프로세스에서 돌린다. */
console.log('[4/9] sheet 로 부르면 그 블록만 온다');
console.log('[5/9] sheet 없이 부르면 맨 앞에 시트 목록이 붙는다');
{
  const here = path.dirname(fileURLToPath(import.meta.url));
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-xl-'));
  const proj = path.join(tmp, 'projects', '시험');
  fs.mkdirSync(proj, { recursive: true });
  fs.writeFileSync(path.join(proj, '20260805-시험-산정내역.md'), FIXTURE, 'utf8');
  fs.writeFileSync(path.join(tmp, 'index.md'), '# 문서 아카이브\n', 'utf8');

  // Windows: 절대경로를 그대로 import specifier 로 주면 ERR_UNSUPPORTED_ESM_URL_SCHEME
  // 로 죽는다("C:/..." 가 file:// URL 이 아니라서다) — pathToFileURL 로 감싼다.
  const probe = `
    process.env.HERMES_DOCS_DIR = ${JSON.stringify(tmp)};
    const { readDocument } = await import(${JSON.stringify(
      pathToFileURL(path.join(here, '..', 'src', 'documents.js')).href,
    )});
    const { FULL_ACCESS } = await import(${JSON.stringify(
      pathToFileURL(path.join(here, '..', 'src', 'config.js')).href,
    )});
    const one = readDocument({ project: '시험', document: '산정내역', sheet: '이자계산', access: FULL_ACCESS });
    const all = readDocument({ project: '시험', document: '산정내역', access: FULL_ACCESS });
    console.log(JSON.stringify({ one, all }));
  `;
  const { spawnSync } = await import('node:child_process');
  const r = spawnSync(process.execPath, ['--input-type=module', '-e', probe], { encoding: 'utf-8' });
  if (r.status !== 0) {
    bad(`probe 가 실패했습니다: ${(r.stderr || '').trim().split('\n')[0]}`);
  } else {
    const { one, all } = JSON.parse(r.stdout.trim().split('\n').pop());
    if (one.error) bad(`sheet:"이자계산" → ${one.error}`);
    else if (one.text.includes('연체이자') && !one.text.includes('상환스케줄')) ok('이자계산 블록만 왔다');
    else bad(`sheet 로 좁혀지지 않았습니다: ${one.text.slice(0, 120)}`);

    if (all.error) bad(`sheet 없이 → ${all.error}`);
    else if (all.text.startsWith('시트 3개: 산정내역 · 이자계산 · 상환스케줄')) ok('맨 앞에 시트 목록');
    else bad(`시트 목록이 안 붙었습니다: ${all.text.slice(0, 80)}`);
  }
  fs.rmSync(tmp, { recursive: true, force: true });
}

console.log('[6/9] 색인에서 엑셀이 시리즈로 안 굳고 「시트 N개」로 적힌다');
{
  const here = path.dirname(fileURLToPath(import.meta.url));
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-xl-brief-'));
  const proj = path.join(tmp, 'projects', '시험');
  fs.mkdirSync(proj, { recursive: true });
  fs.writeFileSync(path.join(proj, '20260805-시험-산정내역.md'), FIXTURE, 'utf8');
  fs.writeFileSync(path.join(tmp, 'index.md'), '# 문서 아카이브\n', 'utf8');

  // Windows: 절대경로를 그대로 import specifier 로 주면 ERR_UNSUPPORTED_ESM_URL_SCHEME
  // 로 죽는다("C:/..." 가 file:// URL 이 아니라서다) — pathToFileURL 로 감싼다.
  const probe = `
    process.env.HERMES_DOCS_DIR = ${JSON.stringify(tmp)};
    const { buildDocumentsBrief } = await import(${JSON.stringify(
      pathToFileURL(path.join(here, '..', 'src', 'documents.js')).href,
    )});
    const { FULL_ACCESS } = await import(${JSON.stringify(
      pathToFileURL(path.join(here, '..', 'src', 'config.js')).href,
    )});
    console.log(JSON.stringify(buildDocumentsBrief({ access: FULL_ACCESS })));
  `;
  const { spawnSync } = await import('node:child_process');
  const r = spawnSync(process.execPath, ['--input-type=module', '-e', probe], { encoding: 'utf-8' });
  if (r.status !== 0) {
    bad(`probe 가 실패했습니다: ${(r.stderr || '').trim().split('\n')[0]}`);
  } else {
    const brief = JSON.parse(r.stdout.trim().split('\n').pop());
    if (brief.includes('시트 3개')) ok('색인 줄에 「시트 3개」');
    else bad(`색인 줄이 「3회차」로 적혔습니다: ${brief}`);
    // 사업장 헤더의 회차 합계는 문서 1건이므로 1이어야 한다 (시트 3이 아니다).
    if (/회차 1\)/.test(brief)) ok('사업장 헤더 회차 1');
    else bad(`사업장 헤더가 시트만큼 부풀었습니다: ${(brief.match(/### .*/) || [''])[0]}`);
  }
  fs.rmSync(tmp, { recursive: true, force: true });
}

console.log('[7/9] 중간 예산에서 엑셀은 접히고, 시리즈는 안 접힌다');
{
  // 시리즈(rank 0)도 2026-09-10 부터 접히지만 **가장 마지막 순서**다. isSeriesDoc 이
  // 시트 문서를 못 빼는 회귀(`!isSheetDoc(doc)` 누락)가 나면 엑셀도 rank 0 이 되어
  // 시리즈와 같은 순서로만 접힌다 — 텍스트의 「그 외 N건」 유무가 아니라
  // foldedOut 으로 "먼저 접혀야 할 것이 실제로 먼저 접혔는지"를 직접 잰다
  // (라벨 문구 「시트 N개」 검사는 [6/9] 가 이미 한다).
  const here = path.dirname(fileURLToPath(import.meta.url));
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-xl-fold-'));
  const proj = path.join(tmp, 'projects', '시험');
  fs.mkdirSync(proj, { recursive: true });
  fs.writeFileSync(path.join(proj, '20260805-시험-산정내역.md'), FIXTURE, 'utf8');
  // 시리즈 문서 — 엑셀이 아니고 회차 2건. 날짜를 엑셀보다 훨씬 옛날로 둔다: 회귀로
  // 둘 다 rank 0 이 되면 동률은 「오래된 것부터」로 깨지므로, 이 시리즈(옛날)가
  // 엑셀(2026-08)보다 먼저 접힌다 — 회귀가 있으면 아래 판정이 반드시 반대로
  // (시리즈가 접히고 엑셀은 안 접힘) 나와 잡힌다.
  const SERIES = [
    '# [주간보고] 시험 주간보고',
    '',
    '> **사업장**: 시험 · **종류**: 주간보고',
    '> **열람**: 공개',
    '',
    '---',
    '',
    '## 2025-02',
    '',
    '**2025-02-15 · 250215_주간보고.hwp**',
    '',
    '본문 2',
    '',
    '## 2025-01',
    '',
    '**2025-01-15 · 250115_주간보고.hwp**',
    '',
    '본문 1',
    '',
  ].join('\n');
  fs.writeFileSync(path.join(proj, '20250215-시험-주간보고.md'), SERIES, 'utf8');
  fs.writeFileSync(path.join(tmp, 'index.md'), '# 문서 아카이브\n', 'utf8');

  const probe = `
    process.env.HERMES_DOCS_DIR = ${JSON.stringify(tmp)};
    const { buildDocumentsBrief } = await import(${JSON.stringify(
      pathToFileURL(path.join(here, '..', 'src', 'documents.js')).href,
    )});
    const { FULL_ACCESS } = await import(${JSON.stringify(
      pathToFileURL(path.join(here, '..', 'src', 'config.js')).href,
    )});
    const full = [];
    const fullText = buildDocumentsBrief({ access: FULL_ACCESS, maxChars: 999999, foldedOut: full });
    const folded = [];
    // 전부 보이는 길이보다 1 글자 적은 예산 — 문서 한 줄(수십 자)을 접어야 겨우
    // 맞으므로, 정확히 하나만 접히게 된다.
    buildDocumentsBrief({ access: FULL_ACCESS, maxChars: fullText.length - 1, foldedOut: folded });
    console.log(JSON.stringify({
      fullFolded: full.length,
      foldedTitles: folded.map((d) => ({ title: d.title, sheet: Boolean(d.meta && d.meta['시트']) })),
    }));
  `;
  const { spawnSync } = await import('node:child_process');
  const r = spawnSync(process.execPath, ['--input-type=module', '-e', probe], { encoding: 'utf-8' });
  if (r.status !== 0) {
    bad(`probe 가 실패했습니다: ${(r.stderr || '').trim().split('\n')[0]}`);
  } else {
    const { fullFolded, foldedTitles } = JSON.parse(r.stdout.trim().split('\n').pop());
    if (fullFolded !== 0) bad(`전부 보이는 예산인데도 ${fullFolded}건이 접혔습니다 — fixture 를 다시 보세요`);
    else if (foldedTitles.length !== 1) bad(`한 건만 접혀야 하는데 ${foldedTitles.length}건 접혔습니다: ${JSON.stringify(foldedTitles)}`);
    else if (foldedTitles[0].sheet) ok('예산이 빠듯하면 엑셀이 먼저 접히고 시리즈는 남는다');
    else bad(`엑셀 대신 시리즈가 접혔습니다 — isSeriesDoc 이 시트를 못 빼는 회귀로 보입니다: ${JSON.stringify(foldedTitles)}`);
  }
  fs.rmSync(tmp, { recursive: true, force: true });
}

console.log('[8/9] 시트 순서가 오름차순이라 요약이 첫 시트를 집는다');
{
  const blocks = splitMessages(FIXTURE);
  const nums = blocks.map((b) => Number((b.text.match(SHEET_RE) || [])[1]));
  if (JSON.stringify(nums) === JSON.stringify([1, 2, 3])) {
    ok('시트 1 → 3 순');
  } else {
    // 일반 문서는 최신 회차가 위(내림차순)다. 엑셀만 반대이고, 날짜가 같아
    // verify_format.py 의 최신순 검사에는 안 걸린다 — 그래서 여기서 본다.
    bad(`시트 순서가 ${JSON.stringify(nums)} 입니다 — [1,2,3] 이어야 요약이 첫 시트를 집습니다`);
  }
  const first = blocks[0].text;
  if (first.includes('산정내역') && !first.includes('backdata')) ok('첫 블록이 시트 1');
  else bad(`첫 블록이 시트 1이 아닙니다: ${first.slice(0, 60)}`);
}

console.log('[9/9] 헤더 계약을 어긴 블록은 시트로 안 세어진다');
{
  // 이 검사가 있어야 SHEET_RE 를 여기 사본으로 되돌리는 것이 빨개진다. 사본은
  // `**날짜 · ` 앞부분을 안 봐서 아래 두 입력을 전부 시트로 세었고, 그러면 검사가
  // 「봇이 못 읽는 헤더」와 「봇이 읽는 헤더」를 구별하지 못한다 — 잡으라고 있는
  // 입력을 못 잡는 검사다.
  const CASES = [
    ['`· ` 가 빠진 헤더', `**2026-08-05 ${SRC} — 시트 1/2: 산정내역**\n\n본문`],
    ['날짜가 없는 헤더', `**${SRC} — 시트 1/2: 산정내역**\n\n본문`],
  ];
  for (const [label, text] of CASES) {
    if (!SHEET_RE.test(text)) ok(`${label} → 시트가 아니다`);
    else bad(`${label} 이 SHEET_RE 를 통과했습니다 — 정규식이 느슨해졌습니다: ${text.split('\n')[0]}`);
  }
  // 그리고 제대로 된 헤더는 여전히 통과해야 한다 (반대쪽으로 조여도 빨개지게).
  const good = `**2026-08-05 · ${SRC} — 시트 1/2: 산정내역**\n\n본문`;
  if (SHEET_RE.test(good)) ok('제대로 된 헤더는 그대로 시트다');
  else bad(`제대로 된 헤더가 SHEET_RE 에 안 걸립니다: ${good.split('\n')[0]}`);
}

process.exit(failed ? 1 : 0);
