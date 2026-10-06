#!/usr/bin/env node
/**
 * 예약 시각을 기다리지 않고 아카이브 위생 점검을 즉시 실행한다.
 *
 *   npm run health -- --dry           콘솔에만 출력 (DM 전송 없음)
 *   npm run health -- --dry --pre     요약 전 마감 점검 (첨부만, 최근 며칠만)
 *   npm run health                    실제 DM 전송
 *
 * ANTHROPIC_API_KEY 는 필요 없다 — 모델을 쓰지 않고 파일과 슬랙 목록만 본다.
 */
import { WebClient } from '@slack/web-api';
import { requireEnv } from '../src/config.js';
import { runHealth } from '../src/archive-health.js';

// --- TYBot 연동 모드 역할 관문 -------------------------------------------------
// 연동 모드에서 발송은 TYBot 이 맡는다. 여기서 또 보내면 같은 날 같은 내용이 두 번
// 나가고, 둘의 숫자가 다르면 어느 쪽이 맞는지 알 방법이 없다.
import { ownsRole, ROLES, RoleNotOwned } from '../src/mode.js';
if (!ownsRole(ROLES.HEALTH)) {
  console.error(new RoleNotOwned(ROLES.HEALTH, 'npm run health').message);
  process.exit(2);
}

/* **`process.exit()` 을 안 쓴다** (2026-09-03). 윈도우에서 슬랙 호출을 한 뒤
 * `process.exit(n)` 을 부르면 `@slack/web-api` 가 열어 둔 비동기 핸들 위로 이벤트 루프가
 * 즉시 끊겨 `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)` 으로 죽고,
 * **종료코드가 n 이 아니라 127 로 뭉개진다** (최소 재현으로 확인: `exit(3)` → 127,
 * `exitCode = 3` → 3). 방아쇠는 WebClient 를 만든 것이 아니라 **실제 호출**이다.
 * 리눅스(VM)에서는 안 나지만 사람이 이 PC 에서 돌리는 자리라 그대로 물린다.
 * 자세한 경위는 `run-backfill.js` 머리말 — 그 파일이 먼저 같은 이유로 고쳐졌다.
 *
 * 함수로 감싸는 것은 `process.exitCode` 로는 실행이 안 멈추기 때문이다. ESM 최상위에서는
 * `return` 이 문법 오류라 함수 몸통이 있어야 조기 종료가 된다. 지금 이 파일에는 조기 종료가
 * 없지만, **뒤에 줄이 붙는 날 조용히 계속 도는 것을 막으려고** 같은 모양으로 둔다. */
async function main() {
  const dry = process.argv.includes('--dry');
  const preDigest = process.argv.includes('--pre');

  const env = requireEnv(['SLACK_BOT_TOKEN']);
  const client = new WebClient(env.SLACK_BOT_TOKEN);

  try {
    const res = await runHealth(client, { dry, preDigest });
    if (!res.sent && res.reason && res.reason !== 'dry-run') {
      console.log(`결과: 전송 안 함 (${res.reason})`);
    }
  } catch (err) {
    console.error('실패:', err.data?.error || err.message);
    process.exitCode = 1;
  }
}

await main();
