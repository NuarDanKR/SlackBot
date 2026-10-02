#!/usr/bin/env node
/**
 * search 의 **문서 구역** 부피를 실제 질의 재생으로 잰다 — measure-hit-cap.js(대화 구역
 * 전용)가 안 재던 곳. 설계 전제 「평균 25,134자」의 주범이 문서 구역(건당 4,000자 ×
 * 최대 12건)으로 좁혀졌는데(2026-09-10 재측정) 그 분포를 잰 적이 없다.
 *
 *   node scripts/measure-doc-section.js
 *
 * 재는 것 (세 벌로 잰다 — 단위가 다르다):
 *   [A] 알맹이 자수  — searchDocuments 가 돌려준 hits[].text · note · outside · 카드 수.
 *                      히트 수 도수·히트당 길이 분위수·잘린 비율도 여기서.
 *                      **`measure-hit-cap` 의 `baseChars` 와 같은 단위인 것은 `hits` 열
 *                      하나뿐이다** — 그쪽은 `base.hits[i].text.length` 만 더하고
 *                      note·outside 를 안 센다(measure-hit-cap.js 의 baseChars 누적부).
 *                      그래서 [A] 의 **합**을 measure-hit-cap 의 수와 직접 견주면 안 된다.
 *   [B] 렌더된 자수  — buildTools 의 search 도구를 **그대로 불러** 모델이 받는 문자열을
 *                      구역별로 가른다(check-doc-card-render 와 같은 방식). 구역 머리·
 *                      note·카드 줄까지 포함한 **진짜 부피**이고, 로그의
 *                      `search(...) N자` 와 같은 단위다. 「문서 구역이 몇 %인가」의 답은
 *                      이쪽이다.
 *   [C] 회차별 대조  — [B] 의 회차별 자수를 **그 회차의 로그 실측 자수와 짝지어** 차이의
 *                      분포를 낸다. 총합·평균 대조는 서로 반대 방향의 한계(②④⑤는 위로,
 *                      ①은 아래로)가 **상쇄돼** 작아 보이므로 충실도의 근거가 못 된다
 *                      (2026-09-11 검토 Important 1). 짝지어 봐야 속지 않는다.
 *
 * ── 카드(r.cards)의 타입 ──
 *
 * `{project,title,date,chars,sections}` **객체의 배열**이다(`companyWideCards` 끝의
 * `return picked.map(...)`, `src/documents/search.js`). 문자열이 아니므로 자수를 String(c).length
 * 로 세면 안 된다 — "[object Object]" 15자가 나온다. 카드가 실제로 차지하는 자수는
 * `src/llm/tools.js` 가 카드마다 한 줄로 그려 넣는 [B] 의 「전사 종합 문서」 구역 길이다. 그리는
 * 규칙(search 도구 run 안의 `outsides.push({ title: '전사 종합 문서 …', render })`)은
 * export 돼 있지 않으므로 여기서 베끼지 않고 [B] 로 잰다.
 *
 * ── 질의를 어디서 꺼내나 ──
 *
 * `hermes-log/*.md` 의 `> **도구** search(...)` 줄을 `parseToolLine`(src/convo-log.js)
 * 으로 되읽는다. 이 파서는 렌더(`toolLine`)의 역함수인데, `toolLine` 이 search 도구의
 * **모든** 입력 값(query·where·document·only)을 키 없이 쉼표로 이어붙여 적기 때문에
 * (예: `search(어느 사업장 할인분양 보고, 그 사업장 채널)`), 되읽은 `call.args` 는 순수
 * query 가 아니라 **그 회차의 입력 값 전체를 이어붙인 문자열**이다. 인자 키는 렌더에서부터
 * 사라져 복원할 수 없다.
 *
 * ── 재생의 한계 (숫자를 보기 전에 읽을 것) ──
 *
 *   ① where 가 이어붙은 회차는 **질의가 실제보다 길다** — 낱말이 하나 늘어난 질의로
 *      재생되므로 히트가 실제보다 적게 잡힐 수 있다(measure-hit-cap 머리말과 같은 한계).
 *   ② 봇이 직접 준 `where`·`document`·`only` 는 **복원할 수 없어 안 넘긴다.** 실제로는
 *      `only:'archive'` 라 문서 구역이 아예 안 돌았던 회차도 여기서는 돈다.
 *   ③ 80자에서 잘린 인자는 원문 일부만 재생된다.
 *   ④ 접근 권한은 전부 FULL_ACCESS 로 재생한다 — 실제 회차는 질문자의 권한에 따라 덜
 *      보였을 수 있다(measure-hit-cap 과 같은 규칙).
 *   ⑤ 아카이브는 **지금 시점**의 것이다 — 8월 회차를 지금 자료로 재생하므로, 그 뒤 들어온
 *      문서가 히트에 섞인다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { parseToolLine } from '../src/convo-log.js';
import { searchDocuments } from '../src/documents.js';
import { searchArchive } from '../src/archive.js';
import { autoNarrow, buildTools } from '../src/claude.js';
import { FULL_ACCESS, LOG_DIR, LOG_ENABLED, TRUNC_PHRASE, config } from '../src/config.js';

const DOC_HIT_MAX_CHARS = config.limits?.docHitMaxChars ?? 4000;

if (!LOG_ENABLED || !LOG_DIR) {
  console.error('대화 로그가 꺼져 있습니다 (config.json 의 log.enabled · log.path).');
  process.exit(2);
}

/* hermes-log md 에서 search 질의를 모은다 (parseToolLine 규약 — measure-hit-cap.js 의 같은 블록).
 *
 * **그 회차에 로그가 적어 둔 결과 자수(`call.chars`)도 함께 들고 온다** — [C] 의 회차별
 * 대조가 이것과 짝지어야 한다. 옛 회차는 그 필드가 없어 null 이다(나중에 생긴 필드). */
const queries = [];
const loggedChars = [];
let truncatedN = 0;
let multiValueN = 0;
let files = [];
try {
  files = fs.readdirSync(LOG_DIR).filter((n) => /^\d{4}-\d{2}\.md$/.test(n));
} catch (e) {
  if (e.code !== 'ENOENT') throw e;
}
if (!files.length) {
  console.error(`재생할 로그가 없습니다: ${LOG_DIR}`);
  process.exit(2);
}
for (const f of files) {
  for (const line of fs.readFileSync(path.join(LOG_DIR, f), 'utf-8').split('\n')) {
    const t = parseToolLine(line);
    if (!t) continue;
    for (const call of t.calls ?? t) {
      if ((call.name ?? call.tool) !== 'search' || !call.args) continue;
      queries.push(call.args);
      loggedChars.push(call.chars ?? null);
      if (call.truncated) truncatedN += 1;
      // 쉼표가 있으면 where·document·only 중 하나가 query 뒤에 함께 이어붙었을 가능성.
      if (call.args.includes(',')) multiValueN += 1;
    }
  }
}
console.log(`재생할 search 질의 ${queries.length}건`);
if (truncatedN) console.log(`  그중 80자에서 잘린 인자 ${truncatedN}건 (원문 일부만 재생됨)`);
if (multiValueN) console.log(`  그중 쉼표가 섞인 것 ${multiValueN}건 (where/document/only 가 query 에 이어붙었을 수 있음)`);
if (!queries.length) {
  console.error('질의가 0건입니다 — 파서 규약이 바뀌었는지 먼저 의심하세요(로그에 search 줄이 있는데 0건이면 재생이 통째로 헛것입니다).');
  process.exit(2);
}

const SEARCH = buildTools({ access: FULL_ACCESS, touched: new Set() }).find((t) => t.name === 'search');

/* 구역 머리는 claude.js 가 쓰는 **다섯 가지 제목뿐**이고 결과 하나에 최대 한 번씩만 나온다.
 *
 * `\n## ` 로 그냥 쪼개면 안 된다 — 문서 본문(md 에서 옮겨 온 원문)에 `## ` 로 시작하는 줄이
 * 그대로 들어 있어, 발췌 한가운데가 새 구역으로 읽힌다(첫 판에서 16.2%가 「기타」로 샜다).
 * `\n\n---\n\n`(구역 구분자)도 같은 이유로 못 쓴다 — 본문에 가로줄이 있을 수 있다.
 * 그래서 **아는 제목의 첫 등장 자리만** 경계로 삼는다. */
const SECTION_TITLES = [
  ['\n## 대화 (사람 발언)', '대화'],
  ['\n## 문서 (원문)', '문서'],
  ['\n## 다른 사업장에서도 (대화)', '대화-밖'],
  ['\n## 다른 사업장에서도 (문서)', '문서-밖'],
  ['\n## 전사 종합 문서', '문서-카드'],
];

/** 렌더된 문자열을 구역별 자수로 가른다. 첫 구역보다 앞은 「머리말」(좁힘 안내 문장). */
function splitSections(text) {
  const marks = [];
  for (const [needle, bucket] of SECTION_TITLES) {
    const i = text.startsWith(needle.slice(1)) ? 0 : text.indexOf(needle);
    if (i !== -1) marks.push({ at: i === 0 ? 0 : i + 1, bucket });
  }
  marks.sort((a, b) => a.at - b.at);
  const out = [];
  if (!marks.length) return [{ bucket: '기타', chars: text.length }];
  if (marks[0].at > 0) out.push({ bucket: '머리말', chars: marks[0].at });
  marks.forEach((m, i) => {
    const end = i + 1 < marks.length ? marks[i + 1].at : text.length;
    out.push({ bucket: m.bucket, chars: end - m.at });
  });
  return out;
}

/* 최근접 순위 분위수 — 정렬 후 floor(n·p) 번째를 그대로 쓴다(보간 없음).
 * **표본이 적으면 위쪽 분위수가 곧 max 다** — 길이 10 이하면 floor(n·0.9) 가 마지막 칸을
 * 가리키므로 q90 === max 가 된다. 이번 실행(히트 수백~천 단위)에는 영향이 없지만,
 * 질의를 몇 건만 넣고 돌리면 p90 이 과대로 읽힌다. */
const qAt = (xs, p) => (xs.length ? [...xs].sort((a, b) => a - b)[Math.min(xs.length - 1, Math.floor(xs.length * p))] : 0);
const q10 = (xs) => qAt(xs, 0.1);
const q50 = (xs) => qAt(xs, 0.5);
const q90 = (xs) => qAt(xs, 0.9);
const sum = (xs) => xs.reduce((a, b) => a + b, 0);
const n = (v) => Math.round(v).toLocaleString('en-US');

// ── [A] 알맹이 자수 + 분포 ───────────────────────────────────────
let docHitChars = 0, docNoteChars = 0, docOutsideChars = 0;
let arcHitChars = 0, arcNoteChars = 0, arcOutsideChars = 0;
let narrowedN = 0, fellBackN = 0, cardQueryN = 0, cardN = 0, cardDocChars = 0;
/* 히트 수는 **낱개 도수**로 센다. 구간으로 묶으면(예전 판의 `9-12`) 9와 12를 못 가르는데,
 * 이 표를 보고 정할 갈래 하나가 바로 「건수 상한(12)을 내릴까」다 — 12에 몰렸으면 크게
 * 줄고 9~10에 퍼졌으면 거의 안 준다. 묶는 순간 그 결정에 필요한 값이 지워진다
 * (2026-09-11 검토 Important 2). */
const hitCountFreq = new Map();
const confirmedLens = [];   // 확정 히트(4,000자 상한)
const partialLens = [];     // 일부만 맞은 히트(1,000자 상한)
let confirmedClipped = 0, partialClipped = 0;
const perQuery = [];

// ── [B] 렌더된 자수 ─────────────────────────────────────────────
const renderedTotals = new Map();   // 갈래 → 자수 합
let renderedAll = 0;
const renderedPerQuery = [];

for (const q of queries) {
  /* 프로덕션과 같은 경로로 재생한다 — search 도구는 `autoNarrow` 로 project 를 채워
   * 부르고(claude.js 의 search `run` 안 `const auto = (where || document) ? … : autoNarrow(…)`
   * — **줄번호는 안 적는다. 조용히 낡는다**), **두 구역이 `project` 없이는 아예 안 붙는다**:
   *   · 카드   — `searchDocuments` 의 `if (r.error || !opts.project || opts.document) return r;`
   *   · outside — `searchDocumentsInner` 의 `if (!opts.project || opts.document || max <= 0) return base;`
   *   (줄번호는 안 적는다 — 2026-09-11 에 1571·1377 이었지만 조용히 낡는다. 찾을 때는 심볼과
   *    위 코드 조각으로 찾는다.)
   * project 없이 재생하면 그 둘이 **늘 0**으로 나오는데 에러는 안 난다 — 「문서 구역이
   * 생각보다 작네」라는 틀린 결론이 조용히 나온다 (2026-09-11 회의적 검증).
   * **거꾸로, 이 둘이 0이 아니게 나왔다는 것이 재생이 그 경로를 탔다는 근거다.** */
  const auto = autoNarrow(q, FULL_ACCESS);
  /* 되돌아가기도 프로덕션 그대로다(claude.js 의 `auto.projectFellBack` 자리) — 스스로 좁혔는데
   * 아무것도 못 받으면 안 좁힌 결과로 되돌아간다. 이걸 빼면 그 회차의 실제 부피를 0 으로 센다. */
  let r = searchDocuments({ query: q, project: auto.project || undefined, access: FULL_ACCESS });
  let fellBack = false;
  if (auto.project && ![...r.hits, ...(r.outside || [])].length) {
    r = searchDocuments({ query: q, access: FULL_ACCESS });
    fellBack = true;
  }
  let a = searchArchive({ query: q, channel: auto.channel || undefined, access: FULL_ACCESS });
  if (auto.channel && ![...a.hits, ...(a.outside || [])].length) {
    a = searchArchive({ query: q, access: FULL_ACCESS });
  }

  if (auto.project) narrowedN += 1;
  if (fellBack) fellBackN += 1;

  const hits = r.hits ?? [];
  const chars = hits.reduce((s, h) => s + h.text.length, 0);
  const noteChars = r.note?.length ?? 0;
  const outsideChars = (r.outside ?? []).reduce((s, h) => s + h.text.length, 0);
  const cards = Array.isArray(r.cards) ? r.cards : [];
  if (cards.length) { cardQueryN += 1; cardN += cards.length; cardDocChars += sum(cards.map((c) => c.chars || 0)); }

  docHitChars += chars; docNoteChars += noteChars; docOutsideChars += outsideChars;
  arcHitChars += (a.hits ?? []).reduce((s, h) => s + h.text.length, 0);
  arcNoteChars += a.note?.length ?? 0;
  arcOutsideChars += (a.outside ?? []).reduce((s, h) => s + h.text.length, 0);

  const k = hits.length;
  hitCountFreq.set(k, (hitCountFreq.get(k) ?? 0) + 1);

  for (const h of hits) {
    // 일부만 맞은 히트에만 score 가 붙는다(documents.js 의 partial 경로) — 상한이 달라서
    // (4,000 대 1,000) 한 통에 넣으면 분위수가 뒤섞인다.
    const isPartial = typeof h.score === 'number';
    (isPartial ? partialLens : confirmedLens).push(h.text.length);
    if (h.text.includes(TRUNC_PHRASE)) { if (isPartial) partialClipped += 1; else confirmedClipped += 1; }
  }

  perQuery.push({
    q: q.slice(0, 40), narrowed: !!auto.project, fellBack, hits: hits.length, chars, outsideChars, cards: cards.length,
    confirmed: hits.filter((h) => typeof h.score !== 'number').length,
    partial: hits.filter((h) => typeof h.score === 'number').length,
  });

  // [B] 진짜 부피 — 도구를 그대로 불러 구역별로 가른다.
  const rendered = await SEARCH.run({ query: q });   // eslint-disable-line no-await-in-loop -- 순차 재생이 목적
  renderedAll += rendered.length;
  const byBucket = new Map();
  for (const s of splitSections(rendered)) {
    renderedTotals.set(s.bucket, (renderedTotals.get(s.bucket) ?? 0) + s.chars);
    byBucket.set(s.bucket, (byBucket.get(s.bucket) ?? 0) + s.chars);
  }
  renderedPerQuery.push({ all: rendered.length, doc: ['문서', '문서-밖', '문서-카드'].reduce((s, b) => s + (byBucket.get(b) ?? 0), 0) });
}

console.log('\n══ [A] 알맹이 자수 — searchDocuments 가 돌려준 것 ══');
console.log('   (measure-hit-cap 의 baseChars 와 같은 단위인 것은 아래 hits 줄 하나뿐이다 — 그쪽은 note·outside 를 안 센다)');
console.log(`질의 ${queries.length}건 · 그중 autoNarrow 가 사업장으로 좁힌 것 ${narrowedN}건 · 좁혔다가 0건이라 되돌아간 것 ${fellBackN}건`);
console.log(`문서 구역 총 알맹이 ${n(docHitChars + docNoteChars + docOutsideChars)}자`);
console.table([
  { 갈래: 'hits[].text', 자수: n(docHitChars), '질의당': n(docHitChars / queries.length) },
  { 갈래: 'note', 자수: n(docNoteChars), '질의당': n(docNoteChars / queries.length) },
  { 갈래: 'outside[].text', 자수: n(docOutsideChars), '질의당': n(docOutsideChars / queries.length) },
]);
console.log(`카드: ${cardQueryN}건의 질의에서 ${cardN}장 (카드가 가리키는 문서 원본 합 ${n(cardDocChars)}자 — 카드 줄 자체의 자수는 [B] 의 「전사 종합 문서」 구역)`);

/* 좁힌 질의와 안 좁힌 질의를 갈라 본다 — 총합 하나로는 「좁히기가 부피를 줄이나」를 못 본다.
 * (autoNarrow 설계 근거가 「자수 25% 감소」였으므로 그 방향이 유지되는지가 여기서 보인다.) */
const grp = (rows) => ({
  '질의 수': rows.length,
  '평균 히트': rows.length ? (sum(rows.map((x) => x.hits)) / rows.length).toFixed(1) : '—',
  '평균 확정': rows.length ? (sum(rows.map((x) => x.confirmed)) / rows.length).toFixed(1) : '—',
  '평균 부분': rows.length ? (sum(rows.map((x) => x.partial)) / rows.length).toFixed(1) : '—',
  '평균 hits 자수': rows.length ? n(sum(rows.map((x) => x.chars)) / rows.length) : '—',
  '평균 outside 자수': rows.length ? n(sum(rows.map((x) => x.outsideChars)) / rows.length) : '—',
  '카드 붙은 질의': rows.filter((x) => x.cards > 0).length,
});
console.log('\n[좁힘 여부별] autoNarrow 가 사업장을 찾았나로 가른 질의별 평균');
console.table([
  { 갈래: '좁힌 질의', ...grp(perQuery.filter((x) => x.narrowed && !x.fellBack)) },
  { 갈래: '좁혔다 되돌아감', ...grp(perQuery.filter((x) => x.fellBack)) },
  { 갈래: '안 좁힌 질의', ...grp(perQuery.filter((x) => !x.narrowed)) },
]);

console.log('\n[히트 수 도수] 질의 몇 건이 문서 히트를 정확히 몇 개 받았나 (묶지 않는다 — 건수 상한 결정의 재료)');
const freqRows = [...hitCountFreq].sort((a, b) => a[0] - b[0]).map(([k, v]) => ({
  '히트 수': k, '질의 수': v, 몫: `${(100 * v / queries.length).toFixed(1)}%`,
}));
console.table(freqRows);
/* **상한을 내리면 「정확히 12건」만 움직이는 것이 아니다.** 안전망(`partialAlsoWhenAtMost`)이
 * 켜져 상한 위로 얹힌 13·14건짜리도 함께 줄어든다 — 12건만 세면 과소평가한다. */
const CAP_HITS = config.limits?.docSearchMaxHits ?? 12;
const atCap = hitCountFreq.get(CAP_HITS) ?? 0;
const atOrOverCap = [...hitCountFreq].filter(([k]) => k >= CAP_HITS).reduce((s, [, v]) => s + v, 0);
const pct = (v) => `${(100 * v / queries.length).toFixed(1)}%`;
console.log(`  건수 상한(${CAP_HITS}건)에 정확히 붙은 질의 ${atCap}건 (${pct(atCap)})`);
console.log(`  상한 이상(${CAP_HITS}건+)을 받은 질의 ${atOrOverCap}건 (${pct(atOrOverCap)})`
  + ` — 상한을 내리면 여기까지 함께 움직인다. 「건수 상한을 내릴까」의 분모는 이쪽이다.`);
console.log('  ※ 이 도수표도 결론 줄과 **같은 상방 편향**을 안고 있다 — 봇이 준 `only`·`where` 를 복원할 수 없어');
console.log("     안 넘겼으므로, 실제로는 `only:'archive'` 라 문서 검색이 아예 안 돌았던 회차도 여기서는 히트를 받았고(한계 ②),");
console.log('     아카이브도 그 뒤 늘었다(한계 ⑤). **상한에 붙은 질의 비율은 위로 치우친 값이다.**');

console.log('\n[히트당 길이 분위수·자수 합] 상한이 갈리므로 확정/일부만 맞은 것을 따로 잰다');
const confirmedSum = sum(confirmedLens);
const partialSum = sum(partialLens);
console.table([
  {
    갈래: `확정 (상한 ${n(DOC_HIT_MAX_CHARS)}자)`,
    건수: confirmedLens.length,
    '자수 합': n(confirmedSum),
    '자수 몫': `${(100 * confirmedSum / (confirmedSum + partialSum)).toFixed(1)}%`,
    p50: n(q50(confirmedLens)),
    p90: n(q90(confirmedLens)),
    max: n(Math.max(0, ...confirmedLens)),
    '상한에 걸려 잘림': confirmedLens.length ? `${(100 * confirmedClipped / confirmedLens.length).toFixed(1)}% (${confirmedClipped}건)` : '—',
  },
  {
    갈래: `일부만 맞음 (상한 ${n(config.limits?.partialHitMaxChars ?? 1000)}자)`,
    건수: partialLens.length,
    '자수 합': n(partialSum),
    '자수 몫': `${(100 * partialSum / (confirmedSum + partialSum)).toFixed(1)}%`,
    p50: n(q50(partialLens)),
    p90: n(q90(partialLens)),
    max: n(Math.max(0, ...partialLens)),
    '상한에 걸려 잘림': partialLens.length ? `${(100 * partialClipped / partialLens.length).toFixed(1)}% (${partialClipped}건)` : '—',
  },
]);
console.log(`  두 갈래 합 ${n(confirmedSum + partialSum)}자 = 위 hits[].text ${n(docHitChars)}자 (맞아야 한다)`);
console.log(`  질의당 평균 — 확정 ${(confirmedLens.length / queries.length).toFixed(1)}건 · 일부만 맞음 ${(partialLens.length / queries.length).toFixed(1)}건`);

const arcAll = arcHitChars + arcNoteChars + arcOutsideChars;
const docAll = docHitChars + docNoteChars + docOutsideChars;
console.log('\n[대화 구역과 대조 — 같은 질의 집합, 같은 단위]');
console.table([
  { 구역: '문서', 'hits': n(docHitChars), 'note': n(docNoteChars), 'outside': n(docOutsideChars), 합: n(docAll), 몫: `${(100 * docAll / (docAll + arcAll)).toFixed(1)}%` },
  { 구역: '대화', 'hits': n(arcHitChars), 'note': n(arcNoteChars), 'outside': n(arcOutsideChars), 합: n(arcAll), 몫: `${(100 * arcAll / (docAll + arcAll)).toFixed(1)}%` },
]);

console.log('\n══ [B] 렌더된 자수 — 도구가 실제로 돌려준 문자열 (로그의 `search(...) N자` 와 같은 단위) ══');
const rows = [...renderedTotals].sort((x, y) => y[1] - x[1]).map(([b, c]) => ({
  구역: b, 자수: n(c), 몫: `${(100 * c / renderedAll).toFixed(1)}%`, '질의당': n(c / queries.length),
}));
console.table(rows);
const docRendered = ['문서', '문서-밖', '문서-카드'].reduce((s, b) => s + (renderedTotals.get(b) ?? 0), 0);
const allLens = renderedPerQuery.map((x) => x.all);
console.log(`합계 ${n(renderedAll)}자 · 질의당 평균 ${n(renderedAll / queries.length)}자 · 중앙값 ${n(q50(allLens))}자 · 최대 ${n(Math.max(...allLens))}자`);

// 질의별 문서 구역 몫 — 총합 하나가 소수의 큰 회차에 끌려가지 않았나 본다.
const docShares = renderedPerQuery.filter((x) => x.all > 0).map((x) => 100 * x.doc / x.all);
console.log(`질의별 문서 구역 몫: 중앙값 ${q50(docShares).toFixed(1)}% · p10 ${q10(docShares).toFixed(1)}% · p90 ${q90(docShares).toFixed(1)}%`
  + ` (총합 기준 ${(100 * docRendered / renderedAll).toFixed(1)}%)`);

/* ── [C] 회차별 대조 ─────────────────────────────────────────────
 *
 * **총합·평균 대조는 충실도의 근거가 못 된다.** 한계들이 서로 반대 방향이라
 * (②`only` 무시·④FULL_ACCESS·⑤늘어난 아카이브는 **위로**, ①이어붙은 where 와 봇이 직접
 * 준 where 는 **아래로**) 합치면 상쇄돼 작아 보인다. 회차마다 짝지어 봐야 속지 않는다
 * (2026-09-11 검토 Important 1).
 *
 * **이 대조 자체에도 한계가 있다** — 로그 자수는 그 회차 **당시** 아카이브의 값이고
 * 재생은 **지금** 아카이브다(한계 ⑤). 그래서 차이에는 경로 차이와 아카이브 성장분이
 * 함께 들어 있다. 「0에 가까워야 맞다」가 아니라 **분포가 좁은가**를 본다. */
const paired = [];
renderedPerQuery.forEach((x, i) => {
  if (loggedChars[i] != null && loggedChars[i] > 0) paired.push({ logged: loggedChars[i], replayed: x.all });
});
console.log('\n══ [C] 회차별 대조 — 재생 자수 vs 그 회차 로그 실측 자수 ══');
if (!paired.length) {
  console.log('  로그에 결과 자수가 적힌 회차가 없습니다 — 짝지어 볼 수 없습니다.');
} else {
  const diffPct = paired.map((p) => 100 * (p.replayed - p.logged) / p.logged);
  const absPct = diffPct.map(Math.abs);
  const within = (t) => `${(100 * absPct.filter((v) => v <= t).length / absPct.length).toFixed(1)}%`;
  console.log(`짝지어진 회차 ${paired.length}건 / ${queries.length}건`);
  console.table([{
    '회차별 차이(재생−로그)': '%',
    p10: `${q10(diffPct).toFixed(1)}%`,
    중앙값: `${q50(diffPct).toFixed(1)}%`,
    p90: `${q90(diffPct).toFixed(1)}%`,
    '±10% 안': within(10),
    '±25% 안': within(25),
    '±50% 안': within(50),
  }]);
  console.log(`  참고 — 총합끼리: 로그 ${n(sum(paired.map((p) => p.logged)))}자 vs 재생 ${n(sum(paired.map((p) => p.replayed)))}자`
    + ` (${(100 * (sum(paired.map((p) => p.replayed)) / sum(paired.map((p) => p.logged)) - 1)).toFixed(1)}%)`
    + ' — 이 총합 수치는 상쇄된 값이라 충실도의 근거가 아니다.');
  console.log(`  중앙값끼리: 로그 ${n(q50(paired.map((p) => p.logged)))}자 vs 재생 ${n(q50(paired.map((p) => p.replayed)))}자`);
}

console.log(`\n▶ 결론: 문서 구역(문서 + 문서-밖 + 카드)이 search 부피의 **${(100 * docRendered / renderedAll).toFixed(1)}%** (${n(docRendered)}자 / ${n(renderedAll)}자)`);
console.log('  ※ 이 값은 **위로 치우친 값이다.** 봇이 준 `only`·`where`·`document` 를 로그에서 복원할 수 없어 안 넘겼으므로,');
console.log('     실제로는 `only:\'archive\'` 라 문서 구역이 아예 안 돌았던 회차도 여기서는 돌았다 (한계 ②). 그 비중은 못 잰다.');
console.log('  ※ 재생이 프로덕션 경로를 탔다는 근거는 총합 일치가 아니라 **카드·outside 가 0이 아니게 나왔다는 것**이다');
console.log('     (둘 다 `project` 없이는 `searchDocuments`·`searchDocumentsInner` 의 게이트에서 막혀 0이 된다).');
console.log('  ※ 위 [A] 와 [B] 의 % 가 다른 것은 정상이다 — [A] 는 알맹이만, [B] 는 구역 머리·note·카드 줄까지 센다.');
console.log('  ※ 나머지 한계는 이 파일 머리말 ①~⑤.');
