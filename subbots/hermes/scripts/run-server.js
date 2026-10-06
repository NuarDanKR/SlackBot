#!/usr/bin/env node
/**
 * 독립 Slack 런타임의 가장 바깥 관문.
 *
 * `src/index.js` 는 ESM 정적 import 때문에 본문보다 먼저 config와 Slack 의존성을
 * 읽는다. TYBot 연동에서 그 뒤에 막으면 설정이 없는 호스트에서는 경계 위반이 아니라
 * `config.json 없음`으로 보인다. 이 파일은 로컬 모드 모듈만 읽고 먼저 판정한다.
 */
import { assertOwnsRole, ROLES } from '../src/mode.js';

try {
  assertOwnsRole(ROLES.ANSWER, 'Hermes 독립 Slack 서비스(npm start)');
  await import('../src/index.js');
} catch (err) {
  console.error(err?.message || String(err));
  process.exitCode = err?.code === 'tybot_mode_role_not_owned' ? 2 : 1;
}
