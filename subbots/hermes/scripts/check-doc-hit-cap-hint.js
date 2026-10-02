#!/usr/bin/env node
/**
 * 문서 히트가 잘렸으면 note 가 「read_document 로 열어 보라」고 말하나 — 세 분기 전부에서.
 *
 *   node scripts/check-doc-hit-cap-hint.js
 *
 * 종료코드: 0 통과 / 1 어긋남 / 1 못 잼(세 분기를 다 밟지 못했을 때)
 *
 * ── 왜 필요한가 ──
 *
 * 확정 히트의 발췌는 회차 앞 docHitMaxChars(4,000자)에서 잘린다. 그런데 잘렸다는
 * 사실이 note 에 없어서, 봇은 그 발췌를 **회차 전체**로 읽었다. 값이 4,000자 밖에
 * 있는 큰 문서에서 「발췌에 없으니 아카이브에 없다」로 닫힌 사고가 2026-09-10 에
 * 재생 2/2 로 재현됐다.
 *
 * 대화 쪽(`archive.js` 의 CAP_HINT·noteWithCapHint)에는 같은 안내가 이미 있었다.
 * 이 검사는 **두 쪽이 다시 갈리는 것**을 막는다.
 *
 * ── 세 분기를 따로 밟는다 ──
 *
 * scanDocuments 는 히트를 세 가지 모양으로 돌려준다. **한 분기만 재고 「전 분기」라고
 * 적으면 나머지 둘의 래퍼는 지워져도 아무도 모른다** (이 검사의 첫 판이 실제로 그랬다 —
 * 질의가 전부 한 낱말이라 PARTIAL_MIN_TERMS=2 에 막혀 부분 일치 분기를 영영 안 밟았고,
 * 단언도 `read_document` 문자열이라 그 두 분기의 **기본 안내문에 이미 그 낱말이 있어**
 * 실패가 구조적으로 불가능했다. 회의적 검증에서 잡혔다, 2026-09-10).
 *
 *   ① 확정 + 부분 함께  — 확정 히트를 maxHits 로 눌러 partialAlsoWhenAtMost 아래로 만든다
 *   ② 부분만            — 확정 0건이 되는 헛낱말을 섞는다
 *   ③ 확정만            — 흔한 한 낱말
 *
 * **단언은 note 전용 문구(`길이 제한으로 잘렸습니다`)로 한다.** 본문 마커는
 * `길이 제한으로 잘림` 이라 글자가 다르고, 세 분기의 기본 note 어디에도 이 문구는 없다 —
 * 그래서 래퍼를 지우면 세 분기 모두에서 실제로 빨개진다.
 *
 * ── 왜 실물 아카이브로 도나 ──
 *
 * scanDocuments 는 아카이브를 직접 읽는 함수라 합성 입력을 넣을 자리가 없다.
 * `check-outside-hits.js`·`check-attachment-marks.js` 와 같은 방식으로 실물에 던지고,
 * **밟지 못한 분기가 있으면 「못 쟀다」로 실패**시킨다 — 안 셈을 통과로 읽지 않는다.
 */
import { searchDocuments } from '../src/documents.js';
import { FULL_ACCESS, TRUNC_PHRASE } from '../src/config.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

// note 에만 나오는 문구. 본문 마커(`길이 제한으로 잘림`)와 글자가 다르다 — 위 주석 참조.
const NOTE_HINT = /길이 제한으로 잘렸습니다/;

/* 사업장 이름이 아닌 일반 낱말만 쓴다 (check-business-names.js 가 「일반 낱말로 뺌」에
 * 넣어 둔 축들이다). 헛낱말은 어느 회차에도 없어서 확정 히트를 0건으로 만든다. */
const CASES = [
  { branch: '③ 확정만', opts: { query: '대출' } },
  { branch: '① 확정+부분', opts: { query: '대출 연체이자 상환', maxHits: 1 } },
  { branch: '② 부분만', opts: { query: '없는낱말xyz 대출' } },
];

/** 돌려받은 모양으로 어느 분기였는지 되짚는다. */
const branchOf = (r) => {
  if (r.partial) return '② 부분만';
  if (/뒤에 함께 실었습니다/.test(r.note || '')) return '① 확정+부분';
  return '③ 확정만';
};

const seen = new Set();

for (const c of CASES) {
  const r = searchDocuments({ ...c.opts, access: FULL_ACCESS });
  const hits = r.hits ?? [];
  const clipped = hits.filter((h) => h.text.includes(TRUNC_PHRASE));
  const got = branchOf(r);

  if (!hits.length) {
    fail(`${c.branch} — '${c.opts.query}' 로 히트 0건이라 못 쟀습니다. 낱말을 바꿔야 합니다.`);
    continue;
  }
  if (got !== c.branch) {
    fail(`${c.branch} 를 밟으려 했는데 ${got} 로 갔습니다 ('${c.opts.query}'). 자료가 바뀌어 낱말을 다시 골라야 합니다.`);
    continue;
  }
  if (!clipped.length) {
    fail(`${c.branch} — 잘린 히트를 하나도 못 만들었습니다 ('${c.opts.query}', 히트 ${hits.length}건). 낱말을 바꿔야 합니다.`);
    continue;
  }

  seen.add(c.branch);
  if (!NOTE_HINT.test(r.note || '')) {
    fail(`${c.branch} — 잘린 히트 ${clipped.length}건인데 note 에 잘림 안내가 없습니다. note=${JSON.stringify(r.note)}`);
  } else {
    pass(`${c.branch} — 히트 ${hits.length}건 중 잘림 ${clipped.length}건, note 가 잘림을 밝히고 read_document 로 보냅니다`);
  }
}

const missed = CASES.map((c) => c.branch).filter((b) => !seen.has(b));
if (missed.length) {
  console.error(`  ✗ 못 잼 — 밟지 못한 분기: ${missed.join(' · ')}. 이 검사는 세 분기를 다 밟아야 뜻이 있습니다.`);
  process.exit(1);
}
process.exit(ok ? 0 : 1);
