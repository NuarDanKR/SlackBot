/**
 * Slack 실시간 조회 — 요약(digest)의 소스이자, 아카이브 마지막 sync 이후의 공백을 메우는 용도.
 *
 * 봇 토큰은 "봇이 초대된 채널"만 볼 수 있다. scripts/check-setup.js 가 미초대 채널을 알려준다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { createSlackLiveApi } from './slack/live-api.js';
import { createSlackLiveRender } from './slack/live-render.js';
import { createSlackLiveWindow } from './slack/live-window.js';
import {
  config, ROOT, FULL_ACCESS, canSeePrivateChannel, isPrivateChannel, canSee,
  DIGEST_STATE_FILE as CONFIGURED_DIGEST_STATE_FILE,
  canonicalChannel, currentChannelNames,
  redactPrivateMentions, BOT_ANSWER_MARK,
} from './config.js';
import { listAllChannels } from './archive.js';

/**
 * `config.limits.liveFetchMaxDays` 가 없을 때의 기본값. **정본은 여기 하나뿐이다**
 * (2026-09-03) — 전에는 `archive-health.js` 만 `config.limits?.liveFetchMaxDays ?? 14` 로
 * 기본값을 두고, 여기(`recentWindow`)와 `claude.js`(도구 설명 문구)는 기본값 없이
 * `config.limits.liveFetchMaxDays` 를 그대로 읽었다. config.json 에 그 키가 없으면
 * 여기는 `Math.min(…, undefined)` = `NaN` 이 되고 `claude.js` 는 도구 설명이
 * "1~undefined" 로 나갔다 — 지금 config.json 에는 값(14)이 있어 우연히 안 갈렸을 뿐이다.
 */
export const DEFAULT_LIVE_FETCH_MAX_DAYS = 14;

// 본문에 넣지 않는 메시지 종류. 참여·이름변경 로그는 아카이브에서 하단 「참여 기록」 자리라
// 메시지 본문에 섞이면 안 된다. ingest 쪽도 같은 목록을 써야 해서 export 한다.
export const SKIP_SUBTYPES = new Set([
  'channel_join', 'channel_leave', 'channel_name', 'channel_purpose',
  'channel_topic', 'channel_archive', 'channel_unarchive', 'bot_add', 'bot_remove',
]);

/**
 * 스레드 답글을 **「채널에도 게시」**한 것 (슬랙의 broadcast).
 *
 * 이 한 건이 `conversations.history` 에 한 번, 부모의 `conversations.replies` 에 또 한 번
 * 온다. 그래서 그냥 두면 같은 발언이 아카이브 md 의 **본문 블록**과 **스레드 답글 줄**
 * 양쪽에 들어가고, 요약 재료(`formatTranscript`)에도 두 번 실린다 — 에러가 안 나고,
 * 모델은 그것을 「같은 말이 두 번 나왔다」로 읽는다.
 * (2026-09-03 실측: 아카이브에 7건 / 4채널. 어느 채널 md 에는 사람이 손으로 「스레드 안에만
 * 두고 별도 블록으로 중복해 넣지 않았습니다」라고 적어 둔 ⚠ 줄이 있다 — 여기서 코드로
 * 정하는 것이 그 관례이고, 안 정하면 백필이 그 블록을 넣어 그 줄이 거짓말이 된다.)
 *
 * **`SKIP_SUBTYPES` 에 넣지 않는다.** 그 목록은 다섯 자리가 함께 보는데, 거기 넣으면
 * ① 요약의 답글 필터와 ② 스레드 맥락(`fetchThreadContext`)에서도 빠져 **어디에도 안 남고**,
 * ③ DM 핸들러(`index.js` 의 `DM_SKIP_SUBTYPES`)는 그렇게 올라온 질문을 **응답도 에러도 없이**
 * 버린다 (2026-09-03 대조 실측: 요약에 실리는 자리가 2 → 0, 스레드 맥락 실림 → 안 실림,
 * DM 답한다 → 버린다). 중복을 지워야 하는 곳은 **최상위로 모으는 두 자리뿐**이라
 * 따로 판정한다 — `keepMessage`(아카이브)와 `fetchOneChannel`(요약).
 *
 * `history` 결과에서 `thread_ts` 가 자기 `ts` 와 다르면 그것은 broadcast 뿐이다 — 보통
 * 스레드 답글은 `history` 에 아예 안 온다. `subtype` 이 안 붙어 오는 경우까지 함께 잡으려고
 * 두 조건을 다 본다 (`convo-log.js` 의 `permalink` 도 같은 기준으로 스레드를 가른다).
 */
export function isThreadBroadcast(m) {
  if (!m) return false;
  return m.subtype === 'thread_broadcast' || Boolean(m.thread_ts && m.thread_ts !== m.ts);
}

/**
 * 이 메시지를 아카이브 md 에 넣나.
 *
 * **판정은 여기 한 자리에만 둔다.** 자동 반영(`ingest/slack-archive.js`)과
 * 백필(`ingest/backfill.js`)이 둘 다 이것을 부른다. 베껴 쓰면 갈리는데 **갈려도
 * 에러가 안 난다** — 한쪽은 넣고 한쪽은 안 넣을 뿐이다. 갈렸는지는
 * `scripts/check-backfill.js` 의 절 ① 이 본다.
 *
 * **둘 다 `conversations.history` 결과에만 이 함수를 쓴다.** 아래 broadcast 판정이
 * 그 전제 위에 서 있다 — 답글 목록에 대고 부르면 스레드 답글이 통째로 빠진다.
 */
export function keepMessage(m, selfId) {
  if (m.subtype && SKIP_SUBTYPES.has(m.subtype)) return false; // 참여·이름변경은 본문에 안 넣는다
  if (isThreadBroadcast(m)) return false;                      // 부모의 스레드 답글 줄로 들어간다
  if (isBotMessage(m, selfId)) return false;                   // 봇이 쓴 것은 전부 (Hermes 포함)
  return Boolean(m.text || (m.files || []).length);
}

/**
 * 정정 댓글 — 이미 올라간 내용을 고치려고 스레드에 다는 답글.
 *
 * **말머리로만** 판정한다. 본문 어디서든 찾으면 "정정 필요한가요?" 같은 문장이 걸린다.
 * 문서 쪽 `[공개]` 태그와 같은 모양이다 (doc-archive/scripts/fetch_slack_files.py).
 *
 * 아카이브에는 원문 그대로 들어가고(봇이 원문과 정정을 같은 블록에서 본다),
 * 일일·주간 요약에서만 빠진다 — 사업 진행이 아니라 기록을 고치는 행위이므로.
 */
export const CORRECTION_RE = /^\s*\[정정\]/;

/**
 * 이 메시지를 **봇이 썼나** — 아카이브·인용·요약에서 빼는 기준 하나.
 *
 * 2026-08-12 까지는 Hermes 자신만 뺐고 다른 봇(`주간 체크인`·`항목 상태 알림` 등)은 일부러
 * 넣었다. 그것이 원문이라는 이유였는데, WHK 결정으로 **봇 전반**을 빼는 것으로 바꿨다.
 * 판정이 여러 곳에 흩어져 있으면 한 곳만 고쳐도 나머지가 조용히 옛 기준으로 남는다
 * (전에 `slack-archive.js` 는 빼는데 `fetch_slack_files.py` 는 안 빼는 상태였다).
 *
 * **구현은 언어마다 하나씩 둘이다 — 고칠 때는 반드시 함께 고친다.**
 *
 *   JS     : 이 함수. 아카이브·인용·요약·위생 점검·DM 핸들러가 전부 이걸 부른다
 *   파이썬 : `doc-archive/scripts/fetch_slack_files.py` 의 `is_bot_message`.
 *            첨부 수집과 승인 반영이 그걸 부른다 (`selfId` 는 안 본다 — 봇 토큰으로
 *            올린 글에는 `bot_id` 가 늘 붙어서 실질 차이가 없다)
 *
 * 2026-08-12 까지 이 자리는 「전부 이 함수 하나를 부른다」고 적고 있었지만 사실이 아니었다 —
 * `archive-health.js` 는 같은 JS 안에서 내용을 베껴 쓰고 있었고(그래서 `selfId` 를 안 봤다),
 * `index.js` 의 DM 핸들러는 `subtype || bot_id` 라는 **다른 규칙**이라 첨부가 딸린 DM 질문을
 * 통째로 버렸으며, `apply_approvals.py` 에는 판정 자체가 없었다. 셋 다 이때 여기로 모았다.
 *
 * `bot_id` 가 봇 판정의 본줄기다 — 봇 계정으로 올린 글에는 예외 없이 붙는다.
 * `selfId` 는 보강이다: Hermes 는 봇 토큰으로 올리므로 `bot_id` 도 함께 붙지만,
 * 자기 글만은 한 겹 더 확실히 걸러야 하는 자리라 남겨 둔다.
 *
 * **`app_id` 는 앱이 사람 이름으로 올린 글을 잡는다** (WHK 결정 2026-08-26). 사람이 앱에게
 * 시켜 보내면 슬랙은 그것을 **그 사람의 메시지로** 표시한다 — `bot_id` 도 `bot_message` 도
 * 없고 `app_id` 만 붙는다. 그래서 `bot_id` 만 보던 동안은 AI 가 만든 요약이 **사람 원문으로**
 * 아카이브에 들어갈 수 있었다. 2026-08-26 #사업장마 에서 29분 사이에 올라온 두 건이
 * 그 예다 — 같은 사람 이름인데 사업장 둘의 PF만기가 한쪽은 9/2, 다른 쪽은 8/31 이었다.
 * 봇 글을 빼는 이유("2차 가공물이 원본 근거로 굳는다")와 같은 모양이라 함께 뺀다.
 *
 * 사람이 손으로 친 글을 잘못 버릴 위험은 실측으로 봤다 — 워크스페이스 전 기간 2,763건 중
 * `app_id` 가 있고 `bot_id` 가 없는 글은 그 2건뿐이었고 둘 다 앱이 보낸 것이었다.
 */
export function isBotMessage(m, selfId) {
  if (!m) return false;
  return Boolean(m.bot_id) || m.subtype === 'bot_message' || Boolean(m.app_id)
    || Boolean(selfId && m.user === selfId);
}

/**
 * 봇이 올린 글에 달린 사람 답글을 담는 **자리표시 블록**.
 *
 * 봇이 올린 글은 아카이브에 블록이 없다 — 봇의 글은 2차 가공물이거나 알림이라 넣지 않기
 * 때문이다. 그래서 거기 달린 사람 답글은 붙을 자리가 없었다.
 *
 * 자리만 만들고 **내용은 안 넣는다.** 헤더(시각·작성자)와 아래 표식 한 줄만 두고, 그 안에
 * 사람이 쓴 답글을 담는다. 봇이 쓴 문장은 한 글자도 아카이브에 들어가지 않으므로
 * "봇이 자기 요약을 원본 근거로 삼는" 일이 여전히 불가능하다.
 * `> 💬 스레드 N건 (봇 답변 — 미수록)` 과 같은 관례다.
 *
 * 작성자 이름은 **슬랙 표시 이름이 아니라 이 상수**를 쓴다. 표시 이름은 바뀌는데
 * `ingest/verify.js` 의 관문은 이 문자열로 블록을 알아보므로, 어긋나면 관문이 자리표시 블록을
 * "봇 글이 아카이브에 들어갔다"로 읽고 매일 자동 반영을 되돌린다. 어느 봇이었는지는
 * 표식 옆의 `[원문]` 링크로 열어 본다.
 */
export const BOT_AUTHOR_LABEL = '봇';
export const BOT_BLOCK_BODY = '(봇 발신 — 본문 미수록)';

/**
 * 정본은 `config.js` 로 옮겼다(2026-09-11) — archive.js 의 `isEchoEntry` 가 이 상수를
 * 써야 하는데 archive.js → slack-live.js 는 이미 반대 방향 import(`listAllChannels`)가
 * 있어 순환이 된다. 위 상단 import 로 로컬 바인딩을 가져오고, 여기서는 기존 import 처
 * (`ingest/slack-archive.js` 등)가 안 깨지게 재수출만 한다. **재수출 구문(`export {…}
 * from`)만 쓰면 이 파일 자신의 스코프에는 이름이 안 생겨** 아래 `fetchThreadContext` 안의
 * 맨 식별자 세 곳(628·659·660행)이 `ReferenceError` 로 죽는다 — 스레드 안에 봇 메시지가
 * 있으면 거의 매번 닿는 자리라 실시간 조회가 광범위하게 크래시했다(2026-09-11 리뷰 발견,
 * 재현: 가짜 슬랙 클라이언트로 `fetchThreadContext` 를 불러 확인). 자세한 사정은
 * `config.js` 의 `BOT_ANSWER_MARK` 주석 참조.
 */
export { BOT_ANSWER_MARK };

/**
 * 「봇이 아예 다루지 않는 채널」을 채널 목록에서 뺀다. 순수 함수다.
 *
 * `archive.js` 의 `withoutSkipped`(읽기 쪽)와 같은 판정인데, 저쪽은 **이름 목록**을 받고
 * 여기는 슬랙이 준 **채널 객체**(`{id, name}`)를 받는다 — 쓰기 두 자리(`fetchWindow` 와
 * `ingest/slack-archive.js` 의 `ingestConversations`)가 이걸 쓴다.
 *
 * **개명도 되짚는다** (2026-09-16). 여기 오는 이름은 슬랙의 **현재** 이름인데 설정에는
 * 사람이 어느 철자로든 적는다. 글자 그대로 대면 개명 순간 skip 줄이 아무것도 안 막아,
 * 안 다루기로 한 **공개** 채널이 아카이브에 다시 쌓이고 일일 요약에 실린다 —
 * 2026-08-10 에 21일간 실제로 벌어진 모양이다. 양쪽을 `canonicalChannel` 로 되짚어 대면
 * 어느 철자를 적어도 산다. 지도는 **한 번만 떠서** 넘긴다 — 이름마다 다시 뜨면
 * `fs.statSync` 비용이 채널 수만큼 쌓인다 (`config.js` 의 `*With` 주석).
 *
 * `map` 을 인자로 받는 이유는 시험이다 — 실물 `.sync-state.json` 없이 가짜 지도로 잰다
 * (`scripts/check-skip-channels.js`).
 */
export function dropSkippedChannels(channels, skip = config.digest?.skipChannels || [], map = currentChannelNames()) {
  const skipSet = new Set([...(skip || [])].map((c) => canonicalChannel(c, map)));
  return (channels || []).filter((ch) => !skipSet.has(canonicalChannel(ch.name, map)));
}

/** `<@U123>` · `<@U123|이름>` 둘 다. 멘션이 본문 어디에 있든 잡는다. */
function mentionsUser(text, userId) {
  if (!userId) return false;
  return new RegExp(`<@${userId}(\\||>)`).test(String(text || ''));
}

const { getSelfId, fetchAllReplies, getUserMap, listBotChannels, listSlackChannels } = createSlackLiveApi();
export { getSelfId, fetchAllReplies, getUserMap, listBotChannels, listSlackChannels };

const { renderText, stamp, normalizeMessage, formatTranscript, channelNameById } =
  createSlackLiveRender({ listAllChannels });
export { renderText, stamp, normalizeMessage, formatTranscript };

/**
 * 질문에 든 채널 멘션 `<#C123>`·`<#C123|이름>` 을 `#이름` 으로 푼다.
 *
 * **왜 질문 경로에 필요한가** — `#` 자동완성으로 채널을 찍으면 봇에게는 ID 만 온다.
 * 모델에게 그 문자열은 아무 뜻이 없고 도구는 채널을 **이름으로** 받는다. 2026-09-21 까지는
 * 색인에 실리던 채널 md 의 `채널 ID` 줄이 우연히 대응표 노릇을 했는데, 그 줄을 빼면서
 * 원본(`.sync-state.json`)을 직접 보게 옮겼다.
 *
 * **못 풀면 ID 를 그대로 둔다** — 여기서는 `renderText` 처럼 줄을 버리지 않는다. 버리면
 * 사람이 던진 질문이 통째로 사라지고, 왜 못 알아들었는지 화면에 아무것도 안 남는다.
 *
 * **권한을 탄다** — 묻는 사람이 못 보는 채널이면 ID 로 되돌린다. 이름을 알려주는 것 자체가
 * 「막힌 자료는 이름조차 밝히지 않는다」(WHK 2026-08-29)를 깨기 때문이다. 판정은 `canSee`
 * 하나로 한다 — 비공개 여부를 확정 못 하면 전체 권한이 아닌 접근에는 닫는다(fail-closed).
 */
export function resolveChannelMentions(text, access) {
  return String(text || '').replace(/<#(C[A-Z0-9]+)(?:\|([^>]*))?>/g, (whole, id, name) => {
    const resolved = name || channelNameById(id);
    if (!resolved) return whole;
    return canSee(access, resolved) ? `#${resolved}` : whole;
  });
}

/**
 * 기간 안의 메시지 + 스레드 답글을 채널별로 수집한다.
 *
 * `forDigest` 는 **요약(일일·주간)에서만** 켠다. Q&A 의 `fetch_recent_slack` 은 끈 채로 써야
 * 한다 — 봇은 답할 때 정정을 **봐야** 하고, 요약에는 **안 실려야** 한다. 정반대라서 한쪽에
 * 맞추면 다른 쪽이 깨진다.
 *
 * @param {object} client Slack WebClient
 * @param {{oldest:number, latest:number, channels?:Array<{id:string,name:string}>, access?:object, forDigest?:boolean}} opts
 *   access 는 config.js 의 열람 권한. 생략하면 전부(FULL_ACCESS) — 사람이 하나뿐인 자리에서만 생략할 것.
 */
export async function fetchWindow(client, {
  oldest, latest, channels, access = FULL_ACCESS, concurrency = 4, forDigest = false,
  threadLookbackDays = config.limits.threadLookbackDays ?? 30,
}) {
  const tz = config.timezone;
  const userMap = await getUserMap(client);
  const selfId = await getSelfId(client);
  let targets = channels || (await listBotChannels(client));
  // skipChannels 는 개명을 되짚어 거른다 — 규칙과 이유는 dropSkippedChannels 주석에.
  targets = dropSkippedChannels(targets);
  // 비공개 채널은 권한에 적힌 것만 남긴다. 비공개 여부는 **슬랙이 알려준 플래그를 먼저** 본다 —
  // config 의 privateChannels 에 안 적힌 비공개 채널이 있을 수 있고(CLAUDE.md 의 어긋남),
  // 그때 config 만 믿으면 비공개 대화가 공개 요약에 실린다. 그런 채널은 이름이 목록에 없으니
  // canSeePrivateChannel 이 false 를 주어 막힌다 (fail-closed).
  targets = targets.filter((ch) => {
    const priv = ch.isPrivate ?? isPrivateChannel(ch.name);
    return !priv || canSeePrivateChannel(access, ch.name);
  });

  const results = [];
  const opts = { oldest, latest, userMap, tz, selfId, forDigest, lookback: threadLookbackDays * 86400 };

  // 채널 45개를 순차로 돌면 슬랙 응답이 너무 느려진다. 소규모 동시 실행으로 처리하되,
  // Slack Tier 3 레이트리밋(약 50req/분)을 넘지 않도록 동시 실행 수를 낮게 잡는다.
  // 429 는 WebClient 가 자동 재시도한다.
  const queue = [...targets];
  const workers = Array.from({ length: Math.min(concurrency, queue.length) }, async () => {
    while (queue.length) {
      const ch = queue.shift();
      if (!ch) break;
      results.push(await fetchOneChannel(client, ch, opts));
    }
  });
  await Promise.all(workers);

  results.sort((a, b) => a.channel.localeCompare(b.channel, 'ko'));
  return results.filter((r) => r.messages.length || r.error);
}

async function fetchOneChannel(client, ch, { oldest, latest, userMap, tz, selfId, lookback, forDigest }) {
  // 슬랙 함정: conversations.history 는 "부모 메시지의 작성 시각" 으로 거른다.
  // 그래서 오늘 지난주 스레드에 답글이 달려도, 그 부모가 기간 밖이면 아예 안 잡힌다.
  // 부모는 더 넓게(lookback) 훑되, 결과에 넣을지는 아래에서 다시 판단한다.
  const raw = [];
  // 「채널에도 게시」한 스레드 답글. 부모의 스레드 줄로 한 번만 싣고, 부모가 이 결과에
  // 아예 없을 때만 아래에서 되살린다 (isThreadBroadcast 머리말).
  const broadcasts = [];
  let cursor;
  try {
    do {
      const res = await client.conversations.history({
        channel: ch.id,
        oldest: String(oldest - lookback),
        latest: String(latest),
        inclusive: true,
        limit: 200,
        cursor,
      });
      for (const m of res.messages || []) {
        if (m.subtype && SKIP_SUBTYPES.has(m.subtype)) continue;
        if (isBotMessage(m, selfId)) continue; // 봇이 올린 것은 재료로 쓰지 않는다 (자기 요약·알림 모두)
        // 봇을 부른 질문도 요약 재료가 아니다. 봇 답변만 빼면 "누가 어느 사업장 LTV 를 물었다" 가
        // 남아 요약의 「답변 대기」에 실린다 — 사업 진행이 아니라 봇을 쓴 기록이다.
        if (forDigest && mentionsUser(m.text, selfId)) continue;
        if (!m.text && !(m.files || []).length) continue;
        if (isThreadBroadcast(m)) { broadcasts.push(m); continue; }
        raw.push(m);
      }
      cursor = res.response_metadata?.next_cursor || undefined;
    } while (cursor);
  } catch (err) {
    // not_in_channel 은 초대가 안 된 것. check-setup.js 가 미초대 채널을 알려준다.
    return { channel: ch.name, id: ch.id, error: err.data?.error || err.message, messages: [] };
  }

  const inWindow = (ts) => Number(ts) >= oldest && Number(ts) <= latest;
  const messages = [];

  for (const m of raw) {
    const parentInWindow = inWindow(m.ts);
    // latest_reply 는 부모에 붙어 오는 "가장 최근 답글 시각". 이 값으로
    // '기간 밖 부모 + 기간 안 답글' 스레드를 정확히 골라낸다.
    // **폴백이 없다** — latest_reply 가 없으면 이 스레드는 통째로 빠진다.
    // 같은 필드를 archive-health.js 의 channelAttachments 와
    // ingest/slack-archive.js 의 grownThreads 도 쓴다. 결측은 `npm run check` [6/6].
    const threadTouched = m.reply_count > 0 && m.latest_reply && inWindow(m.latest_reply);
    if (!parentInWindow && !threadTouched) continue;

    const norm = normalizeMessage(m, userMap, tz);
    norm.parentOutsideWindow = !parentInWindow;

    let sawCorrection = false;
    if (m.reply_count > 0) {
      try {
        const all = await fetchAllReplies(client, { channel: ch.id, ts: m.ts });
        let replies = all.filter(
          (r) => r.ts !== m.ts && !(r.subtype && SKIP_SUBTYPES.has(r.subtype)) && !isBotMessage(r, selfId),
        );
        // 부모가 기간 밖이면 이번 기간에 달린 답글만 (오래된 답글까지 끌고 오지 않게).
        // 부모가 기간 안이면 스레드 전체를 맥락으로 넣는다.
        if (!parentInWindow) replies = replies.filter((r) => inWindow(r.ts));
        if (forDigest) {
          sawCorrection = replies.some((r) => CORRECTION_RE.test(r.text));
          replies = replies.filter((r) => !CORRECTION_RE.test(r.text) && !mentionsUser(r.text, selfId));
        }
        norm.replies = replies.map((r) => normalizeMessage(r, userMap, tz));
      } catch {
        norm.replies = [];
      }
      // 정정이 달린 옛 스레드는 통째로 뺀다. 정정 줄만 빼면 「넵 수정하겠습니다」 같은 확인
      // 답글 하나 때문에 며칠 전 원글이 오늘 요약에 다시 실린다 — 정정하려고 스레드를 건드린
      // 것이 그 사업장을 오늘 진행된 일로 만들어 버린다.
      if (forDigest && !parentInWindow && sawCorrection) continue;
      if (!parentInWindow && norm.replies.length === 0) continue;
    }

    messages.push(norm);
  }

  /* 부모가 이 결과에 **아예 없는** broadcast 만 되살린다.
   *
   * 부모가 봇 글이거나(위 `isBotMessage`), 봇을 부른 글이거나, lookback(기본 30일)보다
   * 오래되면 부모가 통째로 빠진다. 그러면 「채널에도 게시」한 그 발언이 요약 어디에도
   * 안 남는데 **에러도 로그도 없다.** 두 번 싣는 것보다 조용히 잃는 것이 나쁘다.
   * 부모가 있으면 그 스레드 줄로 이미 실렸으므로 여기서는 건너뛴다. */
  if (broadcasts.length) {
    const parents = new Set(messages.map((m) => m.ts));
    for (const b of broadcasts) {
      if (parents.has(b.thread_ts) || !inWindow(b.ts)) continue;
      // 답글 쪽과 **같은 기준**으로 거른다 (위 replies 필터) — 여기만 느슨하면 정정과
      // 봇 호출이 되살아나는 길이 생긴다.
      if (forDigest && (CORRECTION_RE.test(b.text) || mentionsUser(b.text, selfId))) continue;
      messages.push(normalizeMessage(b, userMap, tz));
    }
  }

  messages.sort((a, b) => Number(b.ts) - Number(a.ts));
  return { channel: ch.name, id: ch.id, isPrivate: ch.isPrivate ?? isPrivateChannel(ch.name), messages };
}

/* ── 스레드 맥락 (이어 묻기) ─────────────────────────────────────── */

/* 봇 답변이 있었다는 사실만 남기는 자리표시는 위 `BOT_ANSWER_MARK` 를 쓴다 —
 * 아카이브 md 의 표기와 같은 문구여야 해서 상수 한 곳에 모아 두었다. */

/**
 * 지금 질문이 달린 스레드의 **앞 대화**를 맥락으로 실을 텍스트.
 *
 * 채널에서는 항상 스레드로 답하는데(index.js) 정작 매 질문이 앞뒤 없는 단발이었다.
 * "그럼 만기는?" 같은 후속 질문이 안 됐다.
 *
 * **봇 자신의 답변은 싣지 않는다** (WHK 결정 2026-08-06). 아카이브에 봇 답변을 넣지 않는
 * 것과 같은 규칙이다 — 답변은 아카이브를 읽어 만든 2차 가공물이라, 맥락으로 들어가면 한 번
 * 잘못 요약한 숫자가 다음 답변에 그대로 이어진다. 사람의 앞 질문은 남으므로
 * "사업장나 PF 잔액?" → "그럼 만기는?" 은 그대로 되고, 답변에서만 나온 이름("거기",
 * "그 사업장")은 봇이 도구로 다시 찾는다 — 느린 대신 다시 확인한 값이 나온다.
 *
 * 봇이 낀 적 없는 스레드(사람들끼리의 업무 논의)도 그대로 실린다. 그쪽은 2차 가공물이
 * 아니라 원문이고, 아카이브에 들어갈 바로 그 내용이다.
 *
 * 맥락은 있으면 좋은 것이지 없으면 답을 못 하는 것이 아니다 — 조회가 실패하면 빈 문자열.
 *
 * @param {object} client Slack WebClient
 * @param {{channel:string, threadTs:string, skipTs?:string, access?:object, maxMessages?:number, maxChars?:number}} opts
 *   skipTs 는 지금 묻고 있는 그 메시지. 질문이 맥락에 한 번 더 들어가지 않게 뺀다.
 *   access 는 config.js 의 열람 권한. **주면 안 되는 게 아니라 반드시 줘야 한다** — 아래 가리기가 이걸로 돈다.
 * @returns {Promise<{text:string, count:number}>} count 는 실제로 실린 사람 발언 수
 */
export async function fetchThreadContext(client, {
  channel,
  threadTs,
  skipTs,
  access = null,
  maxMessages = config.limits?.threadContextMaxMessages ?? 20,
  maxChars = config.limits?.threadContextMaxChars ?? 4000,
}) {
  const empty = { text: '', count: 0 };
  if (!threadTs) return empty;

  let messages;
  try {
    messages = await fetchAllReplies(client, { channel, ts: threadTs });
  } catch (err) {
    console.warn('스레드 맥락 조회 실패 — 이번 질문은 단발로 답합니다:', err.data?.error || err.message);
    return empty;
  }

  const userMap = await getUserMap(client);
  const selfId = await getSelfId(client);
  const tz = config.timezone;

  const lines = [];
  for (const m of messages) {
    if (m.ts === skipTs) continue;
    if (m.subtype && SKIP_SUBTYPES.has(m.subtype)) continue;
    // 봇이 쓴 것은 **전부** 뺀다 (WHK 결정 2026-08-12). 전에는 Hermes 자신만 빼고 다른 봇의
    // 글은 원문이라며 그대로 실었는데, 봇 글을 근거로 삼지 않는다는 규칙을 봇 전반으로 넓혔다.
    if (isBotMessage(m, selfId)) {
      // 긴 답변은 여러 메시지로 쪼개져 올라간다. 연속된 것은 한 줄로 접는다.
      if (lines[lines.length - 1] !== BOT_ANSWER_MARK) lines.push(BOT_ANSWER_MARK);
      continue;
    }
    // 사람이 쓴 본문에 비공개 채널 이름과 그 내용이 섞여 들어온다 — 공개 채널 스레드에
    // "비공개가 회의에서 000억으로 정리됐다" 처럼. 아카이브 쪽 searchArchive·readChannel 과
    // 같은 처리를 여기에도 둔다(커밋 9450893 이 건 네 자리에 이어 다섯 번째 자리다).
    // 안 걸면 봇이 그 이름을 알게 되어 "그건 #비공개가에 있습니다" 로 되짚어 말한다
    // (claude.js 의 privateQuoteLine 주석에 적힌 그 실패 모양).
    const raw = renderText(m.text, userMap);
    const text = redactPrivateMentions(raw, access);
    // 가려서 본문이 통째로 사라졌으면 메시지도 통째로 뺀다. 헤더만 남기면 "이 시각에 이 사람이
    // 무언가 말했다" 가 그대로 새고, 맥락으로도 쓸모가 없다.
    if (raw.trim() && !text.trim()) continue;
    const files = (m.files || [])
      .map((f) => f.name)
      .filter(Boolean)
      // 파일 이름에도 자리 이름이 들어간다 ('비공개가_회의록.pdf'). 이름만으로도 드러난다.
      .filter((n) => redactPrivateMentions(n, access) === n);
    if (!text.trim() && !files.length) continue;
    const name = m.user ? userMap.get(m.user) || m.user : m.username || m.bot_profile?.name || '알 수 없음';
    const author = m.bot_id && !m.user ? `${name} (봇)` : name;
    lines.push(
      `**${stamp(m.ts, tz)} · ${author}** — ${text}${files.length ? ` 📎 첨부: ${files.join(', ')}` : ''}`,
    );
  }

  // 넘치면 **앞을 자른다.** 지시어가 가리키는 것은 대개 바로 앞이라, 가까운 쪽을 남긴다.
  let kept = lines.slice(-maxMessages);
  while (kept.length > 1 && kept.join('\n').length > maxChars) kept = kept.slice(1);

  // 자기표시만 남았으면 맥락이 없는 것과 같다.
  if (!kept.some((l) => l !== BOT_ANSWER_MARK)) return empty;
  return { text: kept.join('\n'), count: kept.filter((l) => l !== BOT_ANSWER_MARK).length };
}

/* ── 기간 계산 (config.timezone 기준) ───────────────────────────── */

/**
 * 마지막으로 **실제로 나간** 일일 요약이 어디까지 덮었나 (epoch 초).
 *
 * **코드 저장소 안 `logs/` 에 둔다.** 그 폴더는 `.gitignore` 에 있어 커밋되지 않는다.
 * 자료 저장소(`.sync-state.json`)에 두면 매일 작업 트리가 더러워져 다음 날 자동 반영이
 * 멈춘다 — `config.js` 의 LOG_DIR 주석이 같은 이유로 로그 원본을 여기 둔다.
 * `LOG_ENABLED` 와 무관하게 이 자리를 쓴다: 로그를 꺼도 요약 구간은 이어져야 한다.
 */
/* 자리는 `config.js` 가 정한다 — 인스턴스마다 달라야 해서다(2026-10-08).
 * 여기서 또 조립하면 한쪽만 고쳐지고, 그때 두 인스턴스가 같은 파일을 쓴다. */
const DIGEST_STATE_FILE = CONFIGURED_DIGEST_STATE_FILE;

/** 기록이 없거나 깨졌으면 `null` — 그때는 아래에서 예전처럼 24시간을 쓴다. */
function readDailySentThrough(file = DIGEST_STATE_FILE) {
  try {
    const v = Number(JSON.parse(fs.readFileSync(file, 'utf8')).dailySentThrough);
    return Number.isFinite(v) && v > 0 ? v : null;
  } catch {
    return null;
  }
}

/**
 * 일일 요약이 **실제로 나간 뒤** 그 구간의 끝을 적어 둔다. `digest.js` 가 전송 성공 뒤 부른다.
 *
 * **시도가 아니라 성공만 적는다.** 시도를 적으면 실패한 회차가 구간을 밀어 버려, 고치려던
 * 바로 그 구멍이 그대로 남는다. `--dry` 도 적지 않는다 (아무것도 안 나갔다).
 *
 * 기록에 실패해도 **던지지 않는다** — 요약은 이미 나갔고, 다음 회차가 24시간으로 물러설 뿐이다.
 * 그 물러섬이 조용하지 않도록 콘솔에 남긴다.
 */
export function markDailyDigestSent(latest, file = DIGEST_STATE_FILE) {
  const v = Number(latest);
  if (!Number.isFinite(v) || v <= 0) return false;
  try {
    fs.mkdirSync(path.dirname(file), { recursive: true });
    // 원자적으로 — 쓰다 죽으면 반쪽 JSON 이 남고, 그러면 다음 회차가 조용히 24시간으로 돈다.
    const tmp = `${file}.tmp`;
    fs.writeFileSync(tmp, JSON.stringify({ dailySentThrough: v }, null, 2) + '\n', 'utf8');
    fs.renameSync(tmp, file);
    return true;
  } catch (err) {
    console.error('[digest] 일일 요약 구간 기록 실패 —', err.message);
    return false;
  }
}

const { dailyWindow, weeklyWindow, recentWindow } = createSlackLiveWindow({
  config, stamp, readDailySentThrough, DEFAULT_LIVE_FETCH_MAX_DAYS,
});
export { dailyWindow, weeklyWindow, recentWindow };
