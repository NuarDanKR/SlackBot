/** Slack API paging and process-local identity/name caches. */
export function createSlackLiveApi({ console = globalThis.console } = {}) {
  const userMapCache = { at: 0, map: null };
  let selfIdCache = null;

  /**
   * 봇 자신의 user ID.
   *
   * 요약을 채널로 보내도록 설정하면(digest.deliverTo = 'channel') 그 채널도 수집 대상이라,
   * 어제 올린 요약이 오늘 요약의 재료로 다시 들어온다. 자기가 쓴 글은 소스에서 뺀다.
   * 채널에 남긴 Q&A 답변도 같은 이유로 제외된다.
   */
  async function getSelfId(client) {
    if (selfIdCache !== null) return selfIdCache;
    try {
      // 성공했는데 user_id 가 비어 있는 경우도 캐시하지 않는다 — 실패만 안 캐시하는 것은
      // 반쪽이고, 결과(자기 글이 안 걸러짐)는 똑같다.
      const id = (await client.auth.test()).user_id || '';
      if (id) selfIdCache = id;
      return id;
    } catch {
      // 못 알아내면 이번에는 거르지 않는다 (수집이 멈추는 것보다 낫다). 다만 **실패를 캐시하지
      // 않는다** — 예전에는 빈 값을 박아 두어 부팅 때 auth.test 가 한 번 흔들리면 그 프로세스가
      // 사는 내내(VM 은 몇 주) 봇 자기 글이 안 걸러졌다. 그러면 자기 요약이 아카이브에 들어가
      // 내일 요약의 재료가 된다 — 이 시스템이 금지하는 바로 그 모양이다. 다음 호출에 다시 묻는다.
      return '';
    }
  }

  /**
   * 스레드 답글 **전부** (부모 포함). 부르는 쪽에서 부모를 걸러 쓴다.
   *
   * `conversations.replies` 는 한 번에 200건까지라 커서를 따라가야 한다. 상한에서 끊으면
   * 답글이 200건 넘는 스레드에서 **아카이브에는 있는 답글이 슬랙에는 없는 것처럼 보인다** —
   * 수정·삭제 대조가 그것을 「지워진 것」으로 올리고 `apply_edits.py --apply` 가 실제로 지운다.
   * 같은 파일의 `conversations.history` 호출은 그전부터 커서를 따라가고 있었다 (2026-08-10).
   *
   * 페이지마다 부모가 다시 실려 오므로 ts 로 한 번 걸러 준다.
   */
  async function fetchAllReplies(client, { channel, ts }) {
    const seen = new Set();
    const out = [];
    let cursor;
    do {
      const res = await client.conversations.replies({ channel, ts, limit: 200, cursor });
      for (const m of res.messages || []) {
        if (seen.has(m.ts)) continue;
        seen.add(m.ts);
        out.push(m);
      }
      cursor = res.response_metadata?.next_cursor || undefined;
    } while (cursor);
    return out;
  }

  /**
   * user ID → 표시 이름.
   *
   * **만료를 둔다.** 예전에는 프로세스 수명 내내 캐시했는데, VM 은 몇 주씩 돈다. 그 사이
   * 새로 합류한 사람은 지도에 없어 이름 대신 `U08…` 로 떨어지고, 자동 반영이 매일 이 지도로
   * 쓰므로 **아카이브 md 에 원시 ID 가 영구히 박힌다.** 지금 아카이브에 그런 것이 0건인 건
   * 아직 안 터졌다는 뜻이지 안전하다는 뜻이 아니다.
   *
   * @param {{force?:boolean, maxAgeMs?:number}} opts force 는 자동 반영처럼 되돌릴 수 없는 자리에서 쓴다
   */
  async function getUserMap(client, { force = false, maxAgeMs = 60 * 60 * 1000 } = {}) {
    if (!force && userMapCache.map && Date.now() - userMapCache.at < maxAgeMs) return userMapCache.map;
    const map = new Map();
    let cursor;
    try {
      do {
        const res = await client.users.list({ limit: 200, cursor });
        for (const u of res.members || []) {
          map.set(u.id, u.profile?.display_name || u.profile?.real_name || u.name || u.id);
        }
        cursor = res.response_metadata?.next_cursor || undefined;
      } while (cursor);
    } catch (err) {
      // 갱신에 실패했는데 옛 지도가 있으면 그것을 쓴다 — 이름을 조금 놓치는 것이
      // ID 로 아카이브에 박히는 것보다 낫다. 아예 없으면 그대로 올린다.
      if (userMapCache.map) {
        console.warn('사용자 목록 갱신 실패 — 이전 것을 씁니다:', err.data?.error || err.message);
        return userMapCache.map;
      }
      throw err;
    }
    userMapCache.at = Date.now();
    userMapCache.map = map;
    return map;
  }

  /** 봇이 실제로 멤버인 채널 목록 */
  async function listBotChannels(client) {
    const out = [];
    let cursor;
    do {
      const res = await client.users.conversations({
        types: 'public_channel,private_channel',
        exclude_archived: true,
        limit: 200,
        cursor,
      });
      for (const c of res.channels || []) {
        out.push({ id: c.id, name: c.name, isPrivate: !!c.is_private });
      }
      cursor = res.response_metadata?.next_cursor || undefined;
    } while (cursor);
    return out.sort((a, b) => a.name.localeCompare(b.name, 'ko'));
  }

  /**
   * 워크스페이스의 채널 목록 — **봇이 아직 안 들어간 채널까지** 본다.
   *
   * `listBotChannels`(users.conversations) 와 짝이고, 다른 것은 모집단 하나다. 초대가 필요한
   * 채널을 찾으려면 "봇이 아는 채널"이 아니라 "슬랙에 있는 채널"에서 빼야 한다.
   * (비공개 채널은 봇 토큰으로 안 보인다 — 멤버인 것만 돌아온다.)
   */
  async function listSlackChannels(client) {
    const out = [];
    let cursor;
    do {
      const res = await client.conversations.list({
        types: 'public_channel,private_channel',
        exclude_archived: true,
        limit: 200,
        cursor,
      });
      for (const c of res.channels || []) {
        out.push({ id: c.id, name: c.name, isPrivate: !!c.is_private, members: c.num_members });
      }
      cursor = res.response_metadata?.next_cursor || undefined;
    } while (cursor);
    return out.sort((a, b) => a.name.localeCompare(b.name, 'ko'));
  }

  return { getSelfId, fetchAllReplies, getUserMap, listBotChannels, listSlackChannels };
}
