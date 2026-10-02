/**
 * 과거 대화를 채운다 — 자동 반영이 **일부러 안 하는** 일.
 *
 * `slack-archive.js:523` 이 새 채널에 기준점만 잡고 지나간다("전체 히스토리 읽기는 무인으로
 * 하지 않는다"). 그래서 부트스트랩만 끝난 팀의 봇은 「아직 안 채웠다」만 답한다. 여기서
 * 그 아래를 채운다 — **사람이 부를 때만** 돈다.
 *
 * 읽기만 여기 새로 쓰고, **렌더는 반드시 `slack-archive.js` 의 것을 쓴다.** 읽기가 어긋나면
 * 에러로 드러나지만 렌더가 어긋나면 md 모양이 갈려 **검색이 에러 없이 0건**이 된다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { keepMessage, stamp } from '../slack-live.js';
import { fetchReplies, renderMessage, monthOf } from './slack-archive.js';
import { config, ARCHIVE_DIR, canonicalChannel, currentChannelNames } from '../config.js';
import { SKILL_SCRIPTS, runScript, writeTemp, readJson, writeJson } from './util.js';

/**
 * 백필이 실제로 훑을 채널.
 *
 * **`digest.skipChannels` 를 뺀다 — 자동 반영과 같은 규칙이다.**
 * `slack-archive.js` 가 같은 자리에서 거르며 이유를 적어 뒀다: 「요약만 빠지고 아카이브에는
 * 쌓이면 설정이 막아준다고 믿는 것과 실제가 어긋난다 — 그게 가장 나쁜 상태다」. 백필은
 * 이 필터가 없어서 `--all-channels` 한 번이면 그 채널의 md 가 생겼고, **봇은 채널을
 * `.sync-state.json` 이 아니라 디스크의 md 목록으로 세므로**(`archive.js` 의
 * `listArchivedChannels`) 만들어지는 즉시 색인·검색·인용 대상이 됐다.
 * (최종 검토 F1, 2026-09-02)
 *
 * **2026-09-03 정정.** 이 자리에 「skipChannels 필터는 봇 읽기 경로 어디에도 없다」고
 * 적혀 있었는데, 그건 그때 사실이었고 지금은 아니다. 읽기 경로에도 필터가 생겼다
 * (`archive.js` 의 `listReadableChannels`) — 색인·검색·전문 읽기가 그걸 쓴다.
 * 그렇다고 여기를 빼면 안 된다: 읽기에서 걸러도 md 는 그대로 생기고, 그 파일은
 * 자료 저장소에 커밋돼 팀이 나눠 갖는다. 두 자리 다 막는 것이 맞다.
 *
 * **`--channel` 로 콕 집어도 막는다.** 「사람이 일부러 시켰으니 통과」로 두면 위 주석이
 * 말하는 그 어긋난 상태가 그대로 생긴다. 넣어야 한다면 skipChannels 에서 지우는 것이
 * 옳은 순서다 — 그러면 자동 반영도 함께 다루기 시작해 두 경로가 안 갈린다.
 *
 * **개명도 되짚는다** (2026-09-16). 여기 오는 이름은 슬랙의 **현재** 이름인데 설정에는
 * 사람이 어느 철자로든 적는다. 글자 그대로 대면 개명 순간 skip 줄이 아무것도 안 막고
 * 에러도 안 난다 — 자동 반영(`dropSkippedChannels`)·문서 수집(`is_listed_skip`)과 같은
 * 날 함께 고쳤다. `--channel` 쪽(`only`)도 같은 규칙으로 되짚는다 — skip 만 되짚으면
 * 한 함수 안에서 같은 이름이 두 규칙으로 대져 갈린다. 지도는 한 번만 떠서 쓰고,
 * 시험은 `nameMap` 으로 가짜 지도를 넣는다 (`check-backfill.js` ⑧).
 */
export function backfillTargets(channels, { only, skip, nameMap } = {}) {
  const map = nameMap || currentChannelNames();
  const canon = (n) => canonicalChannel(n, map);
  const skipSet = new Set((skip || []).map(canon));
  const wanted = only ? canon(only) : null;
  return (channels || []).filter((c) => {
    const name = canon(c.name);
    if (skipSet.has(name)) return false;
    return wanted ? name === wanted : true;
  });
}

/**
 * 그 채널의 md 파일 이름 (확장자 없이).
 *
 * **개명해도 md 파일명은 안 바꾼다** — 그래서 옛 이름이 `.sync-state.json` 의 `file` 칸에
 * 화석으로 남는다(`archive.js` 의 `listAllChannels` 주석이 그 계약이다). 백필은 그 파일을
 * 안 보고 `.backfill-state.json` 만 봤는데, 거기엔 백필을 한 번이라도 돌린 채널만 있다.
 * 그래서 개명된 채널은 없는 파일을 가리켜 `archiveFloor` 가 `null` 을 내고, 실행기가
 * **「경계 없음 (아카이브가 비어 있음) — 전부 읽습니다」를 찍은 뒤 새 이름으로 전체 이력을
 * 다시 넣었다.** 그 줄은 거짓말이다 — 아카이브는 비어 있지 않고 다른 파일명 아래에 있다.
 * 결과는 같은 사업장이 **두 이름으로** 봇 색인·검색에 실리는 것이고, 자동 반영은 계속 옛
 * 파일에만 쓰므로 새 파일은 그 시점에 굳는다. (최종 검토 F2, 2026-09-02)
 *
 * 우선순위: 백필 상태의 `file`(그 채널을 이미 백필한 적이 있으면 그때 쓰던 파일) →
 * `.sync-state.json` 의 화석 → 지금 이름. **채널 ID 로 맞춘다** — 이름은 바뀌지만 ID 는 안 바뀐다.
 */
export function mdFileFor(ch, { savedFile, known } = {}) {
  if (savedFile) return savedFile;
  const hit = (known || []).find((k) => k.id === ch.id);
  return hit?.file || ch.name;
}

/**
 * 아카이브 메시지 헤더. `archive.js` 의 `splitMessages`(462행, `/^\*\*(\d{4}-\d{2}-\d{2})[^*]*\*\*\s*$/`)
 * 와 같은 원리로 막는다 — **닫는 `**` 와 줄 끝(`\s*$`)을 요구한다.** 본문 흉내 줄을 막는 것은
 * ` · ` 가 아니라 이 「줄 전체가 헤더 모양으로 끝나야 한다」는 요구다. 예전 판은 접두사만
 * 보고 ` · ` 만 있으면 걸렸는데, 실제로는 그걸로 못 막는다 — 본문에 `**2026-01-01 00:00 ·
 * 김철수** 님이 예전에...` 처럼 닫는 `**` 뒤에 문장이 더 이어지는 줄도 접두사는 그대로
 * 통과해 헤더로 잘못 잡혔다 (2026-09-02 리뷰가 재현·지적). `[^*]*\*\*\s*$` 를 더해
 * 닫는 `**` 다음에 공백(또는 CRLF 의 `\r`)만 있고 줄이 그대로 끝나야만 걸리게 했다.
 *
 * **방향이 갈린다 — 느슨한 쪽이 아니라 엄격한 쪽으로 고른 이유.** 경계가 실제보다
 * **새면(too new)** 안전하다 — 백필이 이미 아카이브에 있는 구간을 다시 읽어 눈에 보이는
 * 중복만 남기고, 사람이 알아채 되돌릴 수 있다. 반대로 **오래되면(too old)** 위험하다 —
 * 진짜 경계와 잘못 잡힌 경계 사이 구간이 통째로 「이미 있다」고 취급돼 조용히 안 읽힌다,
 * 에러도 경고도 없이. 본문 흉내 줄은 늘 실제 경계보다 오래된 값을 들고 있기 쉬우므로
 * (예시가 그렇듯 과거를 인용하는 문장이라서), 못 거르면 경계가 too old 쪽으로 틀린다 —
 * 그래서 여기는 느슨하게 두지 않고 `splitMessages` 만큼 엄격하게 맞춘다.
 */
const HEADER_RE = /^\*\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2})[^*]*\*\*\s*$/;

/**
 * `'YYYY-MM-DD HH:MM'`(설정 시간대) → 그 분이 **시작하는** epoch 초.
 *
 * 시간대 계산을 손으로 하지 않고 `stamp()` 로 **되짚어 확인한다** — 렌더가 쓰는 바로 그
 * 함수라 어긋날 수가 없고, 못 맞추면 조용히 틀리는 대신 던진다.
 */
export function minuteToEpoch(minute, tz) {
  const asUtc = Math.floor(Date.parse(`${minute.replace(' ', 'T')}:00Z`) / 1000);
  const back = Math.floor(Date.parse(`${stamp(asUtc, tz).replace(' ', 'T')}:00Z`) / 1000);
  const epoch = asUtc - (back - asUtc);
  if (stamp(epoch, tz) !== minute) {
    throw new Error(`시각을 되짚지 못했습니다: ${minute} (tz=${tz})`);
  }
  return epoch;
}

/**
 * 그 채널 md 가 **이미 덮고 있는 가장 오래된 자리**.
 *
 * 백필은 이 자리보다 **엄격히 오래된 것만** 읽는다(`fetchRange` 의 `latest` 는
 * `inclusive: false`). 그래서 넣는 것이 이미 있는 것과 겹칠 수 없고, 「이미 있나」를
 * 판정할 일 자체가 없어진다 — 짝짓기·닮은 정도·개수 세기가 다 여기서 필요 없어진다.
 *
 * **경계는 md 에서 그때그때 읽는다.** 상태 파일에 적어 두지 않는 이유가 둘이다:
 * 상태 파일이 날아가도 맞고, 중간에 죽고 다시 돌려도 그 아래부터 이어간다
 * (그 사이 md 에 들어간 만큼 경계가 저절로 내려가므로 두 벌이 생길 길이 없다).
 *
 * @returns {{minute:string, latest:string}|null} 헤더가 하나도 없으면 null (새 채널)
 */
export function archiveFloor(mdPath, tz) {
  if (!fs.existsSync(mdPath)) return null;
  const heads = [];
  for (const line of fs.readFileSync(mdPath, 'utf8').split('\n')) {
    const m = HEADER_RE.exec(line);
    if (m) heads.push(m[1]);
  }
  if (!heads.length) return null;
  const minute = heads.sort()[0];              // 문자열 정렬 = 시간 정렬
  return { minute, latest: String(minuteToEpoch(minute, tz)) };
}

/**
 * 그 md 에 지금 있는 메시지 헤더 수.
 *
 * `HEADER_RE` 를 그대로 쓴다 — 본문 흉내 줄을 헤더로 잘못 세지 않도록 2026-09-02 에
 * 좁힌 바로 그 정규식이다. `writeMonth` 가 넣기 **전/후로 이 수를 재서 차이를
 * 낸다** — 파이썬이 뭐라고 말했는지가 아니라 md 에 실제로 늘어난 줄 수가 진실이다.
 */
function countHeaders(mdPath) {
  if (!fs.existsSync(mdPath)) return 0;
  let n = 0;
  for (const line of fs.readFileSync(mdPath, 'utf8').split('\n')) {
    if (HEADER_RE.test(line)) n += 1;
  }
  return n;
}

/** 한 구간을 커서 끝까지 읽어 **오래된 것부터** 돌려준다. */
export async function fetchRange(client, channelId, { oldest = '0', latest }) {
  const out = [];
  let cursor;
  do {
    const res = await client.conversations.history({
      channel: channelId, oldest, latest, inclusive: false, limit: 200, cursor,
    });
    for (const m of res.messages || []) out.push(m);
    cursor = res.response_metadata?.next_cursor || undefined;
  } while (cursor);
  return out.sort((a, b) => Number(a.ts) - Number(b.ts));
}

/**
 * 기준점 아래를 **최신 달부터 과거로** 하나씩 넘긴다.
 *
 * 달 목록을 미리 만들지 않는 이유: 슬랙이 과거를 어디까지 주는지 미리 알 수 없다
 * (요금제에 따라 90일만 남기도 한다). 달이 바뀌는 것을 보고 넘기면 셀 필요가 없다.
 */
export async function* monthChunks(client, channelId, {
  latest, since, tz, selfId, userMap,
}) {
  const all = await fetchRange(client, channelId, { oldest: '0', latest });
  const desc = all.slice().reverse();          // 최신 → 과거

  let month = null;
  let batch = [];                               // **최신부터** 쌓는다
  let seen = 0;
  let notes = [];
  /* 그 달에서 **마지막으로 본** 메시지의 ts. 최신 → 과거 순으로 도니 곧 가장 오래된 것이다.
   * `batch` 와 따로 두는 이유는 `batch` 에 **거르고 남은 것만** 들어가기 때문이다 —
   * 아래 `oldestSeenTs` 주석 참조. */
  let lastSeenTs = null;

  const flush = async () => {
    if (!month) return null;
    const blocks = [];
    let missedThreads = 0;
    for (const m of batch) {
      let replies = [];
      let unreadReplies = 0;
      if (m.reply_count) {
        try {
          replies = await fetchReplies(client, channelId, m.ts);
        } catch (err) {
          // 조용히 삼키지 않고, 그 달 전체를 죽이지도 않는다.
          notes.push(`스레드 읽기 실패 ${m.ts} (${err.data?.error || err.message})`);
          /* **md 에 흔적을 남긴다.** `renderMessage` 는 `replies` 가 비면 스레드 줄을
           * 아예 안 써서, 이 부모가 「원래 답글이 없던 글」과 구별되지 않았다 —
           * `verify_archive.py` 의 검사 ① 도 볼 머리줄이 없어 조용하다.
           *
           * **자동 반영과 사정이 다르다.** 그쪽은 못 읽은 스레드를 상태에 적어 다음
           * 회차에 다시 읽는데(`slack-archive.js` 의 `unread`), 백필이 넣는 부모는 몇 달
           * 전이라 그 되돌아보기 구간(30일) 밖이고 다음 백필 회차는 이미 경계 위라
           * 다시 안 읽는다. 그래서 여기서는 **영구 유실**이고, 남길 수 있는 것이 흔적뿐이다. */
          missedThreads += 1;
          unreadReplies = m.reply_count;
        }
      }
      blocks.push(renderMessage(m, replies, userMap, tz, selfId, { unreadReplies }));
    }
    const chunk = {
      month, blocks, notes,
      /* 못 읽은 스레드 수. **콘솔 한 줄로만 흘리지 않는다** — `--all-channels` 는 몇
       * 시간짜리라 사람이 로그를 훑지 않고, 마지막 요약과 채널별 표만 본다. 부르는 쪽이
       * 이 값을 모아 「스레드 못 읽음 N건」으로 낸다. */
      missedThreads,
      oldestTs: batch[batch.length - 1]?.ts ?? null,
      /* **거르기 전에 본 것 중 가장 오래된 것.** `oldestTs` 와 갈라 둔 것이 요점이다 —
       * `batch` 에는 `keepMessage` 를 통과한 것만 들어가므로, 한 달이 통째로 걸러지면
       * (참여 알림만 있는 달이 흔하다) `oldestTs` 가 `null` 이 된다. 그 `null` 이
       * ① 이어서 돌릴 기준점과 ② 「가장 오래된 대화」 표에 그대로 들어가면 둘 다 조용히
       * 틀린다 — 표는 `없음` 을 찍고(보존기간 경고가 스스로 거짓말을 한다), 기준점은
       * 위에서부터 다시 훑게 만든다. 두 물음은 「슬랙에 무엇이 있나」이지 「무엇을
       * 남겼나」가 아니다 (2026-09-01 최종 검토에서 잡혔다 — 전 채널 시험 로그에 이미
       * 6채널이 `없음` 으로 찍혀 있었다). */
      oldestSeenTs: lastSeenTs,
      kept: blocks.length, seen,
    };
    month = null; batch = []; seen = 0; notes = []; lastSeenTs = null;
    return chunk;
  };

  for (const m of desc) {
    const mo = monthOf(m.ts, tz);
    if (since && mo < since) break;             // 'YYYY-MM' 은 문자열 비교로 시간 순서다
    if (month && mo !== month) {
      const chunk = await flush();
      if (chunk) yield chunk;
    }
    month = mo;
    seen += 1;
    lastSeenTs = m.ts;                          // 최신 → 과거라 마지막이 그 달의 가장 오래된 것
    /* **`push` 로 뒤에 넣는다 — 이 자리가 조용히 틀리기 쉽다.**
     * 실물 아카이브는 한 달 절 안에서 **최신이 위**다. `--at-month-end` 는 받은 묶음을
     * **재정렬 없이** 그 달 절의 끝에 이어 붙이므로, 묶음이 오래된 것부터면 붙는 구간만
     * 날짜가 거꾸로 읽힌다 — 에러가 안 나고 검색도 그대로 돼서 아무도 눈치채지 못한다.
     * `slack-sync/SKILL.md` 3.5단계도 같은 말을 적고 있다. */
    if (keepMessage(m, selfId)) batch.push(m);   // 최신이 앞
  }
  const last = await flush();
  if (last) yield last;
}

/**
 * 채널 md 가 아예 없을 때의 뼈대.
 *
 * **모양을 지어내지 않았다** — 실물(`slack-export/channels/*.md`)에서 그대로 옮겼다.
 * `src/archive.js` 가 이 모양을 파싱하고, `insert_messages.py` 는 경계를 셋으로 잡는다:
 * 본문 시작 `---`(139행) · `^## 참여 기록`(60행) · 그 앞의 `---`(168행 `find_body_end`).
 * **하나라도 빠지면** 「본문 시작 구분선을 찾지 못했습니다」로 죽거나, 더 나쁘게는
 * 메시지 블록이 하단 구분선 아래로 떨어져 **봇이 못 읽는 자리**에 들어간다.
 *
 * `기간`·`실제 메시지` 는 파생값이라 비워 둔다 — `sync_index.py` 가 다시 센다.
 * `핵심 쟁점` 은 사람이 채운다 (엔진은 요약을 쓰지 않는다).
 */
export function channelSkeleton({ name, isPrivate, channelId, today }) {
  return [
    `# #${name}${isPrivate ? '  🔒 비공개' : ''}`,
    '',
    // 워크스페이스 줄·채널 ID 줄은 2026-09-21 에 뺐다 (WHK 결정). 이 메타 블록은
    // **질문마다** 봇 시스템 프롬프트에 통째로 실리는데(`buildArchiveBriefSplit`),
    // 두 줄은 37채널에 같은 말이 반복돼 5,069자를 먹으면서 읽는 쪽이 0곳이었다 —
    // 워크스페이스는 `slack-export/index.md` 상단에 원본이 있고, 채널 ID↔이름은
    // `.sync-state.json` 이 원본이다(`listAllChannels`). 봇 답변이 채널 링크를
    // 인용한 적도 304회차 중 0건이었다. 링크가 필요하면 `readChannel` 로 md 를 연다.
    '> **기간**: (백필 중) · **실제 메시지**: 0건',
    '> **핵심 쟁점**: (사람이 채웁니다)',
    '',
    '---',
    '',
    '---',
    '',
    '## 참여 기록 (요약)',
    '',
    '(백필로 만든 채널 md 입니다.)',
    '',
    '---',
    '',
    `_출처: Slack #${name} · 백필 ${today}_`,
    '',
  ].join('\n');
}

const MISSING_MARK = /^>\s*(?:⚠️?\s*)?\*\*미수집 구간\*\*/;

/**
 * 「미수집 구간」 표식을 지운다.
 *
 * `verify_archive.py` 의 `FAIL_MARKS`(91행)에 그 문구가 있어서, 남겨 두면 `pre-commit` 이
 * **커밋을 막는다.** 아카이브가 스스로 「여기는 비었다」고 적은 상태로 굳는 것을 막으려는
 * 관문이고, 구간을 채웠으면 그 줄을 지우는 것이 정상 경로다.
 *
 * **낱말이 들었다고 지우지 않는다 — 마커 모양에 맞춘다.** 실물은 인용 줄에 굵은 글씨다
 * (`> ⚠️ **미수집 구간**: 2026-06-18 11:23 이전…`, 2026-08-07 에 4채널에서 지운 것들).
 * 낱말만 보고 지우면 사람이 대화에서 그 말을 쓴 줄이 **에러 없이 사라진다** — 지우는 쪽은
 * 알리는 쪽(`verify_archive.py`)보다 훨씬 비싸므로 여기서는 좁게 잡는다.
 * 모양이 다른데 낱말이 든 줄은 **지우지 말고 `unmatched` 로 올린다** — 안 그러면 관문은
 * 계속 막는데 화면에는 「지운 줄 0」만 보여서 왜 막히는지 알 수가 없다.
 */
export function stripMissingMarks(md) {
  const lines = md.split('\n');
  const kept = [];
  let removed = 0;
  const unmatched = [];
  for (const l of lines) {
    if (MISSING_MARK.test(l)) { removed += 1; continue; }
    // 낱말은 들었는데 마커 모양이 아닌 줄 — **지우지 않고 알린다.**
    if (l.includes('미수집 구간')) unmatched.push(l.trim().slice(0, 80));
    kept.push(l);
  }
  return { text: kept.join('\n'), removed, unmatched };
}

/**
 * 달 하나를 md 에 넣는다.
 *
 * **`--at-month-end` 로 그 달 절의 맨 뒤에 이어 붙인다.**
 * 백필이 넣는 것은 **경계보다 오래된 것뿐**이라(`archiveFloor`), 그 달에 이미 있는
 * 어떤 블록보다도 오래되고 서로는 최신부터 온다. 그래서 맨 뒤에 그대로 붙이면
 * 내림차순이 유지되고, **어디에 넣을지 계산할 것이 없다.**
 * `--in-order` 로 블록마다 자리를 찾게 했던 것은 「이미 찬 달의 중간」에 넣던 시절의
 * 장치인데, 그때 사람이 본문에 인용한 아카이브 헤더 줄이 부모에게서 떨어져 나가
 * **남의 이름 아래로 옮겨 붙는** 사고가 났다 (2026-09-02 검토자 둘이 재현).
 *
 * **실패하면 던진다.** 조용히 넘기면 그 달이 구멍인 채로 「채웠다」가 된다.
 *
 * **`added` 는 파이썬 말투가 아니라 헤더 수로 잰다.** `insert_block` 은 청크
 * **전부**가 이미 있을 때만 「이미 반영됨」을 돌려준다 — 청크 안에서 **일부만**
 * 이미 있으면 새것만 써서 성공하지만 반환 문구는 평범한 「OK 삽입」이다. 그 문구
 * 유무만 보고 「전부 새로 썼다 / 하나도 안 썼다」로 이진 판정하면 그 부분 성공을
 * 「청크 전체를 새로 썼다」로 과대 집계한다 — 이 함수가 있는 이유가 정확히
 * 「넣음」이 거짓말하지 않게 하는 것이라 그 구멍을 다시 열면 안 된다 (2026-09-02,
 * 이진 판정으로 시험했다가 부분 반영 경우를 못 잡는다고 지적받았다). 그래서
 * 넣기 전/후로 `countHeaders`(= `HEADER_RE`, 본문 흉내 줄을 막게 좁힌 바로 그
 * 정규식)로 md 의 헤더 수를 세어 차이를 낸다 — 파이썬이 뭐라고 말하든 md 에
 * 실제로 늘어난 헤더 수가 진실이다.
 *
 * @returns {Promise<{where:string, notes:string[], added:number}>} `notes` 는
 *   `insert_messages.py` 가 찍은 줄 전부다(「이미 반영됨」 포함 — 부르는 쪽이
 *   화면에 그대로 올린다). `added` 가 실제로 늘어난 헤더 수다.
 */
export async function writeMonth(mdPath, month, blocks) {
  /* **블록 사이는 빈 줄 하나다.** 자동 반영이 같은 자리를 `'\n\n'` 으로 잇는다
   * (`slack-archive.js`). `'\n'` 으로 이으면 파싱은 안 깨지지만 — `splitMessages` 는
   * 헤더 줄 전체 일치로 자르므로 — **백필한 구간만 빽빽해져** 기존 구간과 눈에 띄게
   * 다르고, 빈 구간을 채울 때는 한 파일 안에서 들쭉날쭉해진다 (2026-09-01 사본 시험). */
  const content = writeTemp(
    `backfill-${path.basename(mdPath, '.md')}-${month}.md`,
    blocks.join('\n\n') + '\n',
  );
  const before = countHeaders(mdPath);
  const r = await runScript(SKILL_SCRIPTS.insertMessages, [
    /* **`--at-month-end` 로 그 달 절의 맨 뒤에 이어 붙인다.**
     * 백필이 넣는 것은 **경계보다 오래된 것뿐**이라(`archiveFloor`), 그 달에 이미 있는
     * 어떤 블록보다도 오래되고 서로는 최신부터 온다. 그래서 맨 뒤에 그대로 붙이면
     * 내림차순이 유지되고, **어디에 넣을지 계산할 것이 없다.**
     * `--in-order` 로 블록마다 자리를 찾게 했던 것은 「이미 찬 달의 중간」에 넣던 시절의
     * 장치인데, 그때 사람이 본문에 인용한 아카이브 헤더 줄이 부모에게서 떨어져 나가
     * **남의 이름 아래로 옮겨 붙는** 사고가 났다 (2026-09-02 검토자 둘이 재현). */
    '--file', mdPath, '--month', month, '--at-month-end', '--content-file', content,
  ]);
  if (!r.ok) {
    throw new Error(`${month} 넣기 실패: ${r.stderr.trim() || r.stdout.trim()}`);
  }
  /* **파이썬이 찍은 줄을 버리지 않는다.** `insert_messages.py` 는 「이미 반영됨」처럼
   * 사람이 봐야 할 것을 화면에 찍는데, 여기서 `r.stdout` 을 버리면 그 신호가 어디에도
   * 안 닿는다 — 백필이 그 스크립트를 부르는 유일한 자리다 (2026-09-02 검토자 둘이
   * 함께 짚었다). */
  const notes = r.stdout.split('\n').map((s) => s.trim()).filter(Boolean);
  const added = countHeaders(mdPath) - before;
  return { where: notes[notes.length - 1] || '', notes, added };
}

/**
 * 백필 진행 상태. **`.sync-state.json` 에 섞지 않는다** — 그 파일은 매일 회차마다
 * 통째로 다시 쓰이므로 여기 적은 것이 지워진다.
 *
 * 이 파일은 저장소에 안 들어간다(자료 저장소 `.gitignore` + 씨앗 `gitignore.tpl`).
 * 이 기계의 진행 상황이지 아카이브의 내용이 아니다.
 */
export const BACKFILL_STATE = path.join(ARCHIVE_DIR, '.backfill-state.json');

export function loadBackfillState() {
  return readJson(BACKFILL_STATE, { channels: {} });
}

export function saveBackfillState(state) {
  writeJson(BACKFILL_STATE, state);
}

/**
 * 달 하나를 **md 에 넣은 뒤** 상태를 한 칸 내린다.
 *
 * 순서가 요점이다 — 넣기 전에 내리면 중간에 죽었을 때 그 달을 영영 건너뛴다.
 * 넣은 뒤에 내리면 최악이라도 같은 달을 한 번 더 하는 것이고,
 * `insert_messages.py` 는 이미 있는 것을 「이미 반영됨」으로 건너뛴다.
 *
 * 새 객체를 돌려준다 — 부르는 쪽이 실패했을 때 옛 상태로 되돌릴 수 있어야 한다.
 */
export function advance(prev, { month, oldestTs, oldestSeenTs }) {
  /* **`null` 을 그대로 내려보내지 않는다.** 한 달이 통째로 걸러지면 `oldestTs` 가 `null` 인데,
   * 그걸 `latest` 에 넣으면 다음 조회가 기준점을 잃어 위에서부터 다시 훑고, `oldest` 에 넣으면
   * 「가장 오래된 대화」 표가 멀쩡히 채운 채널에 `없음` 을 찍는다. 둘 다 에러가 안 난다.
   * 두 물음의 답은 **거르기 전에 본 것**이다 (2026-09-01 최종 검토). */
  const seen = oldestSeenTs ?? oldestTs;
  return {
    ...prev,
    months: [...prev.months, month],
    latest: seen ?? prev.latest,
    oldest: seen ?? prev.oldest,
  };
}
