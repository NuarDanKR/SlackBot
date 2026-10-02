#!/usr/bin/env node
/**
 * 「좁혔는데 못 찾았으면 밖도 본다」 안전망 점검 — 실제 아카이브를 상대로 돈다.
 *
 * 왜 있나: `where` 는 하드 필터라 지정한 사업장 말고는 아무것도 안 보인다
 * (archive.js 의 `channels = [r.name]`, documents.js 의 `docs = documentsFor(...)`).
 * 통합 전에는 봇이 한 질문에 검색을 여러 번 부르며 좁힘과 넓힘을 섞어서 문제가 안 됐는데,
 * 통합으로 왕복이 줄면서 그 넓은 호출이 사라졌다. 품질 대조에서 잃은 근거를 원본에서
 * 되짚으면 둘 다 **좁힘 없는 검색**이 잡은 것이었다.
 *
 * 보는 것 여덟, 대화·문서 양쪽 모두:
 *  ① 좁혔는데 확정 0건 → 밖의 것이 붙는다
 *  ② 좁혔는데 확정이 있다 → 안 붙는다 (지금 잘 도는 경로를 안 건드린다)
 *  ③ where 없이 부르면 → 안 붙는다 (안 좁힌 검색까지 두 번 훑지 않는다)
 *  ④ 밖의 것에 좁힌 그 사업장이 안 섞인다
 *  ⑤ 못 보는 비공개 채널이 밖으로 안 샌다
 *  ⑥ document 를 함께 주면 안 붙는다 (문서 하나를 짚었다는 뜻)
 *  ⑦ 밖의 것이 전부 부분 일치면 구역 머리에 「일부만 맞은 결과」가 붙는다
 *  ⑧ config 를 0 으로 두면 완전히 꺼진다 (되돌리기가 실제로 되나)
 *
 * 실행: node scripts/check-outside-hits.js
 */
import { searchArchive, assertArchive } from '../src/archive.js';
import { searchDocuments, hasDocuments } from '../src/documents.js';
import { config, FULL_ACCESS, PUBLIC_ACCESS } from '../src/config.js';
import { buildTools } from '../src/claude.js';
import { loadFixtures, skipNote } from './fixtures.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

// 아카이브가 없는 새 클론에서 스택 트레이스가 npm run check 화면에 끼지 않게 한다
// (check-partial-hits.js 와 같은 이유·같은 처리).
try {
  assertArchive();
} catch (e) {
  console.log(`[못잼]  - 건너뜀: ${e.message.split('\n')[0]}`);
  process.exit(0);
}

const A = (q, where, access = PUBLIC_ACCESS) => searchArchive({ query: q, channel: where, access });
const D = (q, where, access = PUBLIC_ACCESS, document) =>
  searchDocuments({ query: q, project: where, document, access });

/* [8/8] 은 되돌리기(config 를 0/-1 로 두는 것)가 실제로 듣는지를 잰다. [1/8]~[4/8]·[6/8]·[7/8]
 * 은 기능이 **켜져 있을 때**의 동작을 잰다 — 그래서 되돌리기를 당겨(=0 으로 설정) 이 스크립트를
 * 그대로 돌리면 [1/8]·[4/8] 이 "밖의 것이 안 붙었다"·"아무것도 재지 못했다"로 빨갛게 뜬다.
 * 사실이 아니라 기능이 꺼져 있을 뿐이라, 아래에서 이 상태를 먼저 가려낸다.
 *
 * **[5/8] 은 그 갈래에 안 넣는다 (2026-09-03).** 자기가 창을 PROBE_MAX 로 넓혀 재는 검사라
 * 설정값과 무관하게 성립하고, 재는 것이 「못 보는 비공개가 밖으로 새나」다 — 안전망을 껐다고
 * 안 재도 되는 축이 아니다. 그전에는 이 갈래에 함께 묻혀 꺼졌다. */
/* 질의는 **자료 저장소**에서 온다 (`scripts/fixtures.js` 참조). 코드 저장소는 팀끼리
 * 나눠 쓰므로 사업장 이름을 여기 둘 수 없고, 그렇다고 가짜 이름으로 바꾸면 없는
 * 사업장으로 좁히게 되어 **에러 없이 헛돈다** — 아무것도 안 나온 것을 「안 샜다」로
 * 읽는다. 없으면 재지 못했다고 말하고 건너뛴다. */
const FX = loadFixtures('outsideHits', [
  'confirmedNoneArchive', 'confirmedNoneDocs', 'confirmedSomeArchive', 'confirmedSomeDocs',
  'alias', 'privQ', 'docPrivQ', 'documentWord', 'mixedArchive', 'mixedDocs',
]);
if (!FX) {
  console.log(skipNote('outsideHits'));
  process.exit(0);
}
const CONFIRMED_NONE_ARCHIVE = FX.confirmedNoneArchive;  // 대화 확정 0 · 부분만
const CONFIRMED_NONE_DOCS = FX.confirmedNoneDocs;        // 문서 확정 0 · 부분만

function checkKillSwitch() {
  console.log('[8/8] config 를 0 으로 두면 완전히 꺼진다');
  const saved = config.limits.outsideWhenNarrowedMaxHits;
  config.limits.outsideWhenNarrowedMaxHits = 0;
  try {
    const a = A(...CONFIRMED_NONE_ARCHIVE);
    const d = hasDocuments() ? D(...CONFIRMED_NONE_DOCS) : { outside: undefined };
    if (a.outside?.length || d.outside?.length) bad('0 으로 뒀는데 밖의 것이 붙습니다 — 되돌리기가 안 됩니다');
    else ok('0 이면 대화·문서 둘 다 안 붙음');
  } finally {
    config.limits.outsideWhenNarrowedMaxHits = saved;
  }

  /* config.json 은 사람이 손으로 고치는 파일이라 음수가 들어갈 수 있다. `slice(0, -1)` 은
   * 빈 배열이 아니라 "마지막 하나만 뺀 전부" 라, `max <= 0` 가드가 없으면 거의 다 붙는다 —
   * 0 하나만 보면 이 구멍을 놓친다. */
  config.limits.outsideWhenNarrowedMaxHits = -1;
  try {
    const a = A(...CONFIRMED_NONE_ARCHIVE);
    const d = hasDocuments() ? D(...CONFIRMED_NONE_DOCS) : { outside: undefined };
    if (a.outside?.length || d.outside?.length) bad('-1 로 뒀는데 밖의 것이 붙습니다 — 음수 방어가 없습니다');
    else ok('-1 이어도 대화·문서 둘 다 안 붙음');
  } finally {
    config.limits.outsideWhenNarrowedMaxHits = saved;
  }
}

const configuredMax = config.limits.outsideWhenNarrowedMaxHits ?? 0;
if (configuredMax <= 0) {
  /* **표시를 달아 화면에 올린다.** 여기서 종료코드는 0 이라 `check-setup.js` 는 이 검사를
   * 통과로 적는다. 그 줄이 말버릇 목록(`건너뜀|재지 못|못 잼|못 쟀`)에 안 물리면 통과 화면에
   * **초록 한 줄만** 남고, 안전망 축 여섯이 안 돈 것이 어디에도 안 보인다 —
   * 2026-09-03 까지 이 줄이 「건너뛰**고**」라 실제로 그랬다. `[못잼]` 은 문구가 아니라
   * 표시로 찾으므로 다음에 말을 바꿔도 안 갈린다 (`check-setup.js` 의 그 자리 주석 참조). */
  console.log(
    `[못잼]  - 기능이 꺼져 있습니다 (config.limits.outsideWhenNarrowedMaxHits = ${configuredMax}) — ` +
      '되돌리기 상태이므로 [1/8]~[4/8]·[6/8]·[7/8]은 재지 못했습니다.'
  );
  /* **[5/8] 만은 값과 무관하게 돈다.** 「못 보는 비공개 채널이 밖으로 새나」는 안전망을
   * 껐다고 안 재도 되는 축이 아니고, 실제로 이 함수는 자기가 창을 넓혀 재므로 설정값이
   * 0 이어도 그대로 성립한다. [8/8] 은 끄기가 실제로 듣는지를 잰다. */
  checkPrivateLeak();
  checkKillSwitch();
  process.exit(failed ? 1 : 0);
}

/* fixture 는 실제 아카이브에 기대고 있다. 아카이브가 자라 전제가 깨지면 조용히 통과시키지
 * 않고 「아카이브가 예상과 다릅니다」로 떨어뜨린다 — 통과와 못 잰 것은 다르다.
 * CONFIRMED_NONE_ARCHIVE·CONFIRMED_NONE_DOCS 는 [8/8] 옆에서 이미 선언했다.
 * [4/8] 은 안 좁힌(wide) 상위 max 안에 그 사업장이 실제로 있어야 필터 제거를 잡는다. 예전
 * 앞서 쓰던 질의는 그 사업장이 넓은 검색에서 9위라 상한(3) 밖이라 못 잡았다
 * (2026-08-19 재검토). 지금 질의는 1위라 필터를 없애면 바로 드러난다. */
const CONFIRMED_SOME_ARCHIVE = FX.confirmedSomeArchive;  // 그 사업장 안에 확정 히트가 있다
const CONFIRMED_SOME_DOCS = FX.confirmedSomeDocs;

const hasConfirmed = (r) => r.hits.some((h) => typeof h.score !== 'number');

console.log('[1/8] 좁혔는데 확정 0건 → 밖의 것이 붙는다');
{
  const a = A(...CONFIRMED_NONE_ARCHIVE);
  if (hasConfirmed(a)) bad(`대화 '${CONFIRMED_NONE_ARCHIVE[0]}'@${CONFIRMED_NONE_ARCHIVE[1]} 에 확정 히트가 생겼습니다 — 아카이브가 예상과 다릅니다`);
  else if (!a.outside?.length) bad('대화: 확정 0건인데 밖의 것이 안 붙었습니다 — 봇이 그 사업장만 보고 멈춥니다');
  else ok(`대화 밖 ${a.outside.length}건 (${a.outside.map((h) => h.channel).join(', ')})`);

  if (hasDocuments()) {
    const d = D(...CONFIRMED_NONE_DOCS);
    if (hasConfirmed(d)) bad(`문서 '${CONFIRMED_NONE_DOCS[0]}'@${CONFIRMED_NONE_DOCS[1]} 에 확정 히트가 생겼습니다 — 아카이브가 예상과 다릅니다`);
    else if (!d.outside?.length) bad('문서: 확정 0건인데 밖의 것이 안 붙었습니다');
    else ok(`문서 밖 ${d.outside.length}건 (${d.outside.map((h) => h.project).join(', ')})`);
  }
}

console.log('[2/8] 좁혔는데 확정이 있다 → 안 붙는다');
{
  const a = A(...CONFIRMED_SOME_ARCHIVE);
  if (!hasConfirmed(a)) bad(`대화 '${CONFIRMED_SOME_ARCHIVE[0]}'@${CONFIRMED_SOME_ARCHIVE[1]} 에 확정 히트가 없습니다 — 아카이브가 예상과 다릅니다`);
  else if (a.outside?.length) bad(`대화: 확정 ${a.hits.length}건인데 밖의 것이 붙었습니다 — 모든 검색이 불어납니다`);
  else ok(`대화 확정 ${a.hits.length}건 · 밖 안 붙음`);

  if (hasDocuments()) {
    const d = D(...CONFIRMED_SOME_DOCS);
    if (!hasConfirmed(d)) bad(`문서 '${CONFIRMED_SOME_DOCS[0]}'@${CONFIRMED_SOME_DOCS[1]} 에 확정 히트가 없습니다 — 아카이브가 예상과 다릅니다`);
    else if (d.outside?.length) bad(`문서: 확정 ${d.hits.length}건인데 밖의 것이 붙었습니다`);
    else ok(`문서 확정 ${d.hits.length}건 · 밖 안 붙음`);
  }
}

console.log('[3/8] where 없이 부르면 안 붙는다');
{
  const a = searchArchive({ query: CONFIRMED_NONE_ARCHIVE[0], access: PUBLIC_ACCESS });
  if (a.outside?.length) bad('대화: 안 좁힌 검색에 밖의 것이 붙었습니다 — 같은 것을 두 번 훑고 있습니다');
  else ok('대화: 안 좁힌 검색에는 안 붙음');

  if (hasDocuments()) {
    const d = searchDocuments({ query: CONFIRMED_NONE_DOCS[0], access: PUBLIC_ACCESS });
    if (d.outside?.length) bad('문서: 안 좁힌 검색에 밖의 것이 붙었습니다');
    else ok('문서: 안 좁힌 검색에는 안 붙음');
  }
}

console.log('[4/8] 밖의 것에 좁힌 그 사업장이 안 섞인다');
{
  const maxN = config.limits.outsideWhenNarrowedMaxHits ?? 0;

  /* 전제: 안 좁힌(wide) 상위 maxN 안에 그 채널/사업장이 실제로 있어야 이 검사가 필터 제거를
   * 잡을 수 있다 — 없으면 필터를 통째로 없애도 우연히 밖으로 안 밀려 초록만 나온다. 아카이브가
   * 자라 랭킹이 밀리면 같은 일이 조용히 재발하므로 fixture 만 바꾸지 않고 전제를 여기 박는다. */
  const wideA = searchArchive({ query: CONFIRMED_NONE_ARCHIVE[0], access: PUBLIC_ACCESS });
  if (!wideA.hits.slice(0, maxN).some((h) => h.channel === CONFIRMED_NONE_ARCHIVE[1])) {
    bad(`대화: 안 좁힌 상위 ${maxN}건에 ${CONFIRMED_NONE_ARCHIVE[1]} 이 없습니다 — 아카이브가 예상과 달라 이 검사가 필터 제거를 못 잡습니다`);
  } else {
    const a = A(...CONFIRMED_NONE_ARCHIVE);
    const dupe = (a.outside || []).filter((h) => h.channel === CONFIRMED_NONE_ARCHIVE[1]);
    if (dupe.length) bad(`대화: 밖의 것에 좁힌 채널(${CONFIRMED_NONE_ARCHIVE[1]})이 ${dupe.length}건 섞였습니다 — 같은 자료가 두 번 실립니다`);
    else ok(`대화: 밖에 ${CONFIRMED_NONE_ARCHIVE[1]} 이 안 섞임`);
  }

  const wideD = hasDocuments()
    ? searchDocuments({ query: CONFIRMED_NONE_DOCS[0], access: PUBLIC_ACCESS })
    : null;
  if (wideD) {
    if (!wideD.hits.slice(0, maxN).some((h) => h.project === CONFIRMED_NONE_DOCS[1])) {
      bad(`문서: 안 좁힌 상위 ${maxN}건에 ${CONFIRMED_NONE_DOCS[1]} 이 없습니다 — 아카이브가 예상과 달라 이 검사가 필터 제거를 못 잡습니다`);
    } else {
      const d = D(...CONFIRMED_NONE_DOCS);
      const dd = (d.outside || []).filter((h) => h.project === CONFIRMED_NONE_DOCS[1]);
      if (dd.length) bad(`문서: 밖의 것에 좁힌 사업장(${CONFIRMED_NONE_DOCS[1]})이 ${dd.length}건 섞였습니다`);
      else ok(`문서: 밖에 ${CONFIRMED_NONE_DOCS[1]} 이 안 섞임`);
    }
  }

  /* 위는 `where` 에 **푼 이름 그대로**를 준 케이스라, 구현을 생문자열 비교로
   * 바꿔도 출력이 글자까지 같아 못 잡는다. 설계 제약은 「사람이 적은 문자열이 아니라 푼
   * 이름으로 가른다」이므로, 같은 질의를 별칭(줄여 쓴 이름)으로 불러 따로 잰다. wide 는 위 wideA 와
   * 같은 질의라 재사용한다.
   * 그 별칭이 한 사업장으로 안 풀리면(후보 2개 이상) 밖이 아예 안 붙어 조용히 초록이
   * 되므로, 그 경우는 "아무것도 재지 못했습니다"로 떨어뜨린다. */
  const ALIAS = FX.alias;
  if (!wideA.hits.slice(0, maxN).some((h) => h.channel === CONFIRMED_NONE_ARCHIVE[1])) {
    bad(`대화: 안 좁힌 상위 ${maxN}건에 ${CONFIRMED_NONE_ARCHIVE[1]} 이 없습니다 — 별칭 검사가 필터 제거를 못 잡습니다`);
  } else {
    const aAlias = A(CONFIRMED_NONE_ARCHIVE[0], ALIAS);
    if (!aAlias.outside?.length) {
      bad(`대화: '${CONFIRMED_NONE_ARCHIVE[0]}'@${ALIAS}(별칭) 이 밖을 하나도 안 내놓습니다 — **이 검사가 아무것도 재지 못했습니다**`
        + `
      질의가 낡았을 수 있습니다 — 고칠 자리: check-fixtures.json 의 outsideHits.confirmedNoneArchive · alias`);
    } else {
      const dupeAlias = aAlias.outside.filter((h) => h.channel === CONFIRMED_NONE_ARCHIVE[1]);
      if (dupeAlias.length) {
        bad(`대화: 별칭('${ALIAS}')으로 좁혀도 밖의 것에 그 사업장(${CONFIRMED_NONE_ARCHIVE[1]})이 ${dupeAlias.length}건 섞였습니다 — 푼 이름이 아니라 생문자열로 가르고 있습니다`);
      } else {
        ok(`대화: 별칭('${ALIAS}') 으로 좁혀도 밖에 ${CONFIRMED_NONE_ARCHIVE[1]} 이 안 섞임 (밖: ${aAlias.outside.map((h) => h.channel).join(', ')})`);
      }
    }
  }

  /* 문서 쪽도 같은 이유로 별칭 검사를 둔다 — documents.js 의 껍데기(searchDocuments 의
   * `h.project !== r.name`)를 사람이 적은 문자열(`opts.project`)과 비교하도록 바꾸는 실수는
   * 푼 이름을 그대로 준 위 [4/8] 앞부분으로는 안 잡힌다(출력이 글자까지 같다). wide 는 위
   * wideD 와 같은 질의라 재사용한다. */
  if (wideD) {
    if (!wideD.hits.slice(0, maxN).some((h) => h.project === CONFIRMED_NONE_DOCS[1])) {
      bad(`문서: 안 좁힌 상위 ${maxN}건에 ${CONFIRMED_NONE_DOCS[1]} 이 없습니다 — 별칭 검사가 필터 제거를 못 잡습니다`);
    } else {
      const dAlias = D(CONFIRMED_NONE_DOCS[0], ALIAS);
      if (!dAlias.outside?.length) {
        bad(`문서: '${CONFIRMED_NONE_DOCS[0]}'@${ALIAS}(별칭) 이 밖을 하나도 안 내놓습니다 — **이 검사가 아무것도 재지 못했습니다**`
        + `
      질의가 낡았을 수 있습니다 — 고칠 자리: check-fixtures.json 의 outsideHits.confirmedNoneDocs · alias`);
      } else {
        const dupeAliasD = dAlias.outside.filter((h) => h.project === CONFIRMED_NONE_DOCS[1]);
        if (dupeAliasD.length) {
          bad(`문서: 별칭('${ALIAS}')으로 좁혀도 밖의 것에 그 사업장(${CONFIRMED_NONE_DOCS[1]})이 ${dupeAliasD.length}건 섞였습니다 — 푼 이름이 아니라 생문자열로 가르고 있습니다`);
        } else {
          ok(`문서: 별칭('${ALIAS}') 으로 좁혀도 밖에 ${CONFIRMED_NONE_DOCS[1]} 이 안 섞임 (밖: ${dAlias.outside.map((h) => h.project).join(', ')})`);
        }
      }
    }
  }
}

checkPrivateLeak();

/* [5/8] 은 **되돌리기 상태에서도 돈다.** 재는 성질이 「공개 권한에서 비공개가 밖으로
 * 새나」라 상한 값과 무관하고(아래 주석 참조), 실제로 이 함수는 자기가 창을 PROBE_MAX 로
 * 넓혀 재고 끝에 되돌린다. 그래서 위 되돌리기 갈래에서도 이것만은 부른다 —
 * **건너뛰면 안 되는 축**이다. 2026-09-03 까지는 `outsideWhenNarrowedMaxHits` 가 0 이면
 * 이 축까지 함께 꺼졌고, 그 사실이 화면에 초록 한 줄로만 남았다. */
function checkPrivateLeak() {
  console.log('[5/8] 못 보는 비공개 채널이 밖으로 안 샌다');
  /* 「전부 열림」에서는 비공개가 실제로 나오는 질의를 골랐다 — 안 그러면 이 검사는
   * 아무것도 안 재고 초록만 낸다.
   *
   * **이 검사만 창을 넓혀 부른다.** 재는 성질은 「공개 권한에서 비공개가 밖으로 새나」이고
   * 그것은 상한과 **무관하다** — 거르는 자리가 상한을 자르기 전이기 때문이다(archive.js 의
   * `scanArchive` 안에서 access 로 거르고, `slice(0, max)` 는 그 뒤다). 그런데 설정된
   * 상한(3)을 그대로 쓰면 비공개 채널이 밖 상위 3건에 **들어야만** 이 검사가 무엇을 잰다.
   * 그 순위는 아카이브가 자라면 밀린다 — 2026-08-19 에는 이 질의의 밖 1위가 어느 비공개
   * 채널이었는데 2026-08-28 에는 다른 사업장 블록이 앞을 채워 그것이 밖 4위로 밀렸고, 검사가
   * "아무것도 재지 못했습니다"로 떨어졌다. **뒤집힌 자리는 커밋 `fed28a9`(그날 07:00 자동
   * 반영)이다** — 그 커밋 앞뒤의 아카이브를 각각 꺼내 같은 질의를 돌려 확인했다(밖 2위 →
   * 4위, 통과 → 실패). 대화 24건이 들어온 큰 회차도 아니고 4건짜리 평범한 회차였다.
   * fixture 를 다른 질의로 바꾸는 것은 같은 복권을 다시 사는 것이라 또 밀린다.
   *
   * 넓혀서 잃는 것은 없다 — 상한 3에서 새는 것은 넓은 창에서도 새므로 넓은 창이 **더 엄한**
   * 검사다. 몇 위에서 잡혔는지는 아래에서 함께 찍어, 다음에 또 밀리면 눈에 보이게 한다. */
  const PROBE_MAX = 50;
  const PRIV_Q = FX.privQ;
  const priv = new Set((config.privateChannels || []).map((s) => s.replace(/^#/, '')));

  const savedMax = config.limits.outsideWhenNarrowedMaxHits;
  config.limits.outsideWhenNarrowedMaxHits = PROBE_MAX;
  try {

    /* 몇 위에서 잡혔는지를 함께 적는다. 설정된 상한(savedMax) 밖이면 「그 창으로는 못 쟀을
     * 것」이라는 뜻이라, 다음에 또 밀렸을 때 사람이 이 줄만 보고 안다. 실패로는 올리지 않는다 —
     * 넓힌 창에서 제대로 재고 있으므로 사실이 아니고, 매번 빨개지면 진짜 실패가 묻힌다. */
    const rank = (list, key) => list.findIndex((h) => priv.has(h[key])) + 1;
    const rankNote = (n) => (n > savedMax ? ` · 밖 ${n}위 — 설정 상한 ${savedMax} 밖이라 넓혀서 쟀습니다` : ` · 밖 ${n}위`);

    const full = A(PRIV_Q[0], PRIV_Q[1], FULL_ACCESS);
    const seenPrivate = (full.outside || []).filter((h) => priv.has(h.channel));
    if (!seenPrivate.length) {
      bad(`대화: '${PRIV_Q[0]}'@${PRIV_Q[1]} 이 전부 열림에서도 비공개를 안 내놓습니다 (밖 ${PROBE_MAX}건까지 봤습니다) — **이 검사가 아무것도 재지 못했습니다**`
        + `
      질의가 낡았을 수 있습니다 — 고칠 자리: check-fixtures.json 의 outsideHits.privQ`);
    } else {
      const pub = A(PRIV_Q[0], PRIV_Q[1], PUBLIC_ACCESS);
      const leaked = (pub.outside || []).filter((h) => priv.has(h.channel));
      if (leaked.length) bad(`대화: 공개 권한인데 비공개 채널이 밖으로 샜습니다: ${leaked.map((h) => h.channel).join(', ')}`);
      else ok(`대화: 전부 열림에선 비공개 ${seenPrivate.length}건이 보이고, 공개만에선 0건 (실제로 잼)${rankNote(rank(full.outside, 'channel'))}`);
    }

    if (hasDocuments()) {
      /* 문서 쪽도 같은 이유로 「전부 열림」에서 비공개가 실제로 나오는 질의를 골랐다.
       * 2026-08-19 재검토 실측: 이 질의의 FULL 밖 목록에 비공개 사업장 둘
       * 이 2건 섞여 있고, PUBLIC 은 마스킹(maskProject)으로 '_승인자료' 로만 보여 0건이다.
       * 창을 넓힌 이유는 대화 쪽과 같다 — 이쪽도 상한 3 안에 드느냐는 랭킹 운이었다. */
      const DOC_PRIV_Q = FX.docPrivQ;
      const fullD = D(DOC_PRIV_Q[0], DOC_PRIV_Q[1], FULL_ACCESS);
      const seenPrivateDocs = (fullD.outside || []).filter((h) => priv.has(h.project));
      if (!seenPrivateDocs.length) {
        bad(`문서: '${DOC_PRIV_Q[0]}'@${DOC_PRIV_Q[1]} 이 전부 열림에서도 비공개를 안 내놓습니다 (밖 ${PROBE_MAX}건까지 봤습니다) — **이 검사가 아무것도 재지 못했습니다**`
        + `
      질의가 낡았을 수 있습니다 — 고칠 자리: check-fixtures.json 의 outsideHits.docPrivQ`);
      } else {
        const pubD = D(DOC_PRIV_Q[0], DOC_PRIV_Q[1], PUBLIC_ACCESS);
        const leakedDocs = (pubD.outside || []).filter((h) => priv.has(h.project));
        if (leakedDocs.length) bad(`문서: 공개 권한인데 비공개 사업장이 밖으로 샜습니다: ${leakedDocs.map((h) => h.project).join(', ')}`);
        else ok(`문서: 전부 열림에선 비공개 ${seenPrivateDocs.length}건이 보이고, 공개만에선 0건 (실제로 잼)${rankNote(rank(fullD.outside, 'project'))}`);
      }
    }

  } finally {
    config.limits.outsideWhenNarrowedMaxHits = savedMax;
  }
}

console.log('[6/8] document 를 함께 주면 안 붙는다');
{
  if (!hasDocuments()) ok('문서 아카이브가 없어 건너뜀');
  else {
    const d = D(CONFIRMED_NONE_DOCS[0], CONFIRMED_NONE_DOCS[1], PUBLIC_ACCESS, FX.documentWord);
    if (d.outside?.length) bad('문서 하나를 짚었는데 다른 사업장을 뒤졌습니다');
    else ok('document 지정 시 밖 안 붙음');
  }
}

console.log('[7/8] 밖의 것이 전부 부분 일치면 구역 머리에 표시가 붙는다');
{
  /* 왜 있나: 항목마다 `(1/4 낱말)` 은 붙지만 **구역 머리에는** 아무 표시가 없어서, 네 낱말 중
   * 하나만 걸린 줄이 확정 결과와 같은 모양으로 「3건」 목록 안에 앉는다. 본 구역은 이미
   * 「— **일부만 맞은 결과**」를 단다(claude.js 의 sectionText). 밖 구역만 그 자리에 `false`
   * 가 못박혀 있었다 (2026-08-20).
   *
   * 섞였을 때(확정+부분)는 **안 붙어야 한다** — 그 표시는 「전부 추측」이라는 뜻이라
   * (archive.js 534행) 확정이 하나라도 있으면 거짓이 된다. */
  const search = buildTools({ access: PUBLIC_ACCESS, touched: new Set() }).find((t) => t.name === 'search');
  const FLAG = '일부만 맞은 결과';
  const head = (text, kind) => text.split('\n').find((l) => l.startsWith(`## 다른 사업장에서도 (${kind})`)) || '';
  const shape = (hits) => {
    const p = hits.filter((h) => typeof h.score === 'number').length;
    return p === hits.length ? '전부부분' : p === 0 ? '전부확정' : '섞임';
  };

  const cases = [
    { kind: '대화', only: 'archive', q: CONFIRMED_NONE_ARCHIVE[0], w: CONFIRMED_NONE_ARCHIVE[1], want: '전부부분' },
    { kind: '대화', only: 'archive', q: FX.mixedArchive[0], w: FX.mixedArchive[1], want: '섞임' },
    { kind: '문서', only: 'documents', q: CONFIRMED_NONE_DOCS[0], w: CONFIRMED_NONE_DOCS[1], want: '전부부분' },
    { kind: '문서', only: 'documents', q: FX.mixedDocs[0], w: FX.mixedDocs[1], want: '섞임' },
  ];
  for (const c of cases) {
    if (c.only === 'documents' && !hasDocuments()) { ok(`${c.kind}: 문서 아카이브가 없어 건너뜀`); continue; }
    const r = c.only === 'archive' ? A(c.q, c.w) : D(c.q, c.w);
    // fixture 가 실제 아카이브에 기대고 있다. 전제가 깨지면 조용히 통과시키지 않는다.
    if (!r.outside?.length) { bad(`${c.kind} '${c.q}': 밖의 것이 안 붙었습니다 — 아카이브가 예상과 다릅니다`); continue; }
    if (shape(r.outside) !== c.want) {
      bad(`${c.kind} '${c.q}': 밖이 ${shape(r.outside)} 인데 ${c.want} 을 기대했습니다 — 아카이브가 예상과 다릅니다`);
      continue;
    }
    const line = head(await search.run({ query: c.q, where: c.w, only: c.only }), c.kind);
    if (!line) { bad(`${c.kind} '${c.q}': 밖 구역이 안 그려졌습니다`); continue; }
    const has = line.includes(FLAG);
    if (c.want === '전부부분' && !has) bad(`${c.kind}: 밖이 전부 부분 일치인데 머리에 표시가 없습니다 — ${line}`);
    else if (c.want === '섞임' && has) bad(`${c.kind}: 밖에 확정이 섞였는데 「전부 추측」 표시가 붙었습니다 — ${line}`);
    else ok(`${c.kind}: 밖이 ${c.want} → 표시 ${has ? '붙음' : '안 붙음'}`);
  }
}

checkKillSwitch();

process.exit(failed ? 1 : 0);
