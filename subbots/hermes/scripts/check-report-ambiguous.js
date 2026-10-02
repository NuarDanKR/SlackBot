#!/usr/bin/env node
/**
 * 아침 DM(`compose`)이 같은-분 중복의 ambiguous **신규분**을 전용 블록으로 내는지.
 *
 *   node scripts/check-report-ambiguous.js
 *
 * 종료코드: 0 통과 / 1 실패
 *
 * 왜 따로 재나 — 갈래 ③(2026-09-22)이 매일 반복되던 「못 짚었습니다」 note 를
 * `.pending-edits.json` 항목으로 바꿨다. 항목은 `compose` 가 그려 줘야 사람에게
 * 닿는데, edited/deleted 블록은 kind 로 거르므로 **ambiguous 는 렌더를 안 더하면
 * 조용히 안 보인다** — 그 무음 회귀를 이 검사가 막는다. 이월분은 기존
 * 「미반영 N건 · 며칠째」 블록이 kind 무관으로 싣는 것을 함께 못 박는다.
 */
import assert from 'node:assert/strict';

for (const s of [process.stdout, process.stderr]) {
  try { s.setDefaultEncoding('utf8'); } catch { /* 콘솔이면 그대로 */ }
}

const { compose } = await import('../src/ingest/report.js');

const amb = { kind: 'ambiguous', channel: '채널가', key: '2026-08-07 07:37', scope: 'message' };

// 신규분 — 전용 블록
const freshText = compose({
  conversations: {
    channels: [],
    pending: { fresh: [amb], carried: 0, carriedItems: [], deferred: 0, today: '2026-09-22' },
  },
});
assert.ok(freshText, 'compose 가 아무것도 안 냈다');
assert.ok(freshText.includes('못 짚은 것 1건'), 'ambiguous 전용 블록이 없다');
assert.ok(freshText.includes('#채널가 2026-08-07 07:37'), '건별 줄이 없다');
assert.ok(freshText.includes('직접 대보고'), '무엇을 하라는지가 없다');
// 자동 반영 안내는 ambiguous 만으로는 안 붙는다 — 스크립트가 못 고치는 건이다
assert.ok(!freshText.includes('슬랙 수정분 반영해줘'), 'ambiguous 에 자동 반영 안내가 붙었다');

// 이월분 — 기존 며칠째 블록에 실린다 (kind 무관)
const carriedText = compose({
  conversations: {
    channels: [],
    pending: {
      fresh: [], carried: 1, deferred: 0, today: '2026-09-22',
      carriedItems: [{ ...amb, firstSeen: '2026-09-19' }],
    },
  },
});
assert.ok(carriedText.includes('미반영 1건'), '이월 머리줄이 없다');
assert.ok(carriedText.includes('3일째 · #채널가 2026-08-07 07:37'), '며칠째 상세줄이 없다');

console.log('PASS report ambiguous: 신규 블록 + 이월 며칠째');
