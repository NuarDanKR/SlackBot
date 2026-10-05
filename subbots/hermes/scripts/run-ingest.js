#!/usr/bin/env node
/**
 * 예약 시각을 기다리지 않고 아카이브 자동 반영을 실행한다.
 *
 *   npm run ingest -- --dry                  파일·상태·git 을 건드리지 않고 화면에만
 *   npm run ingest -- --dry --skip-summary   요약 대조까지 건너뛴다 (모델 호출 없음)
 *   npm run ingest -- --dry --skip-summary --no-edit-scan   수정·삭제 대조도 건너뛴다 (요약 전 17:00 회차와 같은 조합)
 *   npm run ingest                           실제 반영 + 커밋 + push + DM
 */
import { WebClient } from '@slack/web-api';
import { requireEnv } from '../src/config.js';
import { runIngest } from '../src/ingest/index.js';

// --- TYBot 연동 모드 관문 -----------------------------------------------------
// CLI 는 사람이 직접 치는 자리다. 깊은 곳에서 던지면 스택만 보이고 무엇을 해야
// 하는지 안 보이므로, **여기서 먼저** 사람 말로 멈춘다.
import { isTybotMode, ArchiveWriteBlocked } from '../src/mode.js';
if (isTybotMode()) {
  const blocked = new ArchiveWriteBlocked('npm run ingest');
  console.error(blocked.message);
  process.exit(2);
}

/* **`process.exit()` 을 안 쓴다** (2026-09-03). 윈도우에서 슬랙 호출을 한 뒤
 * `process.exit(n)` 을 부르면 `@slack/web-api` 가 열어 둔 비동기 핸들 위로 이벤트 루프가
 * 즉시 끊겨 `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)` 으로 죽고,
 * **종료코드가 n 이 아니라 127 로 뭉개진다** (최소 재현으로 확인: `exit(3)` → 127,
 * `exitCode = 3` → 3). 방아쇠는 WebClient 를 만든 것이 아니라 **실제 호출**이다.
 * 이 파일은 종료코드가 곧 신호라 특히 물린다 — 아래 관문 실패의 **2** 가 127 이 되면
 * 「관문에서 막혔다」와 「크래시했다」를 로그에서 못 가른다.
 * 리눅스(VM)에서는 안 나지만 사람이 이 PC 에서 돌리는 자리라 그대로 물린다.
 * 자세한 경위는 `run-backfill.js` 머리말 — 그 파일이 먼저 같은 이유로 고쳐졌다.
 *
 * 함수로 감싸는 것은 `process.exitCode` 로는 실행이 안 멈추기 때문이다. ESM 최상위에서는
 * `return` 이 문법 오류라 함수 몸통이 있어야 조기 종료가 된다. */
async function main() {
  const dry = process.argv.includes('--dry');
  const skipSummary = process.argv.includes('--skip-summary');
  const scanEdits = process.argv.includes('--no-edit-scan') ? false : undefined;

  const needed = ['SLACK_BOT_TOKEN'];
  if (!skipSummary) needed.push('ANTHROPIC_API_KEY');
  const env = requireEnv(needed);

  const client = new WebClient(env.SLACK_BOT_TOKEN);

  try {
    const res = await runIngest(client, { dry, skipSummary, scanEdits });
    if (!res.sent && res.reason && res.reason !== 'dry-run') {
      console.log(`결과: DM 안 보냄 (${res.reason})`);
    }
    // 관문에서 막혔으면 종료코드로도 알린다 (systemd·로그에서 보이게)
    if (res.gate && !res.gate.passed) { process.exitCode = 2; return; }
  } catch (err) {
    console.error('실패:', err.data?.error || err.message);
    process.exitCode = 1;
  }
}

await main();
