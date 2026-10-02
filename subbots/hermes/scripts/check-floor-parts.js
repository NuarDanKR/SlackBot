#!/usr/bin/env node
/**
 * 문서 색인 바닥의 다섯 조각(floorParts)이 서로를 침범하지 않고, 합이 정확히
 * 원문 길이와 같은가.
 *
 *   node scripts/check-floor-parts.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * `floorParts()` 는 [4/6] 이 찍는 「예외표 1283 · 접힘줄 785 · 시리즈 1201 · 사업장 962 ·
 * 머리말 260」 같은 줄을 만든다. 사업장을 하나 늘렸을 때 사업장 숫자만 움직여야
 * 「어디가 자랐는지」를 사람이 읽을 수 있는데, 줄 사이 구분자(\n)를 다섯 조각에
 * 나눠 세는 계산이 두 번 틀렸다:
 *
 *   ① 조각마다 줄 끝에 그대로 +1 을 더한 버전 — 각 조각 숫자는 정직했지만 다섯
 *      조각의 합이 원문보다 1 컸다 (마지막 줄에는 실제로 뒤따르는 구분자가 없는데도
 *      세었다).
 *   ② 총합만 맞추려고 구분자 전체(줄 수-1)를 머리말 하나에 몰아준 버전 — 총합은
 *      맞았지만 머리말이 다른 네 조각의 구분자까지 떠안아, 사업장이나 시리즈를
 *      늘려도(자기 줄은 안 늘었는데) 머리말 숫자가 함께 움직였다. 이게 이 검사가
 *      막는 회귀다.
 *
 * 두 버전 다 에러 없이 숫자만 조용히 틀렸고, ②는 사람 리뷰 세 번을 통과했다.
 * 그래서 계산 결과가 아니라 계산의 성질(합이 맞나 · 조각이 서로 독립인가)을
 * 검사로 고정한다.
 *
 * **파일도 네트워크도 안 쓴다.** 순수 함수 하나에 합성 입력을 먹인다.
 */
import { floorParts } from '../src/documents.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

/** floorParts 가 돌려주는 "이름 숫자 · 이름 숫자 …" 줄을 {이름:숫자} 로. */
const parse = (line) => Object.fromEntries(
  line.split(' · ').map((p) => {
    const m = p.match(/^(\S+) (-?\d+)$/);
    return [m[1], Number(m[2])];
  }),
);

const sum = (parts) => Object.values(parts).reduce((a, b) => a + b, 0);

// 합성 입력 조각 — 실제 아카이브의 줄 모양을 흉내낸다 (buildDocumentsBrief 가 만드는 모양).
const head = ['# 문서 아카이브', '', '설명 한 줄.', ''];
const site = (name) => `### ${name} (문서 1 · 회차 1)`;
const series = (title) => `- [계약서] ${title}`;
const folded = (n, kinds) => `- 그 외 ${n}건 (${kinds})`;
const missingHeader = '## 변환하지 못한 것';
const missingRow = (name) => `- ${name} 변환 대기`;

// ── ① 합이 정확히 원문 길이와 같다 ──────────────────────────────
const text1 = [
  ...head,
  site('사업장A'), series('계약서1'), folded(5, '계약서·공문'), '',
  site('사업장B'), series('계약서2'), '',
  missingHeader,
  missingRow('파일1'),
  missingRow('파일2'),
].join('\n');

const parts1 = parse(floorParts(text1));
if (sum(parts1) !== text1.length) {
  fail(`합이 원문 길이와 다릅니다 — 합 ${sum(parts1)} / 원문 ${text1.length}자.\n      ${floorParts(text1)}`);
}

// ── ② 조각은 서로의 줄에만 반응한다 ─────────────────────────────
// 한 조각에 줄 하나를 더했을 때 나머지 넷은 그대로여야 한다. 구분자를 한 조각에
// 몰아주는 버전은 줄 수(L)가 바뀔 때마다 그 조각이 함께 움직여 여기서 걸린다.
// 셋 이상의 서로 다른 조각으로 반복한다 — 한 조각에서만 재현되면 우연일 수 있다.

// 사업장에 한 줄 추가
const text2a = [
  ...head,
  site('사업장A'), series('계약서1'), folded(5, '계약서·공문'), '',
  site('사업장B'), series('계약서2'), '',
  site('사업장C (추가)'),
  missingHeader,
  missingRow('파일1'),
  missingRow('파일2'),
].join('\n');
const parts2a = parse(floorParts(text2a));
for (const k of ['예외표', '접힘줄', '시리즈', '머리말']) {
  if (parts2a[k] !== parts1[k]) {
    fail(`사업장 줄을 하나 늘렸는데 ${k} 이 ${parts1[k]} → ${parts2a[k]} 로 움직였습니다 — 조각이 서로 독립이 아닙니다.`);
  }
}
if (parts2a['사업장'] === parts1['사업장']) {
  fail('사업장 줄을 늘렸는데 사업장 숫자가 그대로입니다 — 반영이 안 되고 있습니다.');
}

// 접힘줄에 한 줄 추가
const text2b = [
  ...head,
  site('사업장A'), series('계약서1'), folded(5, '계약서·공문'), folded(2, '보고서'), '',
  site('사업장B'), series('계약서2'), '',
  missingHeader,
  missingRow('파일1'),
  missingRow('파일2'),
].join('\n');
const parts2b = parse(floorParts(text2b));
for (const k of ['예외표', '시리즈', '사업장', '머리말']) {
  if (parts2b[k] !== parts1[k]) {
    fail(`접힘줄을 하나 늘렸는데 ${k} 이 ${parts1[k]} → ${parts2b[k]} 로 움직였습니다 — 조각이 서로 독립이 아닙니다.`);
  }
}
if (parts2b['접힘줄'] === parts1['접힘줄']) {
  fail('접힘줄을 늘렸는데 접힘줄 숫자가 그대로입니다 — 반영이 안 되고 있습니다.');
}

// 시리즈에 한 줄 추가
const text2c = [
  ...head,
  site('사업장A'), series('계약서1'), series('계약서1-추가'), folded(5, '계약서·공문'), '',
  site('사업장B'), series('계약서2'), '',
  missingHeader,
  missingRow('파일1'),
  missingRow('파일2'),
].join('\n');
const parts2c = parse(floorParts(text2c));
for (const k of ['예외표', '접힘줄', '사업장', '머리말']) {
  if (parts2c[k] !== parts1[k]) {
    fail(`시리즈를 하나 늘렸는데 ${k} 이 ${parts1[k]} → ${parts2c[k]} 로 움직였습니다 — 조각이 서로 독립이 아닙니다.`);
  }
}
if (parts2c['시리즈'] === parts1['시리즈']) {
  fail('시리즈를 늘렸는데 시리즈 숫자가 그대로입니다 — 반영이 안 되고 있습니다.');
}

// ── ③ 예외표 절이 아예 없을 때도 합이 맞는다 (`## ` 줄 자체가 없음) ──
const text3 = [
  ...head,
  site('사업장A'), series('계약서1'), folded(5, '계약서·공문'), '',
].join('\n');
const parts3 = parse(floorParts(text3));
if (sum(parts3) !== text3.length) {
  fail(`예외표 절이 없을 때 합이 원문 길이와 다릅니다 — 합 ${sum(parts3)} / 원문 ${text3.length}자.`);
}
if (parts3['예외표'] !== 0) {
  fail(`예외표 절이 없는데 예외표가 0 이 아닙니다 — ${parts3['예외표']}.`);
}

// ── ④ 한 조각이 비어 있을 때도 합이 맞는다 (시리즈 없음) ────────
const text4 = [
  ...head,
  site('사업장A'), folded(5, '계약서·공문'), '',
  missingHeader,
  missingRow('파일1'),
].join('\n');
const parts4 = parse(floorParts(text4));
if (sum(parts4) !== text4.length) {
  fail(`시리즈가 비어 있을 때 합이 원문 길이와 다릅니다 — 합 ${sum(parts4)} / 원문 ${text4.length}자.`);
}
if (parts4['시리즈'] !== 0) {
  fail(`시리즈가 없는데 시리즈가 0 이 아닙니다 — ${parts4['시리즈']}.`);
}

// ── ⑤ 마지막 줄이 예외표가 아니라 다른 조각에 있을 때도 합이 맞는다 ──
// 지금 실물은 예외표절이 파일 맨 끝에 와서 거기서 보정되지만, 함수가 그 자리를
// 가정하면 안 된다 — 예외표 절이 없어 마지막 줄이 사업장인 텍스트로 확인한다.
const text5 = [...head, site('사업장A')].join('\n');
const parts5 = parse(floorParts(text5));
if (sum(parts5) !== text5.length) {
  fail(`마지막 줄이 사업장일 때 합이 원문 길이와 다릅니다 — 합 ${sum(parts5)} / 원문 ${text5.length}자.`);
}

if (ok) {
  console.log('[check-floor-parts] OK — 다섯 조각의 합이 원문 길이와 같고, 조각마다 자기 줄에만 반응합니다.');
} else {
  console.error('\n고칠 곳: src/documents/brief.js 의 floorParts()');
  process.exitCode = 1;
}
