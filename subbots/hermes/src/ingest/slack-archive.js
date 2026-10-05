/**
 * 대화 원문을 아카이브 md 에 반영한다 — `slack-sync` 스킬이 하던 일의 무인 판.
 *
 * 스킬은 Slack MCP 커넥터(WHK 개인 인증)를 써서 사람이 붙어 있어야만 돌았다.
 * 여기서는 **Hermes 봇 토큰**으로 같은 일을 한다. 봇은 42개 채널에 들어가 있고
 * 비공개 #비공개가 도 포함이라 커버리지가 같다.
 *
 * **원문만 넣는다.** 채널 md 첫머리의 요약(메타 블록 `핵심 쟁점` 줄, `## 자금 구조 요약` 표)은
 * 건드리지 않는다 — 그건 사람이 고친다. 원문은 슬랙을 그대로 옮기는 것이라 틀릴 여지가 없지만,
 * 요약은 틀리면 봇이 그 숫자를 근거로 답한다.
 */
import { createArchiveApi } from './archive-api.js';
import { createArchiveFormat } from './archive-format.js';
import { createArchiveChanges } from './archive-changes.js';
import fs from 'node:fs';
import path from 'node:path';
import { config, ARCHIVE_DIR, CHANNELS_DIR, isPrivateChannel, channelRefsIn } from '../config.js';
import {
  listBotChannels, getUserMap, getSelfId, renderText, stamp, SKIP_SUBTYPES,
  CORRECTION_RE, BOT_AUTHOR_LABEL, BOT_BLOCK_BODY, BOT_ANSWER_MARK, isBotMessage, fetchAllReplies,
  keepMessage, dropSkippedChannels,
} from '../slack-live.js';
import { permalink } from '../convo-log.js';
import { SKILL_SCRIPTS, runScript, writeTemp, readJson, writeJson, activeDeferred } from './util.js';
import { assertMayWriteArchive } from '../mode.js';

const STATE_FILE = path.join(ARCHIVE_DIR, '.sync-state.json');
/** 슬랙에서 고쳐지거나 지워졌는데 아직 md 에 반영 안 된 것 (사람이 지시하면 반영한다) */
const PENDING_FILE = path.join(ARCHIVE_DIR, '.pending-edits.json');

/* 메시지 헤더를 세는 일(`**실제 메시지**: N건`)은 여기 없다 — 파생값이라
 * `.claude/skills/slack-sync/scripts/sync_index.py` 한 곳에서 센다 (derive.js 가 부른다). */

const { fetchNewMessages, fetchReplies, fetchWindow, grownThreads } = createArchiveApi({ fetchAllReplies });
const {
  authorOf, renderReply, renderMessage, renderBotPlaceholder, botHeader, monthOf,
  UNREAD_REPLIES_MARK, BODY_TAIL, MD_REPLY_ANY, parseBlocks, writtenFrom,
} = createArchiveFormat({
  renderText, stamp, isBotMessage, BOT_ANSWER_MARK, BOT_AUTHOR_LABEL, BOT_BLOCK_BODY, permalink,
});
const { detectChanges, buildPending, pendingOutOfWindow } = createArchiveChanges({
  parseBlocks, stamp, renderText, renderReply, isBotMessage,
});
export { fetchReplies, renderMessage, monthOf, UNREAD_REPLIES_MARK, BODY_TAIL, parseBlocks, writtenFrom, detectChanges };

/**
 * 그 md 에 지금 있는 **답글 머리줄** 수.
 *
 * `MD_REPLY_ANY` 를 그대로 쓴다 — 날짜 없는 옛 답글까지 잡는 바로 그 정규식이고, 여러 줄
 * 답글의 이어지는 줄은 `> **└` 가 아니라 저절로 빠진다. 정규식을 여기 새로 적으면 세는
 * 기준이 갈리는데, 갈려도 에러가 안 나고 숫자만 달라진다.
 *
 * `backfill.js` 의 `countHeaders` 와 같은 쓰임이다 — 파이썬을 부르기 **전/후로** 세어
 * 차이를 내면 「몇 줄이 실제로 늘었나」가 나온다.
 */
function countReplyLines(mdPath) {
  if (!fs.existsSync(mdPath)) return 0;
  let n = 0;
  for (const line of fs.readFileSync(mdPath, 'utf8').split(/\r?\n/)) {
    if (MD_REPLY_ANY.test(line)) n += 1;
  }
  return n;
}

/**
 * 파이썬이 성공하면서 남긴 `NOTE:` 줄을 보고에 옮긴다.
 *
 * **성공한 회차에도 사람이 알아야 할 것이 나온다.** `insert_messages.py` 는 부모가 모호할
 * 때 붙일 답글이 전부 이미 있으면 정상 종료하는데, 그때 **봇 답변 건수만은 못 올린다** —
 * 어느 블록의 꼬리말을 고칠지 정해야 쓸 수 있는 값이라서다. 멈추지 않는 것이 맞지만
 * 조용히 넘기면 그 건수가 영영 안 맞는다.
 *
 * **문구가 아니라 `NOTE:` 접두만 본다.** 특정 문장을 여기 베껴 두면 파이썬 쪽을 고칠 때
 * 에러 없이 낡는다 — 이 저장소가 여러 번 밟은 자리다. 파이썬이 무엇을 말할지는 파이썬이
 * 정하고, 여기서는 「사람에게 전할 줄」이라는 표시만 읽는다.
 */
function collectNotes(result, stdout) {
  for (const line of String(stdout || '').split(/\r?\n/)) {
    const m = /^NOTE:\s*(.+)$/.exec(line.trim());
    if (m) result.notes.push(m[1].trim());
  }
}

/**
 * 개명 이력의 사슬 — `.sync-state.json` 의 `channels[<id>].aka` 다음 값. 순수 함수다.
 *
 * `file` 은 **맨 처음 이름 하나만** 든다. x → y → z 로 두 번 개명하면 가운데 y 가
 * 어디에도 안 남아, y 시절 대화·색인에 글자로 적힌 y 를 `channelSpellings` 가 못 내
 * 언급 가리기가 못 막는다. 그래서 개명을 감지한 순간(슬랙의 현재 이름 ≠ 기록된 현재
 * 이름) **기록돼 있던 직전 이름을** `aka` 에 시간순으로 덧붙인다.
 *
 * 덧붙이지 않는 경우 셋 — 전부 중복이다:
 * - 직전 이름이 `file`(첫 이름)과 같다 (첫 개명. 첫 이름은 이미 `file` 에 있다)
 * - 직전 이름이 새 이름과 같다 (개명이 아니다 — 부르는 쪽이 이미 거르지만 여기서도 막는다)
 * - 이미 `aka` 에 있다 (y → x 복귀 뒤 다시 x → y 같은 왕복)
 *
 * 읽기 쪽(`config.js` 의 `channelSpellings` 등)과 doc-archive 파이썬의 지도 로딩이
 * 같은 계약으로 이 사슬을 철자로 인식한다.
 *
 * @param {{name?:string, file?:string, aka?:string[]}|undefined} prev 상태에 기록돼 있던 채널 줄
 * @param {string} newName 슬랙이 준 현재 이름
 * @returns {string[]} 다음 `aka` (빈 배열이면 칸을 만들 필요 없음)
 */
export function renameAka(prev, newName) {
  const aka = [...(prev?.aka || [])];
  const before = prev?.name;
  const first = prev?.file || before;
  if (before && before !== newName && before !== first && !aka.includes(before)) {
    aka.push(before);
  }
  return aka;
}

/* ── 채널 하나 처리 ─────────────────────────────────────────────── */

export async function ingestChannel(client, ch, state, { userMap, selfId, tz, dry, editedSince, pendingKeys, scanEdits, retryEdits = false }) {
  const prev = state.channels[ch.id];
  const result = {
    channel: ch.name, id: ch.id, added: 0, threadReplies: 0,
    months: [], notes: [], transcript: '', edited: [], deleted: [], ambiguous: [],
  };

  /* 래치가 둘이다 — 섞어 두면 한쪽 사정이 다른 쪽을 영영 붙잡는다.
   * - `editScanIncomplete`: 기존 후보를 판정하지 못했다 → `.pending-edits.json` 목록을 보존한다
   * - `editScanRetry`: 다시 보면 더 볼 수 있다 → 되돌아보기 기준(`edit_scan_retry`)을 붙잡는다
   *
   * 갈라 둔 이유는 `detectChanges` 쪽에 있다 — 삭제를 못 짚은 회차는 목록은 보존해야 하지만
   * 기준을 붙잡을 이유가 없고, 붙잡으면 그 채널이 영영 재시도로 돈다 (그쪽 `retry` 주석).
   * 여기 다섯 자리는 전부 둘 다 켠다. */
  const scanIncomplete = ({ retry = true } = {}) => {
    result.editScanIncomplete = true;
    if (retry) result.editScanRetry = true;
  };

  /**
   * 비공개 선언 대조 — **fail closed**.
   *
   * 비공개 판정이 두 군데서 나온다. 슬랙이 알려주는 `is_private` 와, 사람이 손으로 적는
   * `config.privateChannels` 다. 아카이브를 읽는 쪽(archive.js)은 슬랙에 접속하지 않으므로
   * **손으로 적은 목록만 본다.** 그래서 슬랙에서 비공개인 채널이 그 목록에 빠져 있으면,
   * 아카이브에 들어가는 순간 봇이 그 내용을 공개 채널 답변에 인용한다.
   *
   * 새 비공개 채널이 생길 때마다 사람이 목록에 추가해야 한다는 뜻이고, 잊으면 조용히 샌다.
   * 그래서 여기서 막는다 — 선언되지 않은 비공개 채널은 **아카이브하지 않고 보고만** 한다.
   * (2026-08-03 실제로 #비공개바 가 이 상태였다.)
   */
  if (ch.isPrivate && !isPrivateChannel(ch.name)) {
    result.error =
      `슬랙에서 비공개인데 config.json 의 privateChannels 에 없습니다 — 아카이브하지 않았습니다. ` +
      `넣으면 봇이 공개 채널 답변에 인용하게 됩니다. 비공개로 다룰 것이면 privateChannels 에 ` +
      `"${ch.name}" 를 추가하고, 아예 다루지 않을 것이면 digest.skipChannels 에 넣으세요.`;
    result.undeclaredPrivate = true;
    return result;
  }

  // 파일명은 상태에 적힌 것을 우선한다. 채널이 개명되면 md 파일명은 그대로이므로,
  // 현재 이름으로 쓰면 빈 파일을 새로 만들어 아카이브가 두 갈래로 갈라진다.
  //
  // **`file` 을 `name` 과 따로 두는 이유**: 예전에는 `name` 하나로 겸했는데, 아래 상태 저장이
  // `name: ch.name` 으로 매번 현재 이름을 덮어써서 개명 처리가 **딱 한 번만** 들었다. 다음
  // 회차에는 `prev.name` 이 이미 새 이름이라 옛 md 를 못 찾고 새 파일을 만든다. `name` 은
  // archive.js 가 채널 ID↔이름 지도로 쓰므로 현재 이름이어야 하고, md 파일명은 안 바뀐다 —
  // 두 값의 수명이 다르므로 칸을 나눈다 (2026-08-10 에 개명된 채널 하나가 실제로 이
  // 상태였다 — 그 채널 ID 를 여기 적어 두었었는데, 실제 ID 는 이 저장소에 안 남긴다.
  // `scripts/check-real-ids.js` 가 그 축이다).
  const stateName = prev?.name;
  const exists = (n) => Boolean(n) && fs.existsSync(path.join(CHANNELS_DIR, `${n}.md`));
  // `file` 이 없는 옛 상태는 `name` 으로 한 번 더 찾아본다 (이 칸이 생기기 전에 쓰인 것).
  const fileName = [prev?.file, stateName].find(exists) || ch.name;
  const mdPath = path.join(CHANNELS_DIR, `${fileName}.md`);
  result.file = fileName;

  if (stateName && stateName !== ch.name) {
    result.notes.push(`채널 개명: ${stateName} → ${ch.name} (md 는 ${fileName}.md 에 계속 씀)`);
    // 옛 이름을 가리키는 설정 줄이 있으면 함께 싣는다 — 그 줄은 이 순간부터 아무것도 안
    // 막는데 에러가 안 난다. 보고 문구는 `report.js` 가 만든다.
    result.renamed = { from: stateName, to: ch.name, inConfig: channelRefsIn(stateName) };
    /* 직전 이름을 `aka` 사슬에 남긴다 (규칙은 `renameAka` 주석). `prev` 는
     * `state.channels[ch.id]` 그 물건이라 여기서 달아 두면 아래 상태 저장 세 곳의
     * `...prev` 가 그대로 실어 간다. dry 회차는 상태 객체도 건드리지 않는다 —
     * dry 의 계약이 「파일·상태를 안 건드린다」이고 시험이 스냅샷 대조로 지킨다. */
    if (!dry) {
      const aka = renameAka(prev, ch.name);
      if (aka.length) prev.aka = aka;
    }
  }

  /* 새 채널 — baseline 만 잡고 보고한다. 전체 히스토리 읽기는 무인으로 하지 않는다. */
  if (!prev) {
    const res = await client.conversations.history({ channel: ch.id, limit: 1 });
    const latest = res.messages?.[0]?.ts || '0';
    result.isNew = true;
    result.notes.push('새 채널 — 기준점만 잡았습니다. 과거 대화는 사람이 따로 추출해야 합니다.');
    if (!dry) state.channels[ch.id] = { name: ch.name, file: fileName, last_ts: latest, threads: {} };
    return result;
  }

  // md 가 없는 채널이 몇 개 있다 — 원본 추출 때 대화가 없어 파일을 만들지 않은 곳이다.
  // 여기서 미리 막지 않는다. **넣을 내용이 실제로 생겼을 때만** 문제이고, 그 전까지는
  // 정상이다. 미리 막으면 조용한 채널 6개가 매일 실패로 잡혀 진짜 실패가 묻힌다.

  /* ① 새 메시지 */
  const raw = await fetchNewMessages(client, ch.id, prev.last_ts);
  const fresh = raw.filter((m) => keepMessage(m, selfId));
  const droppedSystem = raw.length - fresh.length;
  if (droppedSystem) result.notes.push(`시스템·봇 메시지 ${droppedSystem}건 제외`);

  const threads = { ...(prev.threads || {}) };
  const blocks = new Map(); // month → [블록…] (오래된 것부터)

  for (const m of fresh) {
    let replies = [];
    if (m.reply_count) {
      try {
        replies = await fetchReplies(client, ch.id, m.ts);
        /* **읽은 수를 적는다 — 슬랙이 말한 `reply_count` 가 아니라.** 이 칸은 ③ 에서
         * 「md 에 이미 들어간 답글 수」(`knownCount`)로 쓰이고, md 에 들어가는 것은 방금
         * 읽은 것이다. ③ 도 같은 자리에서 `replies.length` 를 적는다. */
        threads[m.ts] = { reply_count: replies.length };
      } catch (err) {
        /* **실패한 뒤에는 「반영됨」을 적지 않는다.**
         * 예전에는 `catch` 밖에서 `reply_count` 를 그대로 적었다. md 에는 답글이 한 건도
         * 안 들어갔는데 상태에는 전체 개수가 적히므로, 다음 회차가 `knownCount` 를 보고
         * 「이미 다 담았다」로 건너뛰었다 — 그 답글은 영영 안 들어온다. `threads` 를 읽는
         * 자리가 저장소에 ③ 하나뿐이라 복구해 줄 다른 경로도 없다.
         *
         * 대신 `unread` 를 달아 **다음 회차가 다시 읽게** 한다. 아래 ② 가 이 표시를 보고
         * 되돌아보기 구간에서 그 부모를 집어 ③ 으로 넘긴다. `last_ts` 를 붙잡아 두는 쪽으로
         * 막지 않는 이유: 그러면 이 메시지가 다음 회차에 다시 ① 로 들어와 답글이 붙은
         * **더 긴 블록**을 만드는데, `insert_messages.py` 의 중복 판정은 「기존 블록이 넣을
         * 블록으로 시작하나」라서 짧은 기존 블록에 안 걸린다 — 같은 발언이 md 에 두 벌
         * 실린다. ③ 으로 넘기면 이미 있는 블록 **안에** 답글만 덧붙는다. */
        result.notes.push(`스레드 읽기 실패 ${m.ts} (${err.data?.error || err.message})`);
        threads[m.ts] = { reply_count: 0, unread: true };
      }
    }
    const month = monthOf(m.ts, tz);
    if (!blocks.has(month)) blocks.set(month, []);
    blocks.get(month).push(renderMessage(m, replies, userMap, tz, selfId));
    result.added += 1;
  }

  /* ② lookback 구간을 되돌아본다 — 나중에 달린 답글(정정 댓글)과, 슬랙에서 고쳐지거나
   *    지워진 메시지를 **같은 조회 결과**로 함께 찾는다. */
  const lookbackSec = (config.limits.threadLookbackDays ?? 30) * 86400;
  const { oldest, messages: windowMsgs } = await fetchWindow(client, ch.id, prev.last_ts, lookbackSec);
  const grown = grownThreads(windowMsgs, prev.last_ts);

  /* 봇이 **이번 회차에 새로 올린** 글은 위 구간 밖이다 — `fetchWindow` 는 `last_ts` 까지만
   * 보고, 그보다 새 메시지는 ① 이 담당하는데 ① 은 봇 글을 버리기 때문이다. 그래서 오늘 요약에
   * 오늘 달린 정정이 이 회차에서 아예 안 보이고 다음 회차로 밀렸다 (2026-08-09 실측: 1회차 0건,
   * 2회차에 잡힘 — 실제로는 하루 늦는다). 여기서 함께 집는다. */
  const botFresh = raw.filter(
    (m) => isBotMessage(m, selfId) && m.reply_count
      && !(m.subtype && SKIP_SUBTYPES.has(m.subtype))
      && !grown.some((g) => g.ts === m.ts),
  );
  /* **지난 회차에 답글을 못 읽은 스레드**를 다시 집는다 (`unread` 표시).
   *
   * `grownThreads` 로는 못 잡는다 — 그 판정은 `latest_reply > last_ts` 인데, 실패한 회차도
   * `last_ts` 는 그대로 전진하므로 다음 회차에는 그 답글이 전부 `last_ts` 보다 오래된
   * 것이 된다. 그래서 아무 데도 안 걸리고 **영영 안 들어왔다.** 표시를 보고 집는 것이
   * 「무엇을 못 읽었나」에 대한 정확한 답이고, 읽는 데 성공하면 ③ 이 개수를 다시 적어
   * 표시가 저절로 지워진다. */
  const unreadPrev = Object.entries(prev.threads || {})
    .filter(([, v]) => v?.unread)
    .map(([ts]) => ts);
  const retry = windowMsgs.filter(
    (m) => unreadPrev.includes(m.ts) && !grown.some((g) => g.ts === m.ts),
  );
  /* 되돌아보기 구간(30일) 밖으로 밀려난 것은 다시 읽을 방법이 없다. **조용히 두지
   * 않는다** — 그 답글은 실제로 아카이브에 없고, 사람이 `slack-sync` 로 손수 넣는 것
   * 말고는 길이 없다. */
  const tooOld = unreadPrev.filter((ts) => !windowMsgs.some((m) => m.ts === ts));
  if (tooOld.length) {
    result.notes.push(
      `답글을 못 읽은 스레드 ${tooOld.length}건이 되돌아보기 구간(${config.limits.threadLookbackDays ?? 30}일) 밖으로 나갔습니다 — `
      + `다시 읽지 못하므로 그 답글은 아카이브에 없습니다 (${tooOld.join(', ')})`,
    );
  }
  const withReplies = [...grown, ...botFresh, ...retry];

  /* 답글은 스레드마다 한 번만 읽어 두 곳에서 같이 쓴다. 답글이 늘어난 스레드는 반영해야
   * 하므로 반드시 읽고, 나머지(수정·삭제 대조용)는 상한까지만 읽는다 — 조용히 자르지 않고
   * 못 본 개수를 보고에 적는다. */
  const repliesByTs = new Map();
  /**
   * @param {object} m 부모 메시지
   * @param {{keep?:boolean}} opts `keep` 이면 실패했을 때 **다음 회차에 다시 읽도록** 표시한다.
   *   반영해야 하는 스레드(`withReplies`)에만 준다 — 수정·삭제 대조용으로 더 읽는 것은
   *   못 읽어도 잃는 자료가 없으므로 표시하면 재시도 목록만 부푼다.
   */
  const readReplies = async (m, { keep = false } = {}) => {
    try {
      repliesByTs.set(m.ts, await fetchReplies(client, ch.id, m.ts));
    } catch (err) {
      result.notes.push(`스레드 읽기 실패 ${m.ts} (${err.data?.error || err.message})`);
      if (scanEdits) scanIncomplete();
      if (keep) {
        // 지금까지 몇 건이 md 에 들어가 있는지는 그대로 둔다 — 못 읽었을 뿐 안 지워졌다.
        threads[m.ts] = { ...(prev.threads?.[m.ts] || { reply_count: 0 }), unread: true };
      }
    }
  };
  for (const m of withReplies) await readReplies(m, { keep: true });
  if (scanEdits) {
    const room = Math.max(0, (config.limits.editScanMaxThreads ?? 60) - repliesByTs.size);
    const rest = windowMsgs.filter((m) => m.reply_count && !repliesByTs.has(m.ts)).reverse(); // 최신부터
    for (const m of rest.slice(0, room)) await readReplies(m);
    if (rest.length > room) {
      scanIncomplete();
      result.notes.push(
        `스레드가 많아 답글 수정·삭제 대조를 ${room}개까지만 했습니다 (${rest.length - room}개 못 봄)`,
      );
    }
  }

  /* ②-2 수정·삭제 감지 — **md 를 고치기 전에** 읽어야 지금 아카이브에 뭐가 있는지가 나온다.
   *     찾아서 알리기만 한다. 반영은 사람이 지시할 때 `slack-sync` 가 한다. */
  if (scanEdits && fs.existsSync(mdPath)) {
    /* 감지는 **알리는 일**이지 아카이브를 쌓는 일이 아니다. 여기서 터진 예외가 위로 올라가면
     * 그 채널의 새 메시지가 그날 통째로 안 들어간다 — 부수 기능이 본업을 막는 모양이다.
     * 그래서 삼키고 보고에 남긴다. */
    try {
      const found = detectChanges({
        md: fs.readFileSync(mdPath, 'utf8'),
        windowMsgs, repliesByTs, userMap, tz, selfId, oldest, lastTs: prev.last_ts,
        editedSince, pendingKeys: pendingKeys || new Set(),
        maxFindings: config.limits.editScanMaxFindings ?? 10,
        requireComplete: retryEdits,
      });
      result.edited = found.edited;
      result.deleted = found.deleted;
      result.ambiguous = found.ambiguous;
      // 감지가 끝까지 돌았다 — buildPending 의 ambiguous 이월 예외가 이 표지를 본다
      // (catch·md 부재 경로는 안 찍혀, 그런 회차의 ambiguous 는 종전대로 보존된다).
      result.editScanComplete = true;
      result.notes.push(...found.notes);
      // 판정하지 못한 후보가 있을 때만 래치한다 — 알리기만 하는 note 는 세지 않는다
      // (알림 한 줄이 래치가 되면 다음 회차가 같은 알림을 또 내어 안 풀린다, 2026-09-15).
      // 기준을 붙잡을지는 `detectChanges` 가 갈라서 알려 준다 (그쪽 `retry` 주석).
      if (found.incomplete) scanIncomplete({ retry: found.retry });
      const from = stamp(String(Number(oldest) + 60), tz);
      const to = stamp(String(Number(prev.last_ts) - 60), tz);
      if (pendingOutOfWindow(pendingKeys, from, to)) {
        /* 기준까지 붙잡는다. 구간 밖 항목 **자체**는 기준을 붙잡아도 안 들어오지만
         * (구간은 `last_ts` 와 lookback 이 정한다), 래치를 놓으면 다음 회차의
         * `retryEdits` 가 꺼져 이 채널의 대조가 느슨해진다 — 그 채널은 손대야 할 것이
         * 실제로 남아 있는 채널이다. 사람이 그 건을 처리하면 저절로 풀린다. */
        scanIncomplete();
        result.notes.push('기존 수정·삭제 후보 중 대조 구간 밖의 항목이 있어 목록을 보존했습니다 — 수동 확인이 필요합니다.');
      }
    } catch (err) {
      scanIncomplete();
      result.notes.push(`수정·삭제 감지 실패 — 아카이브 반영은 계속했습니다 (${err.message})`);
    }
  }

  /* An absent archive cannot establish that previous findings were resolved.
   *
   * 기준도 함께 붙잡는다. md 가 없는 동안 난 수정은 md 가 생기는 회차에 「최근 것」으로
   * 남아 있어야 잡힌다. 이 채널은 `detectChanges` 를 아예 안 부르므로 래치가 note 를
   * 만들지 못하고, 그래서 여기 래치는 스스로 불어나지 않는다 (2026-09-15 검토). */
  if (scanEdits && !fs.existsSync(mdPath)) scanIncomplete();

  /* ③ 나중에 달린 답글 — 정정 댓글(`[정정] …`)이 여기로 온다. */
  const pending = [];    // { header, lines[], botAdded, ts } — 사람 글에 달린 답글
  const botPending = []; // { ts, month, header, lines[] } — 봇 글에 달린 사람 답글
  for (const parent of withReplies) {
    if (fresh.some((m) => m.ts === parent.ts)) continue; // ①에서 스레드째 넣은 것

    const replies = repliesByTs.get(parent.ts);
    if (!replies) continue; // 읽기 실패 — 위에서 이미 알렸다
    /* 무엇이 새 것인지는 **기록된 답글 수**로 본다. last_ts 는 메시지 기준이라 답글보다
     * 뒤처지고, 그걸로 자르면 같은 답글을 매일 다시 새 것으로 본다. 한 번도 추적된 적 없는
     * 스레드는 knownCount 0 이라 전부 새 것이 되는데, 그게 맞다 — md 에 하나도 안 들어가
     * 있다는 뜻이다 (이미 있는 줄은 insert_messages.py 가 걸러 낸다). */
    const knownCount = prev.threads?.[parent.ts]?.reply_count ?? 0;
    threads[parent.ts] = { reply_count: replies.length };
    if (replies.length <= knownCount) continue;

    const added = replies.slice(knownCount);
    const human = added.filter((r) => !isBotMessage(r, selfId));
    const botAdded = added.length - human.length;
    if (!human.length && !botAdded) continue;

    /* 봇이 올린 글은 아카이브에 블록이 없다 — 봇 글은 안 넣기 때문이다. 그래서 여기 달린
     * 사람 답글은 자리표시 블록에 담는다. 자리표시 블록에는 봇의 문장이 한 글자도 안 들어간다.
     *
     * **담는 범위가 Hermes 와 다른 봇이 다르다.**
     * - Hermes 글에 달린 답글은 `[정정]` 만 담는다. 나머지는 봇 답변에 대한 반응이라 원문
     *   없이는 뜻이 안 통하고, 넣으면 맥락 없는 줄이 아카이브에 쌓인다 (WHK 결정 2026-08-09).
     * - 다른 봇(`주간 체크인` 등)의 글에 달린 답글은 **전부** 담는다. 알림에 대고 사람끼리
     *   주고받은 업무 내용이라 그 자체로 기록 가치가 있다 — `주간 업데이트 공유 부탁드립니다`
     *   에 달린 "2시 변경" 이 그 예다 (WHK 결정 2026-08-12). */
    if (isBotMessage(parent, selfId)) {
      const isSelf = Boolean(selfId && parent.user === selfId);
      const keep = isSelf
        ? human.filter((r) => CORRECTION_RE.test(renderText(r.text, userMap)))
        : human;
      if (!keep.length) {
        // 이 알림은 Hermes 글에만 해당한다 — 다른 봇 글은 애초에 답글을 다 담으므로
        // 여기 걸리는 것은 담을 사람 답글이 하나도 없었던 경우뿐이다.
        if (isSelf) result.selfParentThreads = (result.selfParentThreads || 0) + 1;
        continue;
      }
      /* `isSelf` 를 함께 담는다 — 담는 자리(자리표시 블록)는 같아도 **무엇이 담겼는지가
       * 다르다.** Hermes 것은 `[정정]` 만 걸러 남은 것이고, 다른 봇 것은 사람 답글 전부다.
       * 여기서 안 갈라 두면 아래에서 한 칸에 합쳐지고, 보고가 다른 봇 글에 달린 인사 답글까지
       * 「Hermes 글에 달린 `[정정]`」이라고 알린다 (2026-08-28 #사업장마 에서 발생 —
       * Calendar Bot 글에 달린 답글 2건이었다). */
      botPending.push({
        ts: parent.ts,
        month: monthOf(parent.ts, tz),
        header: botHeader(parent.ts, tz),
        lines: keep.map((r) => renderReply(r, userMap, tz)),
        isSelf,
      });
      continue;
    }

    pending.push({
      ts: parent.ts,
      header: `**${stamp(parent.ts, tz)} · ${authorOf(parent, userMap)}**`,
      lines: human.map((r) => renderReply(r, userMap, tz)),
      botAdded,
    });
    result.threadReplies += human.length;
  }
  if (result.selfParentThreads) {
    result.notes.push(
      `Hermes 가 올린 메시지의 스레드 ${result.selfParentThreads}건에 새 답글 — \`[정정]\` 이 아니라 반영하지 않았습니다. ` +
        '기록을 고치는 것이면 답글 맨 앞에 `[정정]` 을 붙여 주세요.',
    );
  }

  if (!result.added && !pending.length && !botPending.length) {
    // 본문에 안 넣는 메시지(참여 로그·봇)만 있었어도 last_ts 는 전진시킨다.
    // 안 그러면 그 채널을 매일 처음부터 다시 훑는다.
    if (!dry) {
      const seen = raw.length ? raw[raw.length - 1].ts : prev.last_ts;
      state.channels[ch.id] = { ...prev, name: ch.name, file: fileName, last_ts: seen, threads };
    }
    return result;
  }

  // 넣을 내용이 생겼는데 넣을 파일이 없다 — 여기서부터가 진짜 문제다.
  if (!fs.existsSync(mdPath)) {
    result.error =
      `md 파일이 없는데 새 메시지 ${result.added}건 · 새 스레드 답글 ${result.threadReplies + botPending.length}건이 생겼습니다 — ${path.basename(mdPath)}. ` +
      '이 채널은 원본 추출 때 대화가 없어 파일이 없습니다. `slack-sync` 로 채널 md 를 먼저 만들어야 합니다.';
    result.added = 0;
    return result;
  }

  // 요약 대조에 쓸 원문 (최신이 위 — 아카이브와 같은 순서)
  result.transcript = [...blocks.keys()]
    .sort()
    .reverse()
    .flatMap((mo) => blocks.get(mo).slice().reverse())
    .join('\n\n');

  /* ④ md 에 삽입 — 오래된 달부터. 새 월 헤딩은 맨 위에 생기므로 이 순서라야 최신이 위로 온다. */
  if (!dry) {
    for (const month of [...blocks.keys()].sort()) {
      // 한 달 안에서는 최신이 위. insert_messages.py 가 통째로 prepend 하므로 여기서 뒤집는다.
      const body = blocks.get(month).slice().reverse().join('\n\n');
      const tmp = writeTemp(`${ch.id}-${month}.md`, body + '\n');
      const r = await runScript(SKILL_SCRIPTS.insertMessages, [
        '--file', mdPath, '--month', month, '--content-file', tmp,
      ]);
      if (!r.ok) {
        result.error = `삽입 실패 (${month}): ${r.stderr.trim() || r.stdout.trim()}`;
        return result;
      }
      result.months.push(month);
    }

    /* ④-b 나중에 달린 답글을 **부모 블록 안에** 덧붙인다. 별도 블록으로 떼면 원문에 걸린
     * 검색이 정정을 못 보고 원문만 인용된다 (searchArchive 는 블록 단위로 돌려준다). */
    for (const p of pending) {
      const args = ['--file', mdPath, '--append-thread', p.header];
      if (p.lines.length) {
        args.push('--content-file', writeTemp(`${ch.id}-${p.ts}-reply.md`, p.lines.join('\n') + '\n'));
      }
      if (p.botAdded) args.push('--bot-added', String(p.botAdded));
      const before = countReplyLines(mdPath);
      const r = await runScript(SKILL_SCRIPTS.insertMessages, args);
      if (!r.ok) {
        // 부모를 못 찾는 것이 대표적이다(표시이름이 바뀌었거나 그 메시지가 미수집 구간).
        // 조용히 넘기면 정정이 사라지므로 실패로 올린다.
        result.error = `스레드 덧붙이기 실패 (${p.header}): ${r.stderr.trim() || r.stdout.trim()}`;
        return result;
      }
      /* **md 에 실제로 늘어난 답글 줄 수로 센다 — 제안한 개수가 아니라.**
       *
       * `insert_messages.py` 의 `append_thread` 는 **하나도 새것이 없을 때만**
       * 「이미 반영됨」을 돌려준다. 다섯 건 중 하나만 새것이면 그 하나를 쓰고 평범한
       * 문구를 내므로, 문구 유무로 이진 판정하면 그 부분 성공이 「다섯 건 덧붙였습니다」가
       * 된다 — DM 에도 커밋 메시지에도 그대로 나간다.
       *
       * **백필이 같은 결함을 먼저 진단하고 고쳤다** (`backfill.js` 의 `writeMonth`,
       * 2026-09-02). 거기서는 `insert_block` 을 부르기 전/후로 md 의 **메시지 헤더 수**를
       * 세어 차이를 냈다. 여기서는 같은 방식으로 **답글 머리줄 수**를 센다 — 파이썬이
       * 뭐라고 말했는지가 아니라 md 에 실제로 늘어난 줄 수가 진실이다. */
      collectNotes(result, r.stdout);
      const wrote = countReplyLines(mdPath) - before;
      if (wrote > 0) {
        result.appended = (result.appended || 0) + wrote;
        // 관문은 이번에 실제로 쓴 것만 본다
        (result.replyProbes ||= []).push({
          file: fileName, header: p.header, reply: p.lines[p.lines.length - 1],
        });
      }
    }
    // DM 에는 실제로 덧붙인 것만 알린다 (탐지 건수가 아니라)
    result.threadReplies = result.appended || 0;

    /* ④-c 봇이 올린 글에 달린 사람 답글 (Hermes 는 `[정정]` 만, 다른 봇은 전부).
     *
     * 부모 블록이 아직 없으면 **자리표시 블록**을 먼저 만든다 — 헤더와 표식뿐이라 봇의 문장은
     * 한 글자도 안 들어간다. 한 번 만들어진 뒤부터는 사람 글과 똑같이 그 블록 안에 덧붙인다.
     * 부모를 **헤더 줄 전체로** 찾는 이유: 같은 분에 사람이 올린 메시지가 있으면 시각만으로는
     * 정정이 그 사람 발언에 붙는다. */
    for (const p of botPending) {
      const exists = fs.readFileSync(mdPath, 'utf8')
        .split(/\r?\n/)
        .some((l) => l.trim() === p.header);
      const args = exists
        ? [
            '--file', mdPath, '--append-thread', p.header,
            '--content-file', writeTemp(`${ch.id}-${p.ts}-fix.md`, p.lines.join('\n') + '\n'),
          ]
        : [
            '--file', mdPath, '--month', p.month, '--in-order',
            '--content-file', writeTemp(
              `${ch.id}-${p.ts}-bot.md`,
              renderBotPlaceholder(ch.id, p.ts, p.lines, tz) + '\n',
            ),
          ];
      const before = countReplyLines(mdPath);
      const r = await runScript(SKILL_SCRIPTS.insertMessages, [...args, '--report']);
      if (!r.ok) {
        // 조용히 넘기면 정정이 사라진다 — 사람 글 쪽과 같은 이유로 실패로 올린다.
        result.error = `봇 글에 달린 답글 반영 실패 (${p.header}): ${r.stderr.trim() || r.stdout.trim()}`;
        return result;
      }
      collectNotes(result, r.stdout);
      /* **실제로 들어간 것만 센다 — 제안한 개수가 아니라.**
       *
       * 세는 규칙은 위 ④-b 와 같은 하나다: `countReplyLines` 를 파이썬 전후로 재어 md 에
       * 실제로 늘어난 답글 머리줄 수를 본다 (커밋 `3d61ef0` 이 일반 답글 쪽에 세운 방식).
       * 여기는 그 위에 **신원**이 더 필요하다 — 이 갈래는 DM 에 정정 문장 목록까지 싣기
       * 때문이다. 그것만 파이썬의 `--report` 에서 받는다.
       *
       * 두 값이 갈리면 조용히 한쪽을 믿지 않고 알린다. 갈릴 수 있는 자리가 실제로 있다 —
       * 파이썬은 「이번에 쓴 덩이」를 세고 md 는 「머리줄」을 세는데, 파이썬이 이어지는 줄에
       * `>` 를 채워 넣는 등 모양을 손보는 갈래가 있어서 원리적으로 같다고 단정할 수 없다. */
      let wrote;
      try {
        wrote = writtenFrom(r.stdout, p.lines);
      } catch (err) {
        result.error = `봇 글에 달린 답글 반영 결과를 읽지 못했습니다 (${p.header}): ${err.message}`;
        return result;
      }
      const grew = countReplyLines(mdPath) - before;
      if (wrote.length !== grew) {
        result.notes.push(
          `봇 글에 달린 답글 — 파이썬이 ${wrote.length}건을 썼다는데 md 는 ${grew}줄 늘었습니다 `
          + `(${p.header}). 어느 쪽이 맞는지 확인해 주세요.`,
        );
      }
      if (wrote.length) {
        if (p.isSelf) {
          result.selfCorrections = (result.selfCorrections || 0) + wrote.length;
          /* 문장을 그대로 들고 올라간다. DM 이 건수만 알리면 「무엇의 값인지 알 수 없는 정정」이
           * 조용히 쌓인다 — 맥락이 충분한지는 기계가 판정할 수 없고(사업장 이름이 없어도 멀쩡한
           * 정정이 있다) 사람이 한 줄 읽으면 3초에 안다. 그래서 판정하지 않고 보여준다.
           * **이번에 실제로 들어간 문장만 싣는다** — 이미 있던 정정을 다시 부르면 사람이
           * 같은 줄을 매일 새 것으로 읽는다. */
          (result.selfFixLines ||= []).push(...wrote);
        } else {
          /* 다른 봇 글에 달린 사람 답글. 정정이 아니라 그냥 대화다 — 건수만 세고 문장은
           * 안 보인다. 무엇을 고치는 것인지 사람이 읽어야 하는 쪽은 정정뿐이다. */
          result.botReplies = (result.botReplies || 0) + wrote.length;
        }
        // 관문이 「답글이 그 블록 **안에** 있나」까지 본다 — 정정이든 아니든 같다.
        // **이번에 쓴 것으로 찔러야 한다** — 안 쓴 줄로 찌르면 관문이 늘 못 찾는다.
        (result.replyProbes ||= []).push({
          file: fileName, header: p.header, reply: wrote[wrote.length - 1],
        });
      }
    }

    /* ⑤ 새 메시지가 있으면 last_ts 를 민다.
     *
     * **헤더의 `기간`·`실제 메시지` 줄은 여기서 안 고친다** (2026-08-10 부터). 그 둘은
     * 파생값이라 `ingest/derive.js` 가 회차마다 **전 채널**을 다시 계산해 맞춘다. 예전에는
     * 이 자리에서 `fresh.length` 가 있을 때만 고쳐서, 새 메시지가 안 들어오는 채널의 헤더는
     * 한 번 어긋나면 영영 안 맞았다. 두 곳에서 고치면 정의가 갈리므로 한쪽만 남긴다. */
    if (fresh.length) {
      const newest = fresh[fresh.length - 1];
      const lastTs = newest.ts;
      // 관문이 "이 메시지가 봇에게 한 건으로 보이나" 를 확인할 때 쓰는 표식
      result.probeHeader = `**${stamp(lastTs, tz)} · ${authorOf(newest, userMap)}**`;
      // summary_reviewed_ts 는 그대로 둔다 — 사람이 요약을 대조해야 전진하는 값이다.
      state.channels[ch.id] = { ...prev, name: ch.name, file: fileName, last_ts: lastTs, threads };
    } else {
      // 답글만 붙은 채널. last_ts 는 훑은 데까지 전진시켜야 같은 답글을 매일 다시 안 본다.
      const seen = raw.length ? raw[raw.length - 1].ts : prev.last_ts;
      state.channels[ch.id] = { ...prev, name: ch.name, file: fileName, last_ts: seen, threads };
    }
  } else {
    result.months = [...blocks.keys()].sort();
    result.preview = [...blocks.values()].flat().slice(0, 3);
    result.pendingReplies = pending.map((p) => ({ header: p.header, lines: p.lines, botAdded: p.botAdded }));
    result.pendingSelfCorrections = botPending
      .filter((p) => p.isSelf)
      .map((p) => ({ header: p.header, lines: p.lines }));
    // 실제 반영 쪽과 같은 기준으로 가른다 — dry 가 다른 이름으로 세면 예고와 결과가 어긋난다
    result.selfCorrections = botPending.filter((p) => p.isSelf).reduce((a, p) => a + p.lines.length, 0);
    result.selfFixLines = botPending.filter((p) => p.isSelf).flatMap((p) => p.lines);
    result.botReplies = botPending.filter((p) => !p.isSelf).reduce((a, p) => a + p.lines.length, 0);
  }

  return result;
}

/* ── 바깥 ──────────────────────────────────────────────────────── */

/**
 * @param {object} client Slack WebClient
 * @param {{dry?:boolean, scanEdits?:boolean}} opts
 *   dry=true 면 파일·상태를 건드리지 않는다.
 *   scanEdits 를 주면 config 의 `digest.ingest.reportEdits` 보다 우선한다. 요약 직전 회차가
 *   이것을 꺼서 쓴다 — 수정·삭제 후보를 하루 두 번 받으면 같은 목록이 두 번 오고,
 *   그러면 곧 안 읽게 된다. 아침 회차가 담당한다.
 */
export async function ingestConversations(client, { dry = false, scanEdits } = {}) {
  assertMayWriteArchive('대화 수집(ingestConversations)');
  const editsEnabled = scanEdits ?? config.digest.ingest?.reportEdits !== false;
  const tz = config.timezone;
  const state = readJson(STATE_FILE);
  if (!state?.channels) {
    throw new Error(`.sync-state.json 을 읽지 못했습니다 — ${STATE_FILE}`);
  }

  // 이름 지도는 **강제로 다시 받는다.** 여기서 못 찾은 사람은 `U08…` 로 md 에 쓰이고,
  // 한 번 쓰이면 원문처럼 굳어 되돌릴 자리가 없다. 하루 한 번이라 비용도 없다.
  const userMap = await getUserMap(client, { force: true });
  const selfId = await getSelfId(client);
  // skipChannels 는 "봇이 아예 다루지 않는 채널" 이다. 요약만 빠지고 아카이브에는 쌓이면
  // 설정이 막아준다고 믿는 것과 실제가 어긋난다 — 그게 가장 나쁜 상태다.
  // 개명도 되짚어 거른다 (2026-09-16) — 규칙과 이유는 dropSkippedChannels 주석에.
  const channels = dropSkippedChannels(await listBotChannels(client));

  /* 수정 감지의 기준선 — **지난 반영 시각**. 그 뒤에 난 편집만 새로 잡는다.
   * 여유 15분을 두는 이유: `last_sync` 는 실행이 **끝날 때** 쓰이므로, 실행 도중(45초쯤)에 난
   * 편집은 `edited.ts < last_sync` 가 되어 영영 안 잡힌다. 여유 때문에 같은 것을 다시 잡아도
   * 아래 `pendingKeys` 가 같은 id 로 묶어 주므로 두 번 알리지 않는다. */
  const lastSync = Date.parse(state.last_sync || '') || 0;
  const editedSince = lastSync ? lastSync / 1000 - 900 : 0;
  // Optional additive state: earliest failed edit watermark per channel (seconds).
  // Keep it across scanEdits:false runs; last_sync still describes ingestion.
  const editRetries = { ...(state.edit_scan_retry || {}) };

  /* 이미 알린 것은 시각과 무관하게 계속 대조한다 — 고칠 때까지 사라지면 안 된다 */
  const prevPending = readJson(PENDING_FILE);
  const pendingByChannel = new Map();
  for (const it of prevPending?.items || []) {
    const id = it.id.split('|')[0];
    if (!pendingByChannel.has(id)) pendingByChannel.set(id, new Set());
    pendingByChannel.get(id).add(`${it.kind}|${it.scope}|${it.key}`);
  }

  const results = [];
  // 채널을 순차로 돈다. 동시에 돌리면 슬랙 레이트리밋(Tier 3)에 걸리고, 상태 파일을
  // 여러 곳에서 고치게 되어 경합이 생긴다. 하루 한 번이라 속도는 문제가 아니다.
  for (const ch of channels) {
    const retry = editRetries[ch.id];
    const channelEditedSince = typeof retry === 'number' && Number.isFinite(retry)
      ? Math.min(retry, editedSince) : editedSince;
    let result;
    try {
      result = await ingestChannel(client, ch, state, {
        userMap, selfId, tz, dry, editedSince: channelEditedSince, pendingKeys: pendingByChannel.get(ch.id),
        scanEdits: editsEnabled, retryEdits: Object.hasOwn(editRetries, ch.id),
      });
    } catch (err) {
      result = { channel: ch.name, id: ch.id, added: 0, error: err.data?.error || err.message };
    }
    results.push(result);
    /* **실패했다고 무조건 기준을 붙잡지 않는다 — 대조를 못 한 실패만 붙잡는다.**
     *
     * 이 래치가 있는 이유는 「수정·삭제 대조를 못 하고 왔으니 그 구간을 다음 회차가 다시
     * 보게 하라」다. 그런데 조건이 `result.error` 였어서, 대조(②-2)가 **끝까지 돌고 난
     * 뒤** 뒤쪽 단계(④-b 답글 덧붙이기 등)에서 난 실패도 기준을 얼렸다. 그러면 그 채널은
     * 대조를 멀쩡히 마쳤는데도 기준이 안 나아가, 실패가 이어지는 동안 **되돌아보기 구간이
     * 계속 넓어진 채로 매일 같은 자리를 다시 훑는다**. 2026-09-22 에 어느 채널의 `edit_scan_retry`
     * 가 그렇게 박혀 있었고, 같은 날 배포된 갈래 ③ 이 약속한 「배포 뒤 래치 소멸」도
     * 그 채널에서는 일어나지 않았다 — 덧붙이기 실패가 매일 다시 걸고 있었기 때문이다.
     *
     * `editScanComplete` 는 ②-2 가 끝까지 돈 회차에만 찍힌다(예외·md 부재 경로는 안 찍힌다).
     * 그래서 조회 실패처럼 **대조 자체를 못 한** 실패는 종전대로 붙잡힌다. */
    if ((result.error && !result.editScanComplete) || result.editScanRetry) {
      editRetries[ch.id] = channelEditedSince;
    } else if (editsEnabled && !result.isNew) {
      delete editRetries[ch.id];
    }
  }

  /* 수정·삭제 — 파일로 남겨 사람이 반영할 수 있게 한다.
   *
   * **대조를 안 한 회차는 이 파일에 손대지 않는다.** 목록은 매번 이번 결과로 새로 쓰이므로,
   * 안 찾고 온 회차의 0건으로 덮으면 아침 회차가 잡아 둔 미반영 목록이 통째로 지워진다.
   * 지워진 것은 다음 아침에 다시 잡히지만, 그때까지 "볼 것이 없다"로 보인다. */
  const today = stamp(String(Date.now() / 1000), tz).slice(0, 10);
  const { items, before } = editsEnabled
    ? buildPending(results, prevPending, today)
    : { items: [], before: new Map() };

  /* **사람이 「나중에」로 미뤄 둔 것은 보고에서 뺀다.**
   *
   * 파일(`.pending-edits.json`)에는 그대로 남긴다 — 만기가 지나면 저절로 돌아와야 하고,
   * `apply_edits.py` 는 자기가 다시 거른다. 빼는 것은 **보고**뿐이다.
   *
   * 09:00 위생 점검은 이미 걸렀는데 여기만 안 걸렀다. 그래서 「미뤄 두면 만기까지
   * 조용합니다」라고 안내해 놓고 다음 날 아침 07:00 에 그 건의 이름과 「N일째」를 다시
   * 불렀다 — 지키지 못하는 약속이 되고, 그런 알림은 곧 안 읽힌다 (2026-08-10 코드 점검). */
  const held = new Set(activeDeferred(state).map(([id]) => id));
  const shown = items.filter((it) => !held.has(it.id));
  const fresh = shown.filter((it) => !before.has(it.id));

  if (!dry) {
    if (Object.keys(editRetries).length) state.edit_scan_retry = editRetries;
    else delete state.edit_scan_retry;
    state.last_sync = new Date().toISOString();
    writeJson(STATE_FILE, state);

    if (editsEnabled) {
      // 남은 것이 없으면 파일을 지운다 — 빈 목록을 남겨 두면 "볼 것이 있다"로 읽힌다.
      if (items.length) writeJson(PENDING_FILE, { generated: new Date().toISOString(), items });
      else if (fs.existsSync(PENDING_FILE)) fs.unlinkSync(PENDING_FILE);
    }
  }

  return {
    dry,
    channels: results.filter(
      (r) => r.added || r.threadReplies || r.selfCorrections || r.botReplies || r.isNew || r.error
        || r.notes?.length || r.edited?.length || r.deleted?.length || r.ambiguous?.length,
    ),
    totalAdded: results.reduce((a, r) => a + r.added, 0),
    totalReplies: results.reduce((a, r) => a + (r.threadReplies || 0), 0),
    /* 봇 글에 달린 사람 답글은 **두 칸**이다. 담기는 자리(자리표시 블록)는 같지만 무엇이
     * 담겼는지가 달라서, 한 칸으로 세면 보고가 반드시 한쪽을 다른 쪽 이름으로 부른다.
     *   selfCorrections — Hermes 글에 달린 `[정정]` (그것만 담긴다)
     *   botReplies      — 다른 봇 글에 달린 사람 답글 (전부 담긴다) */
    totalSelfCorrections: results.reduce((a, r) => a + (r.selfCorrections || 0), 0),
    totalBotReplies: results.reduce((a, r) => a + (r.botReplies || 0), 0),
    // 아래 숫자들은 전부 `shown` 기준이다 — 보고에 쓰이는 값이라 보류한 건은 안 센다.
    totalEdited: shown.filter((it) => it.kind === 'edited').length,
    totalDeleted: shown.filter((it) => it.kind === 'deleted').length,
    /* fresh = 오늘 처음 잡힌 것, carried = 예전에 알렸는데 아직 안 고친 것.
     *
     * `carriedItems` 와 `today` 를 함께 내보낸다 — 개수만 주면 보고가 「미반영 N건」밖에
     * 못 쓰고, **며칠째인지가 안 보이면 안 고쳐진다.** 2026-08-10 에 8/8·8/9 자 두 건이
     * 그 한 줄로만 알려진 채 남아 있었다. `firstSeen` 은 buildPending 이 이월해 두는데
     * 읽는 곳이 없었다. `today` 는 KST 기준이라 받는 쪽이 시간대를 다시 계산할 필요가 없다. */
    pending: {
      fresh,
      carried: shown.length - fresh.length,
      carriedItems: shown.filter((it) => before.has(it.id)),
      deferred: held.size,
      today,
      file: PENDING_FILE,
    },
    newChannels: results.filter((r) => r.isNew).map((r) => r.channel),
    renamed: results.filter((r) => r.renamed).map((r) => r.renamed),
    errors: results.filter((r) => r.error).map((r) => `#${r.channel}: ${r.error}`),
  };
}
