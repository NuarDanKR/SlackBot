#!/usr/bin/env node
/**
 * `redactPrivateMentions` 가 `matchesHiddenPrivate`·`archive.js` 의 `fold` 와 **같은 다듬기
 * 규칙**으로 비공개 채널 이름을 맞대보나 — 띄어 쓴 이름도 걸리나.
 *
 *   node scripts/check-redact-fold.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 *
 * ── 왜 있나 ──
 *
 * `config.js` 의 `redactPrivateMentions` 는 예전에 `line.includes(c)` 로 **설정 문자열
 * 그대로** 대봤다. 같은 파일의 `matchesHiddenPrivate` 와 `archive.js` 의 `withoutSkipped`
 * 는 `fold`(공백·밑줄·붙임표·`#` 를 지운다)로 다듬어 맞댄다. 그래서 사람이 색인에
 * 비공개 채널 이름을 띄어 쓰면 설정의 붙여 쓴 이름과 안 맞아 그 줄이 안 지워졌다 —
 * 문서 주석이 「fail-closed」라고 적은 방침과 반대 방향이었다 (WHK 결정 2026-09-03 로 고침).
 *
 * 진짜 채널 이름을 여기 적지 않는다(`check-business-names.js`) — `config.privateChannels`
 * 를 시험 동안만 합성 이름으로 바꿔치기하고 끝나면 원래대로 돌려놓는다.
 */
import { config, redactPrivateMentions, matchesHiddenPrivate, accessFor, PUBLIC_ACCESS } from '../src/config.js';

let ok = true;
const check = (label, cond, extra = '') => {
  ok &&= !!cond;
  console.log((cond ? '  PASS ' : '  FAIL ') + label + (extra ? `  ${extra}` : ''));
};

// 실제 config 에 없을 합성 이름 — 붙여 쓴 채널명 하나만 privateChannels 에 있다고 가정한다.
const FAKE = '가짜비공개채널';
const originalPrivate = config.privateChannels;

config.privateChannels = [FAKE];
try {
  console.log('\nredactPrivateMentions — 다듬어 맞추기');

  // ① 사람이 띄어 쓴 형태 — 이게 이번에 고친 축이다.
  const spacedLine = `공개 발췌: 가짜 비공개 채널 관련 논의가 있었다.`;
  const afterSpaced = redactPrivateMentions(spacedLine, PUBLIC_ACCESS);
  check('띄어 쓴 이름이 든 줄도 지워진다 (fold 다듬기)', afterSpaced === '',
    `(입력: "${spacedLine}" → 출력: "${afterSpaced}")`);

  // ② 붙임표·밑줄이 섞인 형태도 같은 규칙으로 걸려야 한다.
  const hyphenLine = `공개 발췌: 가짜-비공개_채널 관련 논의가 있었다.`;
  const afterHyphen = redactPrivateMentions(hyphenLine, PUBLIC_ACCESS);
  check('붙임표·밑줄 섞인 이름도 지워진다', afterHyphen === '',
    `(입력: "${hyphenLine}" → 출력: "${afterHyphen}")`);

  // ③ 그 이름을 볼 수 있는 권한이면 지우지 않는다 — fold 다듬기가 권한 판정 자체를
  //    깨지 않았는지 함께 본다.
  const accessToFake = accessFor([FAKE]);
  const afterAllowed = redactPrivateMentions(spacedLine, accessToFake);
  check('볼 수 있는 사람에게는 그대로 남는다', afterAllowed === spacedLine,
    `(출력: "${afterAllowed}")`);

  // ④ 이름과 무관한 줄은 안 건드린다.
  const unrelated = '이 줄은 비공개 채널과 무관합니다.';
  check('무관한 줄은 그대로 남는다', redactPrivateMentions(unrelated, PUBLIC_ACCESS) === unrelated);

  console.log('\nmatchesHiddenPrivate — 같은 fold 규칙을 쓴다 (회귀 방지)');
  check('붙여 쓴 이름 그대로도 잡는다', matchesHiddenPrivate(FAKE, PUBLIC_ACCESS) === true);
  check('띄어 쓴 이름도 잡는다', matchesHiddenPrivate('가짜 비공개 채널', PUBLIC_ACCESS) === true);
  check('볼 수 있는 사람에게는 안 잡힌다', matchesHiddenPrivate(FAKE, accessToFake) === false);
} finally {
  config.privateChannels = originalPrivate;
}

console.log(ok ? '\n전부 통과' : '\n실패 있음');
process.exit(ok ? 0 : 1);
