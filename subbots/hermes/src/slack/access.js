/**
 * Slack access decisions with injected policy and the entry point's cache.
 * Factory creation performs no I/O; callers supply the Slack client per request.
 */
export function createSlackAccess({
  config, normalizeChannel, canonicalChannel, accessFor, PUBLIC_ACCESS, memberCache, console,
}) {
  /** @returns {Promise<Map<string, Set<string>>>} 비공개 채널명 → 멤버 userId 집합 */
  async function privateChannelMembers(client) {
    if (Date.now() - memberCache.at < 10 * 60 * 1000) return memberCache.byChannel;
    const byChannel = new Map();
    // config 에 옛 이름이 적혀 있어도 맞아야 한다 — 슬랙은 현재 이름을 준다.
    // 채널 루프 밖에서 한 번만 만든다 — canonicalChannel 은 부를 때마다 지도를 뜬다(statSync).
    const declared = config.privateChannels.map((c) => canonicalChannel(c));
    try {
      let cursor;
      do {
        const res = await client.conversations.list({
          types: 'private_channel', exclude_archived: true, limit: 200, cursor,
        });
        for (const c of res.channels || []) {
          if (!declared.includes(canonicalChannel(c.name))) continue;
          const ids = new Set();
          let mc;
          do {
            const m = await client.conversations.members({ channel: c.id, limit: 200, cursor: mc });
            (m.members || []).forEach((u) => ids.add(u));
            mc = m.response_metadata?.next_cursor || undefined;
          } while (mc);
          byChannel.set(normalizeChannel(c.name), ids);
        }
        cursor = res.response_metadata?.next_cursor || undefined;
      } while (cursor);
    } catch (e) {
      // 조회가 실패하면 빈 지도를 캐시하지 않는다 — 캐시해 두면 10분간 아무도 자기 채널을
      // 못 읽고, 그 사실이 답변에는 "자료가 없습니다"로만 보인다.
      console.warn('비공개 채널 멤버 조회 실패 — 이번 질문은 비공개를 전부 막습니다:', e.data?.error || e.message);
      return new Map();
    }
    memberCache.at = Date.now();
    memberCache.byChannel = byChannel;
    return byChannel;
  }

  async function resolveAccess(client, { channelId, channelType, userId }) {
    if (channelType === 'im') {
      const byChannel = await privateChannelMembers(client);
      const mine = [...byChannel.entries()].filter(([, ids]) => ids.has(userId)).map(([name]) => name);
      return { access: accessFor(mine), origin: 'DM (1:1 대화)' };
    }
    try {
      const info = await client.conversations.info({ channel: channelId });
      const name = normalizeChannel(info.channel?.name);
      // 비공개 채널에서 온 질문은 **그 채널 하나만** 연다.
      const declared = config.privateChannels.map((c) => canonicalChannel(c));
      const access = declared.includes(canonicalChannel(name)) ? accessFor([name]) : PUBLIC_ACCESS;
      return {
        access,
        origin: `#${name} (${info.channel?.is_private ? '비공개' : '공개'} 채널)`,
      };
    } catch {
      return { access: PUBLIC_ACCESS, origin: '알 수 없는 채널' };
    }
  }
  return { privateChannelMembers, resolveAccess };
}
