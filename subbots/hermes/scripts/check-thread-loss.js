#!/usr/bin/env node
/**
 * 「스레드 답글이 조용히 사라지는 자리」 검사 — 슬랙에 붙지 않는다. 가짜 클라이언트로 돈다.
 *
 *   node scripts/check-thread-loss.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 *
 * ── 무엇을 재나 ──
 *
 * 넷 다 **에러 없이 조용히 틀리는** 종류다. 답글이 안 들어갔는데 상태·보고·md 는
 * 전부 정상으로 보인다.
 *
 *   ① 답글 줄·본문 블록이 반응(이모지)을 **안 싣나**    `renderReply`·`renderMessage`
 *   ② 스레드 읽기가 실패한 회차가 상태를 오염시키지 않고  `ingestChannel` ①③
 *      다음 회차에 **다시 읽나**
 *   ③ 「덧붙인 답글 N건」이 제안한 수가 아니라            `ingestChannel` ④-b
 *      **md 에 실제로 늘어난 수**인가
 *   ④ 백필이 스레드를 못 읽으면 md 에 흔적이 남고 셈이 되나 `monthChunks`
 *
 * **실물 아카이브를 안 건드린다** — `HERMES_DATA_ROOT` 를 임시 폴더로 돌려놓고
 * 그 아래에 가짜 자료 저장소를 만들어 돈다. 그래서 `src/config.js` 를 정적으로
 * import 하면 안 되고(모듈 최상위가 먼저 돌아 실물 경로가 굳는다) 아래에서
 * `await import()` 로 늦게 가져온다.
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

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-thread-loss-'));
const DATA = path.join(TMP, 'data');
fs.mkdirSync(path.join(DATA, 'slack-export', 'channels'), { recursive: true });
fs.writeFileSync(
  path.join(DATA, 'config.json'),
  fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf8'),
  'utf8',
);
process.env.HERMES_DATA_ROOT = DATA;

const { config, CHANNELS_DIR } = await import('../src/config.js');
const { stamp } = await import('../src/slack-live.js');
const {
  renderMessage, ingestChannel, parseBlocks,
} = await import('../src/ingest/slack-archive.js');
const { monthChunks } = await import('../src/ingest/backfill.js');

const TZ = config.timezone;
const userMap = new Map([['U1', '김철수'], ['U2', '박민수']]);
const SELF = 'UBOT';

/* ── 가짜 슬랙 ────────────────────────────────────────────────────
 * 채널 하나의 메시지 목록과 스레드 답글을 들고, `conversations.history` 의
 * oldest/latest 를 실제와 같은 뜻으로 거른다 (oldest 는 배타, latest 는 포함). */
function fakeSlack({ messages, replies = {}, failReplies = new Set() }) {
  const calls = { replies: [] };
  return {
    calls,
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
      replies: async ({ ts }) => {
        calls.replies.push(ts);
        if (failReplies.has(ts)) {
          const err = new Error('thread_not_found');
          err.data = { error: 'thread_not_found' };
          throw err;
        }
        const parent = messages.find((m) => m.ts === ts);
        return { messages: [parent, ...(replies[ts] || [])], response_metadata: {} };
      },
    },
  };
}

/** 'YYYY-MM-DD HH:MM'(KST) → epoch 초 문자열. 되짚어 확인한다. */
function ts(minute) {
  const asUtc = Math.floor(Date.parse(`${minute.replace(' ', 'T')}:00Z`) / 1000);
  const back = Math.floor(Date.parse(`${stamp(asUtc, TZ).replace(' ', 'T')}:00Z`) / 1000);
  const epoch = asUtc - (back - asUtc);
  if (stamp(epoch, TZ) !== minute) throw new Error(`시각을 되짚지 못했습니다: ${minute}`);
  return String(epoch);
}

/** 채널 md 뼈대 — 실물 모양 그대로 (insert_messages.py 가 경계를 셋으로 잡는다) */
function writeChannelMd(name, body = '') {
  const p = path.join(CHANNELS_DIR, `${name}.md`);
  fs.writeFileSync(p, [
    `# #${name}`,
    '',
    '> **기간**: 2026-06-01 ~ 2026-06-30 · **실제 메시지**: 0건',
    '',
    '---',
    '',
    body,
    '',
    '---',
    '',
    '## 참여 기록 (요약)',
    '',
    '(검사용)',
    '',
  ].join('\n'), 'utf8');
  return p;
}

/* ── ① renderReply·renderMessage — 반응을 안 싣나 (건2) ─────────────── */
section('① 반응(이모지) — 아카이브에 안 넣는다');

{
  const parent = { user: 'U1', ts: ts('2026-06-15 10:00'), text: '부모 글', reply_count: 1 };
  const reply = {
    user: 'U2', ts: ts('2026-06-15 10:05'), text: '답글 본문',
    reactions: [{ name: 'white_check_mark', count: 1 }],
  };
  const md = renderMessage(parent, [reply], userMap, TZ, SELF);
  const line = md.split('\n').find((l) => l.startsWith('> **└'));

  /* **반응은 아카이브에 넣지 않는다** (WHK 결정 2026-09-03). 값어치가 없고, 나중에 붙는
   * 반응이 아카이브와 슬랙을 영영 어긋나게 한다 — 반응이 붙어도 슬랙은 `edited` 를 안 달아
   * 수정 감지에 안 걸리고, 그래서 2026-09-01 원인 조사에서 17건이 「안 닫힌다」로 갈렸다.
   * 되살려도 **에러가 안 난다** — md 한 줄이 늘 뿐이라 사람 눈에도 안 띈다. 그래서 검사가 막는다. */
  check('반응이 있어도 답글 줄에 안 실린다',
    line === '> **└ 2026-06-15 10:05 · 박민수** — 답글 본문',
    `실제: ${line}`);

  const both = renderMessage(parent, [{
    user: 'U2', ts: ts('2026-06-15 10:05'), text: '답글 본문',
    reactions: [{ name: 'o', count: 2 }], files: [{ name: '자료.pdf' }],
  }], userMap, TZ, SELF);
  check('반응을 뺐어도 첨부는 그대로 실린다',
    both.includes('— 답글 본문 📎 첨부: `자료.pdf`'),
    `실제: ${both.split('\n').find((l) => l.startsWith('> **└'))}`);

  const body = renderMessage(
    { user: 'U1', ts: ts('2026-06-15 10:00'), text: '부모 글', reactions: [{ name: 'tada', count: 3 }] },
    [], userMap, TZ, SELF);
  check('본문 블록에도 반응 줄이 안 생긴다',
    !body.includes('tada'),
    `실제: ${body}`);

  // 답글 줄은 여전히 답글로 읽혀야 한다
  const blocks = parseBlocks(md);
  check('parseBlocks 가 답글 한 건으로 센다',
    blocks.length === 1 && blocks[0].replies.length === 1,
    `블록 ${blocks.length}개 · 답글 ${blocks[0]?.replies.length}건`);
}

/* ── ② 스레드 읽기 실패 — 상태 오염과 재시도 (건1① · 건3②) ────────── */
section('② 스레드 읽기 실패');

{
  const CH = { id: 'C1', name: '검사가', isPrivate: false };
  writeChannelMd('검사가');

  const parentTs = ts('2026-06-15 10:00');
  const olderTs = ts('2026-06-14 09:00');
  const messages = [
    { user: 'U1', ts: olderTs, text: '앞선 글' },
    { user: 'U1', ts: parentTs, text: '부모 글', reply_count: 2, latest_reply: ts('2026-06-15 10:10') },
  ];
  const replies = {
    [parentTs]: [
      { user: 'U2', ts: ts('2026-06-15 10:05'), text: '첫 답글' },
      { user: 'U2', ts: ts('2026-06-15 10:10'), text: '[정정] 둘째 답글' },
    ],
  };

  const state = {
    channels: { C1: { name: '검사가', file: '검사가', last_ts: ts('2026-06-13 00:00'), threads: {} } },
  };
  const opts = {
    userMap, selfId: SELF, tz: TZ, dry: false, editedSince: 0,
    pendingKeys: new Set(), scanEdits: false,
  };

  // 1회차 — 답글 읽기가 터진다
  const failing = fakeSlack({ messages, replies, failReplies: new Set([parentTs]) });
  const r1 = await ingestChannel(failing, CH, state, opts);
  check('실패를 보고에 적는다', (r1.notes || []).some((n) => n.includes('스레드 읽기 실패')),
    JSON.stringify(r1.notes));

  const after1 = state.channels.C1;
  /* **여기가 오염 자리다.** md 에는 답글이 한 건도 안 들어갔는데 상태에 「답글 2건 반영됨」이
   * 적히면, 다음 회차가 knownCount 를 보고 「이미 다 담았다」로 건너뛴다. */
  check('못 읽은 스레드를 「반영됨」으로 적지 않는다',
    (after1.threads?.[parentTs]?.reply_count ?? 0) === 0,
    `상태: ${JSON.stringify(after1.threads?.[parentTs])}`);
  check('다시 읽어야 할 스레드로 표시한다',
    after1.threads?.[parentTs]?.unread === true,
    `상태: ${JSON.stringify(after1.threads?.[parentTs])}`);

  const md1 = fs.readFileSync(path.join(CHANNELS_DIR, '검사가.md'), 'utf8');
  check('부모 글은 md 에 들어갔다', md1.includes('부모 글'));
  check('답글은 아직 없다', !md1.includes('첫 답글'));

  // 2회차 — 이번에는 읽힌다. 같은 상태 객체로 이어서 돈다.
  const good = fakeSlack({ messages, replies });
  const r2 = await ingestChannel(good, CH, state, opts);
  const md2 = fs.readFileSync(path.join(CHANNELS_DIR, '검사가.md'), 'utf8');
  check('다음 회차가 그 스레드를 다시 읽는다', good.calls.replies.includes(parentTs),
    `읽은 스레드: ${JSON.stringify(good.calls.replies)}`);
  check('못 들어갔던 답글이 이번에 들어간다',
    md2.includes('첫 답글') && md2.includes('[정정] 둘째 답글'));
  check('부모 블록이 두 벌로 갈라지지 않았다',
    md2.split('부모 글').length - 1 === 1,
    `「부모 글」 ${md2.split('부모 글').length - 1}번 나옴`);
  check('덧붙인 답글 수를 2건으로 보고한다', (r2.threadReplies || 0) === 2,
    `threadReplies=${r2.threadReplies}`);
  check('다시 읽었으니 표시가 지워진다',
    !state.channels.C1.threads?.[parentTs]?.unread
    && state.channels.C1.threads?.[parentTs]?.reply_count === 2,
    `상태: ${JSON.stringify(state.channels.C1.threads?.[parentTs])}`);
}

{
  /* **②-b `grownThreads` 가 절대 못 잡는 자리.**
   *
   * 위 ② 는 실패한 부모가 그 회차의 **마지막** 메시지라, 다음 회차에도
   * `latest_reply > last_ts` 가 우연히 성립해 `grownThreads` 가 집어 준다. 부모 뒤에
   * 다른 메시지가 하나만 더 있으면 `last_ts` 가 그것으로 전진해 그 조건이 깨진다 —
   * 그러면 되돌아보기 구간 안에 있는데도 아무 데도 안 걸려 **영영 안 들어온다.**
   * 여기를 막는 것은 `unread` 표시뿐이다. */
  const CH = { id: 'C4', name: '검사라', isPrivate: false };
  writeChannelMd('검사라');

  const parentTs = ts('2026-06-15 10:00');
  const laterTs = ts('2026-06-15 11:00');
  const messages = [
    { user: 'U1', ts: parentTs, text: '뒤에 글이 더 있는 부모', reply_count: 1, latest_reply: ts('2026-06-15 10:10') },
    { user: 'U1', ts: laterTs, text: '더 나중 글' },
  ];
  const replies = { [parentTs]: [{ user: 'U2', ts: ts('2026-06-15 10:10'), text: '늦게 구한 답글' }] };
  const state = {
    channels: { C4: { name: '검사라', file: '검사라', last_ts: ts('2026-06-13 00:00'), threads: {} } },
  };
  const opts = {
    userMap, selfId: SELF, tz: TZ, dry: false, editedSince: 0,
    pendingKeys: new Set(), scanEdits: false,
  };

  await ingestChannel(fakeSlack({ messages, replies, failReplies: new Set([parentTs]) }), CH, state, opts);
  check('실패한 뒤에도 last_ts 는 전진한다 (붙잡지 않는다)',
    state.channels.C4.last_ts === laterTs, `last_ts=${state.channels.C4.last_ts}`);

  const good = fakeSlack({ messages, replies });
  const r2 = await ingestChannel(good, CH, state, opts);
  check('전진했어도 표시를 보고 다시 읽는다 (grownThreads 로는 못 잡는 자리)',
    good.calls.replies.includes(parentTs), `읽은 스레드: ${JSON.stringify(good.calls.replies)}`);
  const md = fs.readFileSync(path.join(CHANNELS_DIR, '검사라.md'), 'utf8');
  check('그 답글이 부모 블록 안에 들어간다', md.includes('늦게 구한 답글'));
  check('덧붙인 수를 1건으로 보고한다', (r2.threadReplies || 0) === 1,
    `threadReplies=${r2.threadReplies}`);
}

/* ── ③ 덧붙인 개수 — 제안한 수가 아니라 실제로 늘어난 수 (건1②) ────── */
section('③ 덧붙인 답글 개수');

{
  const CH = { id: 'C2', name: '검사나', isPrivate: false };
  const parentTs = ts('2026-06-15 11:00');
  const parentHead = `**${stamp(parentTs, TZ)} · 김철수**`;
  const r1Ts = ts('2026-06-15 11:05');
  const r2Ts = ts('2026-06-15 11:10');

  /* 부모 블록과 **첫 답글은 이미 md 에 있다** — 사람이 먼저 손으로 넣어 둔 경우이고,
   * 상태에는 그 사실이 없다(knownCount 0). 그래서 자동 반영은 답글 2건을 제안하지만
   * 실제로 md 에 늘어나는 것은 1건이다. */
  writeChannelMd('검사나', [
    '## 2026-06',
    '',
    parentHead,
    '부모 글',
    '',
    '> 💬 **스레드 (1)**',
    `> **└ ${stamp(r1Ts, TZ)} · 박민수** — 첫 답글`,
  ].join('\n'));

  const messages = [
    { user: 'U1', ts: parentTs, text: '부모 글', reply_count: 2, latest_reply: r2Ts },
  ];
  const replies = {
    [parentTs]: [
      { user: 'U2', ts: r1Ts, text: '첫 답글' },
      { user: 'U2', ts: r2Ts, text: '둘째 답글' },
    ],
  };
  const state = {
    channels: { C2: { name: '검사나', file: '검사나', last_ts: parentTs, threads: {} } },
  };
  const client = fakeSlack({ messages, replies });
  const r = await ingestChannel(client, CH, state, {
    userMap, selfId: SELF, tz: TZ, dry: false, editedSince: 0,
    pendingKeys: new Set(), scanEdits: false,
  });

  const md = fs.readFileSync(path.join(CHANNELS_DIR, '검사나.md'), 'utf8');
  check('둘째 답글만 새로 들어갔다',
    md.split('첫 답글').length - 1 === 1 && md.includes('둘째 답글'));
  check('덧붙인 수를 1건으로 센다 (제안한 2건이 아니라)',
    (r.threadReplies || 0) === 1, `threadReplies=${r.threadReplies}`);

  // 한 번 더 돌리면 늘어나는 것이 없으므로 0 이어야 한다
  const again = await ingestChannel(fakeSlack({ messages, replies }), CH, state, {
    userMap, selfId: SELF, tz: TZ, dry: false, editedSince: 0,
    pendingKeys: new Set(), scanEdits: false,
  });
  check('전부 이미 있으면 0건', (again.threadReplies || 0) === 0,
    `threadReplies=${again.threadReplies}`);
}

/* ── ④ 백필 — 스레드를 못 읽으면 md 에 흔적이 남고 셈이 된다 (건4) ─── */
section('④ 백필의 스레드 읽기 실패');

{
  const parentTs = ts('2026-03-15 10:00');
  const messages = [{ user: 'U1', ts: parentTs, text: '옛 부모 글', reply_count: 3 }];
  const client = fakeSlack({ messages, failReplies: new Set([parentTs]) });

  const chunks = [];
  for await (const c of monthChunks(client, 'C3', {
    latest: ts('2026-09-01 00:00'), tz: TZ, selfId: SELF, userMap,
  })) chunks.push(c);

  check('청크가 하나 나온다', chunks.length === 1, `${chunks.length}개`);
  const c = chunks[0];
  check('실패를 notes 에 적는다', (c.notes || []).some((n) => n.includes('스레드 읽기 실패')));
  check('못 읽은 스레드 수를 센다 (마지막 요약이 쓸 값)', c.missedThreads === 1,
    `missedThreads=${c.missedThreads}`);

  const block = c.blocks[0] || '';
  check('md 블록에 흔적이 남는다', block.includes('💬 스레드 3건 (읽지 못함 — 미수록)'),
    `실제:\n${block}`);

  /* **기존 파서들이 그 줄을 어떻게 읽나** — 흔적을 남기려다 파서를 깨면 안 된다. */
  const blocks = parseBlocks(block);
  check('parseBlocks 가 답글로 세지 않는다',
    blocks.length === 1 && blocks[0].replies.length === 0,
    `답글 ${blocks[0]?.replies.length}건`);
  check('parseBlocks 의 본문이 마커를 안 삼킨다',
    blocks[0].body === '옛 부모 글', `본문: ${JSON.stringify(blocks[0].body)}`);

  /* `verify_archive.py` 의 실패 표식 목록(FAIL_MARKS)에 걸리면 **pre-commit 이 커밋을
   * 막는다.** 몇 시간짜리 백필 결과가 통째로 커밋 불가가 되고, 백필은 경계 아래만
   * 읽으므로 다시 돌려도 그 스레드를 안 읽는다 — 사람이 손으로 풀 길밖에 없다.
   * 그래서 이 마커는 그 목록의 어느 낱말도 쓰지 않는다. */
  const marks = fs.readFileSync(
    path.join(ROOT, '.claude', 'skills', 'slack-sync', 'scripts', 'verify_archive.py'), 'utf8',
  ).match(/FAIL_MARKS = \[(.*?)\]/s)[1].match(/"([^"]+)"/g).map((s) => s.slice(1, -1));
  check('verify_archive 의 실패 표식 낱말을 안 쓴다',
    !marks.some((w) => block.includes(w)), `표식: ${marks.join(', ')}`);

  /* 스레드 머리줄(`> 💬 **스레드 (N)**`)로 읽히면 안 된다 — 그러면 검사 ① 이
   * 「머리줄 N건인데 답글 0개」로 커밋을 막고, `insert_messages.py` 가 나중에
   * 답글을 덧붙일 때 그 숫자를 올린다. */
  check('스레드 머리줄 모양이 아니다',
    !/^>\s*💬\s*\*\*스레드\s*\(\d+\)/m.test(block));
  check('봇 답변 표식 모양이 아니다', !/^>\s*💬\s*스레드\s*\d+건\s*\(봇/m.test(block));

  // 잘 읽힌 회차는 마커가 없어야 한다 (대조군)
  const okClient = fakeSlack({
    messages, replies: { [parentTs]: [{ user: 'U2', ts: ts('2026-03-15 10:05'), text: '답글' }] },
  });
  const okChunks = [];
  for await (const c2 of monthChunks(okClient, 'C3', {
    latest: ts('2026-09-01 00:00'), tz: TZ, selfId: SELF, userMap,
  })) okChunks.push(c2);
  check('읽히면 마커가 안 붙는다', !okChunks[0].blocks[0].includes('읽지 못함'));
  check('읽히면 못 읽은 수가 0', okChunks[0].missedThreads === 0,
    `missedThreads=${okChunks[0].missedThreads}`);
}

fs.rmSync(TMP, { recursive: true, force: true });

console.log(ok ? '\n전부 통과' : '\n실패 있음');
process.exitCode = ok ? 0 : 1;
