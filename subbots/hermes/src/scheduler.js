import { createSchedulerFailure } from './scheduler/failure.js';
import { usageFields } from './llm/usage.js';
/**
 * 예약 실행. 시각은 config.json 의 digest.daily.cron / digest.weekly.cron 에서 바꾼다.
 *
 *   "30 17 * * 1-5"  → 평일 17:30
 *   "0 16 * * 5"     → 금요일 16:00
 *   분 시 일 월 요일   (요일: 0=일 … 5=금)
 */
import cron from 'node-cron';
import { config } from './config.js';
import { runDigest } from './digest.js';
import { runHealth } from './archive-health.js';
import { runIngest } from './ingest/index.js';
import { append as logConversation } from './convo-log.js';
import { errLabel } from './claude.js';
import { isTybotMode } from './mode.js';

/* 전 채널 스캔이 하루 세 번 도는 것에 대하여 — **합치지 않기로 했다** (2026-08-06).
 *
 * 07:00 자동 반영 · 09:00 위생 점검 · 17:30 일일요약이 각각 전 채널을 훑는다. 한 번만 훑어
 * 셋이 나눠 쓰면 API 호출이 1/3 이 되지만, 셋은 **같은 것을 보지 않는다.**
 *
 *   자동 반영 — 마지막 반영 시각 이후. 다 넣고 나서 `.sync-state.json` 의 last_sync 를 **민다**
 *   위생 점검 — 최근 60일 + 스레드 답글 전부(첨부 찾기). 그리고 **밀린 그 last_sync 를 잰다**
 *   일일요약  — 직전 실행 이후 24시간. 보내는 시각에 끝나야 하는 창이다
 *
 * 창이 다른 것보다 **순서가 뜻을 갖는 것**이 결정적이다. 위생 점검은 자동 반영이 민 뒤의
 * last_sync 를 재야 "밀렸다"가 사실이 되고, 일일요약의 24시간은 보내는 순간에 닿아 있어야
 * 하루가 빈틈없이 이어진다(slack-live.js 의 dailyWindow). 07:00 스캔 하나를 셋이 나눠 쓰면
 * 위생 점검은 자기가 재려던 값을 못 재고, 일일요약은 10시간 반 낡은 자료로 그날을 요약한다.
 *
 * 아끼는 것은 슬랙 API 호출뿐이고(모델 비용이 아니다) 셋 다 레이트리밋 안에서 돈다.
 * 정확성을 내주고 살 만한 것이 아니다. 가장 무거운 것은 위생 점검의 60일 첨부 스캔인데,
 * 그건 `files.list` 로 한 번에 받는 길이 있지만 **봇에게 안 보이는 파일이 조용히 빠져**
 * "미변환 0건" 이라는 거짓 통과가 된다. 지금 방식은 채널을 하나씩 열어 못 읽은 채널을
 * 이름으로 보고한다(archive-health.js 의 failed).
 */

/* 요약 전 마감 — 16:00 점검 · 17:00 자동 반영 (2026-08-06).
 *
 * 일일 요약은 슬랙을 직접 읽으므로 대화는 늘 최신이지만, **첨부 문서만은 다르다.** 실시간
 * 조회가 주는 것은 파일 이름뿐이라, 변환된 md 가 없으면 요약이 "무슨 파일이 올라왔다"까지밖에
 * 못 쓴다(digest.js). 변환은 사람이 `doc-archive` 로 하는 일이라 시각을 앞에 둘 수밖에 없다.
 *
 *   16:00 마감 점검 — 오늘 올라온 첨부 중 안 들어간 것을 DM. 사람이 돌릴 시간 90분
 *   17:00 자동 반영 — git pull 로 그 문서 커밋을 VM 에 들이고, 대화 원문도 요약 시각에 붙인다
 *   17:30 일일 요약 — 방금 들어간 문서를 재료로 읽는다
 *
 * 16:00 회차는 최근 2일만 훑어 아침 회차(60일)보다 훨씬 가볍고, 17:00 회차는 요약 대조와
 * 편집·삭제 대조를 끈다(아침 회차 담당). 같은 후보를 하루 두 번 받으면 안 읽게 된다.
 */

// kind → 무엇을 부르고 로그에 뭐라고 쓸지. 설정은 전부 config.digest 아래에 있다.
// export 하는 이유: check-failure-log.js 의 검사 ⑨ 가 이 키들이 LOG_KIND 에 다
// 있는지 직접 먹여 본다 — 목록을 검사 쪽에 따로 적으면 여기 하나 늘 때 거기서 조용히 빠진다.
export const JOBS = {
  daily: { label: '요약', run: (client) => runDigest(client, 'daily') },
  weekly: { label: '요약', run: (client) => runDigest(client, 'weekly') },
  health: { label: '위생 점검', run: (client) => runHealth(client, { sync: true }) },
  healthPre: { label: '요약 전 마감 점검', run: (client) => runHealth(client, { preDigest: true, sync: true }) },
  ingest: { label: '자동 반영', run: (client) => runIngest(client) },
  ingestPre: {
    label: '자동 반영 (요약 전)',
    run: (client) => runIngest(client, { skipSummary: true, scanEdits: false }),
  },
};

/* 잡 여섯을 로그 종류 넷으로 접는다. 07:00·17:00 과 09:00·16:00 은 origin 으로 가른다
 * (위생 점검이 이미 그렇게 한다 — archive-health.js 의 origin).
 * export 하는 이유는 JOBS 와 같다 — check-failure-log.js 의 검사 ⑨ 가 값이 전부
 * BROADCAST 에 있는지까지 이어서 본다. */
export const LOG_KIND = {
  daily: 'daily', weekly: 'weekly',
  health: 'health', healthPre: 'health',
  ingest: 'ingest', ingestPre: 'ingest',
};

/* 자기 실패를 보고하는 잡. sent:true로 보고 완료가 확인되면 추가 DM을 억제한다.
 * 미전송·전달 불명·예외에서는 상태 확인용 보조 알림을 시도한다. */
const SELF_REPORTS = new Set(['ingest', 'ingestPre']);

/* 자동 반영이 throw 하지 않고 정상 반환하며 알리는 실패 셋 (ingest/index.js 의
 * classifyFailure, 2026-08-28 F1) — fatal(중간에 죽음)·gate_failed(관문이 막아 되돌림)·
 * push_failed(커밋은 됐는데 push 만 안 됨). 셋 다 catch 도 안 걸리고 reason 으로만
 * 구분되므로, 여기서 놓치면 그 갈래만 로그에 한 줄도 안 남는다 — 관문에서 막힌 회차가
 * 실제로 그 상태였다. */
const INGEST_FAILURE_REASONS = new Set(['fatal', 'gate_failed', 'push_failed']);

/* 던지지 않고 돌아온 실패에 쓸 기본 문구. res.error 가 없을 때만 쓰는 마지막
 * 기본값이다 — digest.js 는 이제 늘 error 를 채우므로 'generation-failed' 쪽은
 * 거의 쓰이지 않는다. 그래도 손 실행 등 error 가 빌 수 있는 경로가 있어 남겨 둔다
 * (WHK 결정 2026-08-28 — DM 사유 문구가 더 구체적으로 바뀌는 것이 이번 작업의 취지다). */
const FALLBACK_REASON = {
  'generation-failed': '요약 생성 실패 (모델이 거절했거나 빈 답)',
  fatal: '자동 반영이 중단되었습니다',
};

// Codex R3g: retain public failure entry points and shared log-kind policy.
export const { notifyFailure, onFailure } = createSchedulerFailure({
  config, LOG_KIND, logConversation, usageFields, console,
});

// export 하는 이유: check-missed-execution.js 가 놓친 실행 리스너가 실제로 달렸는지·
// onFailure 로 이어지는지를 직접 재려고 이 함수가 만든 task 를 그대로 돌려받는다.
export function schedule(client, kind) {
  const { label, run } = JOBS[kind];
  const spec = config.digest[kind];
  if (!spec?.enabled) {
    console.log(`  ${kind} ${label}: 꺼져 있음 (config.json)`);
    return;
  }
  if (!cron.validate(spec.cron)) {
    console.error(`  ${kind} ${label}: cron 식이 잘못되었습니다 — "${spec.cron}"`);
    return;
  }

  const task = cron.schedule(
    spec.cron,
    async () => {
      const t0 = Date.now();
      const notify = !SELF_REPORTS.has(kind);
      try {
        const res = await run(client);
        /* 던지지 않고 "안 보냈다"로 돌아오는 길이 여럿이다. 'no-activity'(대화 0건)·
         * 'healthy'(이상 없음)·'nothing-to-report' 는 **정상이라 알리지 않는다.**
         * 실패인 것만 잡는다 — 'generation-failed'(요약이 안 나갔다)와 자동 반영이
         * 정상 반환하며 알리는 셋(INGEST_FAILURE_REASONS, 2026-08-28 F1). 이 넷은
         * 자기 DM 이 나가므로 sent 가 true 일 수도 있다 — 그래서 sent 가 아니라
         * reason 으로 가른다. */
        const failed = INGEST_FAILURE_REASONS.has(res?.reason)
          || (res?.sent === false && res.reason === 'generation-failed');
        if (failed) {
          await onFailure(client, kind, label, res.error || FALLBACK_REASON[res.reason], {
            errorType: res.errorType,
            context: res.context,
            attempts: res.attempts,
            accounting: res.accounting,
            origin: res.origin,
            elapsedMs: Date.now() - t0,
            notify: notify || res.sent !== true,
          });
        }
      } catch (err) {
        // 한 번 실패해도 스케줄러는 계속 살아 있어야 한다.
        console.error(`[${kind}] 실행 실패:`, err.data?.error || err.message);
        await onFailure(client, kind, label, err?.type || err?.message || String(err), {
          // hermesType 은 우리가 던진 에러에 붙인 것(예: max_tokens). 그 외엔 errLabel 이
          // 유일한 판정이다(claude.js) — 여기서 따로 err?.type 만 보면 연결 오류처럼 type 이
          // 없는 에러가 이름을 잃고 집계 표에서 (기타)로 묻힌다(2026-08-28 실측).
          errorType: err?.hermesType || errLabel(err),
          requestId: err?.requestID,
          // 던져서 죽은 회차도 기간 라벨을 남긴다 (digest.js 가 err.hermesOrigin 에 실어
          // 보낸다). 없으면 그 갈래의 로그에만 `_기간:_` 줄이 빠져 같은 잡의 로그 모양이
          // 갈린다 — 2026-08-28 최종 검토가 짚고 미뤄 둔 것.
          origin: err?.hermesOrigin,
          // 잡이 죽기 전에 실어 보낸 발자국·시도 이력.
          context: err?.hermesContext,
          attempts: err?.hermesAttempts,
          accounting: err?.hermesAccounting,
          elapsedMs: Date.now() - t0,
          // Unexpected self-reporting failures have not confirmed a report.
          notify: notify || err?.hermesReport?.status !== 'sent',
        });
      }
    },
    { timezone: config.timezone },
  );

  /* node-cron 은 예정 시각을 **1초(기본 `missedExecutionTolerance`)** 넘겨 그 회차를
   * 아예 건너뛸 수 있다 — 이벤트 루프가 그만큼만 막혀도(다른 잡의 무거운 동기 작업 등)
   * 조용히 넘어간다. 리스너를 안 달면 node-cron 자체 로거의 `console.warn` 한 줄이
   * 전부라 VM 로그를 열기 전까지 아무도 모른다 — 이 파일이 이미 겪은 그 실패
   * 모양이다(위 `onFailure` 머리말의 2026-08-06 사고와 같다: "콘솔 로그 한 줄이
   * 전부라 밖에서는 아무 표시가 없었다"). **실행이 아예 안 됐으니 그 잡 자신의 실패
   * 보고(SELF_REPORTS)도 못 도니, 여기서 놓치면 이 회차는 어디에도 안 남는다.** */
  task.on('execution:missed', (ctx) => {
    const when = ctx?.date instanceof Date ? ctx.date.toISOString() : String(ctx?.date ?? '');
    onFailure(client, kind, label, `예약 실행을 건너뛰었습니다 (이벤트 루프 지연) — 예정 시각 ${when}`, {
      errorType: 'missed-execution',
      elapsedMs: 0,
      notify: true,
    }).catch((e) => console.error(`[${kind}] 놓친 실행 알림도 보내지 못했습니다:`, e?.message));
  });

  let next = '';
  try {
    const n = task.getNextRun?.();
    if (n) next = ` · 다음 실행 ${new Intl.DateTimeFormat('sv-SE', {
      timeZone: config.timezone, dateStyle: 'short', timeStyle: 'short',
    }).format(n)}`;
  } catch {}

  console.log(`  ${kind} ${label}: "${spec.cron}" (${config.timezone})${next}`);
  return task;
}

export function startScheduler(client) {
  console.log('예약 작업');
  // 연동 모드에서는 **쓰는 작업을 아예 예약하지 않는다.** 예약해 두고 실행할 때
  // 막으면, 매 회차 실패가 쌓여 위생 점검이 「고장」 으로 보인다. 안 하는 것과
  // 못 하는 것은 화면에서 구별돼야 한다.
  const writesBlocked = isTybotMode();
  schedule(client, 'daily');
  schedule(client, 'weekly');
  schedule(client, 'health');
  schedule(client, 'healthPre');
  if (writesBlocked) {
    console.log('  자동 반영(ingest) — TYBot 연동 모드라 예약하지 않음');
  } else {
    schedule(client, 'ingest');
    schedule(client, 'ingestPre');
  }
  const to = config.digest.deliverTo === 'channel' ? `채널 ${config.digest.channelId}` : `${config.owner.name} 에게 DM`;
  // 위생 점검은 내부 운영 상태라 deliverTo 와 무관하게 항상 본인 DM 이다.
  console.log(`  전달처: 요약 ${to} · 위생 점검 ${config.owner.name} 에게 DM\n`);
}
