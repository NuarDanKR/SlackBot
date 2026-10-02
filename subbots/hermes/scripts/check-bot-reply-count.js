#!/usr/bin/env node
/**
 * 「봇 글에 달린 답글」을 **제안한 개수가 아니라 실제로 쓴 개수**로 세나 — 가짜 슬랙으로 돈다.
 *
 *   node scripts/check-bot-reply-count.js
 *   (`npm run check` 는 `check-archive-contract.js` [8/8] 이 이 파일을 불러서 함께 돈다)
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 *
 * ── 무엇을 재나 ──
 *
 * `ingestChannel` 의 ④-c(`selfCorrections`·`botReplies`) 갈래다. 일반 답글 쪽(④-b)은
 * 커밋 `3d61ef0` 이 「md 를 파이썬 전후로 세는」 방식으로 막았는데 이 갈래는 그 진단을
 * 못 받아서, `insert_messages.py` 가 「이미 반영됨」을 안 말하기만 하면 **넘긴 줄 전부**를
 * 센 채로 DM 에 나갔다.
 *
 * 여기가 ④-b 보다 나쁜 이유: 이 갈래는 개수만이 아니라 **어느 정정 문장이 쓰였는지 목록까지**
 * DM 에 싣는다(`selfFixLines`). 그래서 부풀면 **쓰이지도 않은 정정 문장**이 사람 앞에 나오고,
 * 사람은 그것을 「오늘 반영된 정정」으로 읽는다. 개수를 md 로 다시 세는 것만으로는 그 목록을
 * 못 만든다 — 그래서 `insert_messages.py --report` 가 **무엇을 썼는지**를 돌려주고
 * `writtenFrom` 이 그것을 읽는다 (WHK 결정 2026-09-03, ㉰안).
 *
 * **부풀림이 나는 입력이 무엇인지가 요점이다.** `.sync-state.json` 에 그 스레드 기록이 없으면
 * `knownCount` 가 0 이라 **이미 md 에 있는 답글까지 전부 새것으로 제안된다**(그건 설계다 —
 * 중복은 파이썬이 거른다). 그때 파이썬은 하나만 쓰고 평범한 문구를 내므로, 문구로 이진
 * 판정하면 그 회차가 「정정 2건」이 된다. 아래 ①·② 가 정확히 그 입력이다.
 *
 * **실물 아카이브를 안 건드린다** — `HERMES_DATA_ROOT` 를 임시 폴더로 돌려놓고 그 아래
 * 가짜 자료 저장소를 만든다. 그래서 `src/config.js` 를 정적으로 import 하면 안 되고
 * (모듈 최상위가 먼저 돌아 실물 경로가 굳는다) 아래에서 `await import()` 로 늦게 가져온다.
 * `check-thread-loss.js` 와 같은 방식이다.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

let ok = true;
const check = (label, cond, extra = '') => {
  ok &&= !!cond;
  console.log((cond ? '  PASS ' : '  FAIL ') + label + (extra ? `  ${extra}` : ''));
};

let SECTION = '(시작 전)';
const section = (name) => { SECTION = name; console.log(`\n${name}`); };
const died = (err) => {
  console.log(`  FAIL ${SECTION} 이 예외로 멈췄습니다  ${err?.stack || err}`);
  console.log('\n실패 있음 — 위 절에서 멈춰 뒤 절은 돌지 않았습니다');
  process.exit(1);
};
process.on('unhandledRejection', died);
process.on('uncaughtException', died);

/* ── 가짜 자료 저장소 ─────────────────────────────────────────────── */

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-bot-reply-'));
const DATA = path.join(TMP, 'data');
fs.mkdirSync(path.join(DATA, 'slack-export', 'channels'), { recursive: true });
fs.writeFileSync(
  path.join(DATA, 'config.json'),
  fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf8'),
  'utf8',
);
process.env.HERMES_DATA_ROOT = DATA;

const { config, CHANNELS_DIR } = await import('../src/config.js');
const { stamp, BOT_AUTHOR_LABEL, BOT_BLOCK_BODY } = await import('../src/slack-live.js');
const { ingestChannel } = await import('../src/ingest/slack-archive.js');

const TZ = config.timezone;
const userMap = new Map([['U1', '김철수'], ['U2', '박민수']]);
const SELF = 'UBOT';

/** 채널 하나의 메시지와 스레드 답글을 들고 있는 가짜 슬랙 (check-thread-loss.js 와 같은 모양) */
function fakeSlack({ messages, replies = {} }) {
  return {
    conversations: {
      history: async ({ oldest, latest, inclusive }) => {
        let out = messages.slice();
        if (oldest !== undefined) {
          out = out.filter((m) => (inclusive ? Number(m.ts) >= Number(oldest) : Number(m.ts) > Number(oldest)));
        }
        if (latest !== undefined) out = out.filter((m) => Number(m.ts) <= Number(latest));
        out.sort((a, b) => Number(b.ts) - Number(a.ts));   // 슬랙은 최신부터 준다
        return { messages: out, response_metadata: {} };
      },
      replies: async ({ ts: t }) => {
        const parent = messages.find((m) => m.ts === t);
        return { messages: [parent, ...(replies[t] || [])], response_metadata: {} };
      },
    },
  };
}

/** 'YYYY-MM-DD HH:MM'(설정 시간대) → epoch 초 문자열. 되짚어 확인한다. */
function ts(minute) {
  const asUtc = Math.floor(Date.parse(`${minute.replace(' ', 'T')}:00Z`) / 1000);
  const back = Math.floor(Date.parse(`${stamp(asUtc, TZ).replace(' ', 'T')}:00Z`) / 1000);
  const epoch = asUtc - (back - asUtc);
  if (stamp(epoch, TZ) !== minute) throw new Error(`시각을 되짚지 못했습니다: ${minute}`);
  return String(epoch);
}

function writeChannelMd(name, body = '') {
  const p = path.join(CHANNELS_DIR, `${name}.md`);
  fs.writeFileSync(p, [
    `# #${name}`, '',
    '> **기간**: 2026-06-01 ~ 2026-06-30 · **실제 메시지**: 0건', '',
    '---', '',
    body, '',
    '---', '',
    '## 참여 기록 (요약)', '', '(검사용)', '',
  ].join('\n'), 'utf8');
  return p;
}

const OPTS = {
  userMap, selfId: SELF, tz: TZ, dry: false, editedSince: 0,
  pendingKeys: new Set(), scanEdits: false,
};

/**
 * 「자리표시 블록이 이미 있고, 그 안의 답글 하나가 이미 들어가 있는」 md.
 *
 * 모양은 `renderBotPlaceholder`·`renderReply` 가 내는 것과 같아야 한다 — 파이썬의 중복
 * 판정이 `시각 · 이름` 으로 맞추므로 거기가 어긋나면 이 검사가 「둘 다 새것」으로 흘러
 * 부풀림을 재현하지 못한다. (그래서 아래 ①-a 가 그 재현부터 확인한다.)
 */
function placeholderMd(parentTs, replyTs, name, text) {
  return [
    `**${stamp(parentTs, TZ)} · ${BOT_AUTHOR_LABEL}**`,
    BOT_BLOCK_BODY,
    '',
    '> 💬 **스레드 (1)**',
    `> **└ ${stamp(replyTs, TZ)} · ${name}** — ${text}`,
  ].join('\n');
}

/* ── ① Hermes 자기 글에 달린 `[정정]` ─────────────────────────────── */
section('① Hermes 글에 달린 정정 — 이미 있던 것까지 세지 않나');

{
  const parentTs = ts('2026-06-15 10:00');
  const r1 = ts('2026-06-15 10:05');
  const r2 = ts('2026-06-15 10:09');
  writeChannelMd('검사가', placeholderMd(parentTs, r1, '김철수', '[정정] 첫 정정 — 금액은 100억'));

  const messages = [{
    user: SELF, bot_id: 'B1', ts: parentTs, text: '봇 요약',
    reply_count: 2, latest_reply: r2,
  }];
  const replies = {
    [parentTs]: [
      { user: 'U1', ts: r1, text: '[정정] 첫 정정 — 금액은 100억' },
      { user: 'U2', ts: r2, text: '[정정] 둘째 정정 — 만기는 9/30' },
    ],
  };
  // `threads` 가 비어 있는 것이 요점이다 — knownCount 0 이라 둘 다 새것으로 제안된다.
  const state = {
    channels: { C1: { name: '검사가', file: '검사가', last_ts: parentTs, threads: {} } },
  };

  const r = await ingestChannel(
    fakeSlack({ messages, replies }), { id: 'C1', name: '검사가', isPrivate: false }, state, OPTS,
  );

  const md = fs.readFileSync(path.join(CHANNELS_DIR, '검사가.md'), 'utf8');
  // ①-a 부풀림이 나는 입력인지부터 확인한다. 제안이 1건뿐이면 아래 판정은 헛돈다.
  check('①-a 둘 다 새것으로 제안된 회차다 (부풀림이 날 수 있는 입력)',
    md.includes('첫 정정') && md.includes('둘째 정정'));
  check('①-b 첫 정정이 두 벌로 안 쌓였다',
    md.split('첫 정정').length - 1 === 1, `${md.split('첫 정정').length - 1}벌`);
  check('①-c 정정을 1건으로 센다 (제안한 2건이 아니라)',
    r.selfCorrections === 1, `selfCorrections=${r.selfCorrections}`);
  check('①-d 문장 목록에 이번에 들어간 것만 있다',
    (r.selfFixLines || []).length === 1
      && r.selfFixLines[0].includes('둘째 정정')
      && !r.selfFixLines.some((l) => l.includes('첫 정정')),
    JSON.stringify((r.selfFixLines || []).map((l) => l.slice(-20))));
  check('①-e 관문 프로브도 이번에 쓴 줄을 가리킨다',
    (r.replyProbes || []).length === 1 && r.replyProbes[0].reply.includes('둘째 정정'));

  // 한 번 더 돌리면 새로 들어갈 것이 없다. `threads` 를 다시 비워 같은 입력을 만든다.
  state.channels.C1.threads = {};
  const again = await ingestChannel(
    fakeSlack({ messages, replies }), { id: 'C1', name: '검사가', isPrivate: false }, state, OPTS,
  );
  check('①-f 전부 이미 있으면 0건이고 문장 목록도 비어 있다',
    (again.selfCorrections || 0) === 0 && !(again.selfFixLines || []).length,
    `selfCorrections=${again.selfCorrections}`);
  check('①-g 안 쓴 회차는 관문 프로브도 안 만든다', !(again.replyProbes || []).length);
}

/* ── ② 다른 봇 글에 달린 사람 답글 ────────────────────────────────── */
section('② 다른 봇 글에 달린 사람 답글 — 같은 규칙으로 세나');

{
  const parentTs = ts('2026-06-16 09:00');
  const r1 = ts('2026-06-16 09:10');
  const r2 = ts('2026-06-16 09:20');
  writeChannelMd('검사나', placeholderMd(parentTs, r1, '김철수', '이미 들어간 답글'));

  const messages = [{
    user: 'UOTHER', bot_id: 'B2', ts: parentTs, text: '주간 체크인',
    reply_count: 2, latest_reply: r2,
  }];
  const replies = {
    [parentTs]: [
      { user: 'U1', ts: r1, text: '이미 들어간 답글' },
      { user: 'U2', ts: r2, text: '2시로 변경합니다' },
    ],
  };
  const state = {
    channels: { C2: { name: '검사나', file: '검사나', last_ts: parentTs, threads: {} } },
  };

  const r = await ingestChannel(
    fakeSlack({ messages, replies }), { id: 'C2', name: '검사나', isPrivate: false }, state, OPTS,
  );

  check('②-a 답글을 1건으로 센다 (제안한 2건이 아니라)',
    r.botReplies === 1, `botReplies=${r.botReplies}`);
  check('②-b 다른 봇 것은 정정 칸에 안 들어간다',
    !r.selfCorrections && !(r.selfFixLines || []).length);
}

/* ── ③ 자리표시 블록을 새로 만드는 회차 — 과소계상하지 않나 ───────── */
section('③ 자리표시 블록을 새로 만드는 회차 — 빠뜨리지 않나');

{
  const parentTs = ts('2026-06-17 11:00');
  const r1 = ts('2026-06-17 11:05');
  writeChannelMd('검사다', '## 2026-06\n');   // 블록이 아예 없다

  const messages = [{
    user: SELF, bot_id: 'B1', ts: parentTs, text: '봇 요약',
    reply_count: 1, latest_reply: r1,
  }];
  const replies = { [parentTs]: [{ user: 'U1', ts: r1, text: '[정정] 단가는 8만원' }] };
  const state = {
    channels: { C3: { name: '검사다', file: '검사다', last_ts: parentTs, threads: {} } },
  };

  const r = await ingestChannel(
    fakeSlack({ messages, replies }), { id: 'C3', name: '검사다', isPrivate: false }, state, OPTS,
  );

  const md = fs.readFileSync(path.join(CHANNELS_DIR, '검사다.md'), 'utf8');
  check('③-a 자리표시 블록이 생기고 정정이 그 안에 들어갔다',
    md.includes(BOT_BLOCK_BODY) && md.includes('단가는 8만원'));
  check('③-b 새 블록에 넣은 것도 1건으로 센다 (0 으로 안 샌다)',
    r.selfCorrections === 1, `selfCorrections=${r.selfCorrections}`);
  check('③-c 문장 목록에도 실린다',
    (r.selfFixLines || []).length === 1 && r.selfFixLines[0].includes('단가는 8만원'));
}

fs.rmSync(TMP, { recursive: true, force: true });

if (ok) {
  console.log('\n[check-bot-reply-count] OK — 봇 글에 달린 답글을 실제로 쓴 만큼만 세고,'
    + ' 정정 문장 목록도 이번에 들어간 것만 싣습니다.');
} else {
  console.error('\n실패 있음 — src/ingest/slack-archive.js 의 ④-c 와'
    + ' .claude/skills/slack-sync/scripts/insert_messages.py 의 --report 를 보세요.');
  process.exitCode = 1;
}
