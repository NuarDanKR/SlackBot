#!/usr/bin/env node
/**
 * 검색 낱말 규칙 점검 — `src/search-terms.js` 자체가 스스로 정한 규칙을 지키는지 본다.
 *
 * **여기서 보지 않는 것**: 대화 쪽(`archive.js`)·문서 쪽(`documents.js`)이 실제로 이 모듈을
 * 불러 쓰고 있는지는 이 검사의 범위 밖이다. 누군가 두 파일 중 하나에 `split(/\s+/)` 를
 * 다시 인라인으로 넣어도 이 검사는 계속 통과한다 — `search-terms.js` 하나만 부르기 때문이다
 * (그 모듈이 2026-09-11 부터 `config.js` 를 타므로 자료 저장소는 있어야 한다. 부르는 것은
 * 여전히 이 한 모듈뿐이라 위 한계는 그대로다).
 * 두 구현이 실제로 같은 규칙을 쓰는지 대조하는 것은 `scripts/check-shared-rules.js` 다.
 *
 * 이 저장소는 같은 판정이 두 곳으로 갈려 한쪽만 고쳐진 사고를 겪었다
 * (봇 판정 isBotMessage / is_bot_message). 그래서 낱말 쪼개기는 한 자리에 두고
 * 여기서 못박는다.
 *
 * 실행: node scripts/check-search-terms.js
 */
import { splitTerms, scoreTerms, PARTIAL_MIN_TERMS } from '../src/search-terms.js';

let failed = 0;
const eq = (got, want, what) => {
  const a = JSON.stringify(got);
  const b = JSON.stringify(want);
  if (a === b) console.log(`  ✓ ${what}`);
  else { failed += 1; console.error(`  ✗ ${what}\n      나온 것: ${a}\n      바랄 것: ${b}`); }
};

console.log('[1/2] 낱말 쪼개기');
// 낱말은 가짜다 — 여기서 재는 것은 「공백으로 셋으로 갈리나」뿐이라 뜻은 상관없다.
eq(splitTerms('만기연장 요청서 접수현황'), ['만기연장', '요청서', '접수현황'], '공백으로 쪼갠다');
eq(splitTerms('  PRS   이자  '), ['prs', '이자'], '소문자로 바꾸고 빈 값을 버린다');
eq(splitTerms(''), [], '빈 질의는 빈 목록');
eq(splitTerms(null), [], 'null 도 빈 목록');

console.log('[2/2] 부분 일치 점수');
eq(scoreTerms('중순위 대주 470억 협의', ['중순위', '470억']), 2, '둘 다 들어 있으면 2');
eq(scoreTerms('중순위 대주 협의', ['중순위', '470억']), 1, '하나만 들어 있으면 1');
eq(scoreTerms('무관한 문장', ['중순위', '470억']), 0, '하나도 없으면 0');
eq(PARTIAL_MIN_TERMS, 2, '낱말 1개짜리 질의에는 부분 일치를 걸지 않는다');

process.exit(failed ? 1 : 0);
