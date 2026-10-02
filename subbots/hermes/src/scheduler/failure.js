/**
 * Failure logging and owner notification with injected policy and boundaries.
 * Factory creation performs no I/O. The scheduler retains job and cron policy.
 */
export function createSchedulerFailure({
  config, LOG_KIND, logConversation, usageFields, console,
}) {
  /* 실패를 반드시 본인 DM 으로 알린다 (2026-08-06).
   *
   * 그전에는 실패해도 콘솔 로그 한 줄이 전부라 밖에서는 아무 표시가 없었다. 게다가 대화가 0건인
   * 날은 안 보내는 것이 정상이라(config.json 의 minMessages), **조용한 것만으로는 정상과 실패를
   * 구분할 수 없다.** 2026-08-06 에 일일 요약이 API 과부하로 안 나갔는데, VM 로그를 열기 전까지
   * 아무도 몰랐다.
   *
   * 전달처(deliverTo)와 무관하게 늘 본인 DM 이다 — 운영 상태이지 팀에 알릴 내용이 아니다.
   */
  async function notifyFailure(client, kind, label, reason, requestID) {
    if (!config.owner?.slackUserId) return;
    const when = new Intl.DateTimeFormat('sv-SE', {
      timeZone: config.timezone, dateStyle: 'short', timeStyle: 'medium',
    }).format(new Date());

    const lines = [
      (kind === 'ingest' || kind === 'ingestPre')
        ? `⚠️ *${kind} ${label} 실패* — 이번 회차의 처리·보고 상태를 확인해야 합니다.`
        : `⚠️ *${kind} ${label} 실패* — 이번 회차는 나가지 않았습니다.`,
      `사유: ${reason}`,
      `시각: ${when} (${config.timezone})`,
    ];
    if (requestID) lines.push(`요청 ID: ${requestID}`);
    if (kind === 'daily' || kind === 'weekly') lines.push(`다시 보내려면 VM 에서 \`npm run digest:${kind}\``);

    const im = await client.conversations.open({ users: config.owner.slackUserId });
    await client.chat.postMessage({
      channel: im.channel.id,
      text: lines.join('\n'),
      unfurl_links: false,
      unfurl_media: false,
    });
  }

  /**
   * 실패를 **한 자리에서** 알리고 기록한다 (2026-08-28).
   *
   * 실패가 흘러오는 길이 셋인데 전에는 하나만 봤다 — 던지는 것(요약·위생 점검),
   * `{sent:false, reason:'generation-failed'}`, 그리고 자동 반영의 `result.fatal`.
   * 세 번째는 `ingest/index.js` 의 catch 가 다시 던지지 않아 여기서는 정상 종료로 보였고,
   * 그래서 자동 반영이 죽은 회차는 로그에 한 줄도 안 남았다.
   *
   * **DM 은 갈래마다 다르지만 기록은 전부 여기를 지난다.** 잡마다 자기 자리에서 기록하면
   * 넷으로 흩어지는데, 실패 기록은 없어도 에러가 안 나는 종류라 한 자리를 빠뜨리면
   * 그 잡만 영영 로그에 안 남는다 — 자동 반영이 지금까지 정확히 그 상태였다.
   */
  async function onFailure(client, kind, label, reason, opts = {}) {
    const { errorType, requestId, context, attempts, elapsedMs, origin, target, accounting, notify = true } = opts;

    // 기록을 **먼저** 한다 — DM 이 실패해도 기록은 남아야 한다.
    try {
      logConversation({
        kind: LOG_KIND[kind] || kind,
        ok: false,
        target: target || `${config.owner?.name} DM`,
        origin,
        error: String(reason).slice(0, 500),
        errorType,
        requestId,
        context,
        attempts,
        ...usageFields(accounting),
        elapsedMs,
      });
    } catch (err) {
      // Logging must not prevent the independent owner notification. A write
      // may have partly succeeded, so do not retry it or duplicate its cost.
      console.error(`[${kind}] 실패 기록도 남기지 못했습니다:`, err?.data?.error || err?.message || String(err));
    }

    if (!notify) return;
    // 알림이 실패해도 스케줄러는 살아 있어야 한다.
    try {
      await notifyFailure(client, kind, label, reason, requestId);
    } catch (e) {
      console.error(`[${kind}] 실패 알림도 보내지 못했습니다:`, e?.data?.error || e?.message || String(e));
    }
  }
  return { notifyFailure, onFailure };
}
