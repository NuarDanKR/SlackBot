#!/usr/bin/env node
/**
 * `board-docs.js` 의 투영이 `pendingDocuments()` 의 필드를 빠뜨리지 않았나.
 *
 *   node scripts/check-board-docs.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * `board-docs.js` 는 `pendingDocuments()` 가 돌려준 것에서 상황판이 쓰는 필드만 골라
 * JSON 으로 낸다. 거기서 하나를 빠뜨리면 **에러가 아니라 잘못된 화면**이 된다.
 *
 * 실제로 `disabled` 가 빠져 있었다. 문서 폴더가 없을 때 `pendingDocuments` 는
 * `total: 0` 에 `disabled: true` 를 붙여 돌려주는데, 투영에서 사라져서 `board.py` 의
 * 방어(`if not dp and docs.get("disabled")`)가 **실전 경로에서 한 번도 참이 될 수
 * 없었다** — 문서 아카이브가 통째로 꺼진 채 상황판은 「미변환 첨부 0건」이라고 말했다.
 *
 * 그때 `board-docs.js` 주석은 「모양이 바뀌면 board.py 의 시험이 잡는다」고 적고 있었다.
 * 그런데 `test_board.py` 는 `rows()` 에 값을 **직접 먹일** 뿐 `board-docs.js` 를 한 번도
 * 부르지 않는다(`board-docs`·`probe_docs` 문자열이 하나도 없다). 없는 검사를 가리키는
 * 주석이었고, 그래서 이 파일을 세웠다.
 *
 * **슬랙에 붙지 않는다.** 필드 목록만 보는 검사라 토큰도 네트워크도 필요 없다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { pickBoardFields } from './board-docs.js';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const HEALTH = path.join(HERE, '../src/archive-health/documents.js');

/** `board.py` 가 실제로 읽는 필드. 여기 있는 것이 투영에서 빠지면 화면이 조용히 틀린다. */
const NEEDED = [
  'total',      // 미변환 첨부 건수
  'scanDays',   // "최근 N일 범위" — 안 적으면 수집 개수와 달라 보이는 이유를 알 수 없다
  'byChannel',  // 채널별 내역
  'approvals',  // 승인 미반영
  'deferred',   // 만기 전 보류 (보고에서 뺀 개수)
  'failed',     // 읽지 못한 채널 — **0건이 아니라 "일부만 셌다"의 근거다**
  'restricted', // 봇이 못 받는 첨부 — total 에 안 들어가므로 따로 보여야 한다
  'disabled',   // 문서 폴더가 없다 — 0건과 구별되어야 한다
];

/** `pendingDocuments()` 가 돌려주는 모양. 실제 반환문과 같은 키를 갖는다. */
const FULL = {
  total: 3,
  scanDays: 60,
  byChannel: [{ channel: '사업장나', count: 3, newest: 'a.pdf' }],
  approvals: [{ channel: '비공개가', name: 'b.pdf', state: 'meta' }],
  deferred: [{ channel: '사업장라', name: 'c.hwp', until: '2026-08-23' }],
  failed: ['사업장마'],
  restricted: [{ channel: '비공개나', name: 'd.docx' }],
  disabled: false,
};

let ok = true;
const fail = (msg) => { ok = false; console.error(`✗ ${msg}`); };

/* ── 위 두 목록이 원본과 같은가 ─────────────────────────────────────────
 *
 * `NEEDED`·`FULL` 은 **손으로 베껴 쓴 것**이다. 그래서 이 검사에는 원래 구멍이 있었다:
 * `pendingDocuments()` 에 필드가 하나 늘고 투영이 그것을 안 넘기면, `FULL` 에도 없으므로
 * 위아래 검사가 전부 통과한다 — **`disabled` 가 사라졌던 사고가 정확히 그 모양**이었다
 * (늘어난 필드가 투영에서 빠짐). 베낀 목록으로 베낌을 잡을 수는 없다
 * (2026-08-13 리뷰).
 *
 * 그래서 원본 `archive-health/documents.js` 의 `pendingDocuments()` 안에 있는 `return {` 에서
 * 키를 직접 뽑아 댄다. 세 목록(원본 · NEEDED · FULL)이 어긋나면 멈춘다.
 *
 * **읽기가 어긋나면 실패로 끝난다** — 예컨대 반환문 안 주석에 중괄호가 들어가면 키를 못
 * 찾아 시끄럽게 깨진다. 조용히 통과하는 것보다 낫다(이 검사가 막으려는 것이 그것이다). */
function returnedKeys(src) {
  // 주석을 먼저 걷어낸다 — 한국어 설명에 콜론이 들어가면 그것을 키로 읽는다.
  const code = src.replace(/\/\*[\s\S]*?\*\//g, '').replace(/\/\/[^\n]*/g, '');
  const fn = code.split(/(?:export )?async function pendingDocuments\b/)[1];
  if (!fn) return null;
  const body = fn.split(/\n\s*(?:export )?(?:async )?function /)[0];
  const keys = new Set();
  let from = 0;
  for (;;) {
    const at = body.indexOf('return {', from);
    if (at === -1) break;
    let depth = 0;      // 중괄호
    let inner = 0;      // 소괄호·대괄호 — **이것을 안 세면 `sort((a, b) =>` 의 `a` 를 필드로 읽는다**
    let buf = '';
    let afterColon = false;               // 값 자리인가 (`total: best.size` 의 `size` 를 키로 세지 않으려고)
    let i = at + 'return '.length;
    const top = () => depth === 1 && inner === 0;
    for (; i < body.length; i++) {
      const ch = body[i];
      if (ch === '(' || ch === '[') { inner += 1; buf = ''; continue; }
      if (ch === ')' || ch === ']') { inner -= 1; buf = ''; continue; }
      if (ch === '{') { depth += 1; buf = ''; continue; }
      if (ch === '}') {
        if (top() && !afterColon && buf) keys.add(buf);         // 마지막 축약 속성
        depth -= 1;
        buf = '';
        if (depth === 0) break;
        continue;
      }
      if (ch === ':' && top()) {
        if (buf) keys.add(buf);
        buf = ''; afterColon = true; continue;
      }
      if (ch === ',' && top()) {
        if (!afterColon && buf) keys.add(buf);                  // 축약 속성 (`failed,`)
        buf = ''; afterColon = false; continue;
      }
      if (/[A-Za-z0-9_$]/.test(ch)) { buf += ch; continue; }
      // 공백·줄바꿈은 이름을 끊지 않는다 — 끊으면 `{ failed, approvals }` 처럼 **마지막
      // 축약 속성에 쉼표가 없는** 모양에서 그 하나가 조용히 빠진다.
      if (/\s/.test(ch)) continue;
      buf = '';
    }
    from = i;
  }
  return keys.size ? keys : null;
}

const SOURCE = returnedKeys(fs.readFileSync(HEALTH, 'utf8'));
if (!SOURCE) {
  fail('archive-health/documents.js 의 pendingDocuments() 반환 필드를 읽지 못했습니다 '
    + '— 이 검사가 무엇도 지키지 못하는 상태입니다. check-board-docs.js 의 returnedKeys() 를 고치세요.');
} else {
  for (const k of SOURCE) {
    if (!NEEDED.includes(k)) {
      fail(`pendingDocuments() 에 \`${k}\` 가 늘었는데 이 검사는 모릅니다 — `
        + '상황판이 읽어야 하는 값인지 정하고, 맞으면 board-docs.js 의 투영과 NEEDED·FULL 에 넣으세요.');
    }
  }
  for (const k of NEEDED) {
    if (!SOURCE.has(k)) {
      fail(`NEEDED 의 \`${k}\` 가 pendingDocuments() 반환에 없습니다 — 이름이 바뀌었거나 없어졌습니다.`);
    }
  }
  for (const k of SOURCE) {
    // 표본에 없는 필드는 아래 투영 검사가 한 번도 안 본다.
    if (!(k in FULL)) fail(`FULL 표본에 \`${k}\` 가 없습니다 — 투영 검사가 그 필드를 못 봅니다.`);
  }
}

const got = pickBoardFields(FULL);

for (const k of NEEDED) {
  if (!(k in got)) fail(`\`${k}\` 가 투영에서 빠졌습니다 — board.py 가 이 값을 읽습니다.`);
}

// 값을 바꿔 넘기지 않는지. 상황판은 "새로 세지 않는다"가 원칙이라 여기서 가공하면 안 된다.
for (const k of NEEDED) {
  if (!(k in got)) continue;
  if (JSON.stringify(got[k]) !== JSON.stringify(FULL[k])) {
    fail(`\`${k}\` 의 값이 바뀌었습니다 — ${JSON.stringify(FULL[k])} → ${JSON.stringify(got[k])}`);
  }
}

/* 문서 폴더가 없는 회차. `pendingDocuments` 의 이른 반환과 같은 모양을 넣어,
 * `disabled` 가 **끝까지 살아 나오는지**를 본다. 이게 이 검사의 본래 목적이다. */
const OFF = {
  total: 0, byChannel: [], scanDays: 60, failed: [], approvals: [], deferred: [],
  restricted: [], disabled: true,
};
if (pickBoardFields(OFF).disabled !== true) {
  fail('문서 폴더가 없는 회차에 `disabled` 가 살아 나오지 않습니다 — 상황판이 「0건」이라고 말합니다.');
}

// 빠진 필드는 `[]`·`false` 로 채우되, **없어진 것과 빈 것을 구별할 수 있어야 한다**.
const EMPTY = pickBoardFields({ total: 0, scanDays: 60 });
for (const k of NEEDED) {
  if (!(k in EMPTY)) fail(`필드가 없는 입력에서 \`${k}\` 가 사라집니다 — 받는 쪽이 KeyError 를 봅니다.`);
}

if (ok) {
  console.log(`[check-board-docs] OK — 투영이 ${NEEDED.length}개 필드를 그대로 넘깁니다`
    + ` (원본 반환 필드 ${SOURCE ? SOURCE.size : '?'}개와 대조).`);
} else {
  console.error('\n고칠 곳: scripts/board-docs.js 의 pickBoardFields()');
  process.exitCode = 1;
}
