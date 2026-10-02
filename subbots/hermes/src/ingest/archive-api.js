/** Extracted ingest boundary; dependencies are supplied by slack-archive.js. */
export function createArchiveApi({ fetchAllReplies }) {
/* ── 슬랙에서 읽기 ──────────────────────────────────────────────── */

/** 채널의 last_ts 이후 메시지 (스레드 부모 기준) */
async function fetchNewMessages(client, channelId, lastTs) {
  const out = [];
  let cursor;
  do {
    const res = await client.conversations.history({
      channel: channelId,
      oldest: lastTs,
      inclusive: false,
      limit: 200,
      cursor,
    });
    for (const m of res.messages || []) {
      // oldest 의 경계 포함 여부가 슬랙에서 애매하다. 같은 ts 는 버린다 (중복 방어).
      if (m.ts === lastTs) continue;
      out.push(m);
    }
    cursor = res.response_metadata?.next_cursor || undefined;
  } while (cursor);
  return out.sort((a, b) => Number(a.ts) - Number(b.ts));
}

async function fetchReplies(client, channelId, threadTs) {
  const all = await fetchAllReplies(client, { channel: channelId, ts: threadTs });
  return all.filter((r) => r.ts !== threadTs);
}

/**
 * lookback 구간의 **이미 반영된** 메시지 전부.
 *
 * `conversations.history` 는 **부모의 작성 시각**으로 거른다. 그래서 지난주 메시지에 오늘
 * 정정 댓글이 달려도 새 메시지 조회에는 안 잡힌다. 이 구간을 되돌아보며 ① 답글이 늘어난
 * 스레드와 ② 슬랙에서 고쳐지거나 지워진 메시지를 **같은 조회 결과로 함께** 찾는다.
 *
 * 예전에는 `state.threads` 목록만 훑었다. 그 목록에는 **반영 당시 답글이 있던** 스레드만
 * 들어 있어서, 그때 답글이 0건이던 메시지에 나중에 달린 정정은 감지조차 되지 않고 영영
 * 사라졌다 (2026-08-05 #사업장사 실제 사례).
 */
async function fetchWindow(client, channelId, lastTs, lookbackSec) {
  const out = [];
  /* **소수 6자리로 못박는다.** 슬랙은 oldest 의 소수가 7자리를 넘으면 `ok: true` 에 에러도
   * 없이 **빈 목록**을 돌려준다 (2026-08-06 실측). 슬랙 ts 는 6자리인데 여기서 초 단위를
   * 빼면 부동소수 오차로 7자리가 나올 수 있다 — 그러면 그 채널의 되돌아보기가 조용히
   * 0건이 되어 정정 댓글도 수정·삭제도 영영 안 잡힌다. 오늘 48개 채널은 전부 6자리라
   * 실제로 걸린 적은 없지만, 값에 따라 언제든 걸린다. (같은 버그가 doc-archive 의
   * fetch_slack_files.py 에서는 실제로 터졌다 — --days 가 오늘 첨부를 통째로 놓쳤다.) */
  const oldest = Math.max(0, Number(lastTs) - lookbackSec).toFixed(6);
  let cursor;
  do {
    const res = await client.conversations.history({
      channel: channelId, oldest, latest: lastTs, inclusive: true, limit: 200, cursor,
    });
    out.push(...(res.messages || []));
    cursor = res.response_metadata?.next_cursor || undefined;
  } while (cursor);
  return { oldest, messages: out.sort((a, b) => Number(a.ts) - Number(b.ts)) };
}

/** 그 구간에서 **반영 이후 답글이 달린** 메시지 — 정정 댓글이 여기로 온다
 *
 *  **폴백이 없다** — latest_reply 가 없으면 `[정정]` 이 아카이브에 안 들어오는데
 *  에러가 안 난다. 같은 필드를 archive-health.js 의 channelAttachments 와
 *  slack-live.js 의 fetchRecentSlack 도 쓴다. 결측은 `npm run check` [6/6] 이 센다. */
function grownThreads(windowMsgs, lastTs) {
  return windowMsgs.filter(
    (m) => m.reply_count && m.latest_reply && Number(m.latest_reply) > Number(lastTs),
  );
}

  return { fetchNewMessages, fetchReplies, fetchWindow, grownThreads };
}
