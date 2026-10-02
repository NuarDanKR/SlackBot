/** Slack message normalization, transcript formatting and channel-name cache. */
export function createSlackLiveRender({ listAllChannels, console = globalThis.console }) {
  /**
   * 채널 ID → 이름. `.sync-state.json` 을 그대로 쓴다 (archive.js 의 listAllChannels).
   *
   * 여기서 파일을 새로 읽지 않는 이유는 이 저장소의 규칙 그대로다 — 같은 지도를 두 벌 만들면
   * 반드시 어긋난다. 만료를 두는 것은 자동 반영이 그 파일을 다시 쓰기 때문이다.
   */
  let channelNameCache = { at: 0, map: null };
  function channelNameById(id) {
    if (!channelNameCache.map || Date.now() - channelNameCache.at > 5 * 60 * 1000) {
      const map = new Map();
      try {
        for (const c of listAllChannels()) if (c.name) map.set(c.id, c.name);
      } catch {
        // 상태 파일을 못 읽어도 본문은 나가야 한다. 그때는 ID 그대로 남는다.
      }
      channelNameCache = { at: Date.now(), map };
    }
    return channelNameCache.map.get(id) || '';
  }

  /**
   * 못 푼 채널 링크가 있던 자리에 남기는 표식. 슬랙 본문에는 NUL 이 올 수 없어 안 겹친다.
   * 이 표식이 든 줄은 아래에서 통째로 빠진다.
   */
  const UNRESOLVED_CHANNEL = '\u0000#?\u0000';

  /**
   * 표식이 든 줄을 통째로 뺀다.
   *
   * **로그에 ID 도 채널 이름도 안 찍는다** — 못 푼 ID 는 봇이 못 들어간 비공개 채널을
   * 가리킬 때 생기고, ID 자체가 「그런 자리가 있다」는 사실을 드러낸다. 개수만 남긴다.
   * 지금 그런 ID 는 실측 0건이라(2026-08-27 조사), 이 로그가 찍히는 것 자체가 신호다.
   */
  function dropUnresolvedLines(text) {
    if (!text.includes(UNRESOLVED_CHANNEL)) return text;
    const lines = text.split('\n');
    const kept = lines.filter((l) => !l.includes(UNRESOLVED_CHANNEL));
    console.warn(`못 푼 채널 링크가 든 줄 ${lines.length - kept.length}건을 뺐습니다`);
    return kept.join('\n').trim();
  }

  /**
   * 아카이브 md 와 같은 표기로 정리: 멘션·링크 풀기.
   *
   * `ingest/slack-archive.js` 도 이걸 쓴다 — 아카이브에 **쓰는** 쪽과 실시간으로 **읽는** 쪽이
   * 같은 규칙을 써야 한다. 복제하면 아래 해제 순서(멘션·링크 먼저, `&amp;` 마지막) 같은
   * 규칙이 두 벌이 되어 반드시 어긋난다.
   */
  function renderText(raw, userMap) {
    const out = String(raw || '')
      .replace(/<@([UW][A-Z0-9]+)(\|[^>]*)?>/g, (_, id) => `@${userMap.get(id) || id}`)
      // 채널 링크는 `<#C123|이름>` 과 **`<#C123>`** 두 모양으로 온다. 예전에는 앞의 것만 풀어서
      // 뒤의 것이 ID 째로 아카이브·요약에 남았다 (hermes-log 에 4건). 이름이 안 드러나니 그 자체로
      // 새는 것은 아니지만, `redactPrivateMentions` 는 **이름으로** 줄을 지우므로 비공개 채널을
      // 가리킨 줄이 안 지워진 채 공개 답변 프롬프트로 간다. 그래서 여기서 이름으로 풀어 둔다.
      //
      // **끝내 못 풀면 그 줄을 통째로 뺀다** (WHK 2026-08-29). 못 푸는 것은 `.sync-state.json`
      // 에 없다는 뜻이고, 그건 봇이 초대되지 않은 비공개 채널이라는 뜻이다 — 「막힌 자료는
      // 이름조차 밝히지 않는다」는 규칙을 여기에도 적용한다. 대가는 **안 풀리는 공개 채널이
      // 나오면 그 줄의 공개 내용도 함께 잃는 것**인데, 그런 ID 는 2026-08-27 실측 0건이다.
      // 조용히 잃지 않게 `dropUnresolvedLines` 가 개수를 로그로 남긴다.
      .replace(/<#(C[A-Z0-9]+)(?:\|([^>]*))?>/g, (_, id, name) => {
        const resolved = name || channelNameById(id);
        return resolved ? `#${resolved}` : UNRESOLVED_CHANNEL;
      })
      .replace(/<(https?:\/\/[^|>]+)\|([^>]+)>/g, (_, url, label) => `[${label}](${url})`)
      .replace(/<(https?:\/\/[^|>]+)>/g, (_, url) => url)
      // 슬랙은 본문의 & < > 를 엔티티로 저장한다. 안 풀면 'D&D' 가 'D&amp;D' 로 요약에 실린다.
      // 멘션·링크는 진짜 꺾쇠라 위에서 먼저 처리해야 하고(안 그러면 &lt; 가 가짜 링크로 잡힌다),
      // &amp; 는 맨 마지막에 푼다 — 먼저 풀면 '&amp;lt;'(사용자가 친 &lt;)가 '<' 로 이중 해제된다.
      .replace(/&lt;/g, '<')
      .replace(/&gt;/g, '>')
      .replace(/&amp;/g, '&')
      .trim();
    return dropUnresolvedLines(out);
  }

  /** Slack ts → 'YYYY-MM-DD HH:MM' (아카이브 메시지 헤더에 쓰는 모양) */
  function stamp(ts, tz) {
    const d = new Date(Number(ts) * 1000);
    const f = new Intl.DateTimeFormat('sv-SE', {
      timeZone: tz, year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', hour12: false,
    });
    // 로케일에 따라 좁은 공백(NBSP 등)이 섞여 나올 수 있어 일반 공백으로 통일한다.
    return f.format(d).replace(/\s+/g, ' ').trim().slice(0, 16); // 'YYYY-MM-DD HH:MM'
  }

  function normalizeMessage(m, userMap, tz) {
    const author = m.user ? userMap.get(m.user) || m.user : m.username || m.bot_profile?.name || '봇';
    const files = (m.files || []).map((f) => f.name).filter(Boolean);
    return {
      ts: m.ts,
      when: stamp(m.ts, tz),
      author: m.bot_id && !m.user ? `${author} (봇)` : author,
      text: renderText(m.text, userMap),
      files,
      replyCount: m.reply_count || 0,
      replies: [],
    };
  }

  /** 수집 결과를 아카이브 md 와 같은 모양의 텍스트로 */
  function formatTranscript(entries) {
    const out = [];
    for (const e of entries) {
      if (e.error) {
        out.push(`## #${e.channel}\n(읽기 실패: ${e.error})\n`);
        continue;
      }
      out.push(`## #${e.channel}${e.isPrivate ? ' 🔒' : ''}  — ${e.messages.length}건\n`);
      for (const m of e.messages) {
        const tag = m.parentOutsideWindow ? ' _(기간 밖 원글 — 이번 기간에 답글만 달림)_' : '';
        out.push(`**${m.when} · ${m.author}**${tag}`);
        if (m.text) out.push(m.text);
        if (m.files.length) out.push(`📎 첨부: ${m.files.map((f) => `\`${f}\``).join(', ')}`);
        for (const r of m.replies) {
          out.push(`> └ **${r.when} · ${r.author}** — ${r.text || ''}${r.files.length ? ` 📎 ${r.files.join(', ')}` : ''}`);
        }
        out.push('');
      }
    }
    return out.join('\n');
  }

  // `channelNameById` 를 함께 내보낸다 — 질문 경로(`slack/question.js`)도 채널 멘션을
  // 풀어야 하는데, 같은 지도를 두 벌 만들면 반드시 어긋난다(이 파일 머리의 규칙 그대로).
  return { renderText, stamp, normalizeMessage, formatTranscript, channelNameById };
}
