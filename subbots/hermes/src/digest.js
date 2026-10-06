import { usageFields, attachUsage } from './llm/usage.js';
/**
 * 일일/주간 요약.
 *
 * 대화의 소스는 슬랙 실시간 조회다. 아카이브 동기화 상태와 무관하게 항상 최신이며,
 * 스레드 답글까지 가져온다(요약 대상이 "스레드 대화"이므로 필수).
 *
 * **첨부 문서만 예외다.** 실시간 조회는 파일 이름밖에 안 주므로(slack-live.js), 그것만으로는
 * "무슨 파일이 올라왔다"까지밖에 못 쓴다. 그래서 기간 안에 올라온 문서는 변환된 md
 * (documents/)에서 본문을 꺼내 재료에 함께 싣는다 — 여기만 아카이브에 기댄다.
 * 아직 변환 안 된 것은 실을 방법이 없으므로 **건수로 알린다**(꼬리말).
 */
import { config, accessLabel, FULL_ACCESS, PUBLIC_ACCESS } from './config.js';
import {
  fetchWindow, formatTranscript, dailyWindow, weeklyWindow, markDailyDigestSent,
} from './slack-live.js';
import { documentsUploadedIn } from './documents.js';
import { unconvertedAmong } from './archive-health.js';
import { generateDigest } from './claude.js';
import { toSlackMrkdwn, chunkForSlack } from './format.js';
import { append as logConversation } from './convo-log.js';
import { assertOwnsRole, ROLES } from './mode.js';

function summarize(entries) {
  let messages = 0;
  let replies = 0;
  const failed = [];
  for (const e of entries) {
    if (e.error) {
      failed.push(`#${e.channel}(${e.error})`);
      continue;
    }
    messages += e.messages.length;
    for (const m of e.messages) replies += m.replies.length;
  }
  const live = entries.filter((e) => !e.error && e.messages.length);
  return { messages, replies, failed, channels: live.length };
}

/** 요약을 받을 대상(DM 또는 채널)의 채널 ID */
async function deliveryTarget(client) {
  if (config.digest.deliverTo === 'channel') {
    if (!config.digest.channelId) throw new Error('config.json 의 digest.channelId 가 비어 있습니다.');
    return config.digest.channelId;
  }
  const im = await client.conversations.open({ users: config.owner.slackUserId });
  return im.channel.id;
}

/**
 * @param {object} client Slack WebClient
 * @param {'daily'|'weekly'} kind
 * @param {{dry?:boolean}} opts  dry=true 면 슬랙에 보내지 않고 콘솔에만 출력
 */
export async function runDigest(client, kind, { dry = false } = {}) {
  // 연동 모드에서 요약 발송은 TYBot 이 맡는다. **맨 앞이어야** 한다 —
  // 아래에서 모델을 부르면 막기 전에 비용이 나간다.
  if (!dry) assertOwnsRole(ROLES.DIGEST, '일일·주간 요약 발송(runDigest)');
  const w = kind === 'daily' ? dailyWindow() : weeklyWindow();
  const started = Date.now();
  console.log(`\n[${kind}] ${w.label} 수집 시작…`);

  // 비공개 채널(#비공개가)을 넣을지는 "요약이 어디로 가느냐"로 정한다.
  //   본인 DM  → 포함. 어차피 그 사람만 본다.
  //   채널     → 제외. 그 채널 전원에게 보이므로 넣으면 유출이다.
  // deliverTo 를 바꿀 때 이 판단을 따로 기억해야 한다면 언젠가 반드시 틀린다. 여기서 자동으로 묶는다.
  const toChannel = config.digest.deliverTo === 'channel';
  const access = toChannel ? PUBLIC_ACCESS : FULL_ACCESS;

  // forDigest — 봇을 부른 질문과 `[정정]` 답글을 재료에서 뺀다. 둘 다 사업 진행이 아니라
  // 기록을 고치거나 봇을 쓴 흔적이다. Q&A 쪽(fetch_recent_slack)은 이 옵션을 켜지 않는다.
  const entries = await fetchWindow(client, {
    oldest: w.oldest,
    latest: w.latest,
    access,
    forDigest: true,
  });

  const s = summarize(entries);
  // **채널 이름을 적지 않는다** (WHK 지시 2026-08-05). 이 문구는 stats 에 실려 ① 모델의
  // 요청 메시지와 ② 요약 하단 꼬리말로 들어가고, 전달처가 채널이면 그 채널 전원이 본다.
  // 예전에는 비공개 채널 이름 네 개가 매일 공개 채널에 그대로 게시됐다.
  const excluded = access.full ? '' : ' · 비공개 채널 제외';
  const stats =
    `채널 ${s.channels}개 · 메시지 ${s.messages}건 · 스레드 답글 ${s.replies}건` +
    excluded +
    (s.failed.length ? ` · 읽기 실패 ${s.failed.length}개(${s.failed.join(', ')})` : '');
  console.log(`[${kind}] ${stats} (${((Date.now() - started) / 1000).toFixed(1)}s)`);

  if (s.messages < config.digest.minMessages) {
    console.log(`[${kind}] 대화가 없어 보내지 않습니다.`);
    return { sent: false, reason: 'no-activity' };
  }

  /* 기간 안에 올라온 문서 — **일일 요약에서만.**
   *
   * 주간은 그대로 둔다. 일주일치 문서를 같은 상한에 욱여넣으면 어느 것도 제대로 안 실리고,
   * 주간 프롬프트에는 문서를 어떻게 다룰지가 적혀 있지 않다 (daily.md 의 「첨부 문서」 절).
   *
   * access 를 그대로 넘긴다 — 채널로 나가는 요약이면 PUBLIC_ACCESS 라 비공개 채널 문서가
   * documentsUploadedIn 에서 빠진다. 여기서 판정을 새로 만들지 않는 것이 요점이다.
   * 미반영 건수는 슬랙을 다시 조회하지 않고 방금 받은 entries 의 파일 이름으로 센다. */
  const withDocs = kind === 'daily';
  const uploaded = withDocs
    ? documentsUploadedIn({ oldest: w.oldest, latest: w.latest, access })
    : { docs: [], hidden: 0, gistOnly: 0 };
  /* 꼬리말의 `미반영 N건` 이 뜻하는 것은 **「봇이 아직 못 읽는 자료가 N건」**이다.
   * 그래서 09:00 DM 의 `미변환` 과 값이 다를 수 있다 — 봇이 다운로드 주소를 못 받는 첨부를
   * 여기서는 세고 거기서는 `restricted` 로 따로 뺀다. **버그가 아니다**(WHK 결정 2026-08-13).
   * 자세한 이유와 「맞추려 들면 무엇이 깨지나」는 `unconvertedAmong` 머리말에 있다. */
  const pendingDocs = withDocs
    ? unconvertedAmong(
        entries.flatMap((e) =>
          e.error ? [] : e.messages.flatMap((m) => (m.files || []).map((name) => ({ channel: e.channel, name }))),
        ),
      )
    : 0;
  if (uploaded.docs.length || pendingDocs) {
    console.log(
      `[${kind}] 문서 ${uploaded.docs.length}건 반영` +
        (uploaded.gistOnly ? ` (길이 상한으로 ${uploaded.gistOnly}건은 요지만)` : '') +
        (uploaded.hidden ? ` · 권한으로 제외 ${uploaded.hidden}건` : '') +
        (pendingDocs ? ` · 미반영 ${pendingDocs}건` : ''),
    );
  }

  /* 재료가 여기까지 정상으로 모였다는 사실을 실어 보낸다 (2026-08-28).
   *
   * 이게 없으면 로그만 보고는 「슬랙을 못 읽어 재료가 비었다」와 「재료는 멀쩡한데 모델
   * 호출만 죽었다」가 구별되지 않는다. 2026-08-06 회차를 조사할 때 VM 로그를 열어야
   * 알았던 것이 그 구분이다. stats 는 모델 호출 **전에** 만들어지므로 여기서 살아 있다. */
  let gen;
  try {
    gen = await generateDigest({
      kind,
      transcript: formatTranscript(entries),
      documents: uploaded.docs,
      windowLabel: w.label,
      stats,
    });
  } catch (err) {
    err.hermesContext = `${stats} → 요약 생성에서 중단`;
    /* **회차 라벨을 에러에 실어 보낸다.** 이것이 없으면 던져서 죽은 회차의 로그에만
     * `_기간:_` 줄이 빠져, 같은 잡인데 갈래마다 로그 모양이 달라진다 — 생성 실패로
     * 돌아온 쪽은 `res.origin` 으로 라벨을 넘기는데(아래 `run()` 의 정상 반환) 던진 쪽은
     * 넘길 자리가 없었다. `scheduler.js` 의 catch 가 이 값을 `origin` 으로 받는다. */
    err.hermesOrigin = w.label;
    throw err;
  }
  const { text, refused, model, usage, costUsd, attempts, accounting } = gen;

  if (refused || !text) {
    console.warn(`[${kind}] 요약 생성 실패 — 보내지 않습니다.`);
    /* 사유를 갈라 적는다 — 「거절」과 「빈 답」은 원인이 다르다. 거절은 프롬프트·내용
     * 문제라 다시 걸어도 같고, 빈 답은 대개 일시적이다. errorType 은 집계용이라
     * API 어휘를 그대로 쓴다(refusal 은 stop_reason 값). */
    return {
      sent: false,
      reason: 'generation-failed',
      accounting,
      error: refused ? '모델이 거절했습니다' : '빈 답이 왔습니다',
      errorType: refused ? 'refusal' : 'empty_text',
      context: `${stats} → 요약 생성에서 중단`,
      attempts,
      origin: w.label,
    };
  }

  try {
  const body = toSlackMrkdwn(text);
  /* 꼬리말의 문서 부분 — **숫자만 적는다.** 이 줄은 팀 전원이 보는 자리라 파일 이름·채널 이름을
   * 넣으면 위 stats 에서 채널명을 뺀 것(WHK 지시 2026-08-05)이 무의미해진다.
   * '미반영' 은 올라왔지만 아직 변환 안 된 첨부다 — 그 자료를 두고 물으면 봇이 못 읽는다는 뜻이라
   * 읽는 사람에게 필요한 사실이다. */
  const docStats =
    (uploaded.docs.length ? ` · 문서 ${uploaded.docs.length}건 반영` : '') +
    (pendingDocs ? ` · 미반영 ${pendingDocs}건` : '');
  /* 정정 안내 — **코드가 붙인다. 모델이 쓰게 두지 않는다.** 회차마다 문구가 달라지면 안내가 아니다.
   *
   * 이 요약 스레드에 달린 `[정정]` 도 아카이브에 들어가지만(자리표시 블록), 요약 **본문**은
   * 안 들어가므로 "10/20 입니다" 같은 문장만 남으면 무엇의 값인지 알 수 없게 된다.
   * 사람이 정정을 쓰기 **직전에** 보는 자리가 여기뿐이라, 안내가 있어야 할 곳도 여기다. */
  const footer = `\n\n_${stats}${docStats}_\n_정정은 원문이 있는 채널의 그 메시지 스레드에 달아 주세요._`;

  if (dry) {
    console.log('\n' + '─'.repeat(52));
    console.log(body + footer);
    console.log('─'.repeat(52));
    console.log('\n(--dry 이므로 슬랙에 보내지 않았습니다)\n');
    return { sent: false, reason: 'dry-run', text: body, ...usageFields(accounting) };
  }

  const target = await deliveryTarget(client);
  const parts = chunkForSlack(body + footer);
  for (const part of parts) {
    await client.chat.postMessage({ channel: target, text: part, unfurl_links: false, unfurl_media: false });
  }
  console.log(`[${kind}] 전송 완료 (${parts.length}개 메시지)\n`);

  /* **여기까지 와야 「나갔다」이다.** 다음 회차의 창은 이 시각부터 시작한다 (slack-live.js 의
   * dailyWindow). 앞쪽 어디서 죽어도 이 줄에 못 오므로, 실패한 회차의 구간은 다음 회차가
   * 함께 싣는다 — 2026-09-01 17:30 회차가 죽어 그 24시간이 영영 안 실린 것이 이 줄이 없어서다.
   * 시도가 아니라 성공만 적는 것이 요점이라 `--dry` 는 위에서 이미 돌아간다. */
  if (kind === 'daily') markDailyDigestSent(w.latest);

  // 어디로 갔는지는 채널 ID 가 아니라 이름으로 남긴다 — 나중에 읽을 때 ID 는 아무것도 안 알려준다.
  let targetLabel = `${config.owner.name} DM`;
  if (toChannel) {
    targetLabel = `#${target}`;
    try {
      const info = await client.conversations.info({ channel: target });
      if (info.channel?.name) targetLabel = `#${info.channel.name}`;
    } catch {}
  }

  // 실제로 나간 것만 남긴다 (--dry 는 위에서 이미 돌아간다).
  logConversation({
    kind,
    ok: true,
    target: targetLabel,
    origin: w.label,
    answer: text,
    stats: stats + docStats,
    privateAccess: accessLabel(access),
    model,
    usage,
    costUsd,
    ...usageFields(accounting),
    /* 몇 번 만에 나갔나. 두 번 이상일 때만 로그에 줄이 붙는다(convo-log.js).
     * 통째로 안 나가기 **전에** 과부하가 잦아지는 것을 보려는 값이다. */
    attempts,
    elapsedMs: Date.now() - started,
  });

  return { sent: true, text: body };
  } catch (err) {
    err.hermesOrigin = w.label;
    err.hermesAttempts = attempts;
    err.hermesContext = `${stats} → 요약 생성 후 처리에서 중단`;
    throw attachUsage(err, accounting);
  }
}
