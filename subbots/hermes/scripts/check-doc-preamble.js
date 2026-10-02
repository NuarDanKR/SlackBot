#!/usr/bin/env node
/**
 * 문서 md 첫 회차 앞의 「사람이 손으로 적어 둔 정리」가 검색·색인·**열어 읽기** 셋에 다 실리나.
 *
 *   node scripts/check-doc-preamble.js
 *
 * ── 왜 필요한가 (2026-09-03) ──
 *
 * 채널 쪽에서 같은 결함을 닫고(`check-preamble.js`) **문서 쪽은 안 닫혔다.**
 * `channelBrief` 는 고쳐졌는데 `documents.js` 는 그 함수를 안 쓰고 `metaBlock` 을 직접
 * 부르고, 검색은 `d.entries`(= `splitMessages`)만 훑는데 첫 회차 헤더 앞은 블록이 아니다.
 * `doc-archive/scripts/verify_format.py` 8번 검사가 **월 섹션 안**의 같은 모양을 이미
 * 잡고 있었지만, 첫 월 헤딩보다 **위**에 있는 구간은 그 검사 밖이었다.
 *
 * 실측 2026-09-03 (고치기 전, 실물 아카이브):
 *   · `documents/projects/` 아래 문서 md 431개 중 **12개**에 그 구간이 있고 합계 11,056자
 *   · 그 구간에만 있는 낱말 6개로 `searchDocuments` 를 부르면 **전부 0건**
 *   · 같은 문서 본문에만 있는 낱말 36개(대조군)는 **전부 걸림** — 검색 자체는 멀쩡했다
 *   (`documents/index.md` 39,528자 · `excluded.md` 6,011자에도 같은 구간이 있지만 그 둘은
 *    `listDocuments()` 가 안 읽는 파일이라 애초에 검색 대상이 아니다. 「문서 md 14개
 *    55,596자」로 세면 그 둘을 함께 센 것이다.)
 *
 * 그래서 이 검사는 「0건이 아니게 됐다」가 아니라 **여섯 가지가 동시에 지켜지나**를 본다.
 *
 *   ① 머리말에만 있는 낱말이 문서 검색에 잡힌다
 *   ② 그것이 문서 색인에도 실린다 (제목이)
 *   ③ **반대 방향** — 머리말 본문(숫자)은 색인에 안 실린다.
 *      이게 없으면 누가 색인에 표 원문을 도로 실어도 ①②는 그대로 통과한다
 *      (「색인에는 머리말 제목만, 숫자는 전문으로」 WHK 결정 2026-09-03)
 *   ④ `d.entries` 개수는 **하나도 안 변한다** — 그 수로 회차를 세는 자리가 여럿이다
 *      (색인의 `N회차`·`isSeriesDoc`·`outlineOf`·`ingest/verify.js`·`check-excel-sheets.js`)
 *   ⑤ 대조군 — 본문에만 있는 낱말은 그대로 걸린다. ①이 빨간데 ⑤도 빨가면 머리말이
 *      아니라 검색 자체가 죽은 것이다. 둘을 안 가르면 원인을 엉뚱한 데서 찾는다
 *   ⑥ **열어서 읽을 수 있다** — `read_document` 로 그 문서를 열면 머리말이 온다.
 *      2026-09-03 에 따로 닫은 자리다: 검색은 커밋 00e1c2b 로 닿게 됐지만 **큰 문서는
 *      전문 대신 목차가 와서** 머리말이 한 글자도 안 보였다 (12개 10,918자 중 목차로
 *      빠지는 넷이 7,128자 · 65%). 지금은 목차 앞에 전문이 붙는다
 *      (`src/documents/read.js` 의 `outlineWithPreamble`). 반대 방향 둘을 같이 잰다 —
 *      머리말이 없는 문서에는 안 붙나 · 목차 본체가 안 잘렸나.
 *      **이 축은 낱말을 안 고른다** — ①⑤가 「그 구간에만 있는 낱말」을 못 찾아 12개 중
 *      10개에서 「못 쟀습니다」를 내는데, 「열어서 보이나」는 글자를 그대로 대 보면 되므로
 *      그 한계를 안 물려받는다
 *
 * 앞부분은 **가짜 문서 md 로** 본다 — 이 저장소는 팀끼리 나눠 써서 검사 코드에 진짜
 * 사업장 이름·숫자를 적을 수 없다(`check-business-names.js`). 실물 아카이브는 뒤에서
 * 「머리말이 있는 문서가 검색·색인에 살아 있나」를 세어 본다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  preambleOf, splitMessages, readCached, preambleOutline,
} from '../src/archive.js';
import {
  listDocuments, searchDocuments, buildDocumentsBrief, buildDocumentsBriefSplit, hasDocuments,
  readDocument, outlineOf,
} from '../src/documents.js';
import { FULL_ACCESS, PUBLIC_ACCESS } from '../src/config.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);
const eq = (label, got, want) => (got === want ? pass(label) : fail(`${label} — 받은 값 ${JSON.stringify(got)}`));

/* ── 가짜 문서 md. 실물과 같은 관례를 그대로 흉내낸다 ── */
const FIXTURE = [
  '# 자리가 업무보고',
  '',
  '> **사업장**: 자리가 · **종류**: 업무보고',
  '> **열람**: 공개',
  '',
  '---',
  '',
  '## 회차 요약',
  '',
  '| 작성일 | 수금 누계 |',
  '|---|---|',
  '| 2026-08-02 | **919실** |',
  '',
  '---',
  '',
  '## 2026-08',
  '',
  '**2026-08-02 · 260802_보고.pdf**',
  '본문에만 있는 말 자물쇠',
  '',
  '## 2026-06',
  '',
  '**2026-06-01 · 260601_보고.pdf**',
  '옛날 말',
  '',
].join('\n');

console.log('[1/6] preambleOf 가 문서 md 에서도 사람이 쓴 것만 남긴다');
{
  const pre = preambleOf(FIXTURE);
  eq('  머리말이 사람이 쓴 헤딩으로 시작한다', pre.split('\n')[0], '## 회차 요약');
  eq('  표 안의 숫자가 남아 있다', pre.includes('**919실**'), true);
  eq('  파일 제목(`# 제목`)은 안 들어온다', /^#\s/m.test(pre), false);
  eq('  상단 `>` 메타는 안 들어온다 (색인에 두 번 실리면 안 된다)', pre.includes('>'), false);
  eq('  월 헤딩(`## 2026-08`)은 머리말이 아니다', pre.includes('## 2026-08'), false);
  eq('  회차 본문의 말은 안 들어온다', pre.includes('자물쇠'), false);
}

console.log('[2/6] splitMessages 는 한 글자도 안 변한다 · 제목 뽑기는 한 벌뿐이다');
{
  const blocks = splitMessages(FIXTURE);
  eq('  가짜 md 회차 2개', blocks.length, 2);
  eq('  첫 회차 날짜', blocks[0].date, '2026-08-02');
  eq('  머리말은 회차에 안 들어간다', blocks.map((b) => b.text).join('\n').includes('919실'), false);

  /* **머리말 제목 뽑기는 한 벌이어야 한다** — 채널 색인(`channelBrief`)과 문서
   * 색인(`buildDocumentsBrief`)이 둘 다 `archive.js` 의 `preambleOutline` 을 부른다.
   *
   * 2026-09-03 까지는 `documents.js` 에 `docPreambleOutline` 이라는 **글자까지 같은
   * 사본**이 있었다 — 저쪽이 `export` 가 아니어서 가져다 쓸 수가 없었다. 그때 이 절은
   * 두 몸통을 대 봐서 「갈렸나」를 봤다. `export` 를 붙이고 사본을 지웠으므로 이제 **댈
   * 것이 없다.**
   *
   * **그렇다고 이 절을 지우면 안 된다.** 지우면 다음 사람이 사본을 다시 만들어도 아무도
   * 안 잡는다 — 사본은 만든 날에는 값이 같아서, 「값을 대는」 검사로는 그 순간 통과하고
   * 몇 달 뒤 한쪽만 고쳐질 때 조용히 갈린다. 그래서 재는 것을 **「값이 같나」에서
   * 「사본이 되살아나지 않았나」로 바꿨다.**
   *
   * 못 잡는 것도 적어 둔다 — ③은 **글자가 같은** 사본만 잡는다. 처음부터 다시 쓴 사본은
   * ③을 빠져나가지만, 그 사본을 색인이 쓰려면 ④(색인 줄이 `preambleOutline` 을 부른다)가
   * 걸린다. 둘을 함께 두는 이유가 이것이다. */
  const here = path.dirname(fileURLToPath(import.meta.url));
  const archiveSrc = fs.readFileSync(path.join(here, '..', 'src', 'archive.js'), 'utf8');
  const docsSrc = fs.readFileSync(path.join(here, '..', 'src', 'documents.js'), 'utf8');
  const briefSrc = fs.readFileSync(path.join(here, '..', 'src', 'documents', 'brief.js'), 'utf8');
  const readSrc = fs.readFileSync(path.join(here, '..', 'src', 'documents', 'read.js'), 'utf8');
  const norm = (s) => s.split('\n').map((l) => l.trim()).filter(Boolean).join('\n');
  const bodyOf = (src, name) => {
    const head = src.indexOf(`function ${name}(pre) {`);
    if (head === -1) return null;
    const end = src.indexOf('\n}', head);
    if (end === -1) return null;
    return norm(src.slice(src.indexOf('{', head) + 1, end));
  };

  // ① 저쪽이 내보내고 있나. 안 내보내면 위 import 가 먼저 죽으므로 여기는 확인 사살이다.
  eq('  archive.js 가 preambleOutline 을 export 한다', /export function preambleOutline\(/.test(archiveSrc), true);
  eq('  그것이 실제로 함수다', typeof preambleOutline, 'function');

  // ② 문서 쪽이 그것을 가져다 쓴다 (자기 것을 부르는 게 아니라).
  const importBlock = docsSrc.slice(docsSrc.indexOf('import {'), docsSrc.indexOf("} from './archive.js';") + 1);
  eq('  documents.js 가 archive.js 에서 preambleOutline 을 임포트한다',
    /\bpreambleOutline\b/.test(importBlock), true);

  // Codex R2d: the facade must inject the original function into the new builder.
  eq('  색인 builder 에 원본 preambleOutline 을 주입한다',
    /createDocumentBrief\(\{[\s\S]*?\bpreambleOutline\b[\s\S]*?\}\)/.test(docsSrc), true);

  // ③ 사본이 되살아나지 않았나 — 저쪽 몸통 글자가 문서 쪽 소스 안에 또 있으면 사본이다.
  const body = bodyOf(archiveSrc, 'preambleOutline');
  if (!body) {
    fail('archive.js 에서 preambleOutline 몸통을 못 찾았습니다 — 이름·모양이 바뀌었으면 여기도 고치세요');
  } else if ([docsSrc, briefSrc, readSrc].some((src) => norm(src).includes(body))) {
    fail('documents.js/brief.js/read.js 안에 preambleOutline 의 사본이 다시 생겼습니다'
      + ' — 사본은 만든 날엔 값이 같아 아무 검사도 안 걸리고, 한쪽만 고쳐지는 날 조용히 갈립니다.'
      + ' archive.js 의 것을 임포트해 쓰세요');
  } else {
    pass('  documents.js/brief.js/read.js 에 제목 뽑기 사본이 없다 (한 벌뿐이다)');
  }

  /* ④ 색인 줄이 실제로 그 함수를 부르나. ③을 빠져나간 「다시 쓴 사본」이 여기 걸린다.
   *
   * 줄을 `상단정리:` 로 찾으면 안 된다 — `BRIEF_HEADER` 의 안내문에도 그 글자가 있어서
   * 그쪽이 먼저 걸린다(이 검사를 처음 쓸 때 실제로 그렇게 걸렸다). 색인 줄만 가진 것은
   * **`d.preamble ?` 삼항**이다. */
  const docLine = briefSrc.split('\n').find((l) => l.includes('d.preamble ?'));
  if (!docLine) {
    fail('documents/brief.js 에서 색인의 머리말 줄(`d.preamble ?`)을 못 찾았습니다'
      + ' — 머리말이 색인에서 통째로 빠졌거나 모양이 바뀌었습니다. 바꾼 것이 맞으면 이 절도 함께 고치세요');
  } else {
    eq('  색인의 머리말 줄이 preambleOutline 을 부른다',
      /preambleOutline\(/.test(docLine), true);
  }
}

if (!hasDocuments()) {
  console.log('[3/6] 실물 아카이브');
  console.log('  [못잼] 문서 아카이브가 설정되어 있지 않습니다');
  process.exit(ok ? 0 : 1);
}

console.log('[3/6] 실물 아카이브 — 머리말이 문서 검색에 잡히나 (대조군과 함께)');
const withPre = [];
{
  const wordsOf = (s) => new Set(s.toLowerCase().match(/[가-힣a-z0-9][가-힣a-z0-9.,%]{3,}/g) || []);
  let preChars = 0;
  let entryTotal = 0;
  let entryMismatch = 0;
  let fieldMismatch = 0;
  for (const d of listDocuments()) {
    if (d.broken) continue;
    entryTotal += d.entries.length;
    /* ④ — 파일을 다시 쪼개서 개수를 댄다. 머리말을 `entries` 에 끼우면 여기서 걸린다. */
    if (d.entries.length !== splitMessages(readCached(d.abs)).length) entryMismatch += 1;
    /* **머리말이 있나는 파일에서 직접 잰다 — `d.preamble` 을 믿지 않는다.**
     * 믿으면 그 필드를 지우는 것만으로 아래가 전부 「머리말 있는 문서 0개 → 못 쟀습니다」가
     * 되어 **초록으로 통과한다.** 고치기 전 상태가 바로 그 모양이라, 그러면 이 검사는
     * 자기가 막으려던 것을 못 막는다. */
    const pre = preambleOf(readCached(d.abs));
    if ((d.preamble || '') !== pre) fieldMismatch += 1;
    if (!pre) continue;
    const body = d.entries.map((e) => e.text).join('\n').toLowerCase();
    /* 낱말은 **그 문서의 어느 회차에도 없는** 것으로 고른다. 머리말 헤딩을 그대로 질의로
     * 넣으면 그 낱말이 본문에도 흔해서, 고치기 전에도 그 문서가 나온다 — 그러면 이 검사는
     * 통과하면서 아무것도 안 재게 된다 (채널 쪽 check-preamble.js 가 실제로 그랬다).
     * 「없다」의 기준은 검색과 같은 부분 문자열이다(`scoreTerms` 의 `includes`). */
    const preOnly = [...wordsOf(pre)].filter((w) => !body.includes(w));
    const bodyOnly = [...wordsOf(body)].filter((w) => !pre.toLowerCase().includes(w));
    withPre.push({ d, pre, preOnly, bodyOnly });
    preChars += pre.length;
  }
  console.log(`  - 잰 것: 문서 ${listDocuments().length}개 · 머리말 있는 문서 ${withPre.length}개 `
    + `합계 ${preChars}자 · 회차 ${entryTotal}개`);

  if (entryMismatch) {
    fail(`회차 수가 splitMessages 와 다른 문서가 ${entryMismatch}건 있습니다 — 머리말이 entries 에 섞였습니다`);
  } else {
    pass(`회차 수가 splitMessages 그대로다 (문서 ${listDocuments().length}개)`);
  }
  if (fieldMismatch) {
    fail(`loadDocument 의 preamble 이 preambleOf 와 다른 문서가 ${fieldMismatch}건 있습니다`
      + ' — 검색·색인이 그 필드를 쓰므로 여기가 비면 두 경로 모두에서 머리말이 사라집니다');
  } else {
    pass(`loadDocument 가 머리말을 preambleOf 그대로 들고 있다 (문서 ${listDocuments().length}개)`);
  }
}

if (!withPre.length) {
  console.log('  [못잼] 이 아카이브에는 머리말이 있는 문서가 없습니다 (샐 것이 없습니다)');
} else {
  const idx = (x) => withPre.indexOf(x) + 1;   // 사업장·문서 이름을 화면에 안 적는다
  let missSearch = 0;
  let missControl = 0;
  let unmeasured = 0;
  let unmeasuredControl = 0;
  const hitsFor = (q, x) => searchDocuments({
    query: q, project: x.d.project, document: x.d.slug, access: FULL_ACCESS,
  }).hits.length;

  for (const x of withPre) {
    // ① 머리말에만 있는 낱말
    if (!x.preOnly.length) { unmeasured += 1; } else if (!hitsFor(x.preOnly[0], x)) {
      missSearch += 1;
      fail(`머리말에만 있는 낱말('${x.preOnly[0]}')로 검색했는데 그 문서가 안 나옵니다 (문서 ${idx(x)})`);
    }
    // ⑤ 대조군 — 본문에만 있는 낱말. 이게 함께 빨가면 머리말이 아니라 검색이 죽은 것이다.
    if (!x.bodyOnly.length) { unmeasuredControl += 1; } else if (!hitsFor(x.bodyOnly[0], x)) {
      missControl += 1;
      fail(`대조군 실패 — 본문에만 있는 낱말('${x.bodyOnly[0]}')로도 그 문서가 안 나옵니다 (문서 ${idx(x)})`
        + ' — 머리말이 아니라 문서 검색 자체를 먼저 보세요');
    }
  }
  if (!missSearch) pass(`머리말에만 있는 낱말로 검색하면 그 문서가 나온다 (${withPre.length - unmeasured}개 문서)`);
  if (!missControl) pass(`대조군 — 본문에만 있는 낱말도 그대로 나온다 (${withPre.length - unmeasuredControl}개 문서)`);
  if (unmeasured) console.log(`  [못잼] 검색을 ${unmeasured}개 문서에서 못 쟀습니다 — 머리말에만 있는 낱말을 못 찾았습니다`);
  if (unmeasuredControl) console.log(`  [못잼] 대조군을 ${unmeasuredControl}개 문서에서 못 쟀습니다 — 본문에만 있는 낱말을 못 찾았습니다`);

  console.log('[4/6] 실물 아카이브 — 색인에 머리말 **제목**이 실리나');
  const brief = buildDocumentsBrief({ access: FULL_ACCESS });
  {
    let miss = 0;
    let measured = 0;
    for (const x of withPre) {
      /* 색인에는 헤딩 글자만 실린다 (`archive.js` 의 `preambleOutline`) — `#` 은 떼고 싣는다.
       * **여기서 그 함수를 부르지 않는 것은 일부러다** — 검사가 재는 대상을 그대로
       * 가져다 쓰면 그 함수가 통째로 틀려도 같이 틀려서 늘 통과한다. 첫 줄만 보는
       * 약한 잣대를 따로 두는 이유가 이것이다.
       * 그래서 `## ` 를 붙인 채로 찾으면 안 된다. */
      const head = x.pre.split('\n')[0].trim().replace(/^#+\s+/, '');
      /* 접힌 문서(`그 외 N건`)는 줄 자체가 없으므로 제목도 없는 것이 맞다 — 건너뛴다.
       * 접힘은 예산이 정하는 것이라 여기서 실패로 세면 문서가 늘 때마다 빨개진다. */
      if (!brief.includes(x.d.title)) continue;
      measured += 1;
      if (!brief.includes(head)) {
        miss += 1;
        fail(`색인에 그 문서 줄은 있는데 머리말 제목이 없습니다 (문서 ${idx(x)})`);
      }
    }
    /* **0건이면 통과가 아니다.** 전부 접혀서 잰 것이 하나도 없으면 이 절은 아무것도 안 본
     * 것이고, 그것을 초록으로 두면 색인 경로가 통째로 죽어도 여기는 조용하다. */
    if (!measured) {
      fail(`색인에 머리말 있는 문서가 한 줄도 안 실려 잴 것이 없었습니다 (${withPre.length}개 전부 접힘)`);
    } else if (!miss) {
      pass(`색인에 실린 문서에는 머리말 제목이 함께 있다 (안 접힌 ${measured}개 / 머리말 있는 ${withPre.length}개)`);
    }
  }

  console.log('[5/6] 반대 방향 — 색인에 머리말 **본문(숫자)** 은 안 실린다');
  {
    let leak = 0;
    let unmeasuredLeak = 0;
    for (const x of withPre) {
      /* 재는 낱말은 「머리말 본문에만 있고 헤딩에도 회차에도 없는 것」으로 고른다.
       * 헤딩에 있는 낱말로 재면 제목이 실린 것만으로 「샜다」가 되어 늘 빨갛다. */
      const headHay = x.pre.split('\n')
        .filter((l) => /^#{2,6}\s/.test(l.trim())).join(' ').toLowerCase();
      const bodyOnlyOfPre = x.preOnly.filter((w) => !headHay.includes(w));
      if (!bodyOnlyOfPre.length) { unmeasuredLeak += 1; continue; }
      if (brief.includes(bodyOnlyOfPre[0])) {
        leak += 1;
        fail(`색인에 머리말 본문 글자('${bodyOnlyOfPre[0]}')가 실려 있습니다 — 제목만 실어야 합니다 (문서 ${idx(x)})`);
      }
    }
    if (!leak) pass(`색인에 머리말 본문(숫자)은 안 실린다 (${withPre.length - unmeasuredLeak}개 문서)`);
    if (unmeasuredLeak) {
      console.log(`  [못잼] 새는지를 ${unmeasuredLeak}개 문서에서 못 쟀습니다 — 헤딩 밖에만 있는 낱말을 못 찾았습니다`);
    }
  }

  console.log('[6/6] 열어서 읽을 수 있나 — read_document 가 머리말을 준다 (반대 방향 둘과 함께)');
  {
    /* **여기서는 낱말을 안 고른다.** ①⑤ 는 「그 구간에만 있는 낱말」이 있어야 재는데,
     * 실물에서는 그런 낱말이 없는 문서가 많아 [못잼] 이 여럿 난다. 「열어서 보이나」는
     * 머리말 글자를 **그대로 대 보면** 되므로 그 한계를 안 물려받는다 — 머리말이 있는
     * 문서는 하나도 안 빠지고 재진다.
     *
     * 훑는 대상을 좁히는 것은 시간 때문이다. 문서 431개를 전부 열면 26초가 든다(실측).
     * 머리말이 있거나 목차 갈래로 갈 수 있는 문서만 연다 — 나머지는 전문이 그대로
     * 오는 갈래라 머리말이 빠질 자리가 없다. */
    const cands = [];
    for (const d of listDocuments()) {
      if (d.broken) continue;
      const text = readCached(d.abs);
      const pre = preambleOf(text);
      if (!pre && !outlineOf(text)) continue;
      cands.push({ d, pre });
    }

    let missOpen = 0;      // 머리말이 있는데 열어도 안 온다
    let clipped = 0;       // 예산에 걸려 잘렸다 (잘린 사실이 적혀 있으면 통과)
    let stuckOn = 0;       // 머리말이 없는데 뭔가 앞에 붙었다 (반대 방향 ①)
    let outlineHurt = 0;   // 목차 본체가 상했다 (반대 방향 ②)
    let openedPre = 0;
    let outlines = 0;
    cands.forEach((c, i) => {
      const r = readDocument({ project: c.d.project, document: c.d.slug, access: FULL_ACCESS });
      if (r.error) { missOpen += 1; fail(`read_document 가 오류를 냈습니다 (후보 ${i + 1}): ${r.error}`); return; }

      // 목차 본체가 있으면 그 첫 줄 자리를 잡아 둔다 — 「앞에 붙었나」의 기준선이다.
      const lines = r.text.split('\n');
      const at = lines.findIndex((l) => l.includes('커서 목차만 실었습니다'));
      if (r.outline) {
        outlines += 1;
        const body = at === -1 ? '' : lines.slice(at).join('\n');
        // 목차는 「다음에 무엇을 부를까」의 유일한 안내다. 붙이느라 이게 상하면 안 된다.
        //
        // **줄 수만 세면 안 된다** (회의적 검증 2026-09-11) — 예전에는 `→ ` 로 시작하는
        // 줄이 몇 개인지만 셌다. 그래서 절 갈래에서 「고르라」 줄이 통째로 사라져도
        // (다른 줄이 하나 늘면) 개수가 3→2 로 그대로라 초록이었다. 줄마다 **정체**를
        // 센다 — 「고르라」 정확히 1개 · search 안내 정확히 1개 · section 갈래일 때만
        // 「어느 절에도 안 듭니다」 0개 또는 1개(renderOutline, 2026-09-10).
        const pickLines = (body.match(/^→ .+ 로 하나를 지정해 다시 부르세요\.$/gm) || []).length;
        const searchLines = (body.match(/^→ 문서 전체에서 낱말로 찾으려면 search 에 이 문서를 지정하세요/gm) || []).length;
        const beforeLines = (body.match(/^→ 앞 [\d,]+자는 어느 절에도 안 듭니다/gm) || []).length;
        const arrowsOk = pickLines === 1 && searchLines === 1
          && (beforeLines === 0 || (r.outline === 'section' && beforeLines === 1));
        if (at === -1 || !arrowsOk) {
          outlineHurt += 1;
          fail(`목차 본체가 상했습니다 (후보 ${i + 1}) — 「고르라」 줄 ${pickLines}개 · search 안내 줄 ${searchLines}개`
            + ` · 「어느 절에도 안 듭니다」 줄 ${beforeLines}개 (머리말을 붙이느라 줄이 사라지거나 늘면 안 됩니다)`);
          return;
        }
      }

      if (!c.pre) {
        // 반대 방향 ① — 머리말이 없는 문서에는 아무것도 안 붙는다.
        if (r.outline && at !== 0) {
          stuckOn += 1;
          fail(`머리말이 없는데 목차 앞에 뭔가 붙었습니다 (후보 ${i + 1})`
            + ` — 첫 줄: ${JSON.stringify(lines[0].slice(0, 40))}`);
        }
        return;
      }

      openedPre += 1;
      if (r.text.includes(c.pre)) {
        // 붙었으면 **목차보다 앞**이어야 한다. 뒤에 붙으면 봇이 목차를 읽고 멈춘다.
        if (r.outline && r.text.indexOf(c.pre) > r.text.indexOf('커서 목차만 실었습니다')) {
          missOpen += 1;
          fail(`머리말이 목차 **뒤**에 붙었습니다 (후보 ${i + 1}) — 앞에 붙여야 합니다`);
        }
        return;
      }
      /* 예산에 걸려 잘린 경우. **잘렸다는 사실이 글에 적혀 있으면 통과다** — 조용히
       * 자르면 봇이 「이게 전부」로 읽는다. 실물에서는 아직 한 번도 안 걸린다. */
      if (r.text.includes('앞부분만 실었습니다') && r.text.includes(c.pre.slice(0, 100))) {
        clipped += 1;
        return;
      }
      missOpen += 1;
      fail(`머리말이 있는데 read_document 로 열어도 안 옵니다 (후보 ${i + 1} · 머리말 ${c.pre.length}자`
        + `${r.outline ? ' · 목차 갈래' : ''}) — 큰 문서는 이 길 말고 여는 길이 없습니다`);
    });

    console.log(`  - 잰 것: 연 문서 ${cands.length}개 (그중 목차 갈래 ${outlines}개) · 머리말 있는 것 ${openedPre}개`);
    if (!missOpen) pass(`머리말 있는 문서 ${openedPre}개가 전부 read_document 로 열린다`
      + `${clipped ? ` (그중 ${clipped}개는 예산에 걸려 잘렸고, 잘린 사실이 적혀 있다)` : ''}`);
    if (!stuckOn) pass(`반대 방향 — 머리말이 없는 문서 ${cands.length - openedPre}개에는 아무것도 안 붙는다`);
    if (!outlineHurt) pass(`반대 방향 — 목차 본체 ${outlines}개가 안 잘렸다`);
    if (!openedPre) fail('머리말 있는 문서를 하나도 안 열었습니다 — 이 절이 아무것도 안 쟀습니다');
  }

  // 공통 블록은 권한과 무관해야 한다 (캐시). 자세한 것은 check-brief-split.js.
  const split = buildDocumentsBriefSplit({ access: FULL_ACCESS });
  const pub = buildDocumentsBriefSplit({ access: PUBLIC_ACCESS });
  eq('  공통 블록이 공개/전체에서 같다 (캐시가 깨지지 않았다)', split.common === pub.common, true);
  console.log(`  - 색인 크기: 공통 ${split.common.length}자 / 추가 ${split.extra.length}자`
    + ' (바닥과 상한은 `npm run check` 의 [4/6] 이 잰다)');
}

if (ok) {
  console.log('[check-doc-preamble] OK — 문서 md 첫 회차 앞의 사람 정리가 검색·색인·열어 읽기에 다 실립니다.');
} else {
  console.error('\n고칠 곳: src/documents/store.js 의 loadDocument() · src/documents/search.js 의 scanDocuments() · src/documents/brief.js 의 buildDocumentsBrief()'
    + ' · src/documents/read.js 의 readDocument()/outlineWithPreamble()');
  process.exitCode = 1;
}
