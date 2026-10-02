#!/usr/bin/env node
/**
 * 접힘 줄(`- 그 외 N건 (…)`)의 길이가 **종류 개수에 비례하지 않나**.
 *
 *   node scripts/check-doc-fold-line.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * 이 줄은 색인에서 **접히지 않는다.** 그래서 여기가 자라면 바닥(접을 수 있는 것을
 * 전부 접었을 때 남는 양)이 영구히 오른다. 2026-08-25 에 바닥이 5,986자로 상한
 * 6,000자에 닿아 여유가 14자였고, 그 바닥의 1,471자가 이 줄 36개였다. 문서를 넣을
 * 때마다 건당 8.4자씩 올랐다 — **본업이 한도를 갉아먹고 있었다.**
 *
 * 종류를 전부 적던 것을 세 개까지로 막았고 이 검사가 그 성질을 지킨다. 되돌아가도
 * **에러는 안 난다** — 색인이 조금씩 길어질 뿐이라 몇 주 뒤에야 안다.
 *
 * **파일도 네트워크도 안 쓴다.** 순수 함수 하나에 합성 입력을 먹인다.
 */
import { foldedLine } from '../src/documents.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

/** 종류 이름 목록을 문서 배열로. 메타 한 칸만 있으면 되는 함수다. */
const docs = (kinds) => kinds.map((k) => ({ meta: { 종류: k } }));

/** 괄호 안만 떼어 온다 — 앞의 `- 그 외 N건` 은 건수라 당연히 달라진다. */
const paren = (line) => (line.match(/\((.*)\)$/) || [null, ''])[1];

// ① 종류가 몇이든 이름은 세 개까지만 실린다
const many = [...Array(50)].map((_, i) => `종류${String(i).padStart(2, '0')}`);
const line50 = foldedLine(docs(many));
const shown = many.filter((k) => line50.includes(k));
if (shown.length > 3) {
  fail(`종류가 50개인데 줄에 ${shown.length}개가 실렸습니다 — 세 개까지여야 합니다.\n      ${line50}`);
}

// ② 그래서 괄호 안이 종류 개수에 안 흔들린다
const line5 = foldedLine(docs(many.slice(0, 5)));
if (paren(line50) !== paren(line5)) {
  fail('종류 5개일 때와 50개일 때 괄호 안이 다릅니다 — 개수에 따라 자랍니다.\n'
    + `      5종:  ${line5}\n      50종: ${line50}`);
}

// ③ 종류가 셋 이하면 ` 등` 을 안 붙인다 — 붙이면 더 있는 것처럼 읽힌다
const three = foldedLine(docs(['계약서', '공문', '보고서']));
if (three.includes(' 등')) {
  fail(`종류가 셋뿐인데 ' 등' 이 붙었습니다 — 더 있는 것처럼 읽힙니다.\n      ${three}`);
}

// ④ 넷 이상이면 반드시 붙인다 — 없으면 접힌 종류가 다 보이는 것처럼 읽힌다
const four = foldedLine(docs(['계약서', '공문', '보고서', '약정서']));
if (!four.includes(' 등')) {
  fail(`종류가 넷인데 ' 등' 이 없습니다 — 접힌 종류가 다 보이는 것처럼 읽힙니다.\n      ${four}`);
}

// ⑤ 많은 종류가 앞에 온다 — 세 개만 보일 때 무엇이 보이느냐가 중요하다
const mixed = foldedLine(docs(['공문', '계약서', '계약서', '계약서', '보고서', '보고서']));
if (paren(mixed) !== '계약서·보고서·공문') {
  fail(`많은 종류가 앞에 와야 합니다 — 괄호 안이 '${paren(mixed)}' 입니다.\n      ${mixed}`);
}

// ⑥ 개수가 같으면 훑은 순서를 그대로 둔다.
//    여기가 흔들리면 색인 공통분의 글자가 달라져 프롬프트 캐시가 새로 써진다 —
//    **에러 없이 비용으로만** 드러나므로 검사로만 잡힌다 (커밋 8468500).
const tie = foldedLine(docs(['나', '가', '다', '라']));
if (paren(tie) !== '나·가·다 등') {
  fail(`개수가 같으면 훑은 순서를 그대로 둬야 합니다 — 괄호 안이 '${paren(tie)}' 입니다.`);
}

// ⑦ 메타에 종류가 없는 문서는 '문서' 로 센다
const noKind = foldedLine([{ meta: {} }, { meta: {} }]);
if (paren(noKind) !== '문서') {
  fail(`종류가 없는 문서는 '문서' 로 세야 합니다 — ${noKind}`);
}

if (ok) {
  console.log('[check-doc-fold-line] OK — 접힘 줄이 종류 3개까지만 싣고 개수에 안 비례합니다'
    + ` (50종: ${line50}).`);
} else {
  console.error('\n고칠 곳: src/documents/brief.js 의 foldedLine() 과 FOLD_KINDS_MAX');
  process.exitCode = 1;
}
