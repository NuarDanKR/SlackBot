#!/usr/bin/env node
/**
 * `pf-archiver` 전환 **사전 점검** — 정본의 `visibility` 와 `config.privateChannels` 대조.
 *
 *   HERMES_MODE=pf-archiver node scripts/check-archiver-privacy.js
 *
 * 종료코드: 0 전환해도 됨 / 1 **전환 금지**(비공개 선언 누락) / 2 모드·설정이 아니다
 *
 * ── 왜 전환 전에 세나 ──
 *
 * Hermes 의 공개·비공개 권위는 `config.json` 의 `privateChannels` 다. 정본에도
 * `visibility` 가 있는데, **둘이 엇갈려도 오류가 안 난다.**
 *
 * | 엇갈림 | 무슨 일이 나나 |
 * |---|---|
 * | 정본 비공개 · 선언 없음 | Hermes 가 그 채널을 **공개로** 다룬다. 평범한 답변으로 내용이 나간다 |
 * | 정본 공개 · 선언 비공개 | 닫히는 쪽. 막지는 않지만 둘 중 하나는 틀렸다 |
 * | 선언에 있는데 정본에 없음 | 옛 이름이거나 오타. 그 줄은 아무것도 안 가린다 |
 *
 * 첫 줄만 종료코드 1 이다. 나머지는 적고 0 으로 끝난다 — 닫히는 쪽으로 틀린 것 때문에
 * 전환을 막으면, 사람은 이 검사를 끄는 법부터 배운다.
 *
 * ── 권위는 안 옮긴다 ──
 *
 * 정본의 `visibility` 기본값은 `private` 라(`archive.writer`), 그것을 권위로 올리면
 * PF 의 거의 모든 채널이 한 번에 닫힌다. 그 변경은 **별건 결정**이고, 닫히는 채널 수를
 * 먼저 세야 한다. 이 검사는 그 숫자를 보여 주는 자리이기도 하다.
 */
import { ARCHIVE_SOURCE, archiverConfigProblem, archiverPrivacyAudit } from '../src/config.js';

if (ARCHIVE_SOURCE !== 'archiver') {
  console.error('HERMES_MODE 가 pf-archiver 가 아닙니다 — 대조할 정본이 없습니다.');
  process.exit(2);
}
const problem = archiverConfigProblem();
if (problem) {
  console.error(problem);
  process.exit(2);
}

let audit;
try {
  audit = archiverPrivacyAudit();
} catch (e) {
  // 링크 거부·읽기 실패는 **통과가 아니다.** 못 센 것을 「이상 없음」 으로 적으면
  // 그 다음은 전환이다.
  console.error(`정본을 읽지 못해 대조하지 못했습니다:\n${e.message}`);
  process.exit(2);
}

console.log(`정본 채널 ${audit.checked}개를 config.privateChannels 와 대조했습니다.`);

if (!audit.rawVisibilityInformative) {
  console.log(
    `\n· raw 의 visibility 는 전 채널이 같은 값입니다(${audit.rawVisibilityValues.join(', ') || '없음'}).`
    + '\n  수집기가 raw 에는 그 값을 안 넘겨서(writer 기본값) **정보가 없습니다.**'
    + '\n  대조는 첨부 정본의 visibility 로만 합니다 — 그쪽에만 Slack 의 is_private 가 실립니다.',
  );
}

if (audit.missing.length) {
  console.error(`\n✗ 정본이 비공개인데 선언에 없는 채널 ${audit.missing.length}개 — **전환 금지**`);
  for (const name of audit.missing) console.error(`    ${name}`);
  console.error(
    '\n  이대로 전환하면 Hermes 는 이 채널들을 **공개로** 다룹니다. 오류가 아니라\n'
    + '  평범한 답변으로 내용이 나가고, 내용을 아는 사람만 알아챕니다.\n'
    + '  config.json 의 privateChannels 에 위 이름을 그대로 적은 뒤 다시 돌리세요.',
  );
}
if (audit.extra.length) {
  console.log(`\n· 선언은 비공개인데 정본은 공개라고 적은 채널 ${audit.extra.length}개`);
  for (const name of audit.extra) console.log(`    ${name}`);
  console.log('  닫히는 쪽이라 전환을 막지 않습니다. 둘 중 하나는 틀렸으니 확인하세요.');
}
if (audit.unverifiable.length) {
  console.log(`\n· 정본에 공개 여부 신호가 없는 채널 ${audit.unverifiable.length}개`);
  for (const name of audit.unverifiable) console.log(`    ${name}`);
  console.log(
    '  첨부가 없어 Slack 의 is_private 가 정본 어디에도 안 실렸습니다.\n'
    + '  **전환을 막지는 않습니다** — 막으면 첨부 없는 채널 때문에 영영 전환할 수 없고,\n'
    + '  그 상태의 공개 판정은 지금도 config.json 이 쥐고 있어 달라지는 것이 없습니다.\n'
    + '  다만 이 숫자가 0 이 되기 전에는 정본을 ACL 권위로 올릴 수 없습니다(별건 결정).',
  );
}
if (audit.unknown.length) {
  console.log(`\n· 선언에 있는데 정본에 없는 이름 ${audit.unknown.length}개 (옛 이름·오타)`);
  for (const name of audit.unknown) console.log(`    ${name}`);
  console.log('  그 줄은 아무것도 안 가립니다.');
}
if (!audit.missing.length && !audit.extra.length && !audit.unknown.length
    && !audit.unverifiable.length) {
  console.log('\n✓ 엇갈림 없음.');
}

process.exit(audit.missing.length ? 1 : 0);
