#!/usr/bin/env node
/**
 * 채널 md 첫 메시지 앞의 「사람이 손으로 적어 둔 정리」가 검색과 색인 **양쪽에** 실리나.
 *
 *   node scripts/check-preamble.js
 *
 * ── 왜 필요한가 (2026-09-03) ──
 *
 * `splitMessages` 는 첫 `**날짜 …**` 헤더를 만나야 블록을 열기 때문에, 그 앞의 줄은 어느
 * 블록에도 안 들어갔다. 검색(`scanArchive`)이 그 블록만 훑으므로 그 구간은 **영원히 0건**,
 * 색인(`channelBrief`)은 상단 `>` 인용줄만 가져오므로 거기에도 없었다. 즉 사람이 가장
 * 공들여 정리한 표가 봇에게는 두 경로 모두에서 존재하지 않았다 — **에러는 안 난다.**
 * 실측 2026-09-03: 채널 md 42개 중 6개에 그런 구간이 있고 합계 2,547자였다.
 *
 * 그래서 이 검사는 「0건이 아니게 됐다」가 아니라 **네 가지가 동시에 지켜지나**를 본다.
 *
 *   ① 머리말에만 있는 낱말이 검색에 잡힌다  ② 그것이 색인에도 실린다
 *   ③ 월 헤딩·파일 제목·상단 메타는 머리말로 안 샌다 (사람이 쓴 헤딩과 갈라야 한다)
 *   ④ `splitMessages` 의 블록 수는 **하나도 안 변한다** — 그 수로 회차를 세는 자리가
 *      열 곳이 넘어(`documents.js`·`ingest/verify.js`·`check-excel-sheets.js`·
 *      `check-doc-outline.js`), 여기에 가상 블록을 끼우면 그 수가 전부 하나씩 틀어진다
 *
 * 앞부분은 **가짜 채널 md 로** 본다 — 이 저장소는 팀끼리 나눠 써서 검사 코드에 진짜
 * 사업장 이름·숫자를 적을 수 없다(`check-business-names.js`). 실물 아카이브는 뒤에서
 * 「머리말이 있는 채널이 하나라도 검색·색인에 살아 있나」만 세어 본다.
 */
import {
  preambleOf, splitMessages, searchArchive, buildArchiveBrief, buildArchiveBriefSplit,
  assertArchive, listReadableChannels, readCached,
} from '../src/archive.js';
import { FULL_ACCESS, PUBLIC_ACCESS, CHANNELS_DIR } from '../src/config.js';
import path from 'node:path';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);
const eq = (label, got, want) => (got === want ? pass(label) : fail(`${label} — 받은 값 ${JSON.stringify(got)}`));

/* ── 가짜 채널 md. 실물과 같은 관례를 그대로 흉내낸다 ── */
const FIXTURE = [
  '# #자리가',
  '',
  '> **워크스페이스**: 보기 (example.slack.com)',
  '> **기간**: 2026-06-01 ~ 2026-08-31 · **실제 메시지**: 2건',
  '',
  '---',
  '',
  '## 자금 구조 요약 (2026-07-13 갑돌)',
  '',
  '| 항목 | 잔액 |',
  '|---|---|',
  '| 대출가 | **1,111억** |',
  '',
  '---',
  '',
  '## 2026-08',
  '',
  '**2026-08-02 10:00 · 갑돌**',
  '본문에만 있는 말 자물쇠',
  '',
  '## 2026-06',
  '',
  '**2026-06-01 09:00 · 갑순**',
  '옛날 말',
  '',
].join('\n');

console.log('[1/4] preambleOf 가 사람이 쓴 것만 남긴다');
{
  const pre = preambleOf(FIXTURE);
  eq('  머리말이 사람이 쓴 헤딩으로 시작한다', pre.split('\n')[0], '## 자금 구조 요약 (2026-07-13 갑돌)');
  eq('  표 안의 숫자가 남아 있다', pre.includes('**1,111억**'), true);
  eq('  파일 제목(`# #이름`)은 안 들어온다', /^#\s/m.test(pre), false);
  eq('  상단 `>` 메타는 안 들어온다 (색인에 두 번 실리면 안 된다)', pre.includes('>'), false);
  // ③ 여기가 요점이다 — 월 헤딩과 사람이 쓴 헤딩이 섞이면 안 된다.
  eq('  월 헤딩(`## 2026-08`)은 머리말이 아니다', pre.includes('## 2026-08'), false);
  eq('  본문 블록의 말은 안 들어온다', pre.includes('자물쇠'), false);
  eq('  앞뒤 `---` 와 빈 줄은 떨어진다', pre.endsWith('|') || pre.endsWith('억** |'), true);
}

console.log('[2/4] 머리말이 없는 파일은 빈 문자열이다 (없는 것을 만들지 않는다)');
{
  const plain = FIXTURE.split('\n').filter((l, i) => i < 6 || i > 13).join('\n');
  eq('  제목 + 메타 + 월 헤딩만 있으면 머리말 없음', preambleOf(plain), '');
  eq('  빈 파일도 빈 문자열', preambleOf(''), '');
  eq('  메시지 헤더가 아예 없으면 (메타 뒤) 나머지가 머리말이다',
    preambleOf('# #가\n\n> 메타\n\n---\n\n## 손으로 쓴 것\n내용\n').includes('## 손으로 쓴 것'), true);
}

console.log('[3/4] splitMessages 는 한 글자도 안 변한다 (회차 수를 세는 자리가 열 곳이 넘는다)');
{
  const blocks = splitMessages(FIXTURE);
  eq('  가짜 md 블록 2개', blocks.length, 2);
  eq('  첫 블록 날짜', blocks[0].date, '2026-08-02');
  eq('  머리말은 블록에 안 들어간다', blocks.map((b) => b.text).join('\n').includes('1,111억'), false);
  // 월 헤딩이 앞 블록 꼬리에 딸려 오는 것은 **원래 정상**이다 (ingest/verify.js 주석).
  eq('  월 헤딩이 앞 블록 꼬리에 딸려 오는 것은 그대로다', blocks[0].text.includes('## 2026-06'), true);
}

console.log('[4/4] 실물 아카이브 — 머리말이 검색·색인 양쪽에 실리나');
try {
  assertArchive();
} catch (e) {
  console.log(`  - 건너뜀: ${e.message.split('\n')[0]}`);
  process.exit(ok ? 0 : 1);
}
{
  const MONTH = /^## \d{4}-\d{2}\s*$/;
  const withPre = [];
  let preChars = 0;
  let blockTotal = 0;
  let allBlocks = ''; // 모든 채널의 **블록** 본문을 이은 것
  const wordsOf = (s) => new Set((s.toLowerCase().match(/[가-힣a-z0-9][가-힣a-z0-9.,%]{2,}/g) || []));
  for (const name of listReadableChannels()) {
    const text = readCached(path.join(CHANNELS_DIR, `${name}.md`));
    const blocks = splitMessages(text);
    blockTotal += blocks.length;
    allBlocks += `${blocks.map((b) => b.text).join('\n')}\n`;
    const pre = preambleOf(text);
    if (pre) { withPre.push([name, pre]); preChars += pre.length; }
  }
  const blockHay = allBlocks.toLowerCase();
  console.log(`  - 잰 것: 읽는 채널 ${listReadableChannels().length}개 · 머리말 있는 채널 ${withPre.length}개 ` +
    `합계 ${preChars}자 · 메시지 블록 ${blockTotal}개`);

  if (!withPre.length) {
    console.log('  - 못 쟀습니다: 이 아카이브에는 머리말이 있는 채널이 없습니다 (샐 것이 없습니다)');
  } else {
    const brief = buildArchiveBrief({ access: FULL_ACCESS });
    const split = buildArchiveBriefSplit({ access: FULL_ACCESS });
    const pub = buildArchiveBriefSplit({ access: PUBLIC_ACCESS });

    /* 검색 낱말은 **어느 채널의 어느 블록에도 없는** 것으로 고른다. 머리말 헤딩을 그대로
     * 질의로 넣으면 그 낱말이 본문에도 흔해서, 고치기 전에도 그 채널이 나온다 — 그러면
     * 이 검사는 통과하면서 아무것도 안 재게 된다 (실제로 처음엔 그랬다).
     *
     * 「없다」의 기준은 **검색과 같은 부분 문자열**이다(`scoreTerms` 의 `includes`).
     * 낱말 단위로 따로 재면 `1,111억` 이 본문의 `1,111억을` 안에 들어 있는데도 「없다」가
     * 되어, 고치기 전에도 그 채널이 나오고 검사는 또 헛돈다. */
    let missBrief = 0;
    let missSearch = 0;
    let leakBrief = 0;
    let unmeasured = 0;
    let unmeasuredLeak = 0;
    for (const [name, pre] of withPre) {
      const no = withPre.findIndex((x) => x[0] === name) + 1;
      /* 색인에는 **헤딩 글자만** 실린다 (WHK 결정 2026-09-03) — `#` 은 떼고 싣는다.
       * 그래서 `## ` 를 붙인 채로 찾으면 안 된다. */
      const head = pre.split('\n')[0].trim().replace(/^#+\s+/, '');
      if (!brief.includes(head)) { missBrief += 1; fail(`색인에 그 채널 머리말 제목이 없습니다 (채널 ${no})`); }

      const only = [...wordsOf(pre)].filter((w) => !blockHay.includes(w));
      if (!only.length) { unmeasured += 1; continue; }
      const hits = searchArchive({ query: only[0], access: FULL_ACCESS }).hits.filter((h) => h.channel === name);
      if (!hits.length) { missSearch += 1; fail(`머리말에만 있는 낱말로 검색했는데 그 채널이 안 나옵니다 (채널 ${no})`); }

      /* 반대 방향 — **숫자가 색인에 새면 안 된다.** 이 검사가 없으면 누가 색인에 표
       * 원문을 도로 실어도 위 두 줄은 그대로 통과한다(제목도 있고 검색도 된다).
       * 재는 낱말은 「머리말 본문에만 있고 헤딩에도 블록에도 없는 것」으로 고른다. */
      const headHay = pre.split('\n').filter((l) => /^#{2,6}\s/.test(l.trim())).join(' ').toLowerCase();
      const bodyOnly = only.filter((w) => !headHay.includes(w));
      if (!bodyOnly.length) { unmeasuredLeak += 1; continue; }
      if (brief.includes(bodyOnly[0])) {
        leakBrief += 1;
        fail(`색인에 머리말 본문 글자가 실려 있습니다 — 제목만 실어야 합니다 (채널 ${no})`);
      }
    }
    if (!missBrief) pass(`색인에 머리말 제목이 실린다 (${withPre.length}개 채널 전부)`);
    if (!missSearch) {
      pass(`머리말에만 있는 낱말로 검색하면 그 채널이 나온다 (${withPre.length - unmeasured}개 채널)`);
    }
    if (!leakBrief) {
      pass(`색인에 머리말 본문(숫자)은 안 실린다 (${withPre.length - unmeasured - unmeasuredLeak}개 채널)`);
    }
    if (unmeasured) {
      console.log(`  - 검색은 ${unmeasured}개 채널에서 못 쟀습니다: 머리말에만 있는 낱말을 못 찾았습니다`);
    }
    if (unmeasuredLeak) {
      console.log(`  - 새는지는 ${unmeasuredLeak}개 채널에서 못 쟀습니다: 헤딩 밖에만 있는 낱말을 못 찾았습니다`);
    }

    // 새는 것 — 월 헤딩·파일 제목이 색인에 실리면 머리말 가르기가 깨진 것이다.
    const lines = (split.common + split.extra).split('\n');
    eq('  색인에 월 헤딩 줄이 없다', lines.some((l) => MONTH.test(l.trim())), false);
    eq('  색인에 채널 md 제목줄(`# #이름`)이 없다', lines.some((l) => /^#\s+#/.test(l.trim())), false);

    // 공통 블록은 여전히 권한과 무관해야 한다 (캐시). 자세한 것은 check-brief-split.js.
    eq('  공통 블록이 공개/전체에서 같다 (캐시가 깨지지 않았다)', split.common === pub.common, true);
    console.log(`  - 색인 크기: buildArchiveBrief ${brief.length}자 · 공통 ${split.common.length}자 / 추가 ${split.extra.length}자`);
  }
}

if (ok) {
  console.log('[check-preamble] OK — 첫 메시지 앞의 사람 정리가 검색·색인 양쪽에 실립니다.');
} else {
  console.error('\n고칠 곳: src/archive.js 의 preambleOf() · channelBrief() · scanArchive()');
  process.exitCode = 1;
}
