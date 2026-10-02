#!/usr/bin/env node
/**
 * search 건당 상한의 후보 값을 실제 질의 재생으로 잰다 — autoNarrow(2026-09-03,
 * 실질의 78개 재생) 와 같은 방법. 이 시스템의 결과 축소 실패는 두 번 다
 * 「조용한 자료 없습니다 오답」이었으므로(2026-08-05 · 08-18), 축소는 재생 실측을
 * 통과한 값만 config 에 넣는다.
 *
 *   node scripts/measure-hit-cap.js
 *
 * 재는 것:
 *   · 히트 수 불변  — 상한은 본문만 잘라야 한다 (어긋나면 즉시 실패)
 *   · 절감률       — 후보 상한별 총 자수 감소
 *   · 낱말 보존률  — 잘린 뒤에도 질의의 서로 다른 낱말이 전부 남아 있는 히트 비율
 *                    (clipPartial 이 창을 고르는 기준이므로 대부분 100% 근처여야 한다)
 *
 * ── 질의를 어디서 꺼내나 ──
 *
 * `hermes-log/*.md` 의 `> **도구** search(...)` 줄을 `parseToolLine`(src/convo-log.js)
 * 으로 되읽는다. 이 파서는 렌더(`toolLine`, convo-log.js)의 역함수인데, `toolLine` 이
 * search 도구의 **모든** 입력 값(query·where·document·only)을 키 없이 쉼표로 이어붙여
 * 적기 때문에(예: `search(어느 사업장 할인분양 보고, 그 사업장 채널)`), 되읽은
 * `call.args` 는 순수 query 가 아니라 **그 회차의 입력 값 전체를 이어붙인 문자열**이다.
 * 인자 키는 렌더에서부터 사라져 복원할 수 없다(convo-log.js 의 parseToolLine 주석
 * "인자 **키**도 마찬가지다" 참조). 그래서 이 문자열을 그대로 질의로 재생한다 — where 가
 * 섞여 들어간 회차는 실제보다 낱말이 하나 늘어난 질의가 되지만, 상한 후보를 대는
 * base(무제한) 대 capped(상한) 비교는 **같은 문자열**을 양쪽에 똑같이 넣으므로
 * 내부 일관성은 유지된다. 통계는 report 의 이상 징후 절에 남긴다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { parseToolLine } from '../src/convo-log.js';
import { searchArchive } from '../src/archive.js';
import { FULL_ACCESS, LOG_DIR, LOG_ENABLED } from '../src/config.js';
import { splitTerms, scoreTerms, clipPartial } from '../src/search-terms.js'; // eslint-disable-line no-unused-vars -- clipPartial 은 searchArchive 내부에서 쓰이는 문서화용 참조

if (!LOG_ENABLED || !LOG_DIR) {
  console.error('대화 로그가 꺼져 있습니다 (config.json 의 log.enabled · log.path).');
  process.exit(2);
}

// hermes-log md 에서 search 질의를 모은다 (parseToolLine 규약 — run-log-measure.js 참조).
const queries = [];
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
      if (call.truncated) truncatedN += 1;
      // 쉼표가 있으면 where·document·only 중 하나가 query 뒤에 함께 이어붙었을 가능성.
      if (call.args.includes(',')) multiValueN += 1;
    }
  }
}
console.log(`재생할 search 질의 ${queries.length}건`);
if (truncatedN) console.log(`  그중 80자에서 잘린 인자 ${truncatedN}건 (원문 일부만 재생됨)`);
if (multiValueN) console.log(`  그중 쉼표가 섞인 것 ${multiValueN}건 (where/document/only 가 query 에 이어붙었을 수 있음)`);

const CAPS = [2000, 3000, 4000];
const rows = [];
const rowsFullOnly = [];
for (const cap of CAPS) {
  let baseChars = 0, cappedChars = 0, hitN = 0, keptAll = 0, broken = 0;
  // 「확정 매칭만」 보조 집계 — 아래 설명 참조.
  let baseCharsF = 0, cappedCharsF = 0, hitNF = 0, keptAllF = 0;
  const brokenQueries = [];
  for (const q of queries) {
    const terms = splitTerms(q);
    const base = searchArchive({ query: q, access: FULL_ACCESS });
    const capd = searchArchive({ query: q, access: FULL_ACCESS, hitMaxChars: cap });
    if ((base.hits?.length ?? 0) !== (capd.hits?.length ?? 0)) {
      broken++;
      brokenQueries.push(q);
      continue;
    }
    for (let i = 0; i < (base.hits?.length ?? 0); i++) {
      hitN++;
      baseChars += base.hits[i].text.length;
      cappedChars += capd.hits[i].text.length;
      const kept = scoreTerms(capd.hits[i].text.toLowerCase(), terms) === terms.length;
      if (kept) keptAll++;

      /* base.hits 에는 낱말을 **전부** 맞춘 확정 히트뿐 아니라(scanArchive 의 score===
       * terms.length 조건), 확정이 0건일 때 함께 실리는 「일부만 맞은」 히트도 섞여 있다
       * (partial 경로, terms.length < PARTIAL_MIN_TERMS 아니면 항상 섞일 수 있음). 후자는
       * 원문(자르기 전)에도 낱말 전부가 없으므로, 자른 뒤 "전부 남았나" 를 물으면 상한과
       * 무관하게 항상 진다 — clipPartial 이 원래 없던 낱말을 만들어 낼 수는 없다.
       * 위 표는 브리프 코드 그대로(모든 히트를 분모로)이고, 이 보조 표는 **자르기 전에
       * 이미 낱말을 전부 맞춘 히트만** 분모로 삼아 「상한이 이미 확정된 매칭을 깨는가」를
       * 따로 잰다 — Task 7 이 실제로 답하려는 질문이다. */
      const baseFullMatch = scoreTerms(base.hits[i].text.toLowerCase(), terms) === terms.length;
      if (baseFullMatch) {
        hitNF++;
        baseCharsF += base.hits[i].text.length;
        cappedCharsF += capd.hits[i].text.length;
        if (kept) keptAllF++;
      }
    }
  }
  if (broken) {
    console.error(`✗ 상한 ${cap}: 히트 수가 달라진 질의 ${broken}건 — 상한이 매칭을 건드립니다. 중단.`);
    console.error(`  질의 예: ${brokenQueries.slice(0, 5).map((q) => JSON.stringify(q)).join(' / ')}`);
    process.exit(1);
  }
  rows.push({ cap, 절감: `${(100 * (1 - cappedChars / baseChars)).toFixed(1)}%`, 보존: `${(100 * keptAll / hitN).toFixed(1)}%`, 히트: hitN });
  rowsFullOnly.push({
    cap,
    절감: hitNF ? `${(100 * (1 - cappedCharsF / baseCharsF)).toFixed(1)}%` : '—',
    보존: hitNF ? `${(100 * keptAllF / hitNF).toFixed(1)}%` : '—',
    히트: hitNF,
  });
}
console.log('\n[표 1] 브리프 코드 그대로 — 분모: base.hits 전체(확정 매칭 + 일부만 맞은 것)');
console.table(rows);
console.log('\n[표 2] 보조 — 분모: 자르기 전에 이미 질의 낱말을 전부 맞춘 히트만 (위 주석 참조)');
console.table(rowsFullOnly);
console.log('통과 기준: 보존 ≥ 99% 인 후보 중 절감이 가장 큰 값. 없으면 상한을 넣지 않는다.');
