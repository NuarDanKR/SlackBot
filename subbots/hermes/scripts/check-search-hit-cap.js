#!/usr/bin/env node
/**
 * 대화 히트 건당 상한 — 꺼져 있으면(기본) 동작이 그대로이고, 켜면 창 맞춤으로 잘리는가.
 *
 *   node scripts/check-search-hit-cap.js
 *
 * ── 왜 필요한가 ──
 * 대화 히트는 매칭된 메시지 블록 **전체**를 돌려줘 건당 상한이 없었다 (문서 히트만
 * docHitMaxChars 4,000). search 결과 평균 25,134자의 주범이고 문답 비용의 절반이
 * 도구 결과 캐시 쓰기다 (2026-09-10 실측). 상한은 config(searchHitMaxChars)로만 켠다 —
 * 재생 실측(scripts/measure-hit-cap.js)을 통과한 값만 넣기 위해서다.
 */
import { searchArchive } from '../src/archive.js';
import { FULL_ACCESS, config, TRUNC_PHRASE, truncMarker } from '../src/config.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

/* ⓪ 마커 문구 고정. **기대값은 리터럴 사본이어야 한다** — `TRUNC_PHRASE` 에서 가져오면
 * 동어반복이라 아무것도 못 막는다(상수를 고치면 기대값이 함께 움직인다).
 *
 * 왜 여기 있나: 문구를 config.js 한 자리로 모으면서(2026-09-11) 생산자도 소비자도 같은
 * 상수를 읽게 됐다. 갈림은 그래서 막혔지만, 대신 **상수를 실수로 한 글자 고쳐도 모든
 * 검사가 초록인** 구멍이 생겼다 — 고치기 전에는 검사가 문구의 독립 사본을 들고 있어
 * 그 실수가 빨개졌다. 이 한 줄이 그 구멍을 막는다. 이 문구는 봇에게 가는 텍스트라
 * 바꾸는 것은 곧 동작 변경이고, 바꿔야 한다면 이 기대값도 함께 고치는 것이 맞다.
 *
 * hint 판까지 보는 이유: 여는 「…」가 U+2026 한 글자인 것, 괄호, 힌트 앞 구분자가
 * 「. 」인 것까지 걸린다. 아카이브 상태와 무관한 계약이라 히트 0건 조기 종료보다 앞에 둔다. */
if (truncMarker() !== '…(길이 제한으로 잘림)') {
  fail(`마커 문구가 바뀌었습니다 — 봇에게 가는 텍스트가 바뀝니다: ${JSON.stringify(truncMarker())}`);
}
if (truncMarker('month 를 지정해 좁혀 보세요') !== '…(길이 제한으로 잘림. month 를 지정해 좁혀 보세요)') {
  fail(`마커 hint 판 조립이 바뀌었습니다 — read_channel 안내 텍스트가 바뀝니다: ${JSON.stringify(truncMarker('month 를 지정해 좁혀 보세요'))}`);
}

// 아카이브에 반드시 있는 낱말로 히트를 얻는다. 0건이면 검사 불능 — 그대로 알린다.
const QUERY = '보고';
const base = searchArchive({ query: QUERY, access: FULL_ACCESS });
if (!base.hits?.length) {
  console.error(`  ✗ 검사 불능 — '${QUERY}' 히트 0건. 낱말을 바꿔야 합니다.`);
  process.exit(1);
}

// ① 상한 미설정(기본): 어떤 히트도 잘리지 않는다.
if (config.limits.searchHitMaxChars == null) {
  const clipped = base.hits.filter((h) => h.text.includes(TRUNC_PHRASE));
  if (clipped.length) fail(`상한이 꺼져 있는데 잘린 히트 ${clipped.length}건 — 기본 동작이 바뀌었습니다.`);
} else {
  console.log(`  (config 에 searchHitMaxChars=${config.limits.searchHitMaxChars} 가 켜져 있어 기본-꺼짐 검사는 건너뜁니다)`);
}

// ② 상한을 인자로 강제하면: 모든 히트가 상한+여유 안이고, 히트 수는 같고, note 가 안내한다.
const CAP = 300;
const capped = searchArchive({ query: QUERY, access: FULL_ACCESS, hitMaxChars: CAP });
if (capped.hits.length !== base.hits.length) {
  fail(`상한이 히트 수를 바꿨습니다 ${base.hits.length} → ${capped.hits.length} — 상한은 본문만 잘라야 합니다.`);
}
const over = capped.hits.filter((h) => h.text.length > CAP + 200); // 잘림 문구·헤더 보존 여유
if (over.length) fail(`상한 ${CAP}자를 넘는 히트 ${over.length}건 (최대 ${Math.max(...capped.hits.map((h) => h.text.length))}자).`);
const anyClipped = capped.hits.some((h) => h.text.includes(TRUNC_PHRASE));
if (anyClipped && !/read_channel/.test(capped.note || '')) {
  fail('잘린 히트가 있는데 note 에 read_channel 안내가 없습니다.');
}

/* ③ note 조립이 undefined 를 문자열로 잇지 않는가.
 *
 * spreadNote(archive.js 764행)는 shown >= total(즉 확정 히트가 상한 안에 다 들어온
 * 흔한 경우)이면 note 로 `undefined` 를 돌려준다. 잘림 안내를 템플릿 리터럴로 그냥
 * 이어 붙이면 `"undefined 일부 히트는 길이 제한으로…"` 가 나간다 — 리뷰에서 재현된
 * Critical (hitMaxChars=50 강제 + '대출 이자'·'계약 체결' 질의). 위 QUERY('보고')는
 * 우연히 total > shown 경로만 타서 이 버그를 못 잡았다. */
const anyUndefinedLiteral = (obj) => JSON.stringify(obj).includes('undefined');
if (anyUndefinedLiteral(capped)) {
  fail(`상한을 강제한 결과 문자열에 "undefined" 리터럴이 섞여 있습니다 (note 조립에서 undefined 를 그대로 이었을 가능성) — ${JSON.stringify(capped.note)}`);
}

/* ③-보강: shown >= total 이라 spreadNote 가 진짜 undefined 를 내는 실물 질의를 하나 더
 * 재생해서, 그 분기의 note 가 안내 문장 **단독**으로 나오는지 직접 본다(문자열 검사만으로는
 * "undefined 뒤에 다른 말이 붙어 우연히 다른 문자열에 'undefined' 가 없는" 경우를 놓칠 수
 * 있어서, 여기서는 note 가 힌트와 정확히 같은지 본다). 아카이브가 바뀌어 이 질의가 더는
 * 그 분기를 안 타면(예: 히트가 늘어 total > shown) 건너뛰고 위 ③(문자열 전수 검사)로만
 * 판정한다 — 실물 재생은 보강이지 필수 전제가 아니다. */
const NARROW_QUERY = '보증 한도';
const narrowBase = searchArchive({ query: NARROW_QUERY, access: FULL_ACCESS });
if (narrowBase.hits.length > 2 && narrowBase.note === undefined) {
  const NARROW_CAP = 100;
  const narrowCapped = searchArchive({
    query: NARROW_QUERY, access: FULL_ACCESS, hitMaxChars: NARROW_CAP,
  });
  const narrowClipped = narrowCapped.hits.some((h) => h.text.includes(TRUNC_PHRASE));
  if (!narrowClipped) {
    fail(`'${NARROW_QUERY}' 를 ${NARROW_CAP}자로 눌렀는데 아무것도 안 잘렸습니다 — 재현 질의가 낡았을 수 있습니다.`);
  } else {
    const HINT = '일부 히트는 길이 제한으로 잘렸습니다 — 전문은 read_channel 로 보세요.';
    if (narrowCapped.note !== HINT) {
      fail(`shown>=total 분기(note 원래 undefined)에서 안내 문장이 단독으로 안 나옵니다 — 실제: ${JSON.stringify(narrowCapped.note)}`);
    }
  }
} else {
  console.log(`  (실물 재생 건너뜀 — '${NARROW_QUERY}' 가 더는 shown>=total 분기를 안 탑니다. 위 문자열 전수 검사로만 판정합니다.)`);
}

if (ok) console.log('[check-search-hit-cap] OK — 상한이 꺼지면 그대로, 켜면 본문만 창 맞춤으로 잘립니다.');
else { console.error('\n고칠 곳: src/archive.js scanArchive'); process.exitCode = 1; }
