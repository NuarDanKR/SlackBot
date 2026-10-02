#!/usr/bin/env node
/**
 * 첨부 「본문 수록」 표시 점검 — 실제 아카이브를 상대로 돈다.
 *
 * 왜 있나: 채널 md 의 `📎 첨부: \`파일명.pdf\`` 줄에는 그 파일의 본문이 문서 아카이브에
 * 들어 있는지가 한 글자도 안 적혀 있었다. 봇은 이름만 보고 「본문은 첨부 안에」로 읽었고,
 * 2026-08-18 에 실제로 그렇게 답했다 — 두 파일 다 전문이 아카이브에 있는데도.
 * 채널 md 의 첨부 언급 626건 중 394건(62.9%)이 이미 변환돼 있으므로, 표시가 없으면
 * 그런 문장은 열에 여섯 틀린다. 2026-08-06 에 있었던 사고와 같은 종류다.
 *
 * ⑤ 는 **실물 아카이브에 던지는 질의**라 팀마다 다르다. 질의는 자료 저장소의
 * `check-fixtures.json` 에 있고 (`scripts/fixtures.js` 참조), 없으면 그 항목만 건너뛴다.
 *
 * 보는 것 일곱:
 *  ① 회차 헤더에서 원본 파일명을 뽑는 규칙이 엑셀 헤더에서도 맞다 (픽스처 — 아카이브 불필요)
 *  ② 변환돼 있는 첨부에는 표시가 붙는다
 *  ③ 변환 안 된 첨부에는 안 붙는다 (붙으면 없는 본문을 있다고 말하게 된다)
 *  ④ 볼 수 없는 비공개 문서는 표시하지 않는다 — 표시 자체가 「그 자료가 있다」는 누설이다
 *  ⑤ 2026-08-18 그 두 파일이 실제로 표시된다 (사고 재현)
 *  ⑥ llm/tools.js 가 대화 검색과 read_channel 양쪽에서 실제로 부른다
 *  ⑦ **실물 채널 md 의 첨부 줄**이 그대로 지나간다 — ②③은 문서 쪽 이름으로 줄을 만들어
 *     재므로 두 아카이브 사이에서 글자가 갈리는 부류(정규화 형태)를 원리상 못 잡는다
 *
 * 실행: node scripts/check-attachment-marks.js
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { searchArchive, assertArchive } from '../src/archive.js';
import {
  markArchivedAttachments, listDocuments, hasDocuments, ARCHIVED_MARK, entryFileName,
} from '../src/documents.js';
import { PUBLIC_ACCESS, FULL_ACCESS, CHANNELS_DIR } from '../src/config.js';
import { loadFixtures, skipNote } from './fixtures.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

/**
 * 실물 채널 md 의 `📎 첨부:` 줄을 전부 모은다 — `{ channel, line, names[] }`.
 *
 * **가짜 줄을 만들지 않으려고 있다.** 이름을 코드에서 만들어 재면 대는 양쪽이 같은
 * 문자열이라, 두 아카이브 사이에서 글자가 갈리는 부류(정규화 형태·공백·꼬리)를
 * 원리상 못 잡는다. 아래 [4/7]·[7/7] 이 이 함수를 쓴다.
 */
function channelAttachmentLines() {
  const out = [];
  for (const f of fs.readdirSync(CHANNELS_DIR)) {
    if (!f.endsWith('.md')) continue;
    const text = fs.readFileSync(path.join(CHANNELS_DIR, f), 'utf8');
    for (const line of text.split(/\r?\n/)) {
      if (!line.includes('📎 첨부:')) continue;
      const names = [...line.matchAll(/`([^`]+)`/g)].map((m) => m[1].trim());
      if (names.length) out.push({ channel: f.replace(/\.md$/, ''), line, names });
    }
  }
  return out;
}

/** 그 파일 이름이 적힌 실물 첨부 줄 하나. 없으면 null — 「없다」가 아니라 「못 잰다」다. */
function channelAttachmentLine(name) {
  const want = name.normalize('NFC');
  const hit = channelAttachmentLines().find((r) => r.names.some((n) => n.normalize('NFC') === want));
  return hit ? hit.line : null;
}

/* 파일명 뽑기는 `documents.js` 의 `entryFileName` 을 **그대로 가져다 쓴다** — 여기 사본을
 * 두면 안 된다. 예전 사본은 엑셀 헤더의 `— 시트 n/N: 이름` 꼬리를 몰랐는데, 표시를 붙이는
 * 쪽도 똑같이 몰라서 **둘이 같이 틀린 채로 검사가 초록이었다** (2026-09-03). 사본을 되돌리면
 * 아래 [2/7] 이 다시 자기 자신을 재게 되므로, 그때는 [1/7] 픽스처만이 규칙을 지킨다.
 *
 * [1/7] 은 **아카이브 없이도 돈다** — 픽스처만 쓰는 순수 함수 시험이라 아래 건너뛰기보다
 * 위에 둔다. 이 성질을 실물에만 기대어 재면, 엑셀이 아직 없는 팀에서는 규칙이 깨져도
 * 아무 줄도 빨개지지 않는다. */
console.log('[1/7] 회차 헤더에서 원본 파일명을 뽑는 규칙이 엑셀 헤더에서도 맞다');
{
  const SRC = '260805_시험_산정내역.xlsx';
  const CASES = [
    ['일반 문서', `**2026-08-05 · 260805_보고.hwp**\n\n본문`, '260805_보고.hwp'],
    // 엑셀은 파일명 뒤에 `— 시트 n/N: 이름` 이 붙는다 (— 는 em dash U+2014, 앞뒤로 보통 공백).
    // 채널 md 의 `📎 첨부: \`파일명\`` 에는 이 꼬리가 없으므로 떼고 맞춰야 한다.
    ['엑셀 시트 1', `**2026-08-05 · ${SRC} — 시트 1/3: 산정내역**\n\n표`, SRC],
    ['엑셀 시트 10/12', `**2026-08-05 · ${SRC} — 시트 10/12: 2023년 결산.미지급이자**\n\n표`, SRC],
    // 파일명 안에 괄호·공백·별표가 아닌 특수문자가 섞여도 꼬리만 떨어져야 한다 (실물에 있다).
    ['괄호 든 엑셀', '**2026-06-24 · 260624_수금 현황(6월말)_취합.xlsx — 시트 1/2: 상세**', '260624_수금 현황(6월말)_취합.xlsx'],
  ];
  for (const [label, text, want] of CASES) {
    const got = entryFileName({ text });
    if (got === want) ok(`${label} → '${got}'`);
    else bad(`${label}: '${got}' 가 나왔습니다 — '${want}' 여야 합니다`);
  }
}

// check-partial-hits.js 와 같은 이유로 아카이브가 없는 새 클론에서는 조용히 건너뛴다.
try {
  assertArchive();
} catch (e) {
  console.log(`  - 건너뜀: ${e.message.split('\n')[0]}`);
  process.exit(failed ? 1 : 0);
}
if (!hasDocuments()) {
  console.log('  - 건너뜀: 문서 아카이브가 없습니다');
  process.exit(failed ? 1 : 0);
}

console.log('[2/7] 변환돼 있는 첨부에는 표시가 붙는다 — 공개 문서 전부');
{
  /* **한 건만 보지 않는다.** 예전에는 `.find(Boolean)` 으로 맨 앞 문서 하나만 봤는데,
   * 훑는 순서가 사업장 이름순이라 그 한 건은 늘 엑셀이 아니었다. 그래서 엑셀 첨부에
   * 표시가 영영 안 붙던 2026-09-03 결함을 이 검사가 통째로 못 봤다 — 실물 50자리다.
   * 전부 보면 확장자 한 종류가 통째로 빠지는 부류를 다음에 잡는다. */
  const names = [...new Set(
    listDocuments().filter((d) => !d.private).flatMap((d) => d.entries.map(entryFileName)).filter(Boolean),
  )];
  // 문서를 아직 하나도 안 넣은 저장소는 **잴 것이 없는 것**이지 고장이 아니다.
  // 여기서 실패로 내면 아카이브를 채우기 전인 팀이 매일 빨간 줄을 본다.
  if (!names.length) console.log('  - 건너뜀: 공개 문서가 하나도 없어 잴 것이 없습니다');
  else {
    // 한 줄씩 부르면 이름마다 문서 전체를 다시 훑는다 — 한 덩이로 만들어 한 번만 부른다.
    const marked = markArchivedAttachments(names.map((n) => `📎 첨부: \`${n}\``).join('\n'), PUBLIC_ACCESS)
      .split('\n');
    const missed = names.filter((n, i) => !marked[i].includes(ARCHIVED_MARK));
    const extOf = (n) => (n.match(/\.([A-Za-z0-9]+)$/) || [, '(확장자 없음)'])[1].toLowerCase();
    if (!missed.length) ok(`공개 문서의 원본 파일명 ${names.length}건 전부 표시 붙음`);
    else {
      const byExt = {};
      for (const n of missed) byExt[extOf(n)] = (byExt[extOf(n)] || 0) + 1;
      bad(`변환돼 있는데 표시가 안 붙는 이름 ${missed.length}/${names.length}건 `
        + `(${Object.entries(byExt).map(([k, v]) => `${k} ${v}`).join(' · ')}) — 예: ${missed.slice(0, 3).join(' / ')}`);
    }
  }
}

console.log('[3/7] 변환 안 된 첨부에는 안 붙는다');
{
  const line = '📎 첨부: `zzqqxx 그런파일 없음.xlsx`';
  const out = markArchivedAttachments(line, FULL_ACCESS);
  if (out === line) ok('없는 파일명 → 표시 안 붙음');
  else bad(`변환된 적 없는 이름에 표시가 붙었습니다: ${out}`);
}

console.log('[4/7] 볼 수 없는 비공개 문서는 표시하지 않는다');
{
  // **공개 문서에도 같은 이름이 있으면 표시되는 것이 맞다.** 같은 파일을 공개 채널과
  // 비공개 채널에 함께 올리는 일이 실제로 있어서(2026-08 실측 — 그 파일 이름은 자료
  // 저장소 `사고기록.md` 의 「같은 파일이 공개·비공개 양쪽에 있다」 절), 그런 이름으로 시험하면
  // 멀쩡한 동작을 누설로 읽는다. 비공개에만 있는 이름을 골라야 이 성질을 실제로 잰다.
  const docs = listDocuments();
  const visible = new Set(docs.filter((d) => !d.private).flatMap((d) => d.entries.map(entryFileName)));
  const name = docs.filter((d) => d.private)
    .flatMap((d) => d.entries.map(entryFileName))
    .find((n) => n && !visible.has(n));
  if (!name) console.log('  [못잼] 비공개에만 있는 첨부가 없어 이 항목은 시험할 수 없습니다');
  else {
    /* **실물 채널 md 의 그 줄을 그대로 통과시킨다.** 예전에는 문서 쪽 이름으로
     * `📎 첨부: \`이름\`` 을 만들어 재서, 대는 양쪽이 **같은 문자열**이라 늘 통과했다 —
     * 자기를 재고 있었던 것이다. 그 상태로 2026-09-03 의 정규화 어긋남(문서 쪽 이름
     * 하나가 조합형이라 `names.has` 가 false 였다)을 통째로 못 봤고, 하필 이 항목이
     * 고르는 파일이 바로 그 파일이었다.
     * 실물 줄이 없으면 「없다」가 아니라 **못 잰 것**이다. */
    const line = channelAttachmentLine(name);
    if (!line) console.log(`  [못잼] '${name}' 을 적은 \`📎 첨부:\` 줄이 채널 md 에 없습니다`);
    else {
      const pub = markArchivedAttachments(line, PUBLIC_ACCESS);
      const full = markArchivedAttachments(line, FULL_ACCESS);
      if (pub.includes(ARCHIVED_MARK)) bad(`공개 권한에 비공개 문서 '${name}' 이 표시됐습니다 — 존재가 샙니다`);
      else if (!full.includes(ARCHIVED_MARK)) bad(`전체 권한에서도 '${name}' 이 표시되지 않았습니다 (실물 채널 md 줄)`);
      else ok(`비공개 문서 '${name}' — 실물 채널 md 줄로 재서 공개 권한엔 표시 없음 · 전체 권한엔 표시됨`);
    }
  }
}

console.log('[5/7] 2026-08-18 사고 재현 — 그 두 파일이 표시된다');
{
  // 질의·채널·파일 이름은 **그 팀의 실물**이라 자료 저장소에서 읽는다. 셋이 한 벌이다.
  const F = loadFixtures('attachmentMarks', ['query', 'channel']);   // want 는 아래에서 따로 본다
  // 채널의 **첫** 히트가 아니라 볼 파일 이름이 든 히트를 고른다 — 같은 질의에 걸리는
  // 새 메시지가 들어오면 첫 히트가 바뀌어, 표시는 멀쩡한데 이 검사만 빨개졌다 (2026-09-30).
  const hit = !F ? null : searchArchive({ query: F.query, access: PUBLIC_ACCESS })
    .hits.find((h) => h.channel === F.channel && (F.want || []).some((n) => h.text.includes(n)));
  if (!F) console.log(skipNote('attachmentMarks'));
  else if (!hit) bad(`'${F.query}' 에서 #${F.channel} 히트가 안 나왔습니다 — 아카이브가 예상과 다릅니다`);
  else {
    const WANT = F.want || [];
    const marked = markArchivedAttachments(hit.text, PUBLIC_ACCESS);
    // 이름 바로 뒤에 표시가 붙었는지까지 본다. 줄 어딘가에 있기만 한 것으로는
    // 「두 파일 중 하나만 붙었다」를 못 잡는다.
    const missing = WANT.filter((n) => !marked.includes(`\`${n}\` ${ARCHIVED_MARK}`));
    // 볼 이름이 하나도 없으면 「전부 붙었다」가 아니라 **아무것도 안 잰 것**이다.
    if (!WANT.length) bad('질의 뭉치의 attachmentMarks.want 가 비어 있어 아무것도 재지 못했습니다');
    else if (missing.length) bad(`표시가 안 붙은 파일 ${missing.length}건: ${missing.join(' / ')}`);
    else ok(`첨부 ${WANT.length}건 모두 표시됨`);
  }
}

console.log('[6/7] llm/tools.js 가 대화 검색과 read_channel 양쪽에서 부른다');
{
  const here = path.dirname(fileURLToPath(import.meta.url));
  const src = fs.readFileSync(path.join(here, '..', 'src', 'llm', 'tools.js'), 'utf8');
  // 한쪽만 부르면 나머지 경로는 조용히 옛 동작으로 남는다 — check-shared-rules.js 가
  // 두 언어의 봇 판정을 함께 보는 것과 같은 이유다.
  const n = (src.match(/markArchivedAttachments\s*\(/g) || []).length;
  if (n >= 2) ok(`llm/tools.js 에서 ${n}곳이 부릅니다`);
  else bad(`llm/tools.js 에서 ${n}곳만 부릅니다 — 대화 검색과 read_channel 둘 다여야 합니다`);
}

console.log('[7/7] 실물 채널 md 의 첨부 줄이 그대로 지나간다 (두 아카이브의 글자가 갈렸나)');
{
  /* 위 [2/7]·[3/7] 은 **문서 쪽 이름으로 만든 줄**을 잰다. 그래서 두 아카이브 사이에서
   * 글자가 갈리는 부류를 원리상 못 잡는다 — 한글은 같은 글자를 완성형과 조합형 두 가지로
   * 적을 수 있고, 눈으로는 똑같은데 문자열 비교가 false 다. 실측 2026-09-03: 채널 md
   * 첨부 772자리 중 **1자리**(문서 쪽 이름이 조합형)가 그렇게 표시를 잃고 있었고,
   * [2/7]~[4/7] 어느 것도 안 걸렸다.
   *
   * 그래서 여기서는 **채널 md 의 줄을 그대로** 통과시키고, 붙어야 할 곳에 붙었는지를
   * 「NFC 로 맞췄을 때 문서에 있나」로 판정한다. 표시 규칙이 정규화를 안 하면 빨개진다. */
  const rows = channelAttachmentLines();
  const docNames = new Set(
    listDocuments().flatMap((d) => d.entries.map(entryFileName)).filter(Boolean)
      .map((n) => n.normalize('NFC')),
  );
  if (!rows.length) console.log('  [못잼] 채널 md 에 `📎 첨부:` 줄이 하나도 없습니다');
  else if (!docNames.size) console.log('  [못잼] 문서 회차에서 원본 파일명을 하나도 못 뽑았습니다');
  else {
    // 한 덩이로 한 번만 부른다 — 줄마다 부르면 문서 전체를 그때마다 다시 훑는다.
    const marked = markArchivedAttachments(rows.map((r) => r.line).join('\n'), FULL_ACCESS).split('\n');
    let should = 0;
    let seats = 0;
    const missed = [];
    const extra = [];
    rows.forEach((r, i) => {
      for (const n of r.names) {
        seats += 1;
        const want = docNames.has(n.normalize('NFC'));
        const got = marked[i].includes(`\`${n}\` ${ARCHIVED_MARK}`);
        if (want) should += 1;
        if (want && !got) missed.push(`#${r.channel} · ${n}`);
        if (!want && got) extra.push(`#${r.channel} · ${n}`);
      }
    });
    if (!should) console.log('  [못잼] 채널 md 첨부 이름 중 문서에 있는 것이 하나도 없습니다');
    else if (missed.length) {
      bad(`실물 첨부 줄 ${missed.length}/${should}자리에 표시가 안 붙습니다 — 두 아카이브의 글자가 갈렸습니다`
        + ` (같은 이름인데 정규화 형태가 다르면 여기가 걸립니다). 예: ${missed.slice(0, 3).join(' / ')}`);
    } else if (extra.length) {
      bad(`문서에 없는 이름 ${extra.length}자리에 표시가 붙었습니다 — 없는 본문을 있다고 말하게 됩니다.`
        + ` 예: ${extra.slice(0, 3).join(' / ')}`);
    } else {
      ok(`실물 첨부 ${rows.length}줄 · 붙어야 할 ${should}자리 전부 붙고 나머지엔 안 붙음`);
      console.log(`  [보임] 채널 md 첨부 언급 ${seats}자리 중 수록 ${should}자리`);
    }
  }

  /* **반대 방향 — 채널 쪽이 조합형으로 적혀 있어도 붙나.**
   *
   * 위 실물 대조는 「문서 쪽이 조합형」인 경우만 잰다. 지금 아카이브의 채널 md 이름은
   * 전부 완성형이라, **붙이는 쪽(`markArchivedAttachments`)의 정규화를 빼도 실물로는
   * 아무 줄도 안 빨개진다** (2026-09-03 실측). 그쪽이 죽은 것을 잡을 자리가 없으면
   * 두 곳 중 하나만 고친 상태가 조용히 굳는다 — 이 결함이 처음 생긴 방식이 그것이다.
   *
   * 그래서 여기서만 줄을 만든다. 자기를 재는 것이 아니다 — 대는 두 값의 정규화 형태를
   * **일부러 어긋내서** 넣기 때문이다. 이름 자체는 실물에서 가져온다. */
  const nfdName = [...docNames].find((n) => n.normalize('NFD') !== n);
  if (!nfdName) console.log('  [못잼] 조합형으로 바꿔 볼 수 있는 이름(한글)이 문서에 없습니다');
  else {
    const line = `📎 첨부: \`${nfdName.normalize('NFD')}\``;
    if (markArchivedAttachments(line, FULL_ACCESS).includes(ARCHIVED_MARK)) {
      ok('채널 md 가 조합형으로 적혀 있어도 표시가 붙는다 (붙이는 쪽 정규화)');
    } else {
      bad('채널 md 이름이 조합형이면 표시가 안 붙습니다 — markArchivedAttachments 가 NFC 로 안 맞춥니다');
    }
  }
}

process.exit(failed ? 1 : 0);
