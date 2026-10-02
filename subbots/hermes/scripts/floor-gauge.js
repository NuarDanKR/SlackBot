#!/usr/bin/env node
/**
 * 색인 계기판을 한 번 찍는다 — pre-commit 훅과 7.5 점검표(review_batch.py)가 부른다.
 * 막지 않는다(항상 종료코드 0) — 신호는 「시리즈가 접히기 시작했다」이고, 판단은 사람이 한다.
 *
 *   node scripts/floor-gauge.js
 */
import { indexGauge } from '../src/documents.js';

const g = indexGauge();
console.log(`문서 색인 바닥 ${g.floor}자 — ${g.parts}`);
if (g.foldedSeries.length) {
  console.log(`⚠ 지금 예산에서 접혀 색인에 안 보이는 시리즈 ${g.foldedSeries.length}건 (검색에는 걸립니다):`);
  for (const t of g.foldedSeries) console.log(`  - ${t}`);
} else {
  console.log('접힌 시리즈 없음 — 색인 공간 여유 있음');
}
