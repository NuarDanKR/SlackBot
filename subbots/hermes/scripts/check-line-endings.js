#!/usr/bin/env node
/**
 * 파일을 읽는 자리(`readCached`)가 줄 끝을 LF 로 맞춰 주는가.
 *
 *   node scripts/check-line-endings.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * 저장소에 담긴 md 는 전부 LF 다. 그런데 **윈도우에서 클론하면 `core.autocrlf=true` 가
 * 체크아웃할 때 작업 트리 사본을 CRLF 로 바꾼다.** 내용은 같은 커밋인데 꺼내 놓은
 * 바이트가 달라지는 것이고, 이게 조용히 두 가지를 만든다.
 *
 *   ① **원문을 그대로 프롬프트에 싣는 자리에 `\r` 이 함께 실린다.** `documents.js` 의
 *      `missingSection()` 은 index.md 의 「변환하지 못한 것」 절을 통째로 옮기는데,
 *      `\n` 으로만 쪼개므로 줄마다 남은 `\r` 이 색인 글자수에 그대로 들어간다.
 *      2026-08-25 에 rebase 한 번으로 문서 색인 바닥이 **4,571 → 4,583 자**가 됐고
 *      (그 절 13줄, `.trim()` 이 마지막 하나를 떼어 정확히 +12), 대화 색인도
 *      26,586 → 26,588 로 함께 움직였다. 문서는 한 건도 안 늘었는데 그랬다.
 *
 *   ② **줄 끝에 걸리는 코드가 이 PC 에서만 어긋난다.** `$` 로 끝을 묶은 정규식은
 *      앞에 `\r` 이 있으면 안 맞는다. 아카이브 md 포맷은 파싱 계약이고 계약이 깨지면
 *      **에러 없이 검색이 0건**이 되는데, VM(리눅스)은 LF 라 거기서는 안 나타난다.
 *      즉 로컬 검사가 초록이어도 VM 과 같다는 보장이 안 된다 — 통과한 것이 다른
 *      바이트열이기 때문이다.
 *
 * 그래서 읽는 자리 하나에서 줄 끝을 맞춘다. `readCached` 는 archive.js·documents.js 가
 * 파일을 읽는 **유일한 통로**라 여기서 맞추면 그 아래는 전부 같은 판을 본다.
 *
 * 파이썬 도구(`verify_format.py` 등)는 universal newlines 라 애초에 `\r` 을 못 보므로
 * 이 문제의 범위 밖이다 — JS 만 해당한다.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { readCached } from '../src/archive.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-eol-'));
const write = (name, bytes) => {
  const p = path.join(dir, name);
  fs.writeFileSync(p, bytes);
  return p;
};

try {
  // ── ① CRLF 로 저장된 파일을 읽으면 `\r` 이 안 남는다 ──
  const crlf = write('crlf.md', '## 변환하지 못한 것\r\n\r\n| 대상 | 상태 |\r\n');
  const got = readCached(crlf);
  if (got.includes('\r')) {
    const n = (got.match(/\r/g) || []).length;
    fail(`CRLF 파일을 읽었더니 \\r 이 ${n}개 남았습니다: ${JSON.stringify(got)}`);
  } else {
    pass('CRLF 파일을 읽어도 \\r 이 안 남는다');
  }

  // ── ② 내용은 그대로다 — 줄 끝만 맞추고 글자는 안 건드린다 ──
  const want = '## 변환하지 못한 것\n\n| 대상 | 상태 |\n';
  if (got !== want) {
    fail(`내용이 달라졌습니다.\n     기대: ${JSON.stringify(want)}\n     실제: ${JSON.stringify(got)}`);
  } else {
    pass('줄 끝만 맞추고 글자는 그대로다');
  }

  // ── ③ 이미 LF 인 파일은 손대지 않는다 ──
  const lfText = '# 제목\n\n본문 한 줄\n';
  const lf = write('lf.md', lfText);
  if (readCached(lf) !== lfText) {
    fail('LF 파일이 바뀌었습니다 — 정규화가 과하게 걸립니다');
  } else {
    pass('LF 파일은 그대로 통과한다');
  }

  // ── ④ 홀로 있는 CR 은 줄바꿈이 아니므로 건드리지 않는다 ──
  //   CRLF → LF 만 바꾼다. `\r` 을 전부 지우면 본문에 든 CR 까지 사라진다.
  const loneText = '값: A\rB\n';
  const lone = write('lone.md', loneText);
  if (readCached(lone) !== loneText) {
    fail('본문 안의 홀로 있는 CR 까지 지웠습니다 — CRLF 만 바꿔야 합니다');
  } else {
    pass('본문 안의 홀로 있는 CR 은 그대로 둔다');
  }

  // ── ⑤ 캐시로 돌려주는 값도 맞춰진 판이어야 한다 ──
  //   캐시에 원문을 담아두고 첫 호출에만 맞추면, 두 번째부터 CRLF 가 새어 나온다.
  const again = readCached(crlf);
  if (again.includes('\r')) {
    fail('캐시 적중 때 \\r 이 든 판이 나왔습니다 — 맞춘 값을 캐시에 담아야 합니다');
  } else if (again !== got) {
    fail('같은 파일인데 두 번째 읽은 값이 다릅니다');
  } else {
    pass('캐시로 돌려줄 때도 맞춰진 판이 나온다');
  }
} finally {
  fs.rmSync(dir, { recursive: true, force: true });
}

if (ok) {
  console.log('[check-line-endings] OK — 읽는 자리에서 줄 끝이 LF 로 맞춰집니다.');
} else {
  console.error('\n고칠 곳: src/archive.js 의 readCached()');
  process.exitCode = 1;
}
