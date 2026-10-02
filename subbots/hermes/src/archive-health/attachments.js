/** Slack attachment collection; classification and collection windows are unchanged. */
export function createHealthAttachments({ fetchAllReplies, isBotMessage, extOf, PUBLIC_TAG, archiveChannel = (n) => n }) {
  /**
   * 채널 본문 + 스레드 답글의 첨부를 모은다. 답글로만 자료를 올리는 스레드가 흔하다.
   *
   * 각 첨부에 그 파일이 **속한 스레드에 `[공개]` 댓글이 있는지**를 `publicTag` 로 붙인다.
   * 파일이 본문에 붙었든 답글로 올라왔든 같은 스레드를 보게 하려고 양쪽에 같은 값을 준다
   * (`fetch_slack_files.py` 의 `_thread_texts` 와 같은 판정).
   *
   * **부모를 훑는 구간(`parentOldest`)과 첨부를 세는 구간(`oldest`)이 다르다.**
   * `conversations.history` 는 **부모의 작성 시각**으로 거르므로, 두 구간을 같게 두면
   * 사흘 전 스레드에 오늘 답글로 올린 자료가 아예 안 보인다 — 16:00 마감 점검이 딱 그 창(2일)이라
   * "올릴 것 다 올렸나"를 묻는 자리에서 오늘 올라온 파일을 놓쳤다 (2026-08-10).
   * 부모는 넓게 훑되 **첨부는 자기 시각으로** 걸러서 세는 개수는 그대로 둔다.
   */
  async function channelAttachments(client, ch, oldest, parentOldest = oldest) {
    const out = [];
    const collect = (m, publicTag) => {
      // 넓힌 것은 부모를 찾기 위해서다. 창 밖에 올라온 파일까지 세면 "오늘 밀린 것"이 부풀어
      // 매일 줄지 않는 숫자가 된다.
      if (Number(m.ts) < Number(oldest)) return;
      /* 봇이 올린 첨부는 세지 않는다 (WHK 결정 2026-08-12). **여기와 `doc-archive` 의
       * `fetch_slack_files.py` 는 같은 판정이어야 한다** — 한쪽만 빼면 DM 은 「미변환 N건」인데
       * 수집은 「새 첨부 0건」이 되어, 사람이 매일 없는 일을 찾게 된다
       * (2026-08-04 에 34건 vs 0건으로 실제 있었던 모양이다).
       *
       * **판정을 여기 베껴 쓰지 않는다.** 2026-08-12 까지 이 줄은 `isBotMessage` 의 내용을
       * 손으로 옮겨 적은 것이었고, 그래서 같은 JS 안에 독립 구현이 둘이었다. 지금은 부른다. */
      if (isBotMessage(m)) return;
      for (const f of m.files || []) {
        if (!f.id) continue;
        /* `hasUrl` 은 **봇이 그 파일을 받을 수 있나**다. `doc-archive` 수집은 이것이 없으면
         * 「다운로드 URL 없음 (권한 제한 파일)」로 건너뛰고 상태 파일에 아무 기록도 안 남긴다
         * (`fetch_slack_files.py` 의 `not rec["url"]` 자리). 여기서 이 값을 안 보면 그 건이
         * **매일 「미변환」으로 다시 세어져** 영영 안 줄어드는 숫자가 된다 — 사람은
         * `doc-archive` 를 돌리고 「새 첨부 0건」을 받는다. */
        /* `channel` 은 **사업장 이름이지 슬랙의 현재 이름이 아니다.** 개명된 채널은 둘이
         * 갈리고, 여기서 현재 이름을 쓰면 `nameKey` 가 저장된 기록(개명 전 이름)과 안 맞아
         * 제외·보류·물림이 전부 풀린다 — 에러 없이 「미변환 N건」만 늘어난다.
         * `fetch_slack_files.py` 가 같은 자리에서 같은 판정을 한다.
         * 사람에게 보여 줄 이름과 `privateChannels` 대조는 `slackChannel` 쪽이다. */
        out.push({
          id: f.id, name: f.name || f.id, ext: extOf(f),
          channel: archiveChannel(ch.name), slackChannel: ch.name,
          ts: m.ts, publicTag,
          hasUrl: Boolean(f.url_private_download),
        });
      }
    };

    let cursor;
    do {
      const res = await client.conversations.history({
        channel: ch.id,
        oldest: String(parentOldest),
        limit: 200,
        cursor,
      });
      for (const m of res.messages || []) {
        let replies = [];
        // 답글을 실제로 볼 이유가 있을 때만 부른다. 부모 구간을 넓혔으므로 조용한 옛 스레드까지
        // 매번 부르면 레이트리밋(Tier 3)에 걸린다. 창 안의 부모는 latest_reply 도 창 안이라
        // 이 조건으로 빠지지 않는다.
        // **이 필드에 기대는 자리가 셋이다** — 여기(첨부 후보) · slack-live.js 의
        // fetchRecentSlack · ingest/slack-archive.js 의 grownThreads. 여기만 `|| m.ts`
        // 폴백이 있고 뒤 둘은 없다. 결측은 `npm run check` [6/6] 이 센다.
        if (m.thread_ts && m.reply_count && Number(m.latest_reply || m.ts) >= Number(oldest)) {
          const r = await fetchAllReplies(client, { channel: ch.id, ts: m.thread_ts });
          replies = r.filter((x) => x.ts !== m.thread_ts);
        }
        const tagged = replies.some((x) => PUBLIC_TAG.test(x.text || ''));
        collect(m, tagged);
        for (const x of replies) collect(x, tagged);
      }
      cursor = res.response_metadata?.next_cursor || undefined;
    } while (cursor);

    return out;
  }

  return { channelAttachments };
}
