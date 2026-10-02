#!/usr/bin/env node
/**
 * 예약 시각을 기다리지 않고 요약을 즉시 실행한다. 프롬프트 손볼 때 쓴다.
 *
 *   node scripts/run-digest.js daily --dry     콘솔에만 출력 (슬랙 전송 없음)
 *   node scripts/run-digest.js daily           실제 전송
 *   node scripts/run-digest.js weekly --dry
 */
import { WebClient } from '@slack/web-api';
import { requireEnv } from '../src/config.js';
import { runDigest } from '../src/digest.js';
import { onFailure } from '../src/scheduler.js';
import { errLabel } from '../src/claude.js';

/* **`process.exit()` 을 안 쓴다** (2026-09-03). 윈도우에서 슬랙 호출을 한 뒤
 * `process.exit(n)` 을 부르면 `@slack/web-api` 가 열어 둔 비동기 핸들 위로 이벤트 루프가
 * 즉시 끊겨 `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)` 으로 죽고,
 * **종료코드가 n 이 아니라 127 로 뭉개진다** (최소 재현으로 확인: `exit(3)` → 127,
 * `exitCode = 3` → 3). 방아쇠는 WebClient 를 만든 것이 아니라 **실제 호출**이다.
 * 여기 아래 catch 는 `onFailure` 로 슬랙을 한 번 더 부른 **뒤**라 정확히 그 자리다.
 * 리눅스(VM)에서는 안 나지만 사람이 이 PC 에서 돌리는 자리라 그대로 물린다.
 * 자세한 경위는 `run-backfill.js` 머리말 — 그 파일이 먼저 같은 이유로 고쳐졌다.
 *
 * 함수로 감싸는 것은 `process.exitCode` 로는 실행이 안 멈추기 때문이다. ESM 최상위에서는
 * `return` 이 문법 오류라 함수 몸통이 있어야 조기 종료가 된다 — 아래 사용법 안내가
 * `return` 없이 그냥 흘러가면 인자가 틀린 채로 실제 요약을 돌린다. */
async function main() {
  const kind = process.argv[2];
  const dry = process.argv.includes('--dry');

  if (!['daily', 'weekly'].includes(kind)) {
    console.error('사용법: node scripts/run-digest.js <daily|weekly> [--dry]');
    process.exitCode = 1;
    return;
  }

  const env = requireEnv(['SLACK_BOT_TOKEN', 'ANTHROPIC_API_KEY']);
  const client = new WebClient(env.SLACK_BOT_TOKEN);

  const t0 = Date.now();
  try {
    const res = await runDigest(client, kind, { dry });
    if (!res.sent && res.reason && res.reason !== 'dry-run') {
      console.log(`결과: 전송 안 함 (${res.reason})`);
      /* 손으로 돌린 회차도 실패는 남긴다 — 다시 보내려고 돌리는 자리라 여기가 가장
       * 알고 싶은 곳이다. **DM 은 안 보낸다**(notify:false) — 사람이 앞에서 보고 있다.
       * target 도 반드시 명시한다 — 안 주면 onFailure 기본값이 "OO DM" 이 되어,
       * DM 이 안 갔는데도 로그를 읽는 사람은 갔다고 읽는다. */
      if (res.reason === 'generation-failed') {
        await onFailure(client, kind, '요약', res.error, {
          errorType: res.errorType, context: res.context, attempts: res.attempts,
          origin: res.origin, elapsedMs: Date.now() - t0, notify: false,
          target: '(손 실행 — 콘솔)',
          accounting: res.accounting,
        });
      }
    }
  } catch (err) {
    console.error('실패:', err.data?.error || err.message);
    await onFailure(client, kind, '요약', err?.type || err?.message || String(err), {
      // scheduler.js 의 catch 와 글자까지 같은 모양이어야 한다 — errLabel 이 유일한 판정
      // 자리다(claude.js). 여기서 따로 err?.type 만 보면 연결 오류처럼 type 이 없는 에러가
      // 이름을 잃고 집계 표에서 (기타)로 묻힌다(2026-08-28 실측 — 이 줄이 세 번째로 갈린 자리였다).
      errorType: err?.hermesType || errLabel(err),
      requestId: err?.requestID,
      context: err?.hermesContext,
      attempts: err?.hermesAttempts,
      accounting: err?.hermesAccounting,
      elapsedMs: Date.now() - t0,
      notify: false,
      target: '(손 실행 — 콘솔)',
    });
    process.exitCode = 1;
  }
}

await main();
