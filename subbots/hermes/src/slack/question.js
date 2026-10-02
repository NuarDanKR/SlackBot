/**
 * Slack question handling with injected model, access, presentation and logging.
 * Factory creation performs no I/O. The entry point retains event registration.
 */
export function createSlackQuestion({
  resolveAccess, accessLabel, fetchThreadContext, answerQuestion,
  toSlackMrkdwn, chunkForSlack, logConversation, permalink, usageFields, console,
  resolveChannelMentions,
}) {
  async function handleQuestion({ client, event, text }) {
    const t0 = Date.now();
    // 멘션은 <@U123> 이지만 표시이름이 붙어 <@U123|홍길동> 으로 오는 경우도 있다. 둘 다 걷어낸다.
    let question = text.replace(/<@[UW][A-Z0-9]+(\|[^>]*)?>/g, '').trim();

    // 채널이든 DM 이든 **묻는 사람의 그 메시지 밑에** 스레드로 답한다.
    // 채널은 어지럽히지 않으려는 것이고, DM 은 이어 묻기 때문이다. 2026-08-06 까지 DM 은
    // 평문으로 답했는데(1:1 이라 어지럽힐 남이 없다는 이유, 이어 묻기가 생기기 나흘 전 결정),
    // 그러면 이어 물으려고 스레드를 열 수 있는 자리가 **봇 답변 밑뿐**이라 뿌리가 봇 자기 글이 된다.
    // 봇 답변은 맥락에 안 싣기로 했으므로(slack-live.js 의 fetchThreadContext) 남는 게 없어
    // **DM 첫 후속 질문은 앞 대화가 늘 0건**이었고, 원래 질문은 스레드 밖이라 영영 안 보였다.
    // 2026-08-06 실측(qa 로그 threadContext 0건)으로 확인하고 채널과 같게 맞췄다.
    const isDM = event.channel_type === 'im';
    const replyTo = event.thread_ts || event.ts;
    const post = (body) =>
      client.chat.postMessage({
        channel: event.channel,
        ...(replyTo ? { thread_ts: replyTo } : {}),
        text: body,
        unfurl_links: false,
        unfurl_media: false,
      });

    // 대화 로그의 공통 부분. 실패한 질문도 같은 모양으로 남겨야 나중에 "왜 답을 못 했나"를 본다.
    const logBase = {
      userId: event.user,
      channelId: event.channel,
      isDM,
      threadTs: event.thread_ts || null,
      permalink: permalink(event.channel, event.ts, event.thread_ts),
    };

    if (!question) {
      await post('무엇을 찾아드릴까요? 예: `사업장나 PF 잔액`, `사업장가 연체이자 쟁점 정리해줘`');
      logConversation({ ...logBase, kind: 'empty', ok: true, elapsedMs: Date.now() - t0 });
      return;
    }

    const eyes = { channel: event.channel, timestamp: event.ts, name: 'eyes' };
    await client.reactions.add(eyes).catch(() => {});

    // catch 에서도 써야 하므로 try 밖에 둔다.
    let asker = event.user;
    let origin = null;
    let access = null;
    let accounting;

    try {
      ({ access, origin } = await resolveAccess(client, {
        channelId: event.channel,
        channelType: event.channel_type,
        userId: event.user,
      }));

      // 채널 멘션 `<#C123>` 을 이름으로 푼다. **`#` 자동완성으로 채널을 찍으면 봇에게는
      // ID 만 온다** — 그 문자열은 모델에게 아무 뜻이 없고, 도구는 채널을 **이름으로** 받는다.
      // 2026-09-21 까지는 색인에 실리던 채널 md 의 `채널 ID` 줄이 우연히 그 대응표 노릇을
      // 했는데(8월 로그에 이 유형 11건), 그 줄을 빼면서 여기로 옮겼다. 원본은
      // `.sync-state.json` 이라 개명에도 안 흔들린다.
      //
      // **권한을 탄다** — 못 풀거나 묻는 사람이 못 보는 비공개 채널이면 ID 를 그대로 둔다.
      // 이름을 알려주는 것 자체가 「막힌 자료는 이름조차 밝히지 않는다」를 깨기 때문이다.
      question = resolveChannelMentions(question, access);

      try {
        const u = await client.users.info({ user: event.user });
        asker = u.user?.profile?.display_name || u.user?.real_name || event.user;
      } catch {}

      // 스레드에서 이어 묻기. 앞 대화가 있으면 실어 "그럼 만기는?" 같은 후속 질문이 되게 한다.
      // 스레드가 아니면(채널 첫 멘션·DM 평문) 앞 대화라는 것이 없으므로 부르지 않는다.
      // access 를 함께 넘긴다 — 같은 채널 사람들이 볼 수 있는 글이라도, 거기 적힌 **다른**
      // 비공개 채널 이름은 가려야 한다 (위 resolveAccess 로 이미 정해져 있다).
      const thread = event.thread_ts
        ? await fetchThreadContext(client, {
            channel: event.channel,
            threadTs: event.thread_ts,
            skipTs: event.ts,
            access,
          })
        : { text: '', count: 0 };

      console.log(
        `\n[질문] ${asker} @ ${origin} (비공개열람 ${accessLabel(access)})` +
          (thread.count ? ` · 스레드 앞 대화 ${thread.count}건` : '') +
          `\n  "${question.slice(0, 120)}"`,
      );

      const {
        text: answer, channels, toolCalls, usage, model, costUsd, refused, cacheMiss, truncated,
        cacheBaseline, toolLimit, accounting: answerAccounting,
      } = await answerQuestion({
        question, access, origin, asker, slackClient: client, threadContext: thread.text,
      });

      accounting = answerAccounting;
      for (const part of chunkForSlack(toSlackMrkdwn(answer))) {
        await post(part);
      }

      logConversation({
        ...logBase,
        kind: 'qa',
        ok: true,
        asker,
        origin,
        privateAccess: accessLabel(access),
        question,
        // 앞 대화를 몇 건 실었는지. 이게 없으면 로그만 보고는 "이 답이 단발이었나 이어 묻기였나"를
        // 알 수 없어, 지시어가 엉뚱하게 풀린 답을 나중에 되짚을 수 없다.
        threadContext: thread.count,
        answer,
        channels,
        toolCalls,
        usage,
        model,
        costUsd,
        ...usageFields(accounting),
        refused,
        // 상한에 걸려 잘린 답. 슬랙에는 표시가 붙어 나가지만 로그에도 남겨야 나중에 "이 답이
        // 왜 짧았나"를 되짚을 수 있다 — 전에는 잘려도 `ok: true` 한 줄이라 로그만 보고는
        // 완성된 짧은 답과 구별할 수 없었다 (2026-08-28).
        truncated,
        // 자료를 찾아 읽기를 상한(10회)까지 되풀이하고 답을 못 맺은 회차. `truncated` 와 갈라
        // 적는다 — 둘을 한 통에 담으면 나중에 "왜 짧았나"를 되짚을 때 원인이 섞인다. 이 값이
        // 없던 동안에는 이 회차가 `⚠️ 실패` 도 없이 정상 답변과 값이 완전히 같았다 (2026-09-03).
        toolLimit,
        // 캐시가 안 맞은 사유. 시스템 색인은 2.7만 토큰이라 한 번 다시 쓰면 약 $0.27 이 든다 —
        // 어느 사유로 다시 쓰였는지가 안 보이면 줄일 자리를 못 고른다 (convo-log.js 가 집계한다).
        cacheMiss,
        // 대조 기준이 있었나. **false 면 첫 회차 진단이 아예 안 온 회차다** — 사유 줄이 없는
        // 것이 「맞았다」가 아니라 「못 쟀다」라는 뜻이고, 이 줄이 없으면 둘을 가릴 수 없다.
        cacheBaseline,
        elapsedMs: Date.now() - t0,
      });
    } catch (err) {
      console.error('질문 처리 실패:', err);
      logConversation({
        ...logBase,
        kind: 'qa',
        ok: false,
        asker,
        origin,
        privateAccess: accessLabel(access),
        question,
        error: (err.data?.error || err.message || '').slice(0, 500),
        ...usageFields(err.hermesAccounting || accounting),
        elapsedMs: Date.now() - t0,
      });
      await post(
        `답변 중 오류가 났습니다: \`${(err.data?.error || err.message || '').slice(0, 200)}\`\n잠시 후 다시 시도해 주세요.`,
      ).catch(() => {});
    } finally {
      await client.reactions.remove(eyes).catch(() => {});
    }
  }
  return { handleQuestion };
}
