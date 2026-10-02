#!/usr/bin/env node
/**
 * 예산이 부족하면 시리즈도 접히는가 — 그리고 접히는 순서가 오래된 것부터인가.
 *
 *   node scripts/check-series-fold.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 ──
 * 2026-09-10 까지 접기 루프는 `if (c.rank === 0) break;` 로 시리즈 앞에서 포기했다.
 * 그래서 안 접히는 줄이 자랄수록 바닥이 상한(6,000자)을 향해 올라갔고, 넘으면 접기로
 * 못 줄이는 구조였다 (설계: 워크스페이스 docs/superpowers/specs/2026-09-10-*-design.md).
 * 이제 시리즈는 **가장 마지막 순서로** 접힌다 — 이 검사는 그 동작이 되돌아가는 것을 막는다.
 *
 * ── 왜 `/^- \[/` 줄 수 세기도, 렌더 텍스트의 제목 대조도 아니고 구조 데이터인가 ──
 * 첫 버전은 `- [` 로 시작하는 줄 수를 "시리즈 줄 수"로 셌다. 이 접두는 `docLine()` 의
 * 공통 포맷이라 시리즈든 아니든 안 접힌 모든 개별 문서 줄에 똑같이 붙어서, 중간 예산에서
 * 비시리즈만 접혀도 줄 수가 줄어 "시리즈가 먼저 접혔다"는 거짓 실패를 냈다(2026-09-10
 * 첫 구현 검증 중 발견).
 *
 * 두 번째 판은 렌더된 텍스트에서 `] 제목` 뒤가 줄끝·` — `·` (` 중 하나인지로 제목을
 * 찾았다. 그런데 제목 하나가 다른 문서 제목의 접두인 실물이 6쌍 있다 (예: "본부별
 * 주간보고" 는 "본부별 주간보고 (경영혁신실 취합)" 의 접두) — 앞쪽이 실제로 접혀도
 * 뒤쪽 문서 줄의 ` (` 앞부분이 같은 경계 조건을 만족해 "안 접혔다"고 오판했다. production
 * 예산(6,000자)에서 실측 재현됨 (2026-09-10 리뷰).
 *
 * 그래서 렌더된 **텍스트를 다시 읽지 않는다.** `buildDocumentsBrief` 의 접기 루프는 어떤
 * 문서를 접었는지 이미 구조적으로 안다(`c.g.folded.push(c.d)`) — 그 결과를 `foldedOut`
 * 배열로 그대로 받아 문서 객체(`project`+`slug`)로 식별한다. `indexGauge` 도 같은
 * `foldedOut` 을 쓴다 — 두 벌로 각자 판별을 다시 만들면 한쪽만 고쳤을 때 접기가 세는
 * 기준과 검사가 확인하는 기준이 조용히 갈린다.
 *
 * **"안 접힘"과 "애초에 이 access 로 안 보임"은 다른 것이라 하나로 뭉뚱그리면 또 다른
 * 오판이 난다.** `canSeeDoc` 은 export 되어 있지 않아 여기서 직접 권한을 걸러 볼 수
 * 없으므로, "이 access 로 볼 수 있는 문서 전부"를 `visibleOut` 으로 따로 받는다 —
 * `listDocuments()` 로 모은 시리즈 문서 중 비공개인 것(canSeeDoc 탈락)은 `foldedOut` 에도
 * 안 잡히므로, `visibleOut` 없이 "안 접힌 것 = 보이는 것"이라고 단순화하면 비공개 문서가
 * "보이는 시리즈"로 잘못 섞인다(2026-09-10 리뷰 검증 중 실물에서 확인 — 사업장·문서
 * 이름은 자료 저장소에만 적는다, 코드 저장소는 팀끼리 나눠 쓴다).
 */
import { buildDocumentsBrief, isSeriesDoc, indexGauge } from '../src/documents.js';
import { PUBLIC_ACCESS } from '../src/config.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

// 문서 하나를 가리키는 안정적인 키. 제목은 실물에 접두 관계가 있어(위 docstring) 식별자로
// 못 쓴다 — slug 는 사업장 폴더 안의 파일명이라 project 와 묶으면 겹치지 않는다.
const key = (d) => `${d.project}/${d.slug}`;

// roomy(예산 사실상 무제한) 한 번으로 "이 access 로 볼 수 있는 문서 전부"(visibleOut)와
// "그중 실제로 접힌 것"(foldedOut, 정상적으로는 거의 0건)을 함께 받는다.
const roomyFolded = [];
const roomyVisible = [];
const roomy = buildDocumentsBrief({
  access: PUBLIC_ACCESS, maxChars: 100000, foldedOut: roomyFolded, visibleOut: roomyVisible,
});
const visibleSeriesDocs = roomyVisible.filter((d) => isSeriesDoc(d));
if (visibleSeriesDocs.length === 0) {
  fail('공개 접근에서 보이는 시리즈 문서가 하나도 없어 이 검사가 아무것도 확인하지 못합니다.');
}
if (roomyFolded.length > 0) {
  fail(`예산 100,000자인데 ${roomyFolded.length}건이 접혔습니다 — roomy 가 "거의 다 보임"을 전제하는 다른 계산(중간 예산 등)이 흔들립니다.`);
}

// ① 예산 0 이면 전부 접힌다 — 보이던 시리즈가 하나도 안 남아야 한다.
const floorFolded = [];
const floor = buildDocumentsBrief({ access: PUBLIC_ACCESS, maxChars: 0, foldedOut: floorFolded });
const floorFoldedKeys = new Set(floorFolded.map(key));
const leftAtFloor = visibleSeriesDocs.filter((d) => !floorFoldedKeys.has(key(d)));
if (leftAtFloor.length > 0) {
  fail(`예산 0 인데 시리즈 ${leftAtFloor.length}건이 안 접혔습니다 — rank 0 에서 포기하는 옛 동작입니다.\n      예: ${leftAtFloor[0].title.slice(0, 60)}`);
}

// ② 넉넉한 예산과 중간 예산 사이에서는 시리즈가 접히지 않는다 — 접기는 마지막 수단이어야 한다.
const midBudget = Math.floor((floor.length + roomy.length) / 2);
const midFolded = [];
buildDocumentsBrief({ access: PUBLIC_ACCESS, maxChars: midBudget, foldedOut: midFolded });
const midFoldedKeys = new Set(midFolded.map(key));
const missingAtMid = visibleSeriesDocs.filter((d) => midFoldedKeys.has(key(d)));
if (missingAtMid.length > 0) {
  fail(`중간 예산(${midBudget}자)에서 시리즈 ${missingAtMid.length}건이 이미 접혔습니다 — 단발보다 시리즈가 먼저 접히고 있습니다.\n      예: ${missingAtMid[0].title.slice(0, 60)}`);
}

// ③ indexGauge — 예산을 좁히면 접힌 시리즈가 잡히고, 넉넉하면 0건이어야 한다.
const tight = indexGauge({ budget: Math.max(1, Math.floor(floor.length * 0.9)) });
if (tight.foldedSeries.length === 0) {
  fail('예산을 바닥 밑으로 좁혔는데 접힌 시리즈가 0건입니다 — indexGauge 가 접힘을 못 봅니다.');
}
const looseGauge = indexGauge({ budget: 100000 });
if (looseGauge.foldedSeries.length !== 0) {
  fail(`예산이 넉넉한데 접힌 시리즈 ${looseGauge.foldedSeries.length}건 — indexGauge 의 접힘 판별이 어긋났을 수 있습니다.`);
}

if (ok) {
  console.log('[check-series-fold] OK — 예산 0 에서 시리즈까지 전부 접히고, 여유 예산에서는 시리즈가 안 접힙니다.');
} else {
  console.error('\n고칠 곳: src/documents/brief.js 의 buildDocumentsBrief 접기 루프');
  process.exitCode = 1;
}
