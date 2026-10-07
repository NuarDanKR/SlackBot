/**
 * 색인 쪼개기 검증 — 절감이 실제로 걸리는지, 내용이 없어지지 않았는지 두 가지만 본다.
 *
 * ① 공통 블록이 **모든 권한 조합에서 바이트 단위로 같은가.** 한 글자만 달라도 캐시가 안 맞아
 *    절감이 통째로 사라지는데, 그 실패는 **에러 없이** 비용으로만 나타난다 — 며칠 뒤 로그를
 *    집계해야 알게 된다. 그래서 여기서 해시로 못박는다.
 * ② (공통 + 추가분) 이 예전 색인의 줄을 하나도 안 잃었는가. 순서는 바뀌지만 줄은 그대로여야 한다.
 *
 * 실행: node scripts/check-brief-split.js
 */
import crypto from 'node:crypto';
import { config, PUBLIC_ACCESS, FULL_ACCESS, accessFor } from '../src/config.js';
import { buildArchiveBrief, buildArchiveBriefSplit } from '../src/archive.js';
import { buildDocumentsBrief, buildDocumentsBriefSplit, hasDocuments } from '../src/documents.js';

const sha = (s) => crypto.createHash('sha256').update(s).digest('hex').slice(0, 12);

/* 접힘 집계 줄(`- 그 외 11건 (계약서 3 · …)`)은 **내용이 아니라 세어 본 결과**다. 예산을
 * 나누면 접히는 개수가 달라져 이 줄이 통째로 바뀌는데, 그것을 "자료가 사라졌다" 로 읽으면
 * 진짜 누락이 그 소음에 묻힌다. 그래서 비교에서 빼고 접힌 건수는 아래에서 따로 센다. */
const FOLD_LINE = /^-\s*그 외 \d+건/;

/* 채널 수를 적은 두 줄(`### 공통 · 운영 (12)` · `> **채널**: 총 50개 …`)도 내용이 아니라
 * **세어 본 결과**다. 공통 블록은 가린 뒤 남은 행으로 그 수를 다시 적으므로(archive.js 의
 * recountVisibleChannels) 예전 색인과 숫자가 다르고, 그것을 "줄이 사라졌다" 로 읽으면 진짜
 * 누락이 묻힌다. 그래서 **그 두 줄의 수만** 지우고 비교한다 — 제목·머리표는 그대로 두므로
 * 절이나 총계 줄이 통째로 없어지면 여전히 잡히고, 다른 줄은 한 글자도 안 건드린다.
 * (숫자 자체가 맞는지는 아래 ③ 이 표 행을 직접 세어 따로 본다.) */
/* 문서 절 머리(`### _승인자료 (문서 6 · 회차 6)`)도 같은 이유로 수를 지운다 — 하지만
 * **`_승인자료` 절 하나만** 이다.
 *
 * **`_승인자료` 때문에 반드시 필요하다.** 그건 비공개 채널 이름을 가리는 가상 사업장이라
 * 건수가 **보는 사람마다 다를 수밖에 없다** — 공통 블록은 누구에게나 같아야 하므로 승인
 * 문서를 전부(2건) 세지만, 그중 하나가 자기 채널 것인 사람에게는 예전 색인에서 가려진
 * 것이 1건뿐이었다. 그 차이를 "줄이 사라졌다" 로 읽으면 진짜 누락이 묻힌다.
 *
 * 승인 문서가 **서로 다른 비공개 채널에서 2건 이상**이 되어야 드러난다. 1건일 때는 그
 * 채널 멤버에게 절이 «없다가 생기는» 쪽이라 (사라지는 것이 아니라) 조용했다 —
 * 2026-08-20 에 `#비공개가` 문서가 승인되어 2건이 되면서 처음 빨개졌다.
 *
 * **다른 사업장에는 이 정규화를 걸지 않는다.** `documents.js` 가 절 건수를
 * `docs.length + folded.length` 로 매기므로, 접혔든 안 접혔든 그 수는 문서가 실제로
 * 늘거나 줄 때만 바뀐다 — 접힌 문서 하나에게는 `그 외 N건` 줄이 이미 ①에서 빠진 뒤라
 * 이 건수가 전후 비교에 남은 **유일한** 비교 대상이다. `_승인자료` 처럼 보는 사람마다
 * 달라야 하는 경우가 아니면 이 신호를 지우면 안 된다 — 지우면 한 사업장 것을 고치려다
 * 전체 사업장에서 「문서가 하나 사라져도 안 잡히는」 사각을 만든다.
 *
 * 절이 **통째로** 없어지면 정규화한 뒤에도 없으므로 여전히 잡히고, 문서 한 건 한 건은
 * 자기 줄(`- [종류] 제목 — 날짜`)이 따로 있어 그 줄로 잡힌다. 수만 무시한다. */
const COUNT_NORM = [
  [/^(###\s+.+?)\s*\(\d+\)$/, '$1 (N)'],
  [/^(###\s+_승인자료)\s*\(문서 \d+ · 회차 \d+\)$/, '$1 (문서 N · 회차 N)'],
  [/^(>\s*\*\*채널\*\*:).*$/, '$1 …'],
];
const normCount = (l) => COUNT_NORM.reduce((s, [re, to]) => s.replace(re, to), l);
const lines = (s) => s.split('\n').map((l) => normCount(l.trim())).filter((l) => l && !FOLD_LINE.test(l));
const foldedCount = (s) =>
  [...s.matchAll(/^-\s*그 외 (\d+)건/gm)].reduce((n, m) => n + Number(m[1]), 0);

/** 권한 조합 — 공개 전용 · 비공개 채널 하나씩 · 여러 개 든 DM · 전체 */
const priv = config.privateChannels;
const cases = [
  ['공개 전용', PUBLIC_ACCESS],
  ...priv.map((c) => [`#${c} 멤버`, accessFor([c])]),
  ...(priv.length > 1 ? [['DM (비공개 2개)', accessFor(priv.slice(0, 2))]] : []),
  ['전체', FULL_ACCESS],
];

let failed = 0;
const fail = (msg) => {
  failed += 1;
  console.error(`  ✗ ${msg}`);
};

/* ── 진행 표시 ──────────────────────────────────────────────────────────
 *
 * 이 검사의 비용은 **권한 조합 수 × 색인 생성**이다. 2026-10-07 TYIT 에서 12분을
 * 돌았는데, 조합별 결과를 **끝난 뒤에만** 찍고 있어서 화면의 마지막 줄은 늘 이미
 * 끝난 항목이었다 — 사람이 본 것은 멈춘 화면이다.
 *
 * 그래서 조합마다 **일을 시작하기 전에** 이름을 찍고, 끝나면 그 조합에 걸린 시간을
 * 붙인다. 느린 자리를 다음 사람이 숫자로 받는다.
 *
 * (`check-setup.js` 의 자식 러너와 같은 생각이지만 서식을 가져다 쓰지는 않는다 —
 *  이 파일은 그 러너 **안에서** 도는 자식이고, 자식이 부모의 서식을 흉내 내면 한
 *  화면에 같은 모양이 두 겹으로 겹친다.) */
const startedAll = Date.now();
const ms = (t) => (t < 1000 ? `${Math.round(t)}ms` : `${(t / 1000).toFixed(1)}초`);
/** 일을 시작하기 **전에** 찍고, 끝나면 걸린 시간을 돌려준다. */
const begin = (label) => {
  console.log(`  … ${label}`);
  const t = Date.now();
  return () => Date.now() - t;
};

console.log(`권한 조합 ${cases.length}개 — ${cases.map(([l]) => l).join(' · ')}`);

/* ── ① 공통 블록이 모두 같은가 ── */
console.log('\n① 공통 블록이 권한과 무관하게 같은가');
const commons = new Map();
const startedCommon = Date.now();
for (const [label, access] of cases) {
  const done = begin(label);
  const a = buildArchiveBriefSplit({ access }).common;
  const d = hasDocuments() ? buildDocumentsBriefSplit({ access }).common : '';
  const key = `${sha(a)}/${sha(d)}`;
  if (!commons.has(key)) commons.set(key, []);
  commons.get(key).push(label);
  console.log(`  ✓ ${label} — 공통 ${a.length + d.length}자 (${ms(done())})`);
}
const commonMs = Date.now() - startedCommon;
if (commons.size === 1) {
  const [key, labels] = [...commons.entries()][0];
  console.log(`  ✓ ${labels.length}개 권한 조합이 모두 같은 공통 블록 (${key}) · ${ms(commonMs)}`);
} else {
  fail(`공통 블록이 ${commons.size}종류로 갈렸습니다 — 캐시가 안 걸립니다`);
  for (const [key, labels] of commons) console.error(`      ${key}  ${labels.join(', ')}`);
}

/* ── ② 줄이 없어지지 않았는가 ── */
console.log('\n② 쪼갠 뒤에도 보이던 줄이 그대로인가');
const startedLines = Date.now();
for (const [label, access] of cases) {
  const done = begin(label);
  const before = new Set([
    ...lines(buildArchiveBrief({ access })),
    ...(hasDocuments() ? lines(buildDocumentsBrief({ access })) : []),
  ]);
  const sp = buildArchiveBriefSplit({ access });
  const dp = hasDocuments() ? buildDocumentsBriefSplit({ access }) : { common: '', extra: '' };
  const after = new Set([
    ...lines(sp.common), ...lines(sp.extra), ...lines(dp.common), ...lines(dp.extra),
  ]);
  const lost = [...before].filter((l) => !after.has(l));
  const size = sp.common.length + sp.extra.length + dp.common.length + dp.extra.length;
  // 접힌 문서는 색인에서만 안 보이고 검색에는 그대로 걸린다. 그래도 **늘면** 봇이 제목을
  // 못 보는 문서가 늘어난다는 뜻이라 함께 보인다 (CLAUDE.md 「접힘은 고장이 아니라 정상 상태」).
  const foldBefore = hasDocuments() ? foldedCount(buildDocumentsBrief({ access })) : 0;
  const foldAfter = foldedCount(dp.common) + foldedCount(dp.extra);
  if (lost.length) {
    fail(`${label} — ${lost.length}줄이 사라졌습니다 (${ms(done())})`);
    for (const l of lost.slice(0, 5)) console.error(`      ${l.slice(0, 100)}`);
  } else {
    // 문서 색인은 공개·비공개가 예산을 나눠 갖게 되어 공개 쪽이 **늘어날 수** 있다. 그건 이득이다.
    const grew = [...after].filter((l) => !before.has(l)).length;
    const fold = foldAfter === foldBefore ? `접힘 ${foldAfter}건` : `접힘 ${foldBefore}→${foldAfter}건`;
    console.log(
      `  ✓ ${label} — 잃은 줄 0${grew ? ` · 늘어난 줄 ${grew}` : ''} · ${fold}` +
        ` · 공통 ${sp.common.length + dp.common.length}자 / 추가 ${sp.extra.length + dp.extra.length}자` +
        ` (추가분 ${((sp.extra.length + dp.extra.length) / size * 100).toFixed(1)}%) · ${ms(done())}`,
    );
  }
}
const lineMs = Date.now() - startedLines;

/* ── ③ 적힌 채널 수가 그 블록에 실제로 남은 표 행과 맞는가 ──
 * 색인 원본의 수는 사람이 보는 참값이라, 가리기가 행을 지운 공통 블록과는 다르다. 그 차이가
 * 그대로 실리면 「내가 못 보는 자리가 몇 개 있다」가 답변에 나간다 — 실제로 나갔다
 * ("채널 목록(공개 46 + 비공개 4, 총 50개)에도 그 이름은 없고…"). 그래서 한 줄씩 찍어 보인다.
 * 세는 것은 archive.js 를 부르지 않고 **문자열을 여기서 다시 훑는다** — 같은 코드로 세면
 * 그 코드가 틀렸을 때 검사도 함께 틀린다. */
console.log('\n③ 공통 블록에 적힌 채널 수 = 그 블록에 실제로 남은 표 행 (공개 전용)');
const startedRows = Date.now();
{
  const doneRows = begin('공개 전용 표 행 세기');
  const { common } = buildArchiveBriefSplit({ access: PUBLIC_ACCESS });
  const src = common.split('\n');
  const secs = [];
  let cur = null;
  let sep = false; // 정렬 구분선을 지난 뒤의 `|` 줄만 데이터 행이다 (머리글 행 제외)
  for (const line of src) {
    const m = line.match(/^(###\s+.+?)\s*\((\d+)\)\s*$/);
    if (m) { cur = { title: m[1], printed: Number(m[2]), rows: 0 }; secs.push(cur); sep = false; continue; }
    if (/^#{1,6}\s/.test(line)) { cur = null; sep = false; continue; }
    if (!cur) continue;
    if (/^\|[\s:|-]+\|\s*$/.test(line)) { sep = true; continue; }
    if (line.startsWith('|')) { if (sep) cur.rows += 1; } else sep = false;
  }
  if (!secs.length) fail('`### 제목 (N)` 절을 하나도 못 찾았습니다 — 색인 형식이 바뀌어 다시 세지 못합니다');
  for (const s of secs) {
    if (s.printed === s.rows) console.log(`  ✓ ${s.title} (${s.printed}) — 보이는 행 ${s.rows}`);
    else fail(`${s.title} — 적힌 수 ${s.printed} ≠ 보이는 행 ${s.rows}`);
  }
  const total = secs.reduce((n, s) => n + s.rows, 0);
  const totalLine = src.find((l) => /^>\s*\*\*채널\*\*:/.test(l));
  const want = `> **채널**: 총 ${total}개`;
  if (!totalLine) fail('총계 줄(`> **채널**: …`)이 없습니다');
  else if (totalLine.trim() === want) console.log(`  ✓ ${want} — 절 합계와 같음`);
  else fail(`총계 줄이 「${want}」 가 아닙니다 (공개·비공개 내역이 남아 있으면 그것부터 샙니다) — ${totalLine.trim()}`);
  console.log(`  · 절 ${secs.length}개 (${ms(doneRows())})`);
}
const rowMs = Date.now() - startedRows;

/* 숫자를 `[보임]` 으로 적는다 — 통과해도 `check-setup` 이 올려 준다.
 *
 * PF 인계 문서(§PF 개발자 확인 사항 1·3)가 요구하는 「`check-brief-split` 소요 시간」
 * 과 「공개·비공개 권한 조합 수」가 그것이다. 사람이 초시계를 들고 재게 하면 아무도
 * 안 잰다. 느려지는 것은 **에러가 아니라 비용**이라 이 줄이 없으면 며칠 뒤에야 안다. */
console.log(
  `\n[보임] 권한 조합 ${cases.length}개 · 전체 ${ms(Date.now() - startedAll)}` +
    ` (①공통 ${ms(commonMs)} · ②줄 ${ms(lineMs)} · ③행 ${ms(rowMs)})`,
);

console.log(failed ? `\n실패 ${failed}건` : '\n이상 없음');
process.exit(failed ? 1 : 0);
