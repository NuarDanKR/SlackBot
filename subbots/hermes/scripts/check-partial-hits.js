#!/usr/bin/env node
/**
 * 부분 일치 되돌림 점검 — 실제 아카이브를 상대로 돈다.
 *
 * 보는 것 다섯:
 *  ① 모든 낱말이 든 결과가 있으면 예전과 똑같이 그것만 준다 (partial 이 안 붙는다)
 *  ② 0건일 때만 일부 맞은 것을 겹친 개수 순으로 준다
 *  ③ 자리마다 **가장 잘 맞은 것**을 내놓는다 (가장 최근 것이 아니라)
 *  ④ 몇 낱말 맞았는지가 결과에 실제로 보인다
 *
 * ③④ 가 왜 있나: 부분 일치를 `pickSpread` 에 태우면서 그 앞의 점수 정렬이 죽어 있었다.
 * `pickSpread` 는 확정 히트용이라 자리마다 목록을 **날짜순으로 다시 정렬**하는데, 확정
 * 히트는 낱말을 다 갖고 있어 점수가 같으니 그게 맞다. 부분 일치는 점수가 갈리는데도 같은
 * 함수를 써서 자리마다 「가장 최근 것」이 나갔다 — 2026-08-18 실측으로 문서 8개 질의 중 8개,
 * 대화 6개 중 5개에서 역전이 났고, 2026-08-18 사고의 그 질의는 돌려준 12건이 **전부 1점**인데
 * 2점짜리 6건이 통째로 빠져 있었다. 게다가 도구 설명과 안내문이 봇에게 「겹친 개수 순」이라고
 * 적고 있어서, 봇은 날짜순인 목록을 관련도 순으로 읽었다.
 *
 * 실행: node scripts/check-partial-hits.js
 */
import { searchArchive, assertArchive, pickSpread } from '../src/archive.js';
import { searchDocuments, hasDocuments } from '../src/documents.js';
import { FULL_ACCESS } from '../src/config.js';
import { loadFixtures, skipNote } from './fixtures.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

// 이 검사는 실제 아카이브를 상대로 돈다. 아카이브가 없는 새 클론에서 assertArchive() 를
// 잡지 않고 두면 node 가 원본 스택 트레이스를 그대로 찍는다 — npm run check 의 [1/6] 은
// 그걸 그대로 들여쓰기만 해서 보여주므로 다른 검사들의 깔끔한 ✓/✗ 사이에 스택 트레이스가
// 낀다. [3/6] 이 아카이브 없음을 assertArchive() 의 메시지 한 줄로 보여주는 것과 같은 자리다.
try {
  assertArchive();
} catch (e) {
  console.log(`  - 건너뜀: ${e.message.split('\n')[0]}`);
  process.exit(0);
}

/* 질의는 **자료 저장소**에서 온다 (`scripts/fixtures.js` 참조). 이 검사는 순수 함수가
 * 아니라 실물 아카이브를 상대로 돌아서, 질의가 그 팀의 자료와 맞아야 무엇이든 잰다.
 * 가짜 낱말로 바꾸면 0건이 나오고 검사는 그것을 「아카이브가 예상과 다르다」로 읽는다. */
const FX = loadFixtures('partialHits', [
  'common', 'commonPair', 'incident', 'many', 'narrowArchive', 'narrowDocs',
]);
if (!FX) {
  console.log(skipNote('partialHits'));
  process.exit(0);
}

console.log('[1/6] 걸리는 질의는 예전 그대로');
const hit = searchArchive({ query: FX.common, access: FULL_ACCESS });
if (!hit.hits.length) bad(`'${FX.common}' 가 0건입니다 — 아카이브가 예상과 다릅니다`);
else if (hit.partial) bad('모든 낱말이 걸렸는데 partial 이 붙었습니다');
else ok(`'${FX.common}' ${hit.hits.length}건 · partial 안 붙음`);

console.log('[2/6] 아무 데도 함께 있지 않은 두 낱말');
const q = `${FX.common} zzqqxx없는낱말`;
const p = searchArchive({ query: q, access: FULL_ACCESS });
if (!p.partial) bad(`'${q}' 에 partial 이 안 붙었습니다`);
else if (!p.hits.length) bad('partial 인데 돌려준 것이 0건입니다');
else if (!/일부/.test(p.note || '')) bad(`note 가 부분 일치임을 안 밝힙니다: ${p.note}`);
else ok(`'${q}' → 일부 맞은 것 ${p.hits.length}건 · note 있음`);

// 이 검사는 사실 PARTIAL_MIN_TERMS 자체를 시험하지 못한다 — 낱말이 하나면 점수는 0(버려짐)
// 아니면 1(=terms.length, 완전일치)뿐이라 partials 가 애초에 안 생긴다. 그래도 "낱말 1개
// 질문은 부분 일치를 절대 내지 않는다"는 겉보기 동작은 지킬 값이라 그대로 둔다.
console.log('[3/6] 낱말이 하나면 부분 일치가 아예 생기지 않는다');
const one = searchArchive({ query: 'zzqqxx없는낱말', access: FULL_ACCESS });
if (one.partial) bad('낱말 1개짜리에 partial 이 붙었습니다');
else ok('낱말 1개 → 부분 일치가 생길 수 없음 (partial 없음)');

if (hasDocuments()) {
  console.log('[문서] 같은 규칙');
  const d = searchDocuments({ query: '계약 zzqqxx없는낱말', access: FULL_ACCESS });
  if (!d.partial) bad("문서 쪽 '계약 zzqqxx없는낱말' 에 partial 이 안 붙었습니다");
  else if (!d.hits.length) bad('문서 쪽 partial 인데 0건입니다');
  else ok(`문서 쪽 일부 맞은 것 ${d.hits.length}건`);

  const one = searchDocuments({ query: 'zzqqxx없는낱말', access: FULL_ACCESS });
  if (one.partial) bad('문서 쪽 낱말 1개짜리에 partial 이 붙었습니다');
  else ok('문서 쪽 낱말 1개 → 부분 일치가 생길 수 없음 (partial 없음)');
}

console.log('[4/6] 자리마다 가장 잘 맞은 것을 내놓는다 (pickSpread 단위 시험)');
{
  // 자리 ㄱ 은 「옛것인데 2점」과 「최근인데 1점」을 갖고 있다. 몫이 1건이면 2점이 나가야 한다.
  // 날짜순으로만 고르면 1점짜리가 나가고 2점은 아예 안 보인다 — 실제로 그랬다.
  const groups = new Map([
    ['ㄱ', [{ score: 2, date: '2026-01-01', tag: '옛것 2점' }, { score: 1, date: '2026-08-01', tag: '최근 1점' }]],
    ['ㄴ', [{ score: 1, date: '2026-08-02', tag: 'ㄴ 1점' }]],
  ]);
  const byScore = (a, b) => b.score - a.score || b.date.localeCompare(a.date);
  const picked = pickSpread(groups, 2, 1, byScore);
  const fromA = picked.find((p) => p.tag.startsWith('옛것') || p.tag.startsWith('최근'));
  if (!fromA) bad('자리 ㄱ 에서 아무것도 안 나왔습니다');
  else if (fromA.score !== 2) bad(`자리 ㄱ 이 ${fromA.tag} 을 내놨습니다 — 점수가 더 높은 것이 있는데 밀렸습니다`);
  else if (picked.length !== 2) bad(`자리 나눠 갖기가 깨졌습니다 — ${picked.length}건 (2건이어야 함)`);
  else ok('몫이 1건일 때 자리 ㄱ 은 「옛것 2점」을 내놓고, 자리 ㄴ 도 제 몫을 받음');

  // 비교 기준을 안 주면 예전 그대로 날짜순이어야 한다 — 확정 히트가 그 경로다.
  const legacy = pickSpread(groups, 2, 1);
  const legacyA = legacy.find((p) => p.tag.startsWith('옛것') || p.tag.startsWith('최근'));
  if (legacyA?.score !== 1) bad('비교 기준 없이 부르면 예전대로 날짜순이어야 하는데 아닙니다');
  else ok('비교 기준을 안 주면 예전 그대로 날짜순 (확정 히트 경로는 그대로)');
}

console.log('[5/6] 부분 일치 결과가 점수순이고, 몇 낱말 맞았는지가 보인다');
{
  /* 질의 둘을 함께 본다.
   *  ㄱ) 흔한 낱말 둘 + 어디에도 없는 낱말 하나.
   *      없는 낱말이 완전 일치를 **반드시** 막으므로 부분 일치가 확실히 난다. 고치기 전에는
   *      2점과 1점이 섞여 나왔는데, 고친 뒤에는 예산 12칸이 전부 2점으로 찬다 — 그게 요점이다.
   *      (실제 낱말 셋으로 고르면 완전 일치가 하나라도 생기는 순간 검사가 조용히 빠진다.
   *       '만기 연장 요청 공문' 이 그랬다.)
   *  ㄴ) 2026-08-18 사고의 그 질의. 2점짜리가 12칸보다 적어서
   *      2점과 1점이 섞여 나오고, 그래야 **순서**를 실제로 시험할 수 있다.
   * 둘 중 하나라도 점수가 섞여 있어야 한다 — 다 같은 점수면 순서는 통과가 아니라 못 잰 것이다. */
  const QUERIES = [`${FX.commonPair} zzqqxx없는낱말`, FX.incident];
  let mixedSeen = false;

  for (const Q of QUERIES) {
    const cases = [['대화', searchArchive({ query: Q, access: FULL_ACCESS })]];
    if (hasDocuments()) cases.push(['문서', searchDocuments({ query: Q, access: FULL_ACCESS })]);

    for (const [label, r] of cases) {
      if (!r.partial) { bad(`${label} 쪽 '${Q}' 가 부분 일치가 아닙니다 — 아카이브가 예상과 다릅니다`); continue; }
      const missing = r.hits.filter((h) => typeof h.score !== 'number' || typeof h.termCount !== 'number');
      if (missing.length) { bad(`${label} 쪽 ${missing.length}건에 score·termCount 가 없습니다 — 봇이 관련도를 못 봅니다`); continue; }
      const scores = r.hits.map((h) => h.score);
      if (new Set(scores).size > 1) mixedSeen = true;
      const inverted = scores.findIndex((s, i) => i > 0 && s > scores[i - 1]);
      if (inverted >= 0) bad(`${label} 쪽 '${Q}' 순서가 점수순이 아닙니다: ${scores.join(',')}`);
      else if (r.hits.some((h) => h.score >= h.termCount)) bad(`${label} 쪽에 낱말을 다 맞춘 것이 부분 일치로 들어 있습니다`);
      else ok(`${label} '${Q}' → ${r.hits.length}건 · 점수 ${scores.join(',')} · 낱말 ${r.hits[0].termCount}개`);
    }
  }
  if (!mixedSeen) bad('어느 질의도 점수가 섞여 나오지 않아 **순서를 실제로 시험하지 못했습니다**');
  else ok('점수가 섞인 결과가 있어 내림차순을 실제로 시험함');
}

/**
 * 「확정 히트가 적은」 질의를 **그때그때 만든다.**
 *
 * ── 왜 (2026-09-11) ──
 *
 * 아래 [6/6] 은 확정 히트가 1~2건인 자리에서만 잴 수 있는데, 질의가 자료 저장소의
 * **고정 문자열**이라 **자료가 늘면 저절로 빨개졌다.** 실제로 그날 첨부 12건이 들어오며
 * 씨앗 질의의 확정 히트가 2건에서 3건이 됐고, 코드는 한 글자도 안 바뀌었는데 `npm run
 * check` 가 빨개졌다. **고장이 아닌데 빨간 줄을 매일 보면 사람은 무시하는 법을 배운다.**
 *
 * 기준을 3건으로 올리는 식의 「느슨하게 고치기」는 하지 않는다 — 그건 시험이 겨눈 자리를
 * 옮기는 것이다. 대신 **씨앗 질의에 낱말 하나를 붙여 좁힌다**: 낱말을 더하면 전부 맞는
 * 문서만 남으므로 확정 히트가 준다. 붙일 낱말은 **그 확정 히트의 본문**에서 고르므로
 * 적어도 그 한 건은 계속 맞는다(1건 아래로는 안 내려간다).
 *
 * **성공 조건은 「확정이 1~2건」뿐이다.** 「부분 일치가 붙었나」는 여기서 안 본다 —
 * 그것까지 조건에 넣으면, 부분 일치가 통째로 죽은 진짜 회귀에서 **조건을 만족하는 질의가
 * 없다**가 되어 검사가 조용히 넘어간다. 그 판정은 아래 본 검사가 해야 한다.
 */
const TOKEN_RE = /[가-힣A-Za-z0-9]{2,12}/g;

function narrowToFew(seed, run, label, max = 2, tries = 12) {
  const look = (q) => {
    const r = run(q);
    return { r, confirmed: r.hits.filter((h) => typeof h.score !== 'number') };
  };
  const first = look(seed);
  if (!first.confirmed.length) {
    return { fail: `${label} '${seed}' 에 확정 히트가 없습니다 — 아카이브가 예상과 다릅니다` };
  }
  if (first.confirmed.length <= max) return { query: seed, ...first };

  const base = new Set((seed.toLowerCase().match(TOKEN_RE) || []));
  const words = [...new Set((first.confirmed[0].text || '').match(TOKEN_RE) || [])]
    .filter((w) => !base.has(w.toLowerCase()))
    .sort((a, b) => b.length - a.length)   // 긴 낱말이 더 잘 좁힌다
    .slice(0, tries);
  for (const w of words) {
    const q = `${seed} ${w}`;
    const got = look(q);
    if (got.confirmed.length >= 1 && got.confirmed.length <= max) {
      return { query: q, ...got, narrowed: true, from: first.confirmed.length };
    }
  }
  return {
    cannot: `${label} 씨앗 질의의 확정 히트가 ${first.confirmed.length}건이고, 낱말 ${words.length}개를 `
      + `붙여 봐도 ${max}건 이하로 못 좁혔습니다 — 「확정이 적을 때」 갈래를 이번에는 재지 못했습니다`,
  };
}

console.log('[6/6] 확정 히트가 적으면 부분 일치를 함께 준다');
{
  /* 왜: 안전망이 **0건일 때만** 켜져서, 낱말을 많이 붙인 좁은 질의가 딱 한 곳을 맞히면
   * 봇이 「찾았다」고 읽고 멈췄다. 2026-08-18 실측 — 낱말 넷을 붙인 질의는
   * 대화 확정 히트가 1건뿐인데, 같은 자리를 두 낱말로 물으면 다른 사업장이
   * 첫 히트로 나온다. 한 건 맞혔다고 멈추면 그 자리를 영영 못 본다. */
  // **대화·문서 양쪽을 다 본다.** 같은 모양으로 고쳐 놓고 한쪽만 재면, 나머지가 조용히
  // 옛 동작으로 남는다 (check-shared-rules.js 가 두 언어를 함께 보는 것과 같은 이유).
  const cases = [['대화', FX.narrowArchive, (q) => searchArchive({ query: q, access: FULL_ACCESS })]];
  if (hasDocuments()) cases.push(['문서', FX.narrowDocs, (q) => searchDocuments({ query: q, access: FULL_ACCESS })]);

  for (const [label, seed, run] of cases) {
    const pick = narrowToFew(seed, run, label);
    if (pick.fail) { bad(pick.fail); continue; }
    if (pick.cannot) {
      // 재지 못한 것은 **초록으로 넘기지 않는다.** 실패로도 만들지 않는다 — 자료가
      // 자란 것은 고장이 아니다. `[못잼]` 표시는 `check-setup.js` 가 화면에 올린다.
      console.log(`[못잼]  - ${pick.cannot}`);
      continue;
    }
    const { query: Q, r, confirmed } = pick;
    const extra = r.hits.filter((h) => typeof h.score === 'number');
    const how = pick.narrowed ? ` (씨앗 확정 ${pick.from}건 → 낱말 하나를 붙여 좁힘)` : '';

    if (!extra.length) bad(`${label} 확정 ${confirmed.length}건뿐인데 부분 일치가 안 붙었습니다 — 봇이 그것만 보고 멈춥니다`);
    // 확정이 먼저 와야 한다. 부분 일치가 앞에 오면 봇이 추측을 근거로 먼저 읽는다.
    else if (r.hits.findIndex((h) => typeof h.score === 'number') < confirmed.length) {
      bad(`${label} 부분 일치가 확정 히트보다 앞에 있습니다`);
    } else if (r.partial) bad(`${label} 확정 히트가 있는데 partial 플래그가 켜졌습니다 — 전부 추측이라는 뜻이 됩니다`);
    else if (!/일부만 맞은/.test(r.note || '')) bad(`${label} note 가 부분 일치를 함께 실었다고 안 밝힙니다: ${r.note}`);
    else ok(`${label} '${Q}' → 확정 ${confirmed.length}건 + 일부만 맞은 ${extra.length}건 · 확정이 앞 · note 있음${how}`);
  }

  /* 확정 히트가 넉넉하면 덧붙이지 않는다 — 안 그러면 모든 검색이 부분 일치로 불어난다.
   *
   * **낱말이 여럿인 질의라야 이걸 실제로 잰다.** 처음에는 낱말 하나로 대조했는데,
   * 낱말이 하나면 PARTIAL_MIN_TERMS 에 걸려 부분 일치가 **애초에 생기지 않는다** — 상한을
   * 50 으로 밀어도 초록이었다. 통과가 아니라 못 잰 것이었다 (2026-08-19). */
  const MANY = FX.many;
  const many = searchArchive({ query: MANY, access: FULL_ACCESS });
  if (many.partial) bad(`'${MANY}' 가 부분 일치라 「확정이 많을 때」를 시험하지 못했습니다`);
  else if (many.hits.length <= 2) bad(`'${MANY}' 확정 히트가 ${many.hits.length}건뿐이라 시험하지 못했습니다`);
  else if (many.hits.some((h) => typeof h.score === 'number')) bad(`'${MANY}' 는 확정 ${many.hits.length}건인데 부분 일치가 붙었습니다`);
  else ok(`'${MANY}' 확정 ${many.hits.length}건 — 부분 일치 안 붙음`);
}

process.exit(failed ? 1 : 0);
