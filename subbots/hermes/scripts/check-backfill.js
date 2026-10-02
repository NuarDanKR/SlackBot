#!/usr/bin/env node
/**
 * 백필 엔진 검사 — 슬랙에 붙지 않는다. 가짜 클라이언트로 돈다.
 *
 *   node scripts/check-backfill.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { keepMessage, stamp, SKIP_SUBTYPES } from '../src/slack-live.js';
import { fetchRange, monthChunks } from '../src/ingest/backfill.js';
import { channelSkeleton, stripMissingMarks } from '../src/ingest/backfill.js';
import { advance } from '../src/ingest/backfill.js';
import { archiveFloor, minuteToEpoch } from '../src/ingest/backfill.js';
import { writeMonth } from '../src/ingest/backfill.js';
import { backfillTargets, mdFileFor } from '../src/ingest/backfill.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

let ok = true;
const check = (label, cond, extra = '') => {
  ok &&= !!cond;
  console.log((cond ? '  PASS ' : '  FAIL ') + label + (extra ? `  ${extra}` : ''));
};

/* **예외로 죽으면 어느 절에서 죽었는지 남긴다.**
 * 절들이 최상위에서 `await` 하므로 그중 하나가 던지면 프로세스가 그대로 끝난다 —
 * FAIL 줄이 하나도 안 찍히고, 뒤 절은 돌지도 않는다. 그러면 `check-setup.js` 는
 * 「종료코드가 0 이 아니다」만 알고 **어디가 깨졌는지는 아무 데도 안 남는다.**
 * 검사가 조용히 갈리는 것을 막으려고 만든 파일에서 그 일이 나면 안 된다.
 * (2026-08 에 「지금은 이 블록이 파일 끝이라 가려지는 것이 없다」며 미뤄 뒀는데,
 *  그 뒤 아래에 ④⑤ 절이 붙어 그 근거가 깨졌다. 2026-09-01 최종 검토에서 잡혔다.)
 *
 * **절을 이어서 돌리지는 못한다** — 최상위 `const` 를 절들이 나눠 쓰고 있어 그러려면
 * 파일을 함수로 갈라야 한다. 여기서는 「조용히 죽지 않게」까지만 한다. */
let SECTION = '(시작 전)';
const section = (name) => { SECTION = name; console.log(`\n${name}`); };
const died = (err) => {
  console.log(`  FAIL ${SECTION} 이 예외로 멈췄습니다  ${err?.stack || err}`);
  console.log('\n실패 있음 — 위 절에서 멈춰 뒤 절은 돌지 않았습니다');
  process.exit(1);
};
process.on('unhandledRejection', died);
process.on('uncaughtException', died);

/* ── ① keepMessage — 「이 메시지를 아카이브에 넣나」 ────────────────── */
section('① keepMessage');

check('사람이 쓴 본문은 넣는다', keepMessage({ user: 'U1', text: '안녕' }, 'U0') === true);
check('참여 로그는 뺀다', keepMessage({ user: 'U1', subtype: 'channel_join' }, 'U0') === false);
check('봇이 쓴 것은 뺀다', keepMessage({ bot_id: 'B1', text: 'x' }, 'U0') === false);
check('앱이 사람 이름으로 올린 것도 뺀다',
  keepMessage({ user: 'U1', app_id: 'A1', text: 'x' }, 'U0') === false);
check('본문이 없고 첨부만 있어도 넣는다',
  keepMessage({ user: 'U1', files: [{ id: 'F1' }] }, 'U0') === true);
check('본문도 첨부도 없으면 뺀다', keepMessage({ user: 'U1' }, 'U0') === false);

/* 「채널에도 게시」한 스레드 답글(broadcast)은 본문 블록으로 넣지 않는다 — 부모의 스레드
 * 줄로 이미 들어간다. 안 빼면 같은 발언이 md 에 두 번 실리는데 **에러가 안 난다**
 * (2026-09-03 실측 7건 / 4채널). subtype 이 안 붙어 와도 `thread_ts` 로 잡는다. */
check('채널에도 게시한 답글은 본문 블록으로 안 넣는다',
  keepMessage({ user: 'U1', subtype: 'thread_broadcast', ts: '2.0', thread_ts: '1.0', text: 'x' }, 'U0') === false);
check('subtype 없이 와도 thread_ts 로 잡는다',
  keepMessage({ user: 'U1', ts: '2.0', thread_ts: '1.0', text: 'x' }, 'U0') === false);
check('스레드 부모(thread_ts == ts)는 그대로 넣는다',
  keepMessage({ user: 'U1', ts: '1.0', thread_ts: '1.0', text: 'x' }, 'U0') === true);
/* **`SKIP_SUBTYPES` 에 넣어 고치면 안 된다** — 그 목록은 요약의 답글 필터·스레드 맥락·
 * DM 핸들러도 함께 보므로, 넣으면 그 발언이 요약 어디에도 안 남고 DM 질문은 응답도 에러도
 * 없이 버려진다 (slack-live.js 의 isThreadBroadcast 머리말). */
check('thread_broadcast 를 SKIP_SUBTYPES 에 넣지 않았다', !SKIP_SUBTYPES.has('thread_broadcast'));

/* 두 모듈이 이 함수를 **부르는지** 소스로 본다. 누가 다시 네 줄로 베껴 넣으면
 * 호출이 사라져 여기서 잡힌다. 판정만 대보면 그 상황이 안 잡힌다.
 *
 * **`\bkeepMessage\b` 로 재면 안 된다** — 그건 import 줄에도 걸려서, 호출부만
 * 옛 네 줄로 되돌리고 import 를 남겨 두면 **통과한다**. 이 검사의 존재 이유가
 * 정확히 그 「갈려도 조용한」 상황이라, 여는 괄호까지 봐야 호출만 잡힌다.
 * (2026-09-01 Task 1 검토에서 드러났다 — 실제로 그 상태로 PASS 가 났다) */
const usesKeep = (rel) => {
  const src = fs.readFileSync(path.join(ROOT, rel), 'utf8');
  return /keepMessage\s*\(/.test(src);
};
check('slack-archive.js 가 keepMessage 를 쓴다', usesKeep('src/ingest/slack-archive.js'));

/* ── ② fetchRange — 커서를 끝까지 넘긴다 ──────────────────────────── */
section('② fetchRange');

/** 가짜 슬랙. `pages` 를 순서대로 돌려주고 마지막 장에만 커서가 없다. */
const fakeClient = (pages, calls = []) => ({
  conversations: {
    history: async (args) => {
      calls.push(args);
      const i = args.cursor ? Number(args.cursor) : 0;
      return {
        messages: pages[i] || [],
        response_metadata: i + 1 < pages.length ? { next_cursor: String(i + 1) } : {},
      };
    },
  },
});

const msg = (ts, text = 'x') => ({ user: 'U1', ts, text });

{
  const calls = [];
  const client = fakeClient([
    [msg('300'), msg('290')],
    [msg('200'), msg('190')],
    [msg('100')],
  ], calls);
  const got = await fetchRange(client, 'C1', { oldest: '0', latest: '400' });
  check('세 장을 다 읽는다', got.length === 5, `읽은 것 ${got.length}건`);
  check('오래된 것부터 정렬된다', got[0].ts === '100' && got[4].ts === '300');
  check('latest 를 그대로 넘긴다', calls[0].latest === '400');
  check('커서를 이어 넘긴다', calls[2].cursor === '2');
}

/* ── ③ monthChunks — 달이 바뀔 때 넘긴다 · since 에서 멈춘다 ───────── */
section('③ monthChunks');

/** 2026-03-15, 2026-03-01, 2026-02-20, 2026-01-05 (UTC 기준 ts) */
const TS = {
  m3a: '1773532800', m3b: '1772323200', m2: '1771545600', m1: '1767571200',
};

{
  const client = fakeClient([[msg(TS.m3a), msg(TS.m3b), msg(TS.m2), msg(TS.m1)]]);
  const out = [];
  for await (const c of monthChunks(client, 'C1', {
    latest: '9999999999', tz: 'UTC', selfId: 'U0', userMap: new Map(),
  })) out.push(c);
  check('달마다 하나씩 넘긴다', out.length === 3, `넘긴 달 ${out.map((c) => c.month).join(' · ')}`);
  check('최신 달부터 나온다', out[0].month === '2026-03');
  check('같은 달은 함께 묶인다', out[0].blocks.length === 2);
  check('그 달의 가장 오래된 ts 를 함께 준다', out[0].oldestTs === TS.m3b);
  check('한 달 안에서 최신이 앞이다',
    out[0].blocks[0].includes('2026-03-15') && out[0].blocks[1].includes('2026-03-01'),
    out[0].blocks.map((b) => b.slice(2, 12)).join(' → '));
}

{
  const client = fakeClient([[msg(TS.m3a), msg(TS.m2), msg(TS.m1)]]);
  const out = [];
  for await (const c of monthChunks(client, 'C1', {
    latest: '9999999999', since: '2026-02', tz: 'UTC', selfId: 'U0', userMap: new Map(),
  })) out.push(c);
  check('since 보다 오래된 달은 안 넘긴다',
    out.map((c) => c.month).join() === '2026-03,2026-02',
    `넘긴 달 ${out.map((c) => c.month).join(' · ')}`);
}

{
  const client = fakeClient([[msg(TS.m3a), { user: 'U1', ts: TS.m3b, subtype: 'channel_join' }]]);
  const out = [];
  for await (const c of monthChunks(client, 'C1', {
    latest: '9999999999', tz: 'UTC', selfId: 'U0', userMap: new Map(),
  })) out.push(c);
  check('걸러진 것은 blocks 에 안 들어가지만 seen 에는 센다',
    out[0].blocks.length === 1 && out[0].seen === 2 && out[0].kept === 1);
}

/* 스레드 답글 경로 — `reply_count` 가 붙은 메시지가 하나도 없으면 이 가지가
 * 한 번도 안 돌아서, 인자 순서가 틀려도 검사가 통과한다. */
{
  const client = fakeClient([[{ user: 'U1', ts: TS.m3a, text: 'x', reply_count: 2 }]]);
  client.conversations.replies = async ({ ts }) => ({
    messages: [
      { user: 'U1', ts, text: 'x' },                        // 부모 — fetchReplies 가 뺀다
      { user: 'U2', ts: `${Number(ts) + 1}`, text: '답글' },
    ],
    response_metadata: {},
  });
  const out = [];
  for await (const c of monthChunks(client, 'C1', {
    latest: '9999999999', tz: 'UTC', selfId: 'U0', userMap: new Map(),
  })) out.push(c);
  check('스레드 답글이 블록에 딸려 온다', out[0].blocks[0].includes('답글'),
    out[0].blocks[0].slice(0, 80));
}

/* 스레드 읽기가 실패해도 그 달 전체가 죽으면 안 된다. */
{
  const client = fakeClient([[{ user: 'U1', ts: TS.m3a, text: 'x', reply_count: 1 }]]);
  client.conversations.replies = async () => { throw new Error('ratelimited'); };
  const out = [];
  for await (const c of monthChunks(client, 'C1', {
    latest: '9999999999', tz: 'UTC', selfId: 'U0', userMap: new Map(),
  })) out.push(c);
  check('스레드 읽기가 실패해도 그 달은 넘어온다', out.length === 1 && out[0].blocks.length === 1);
  check('실패를 조용히 삼키지 않고 notes 에 남긴다',
    out[0].notes.length === 1 && out[0].notes[0].includes('ratelimited'),
    JSON.stringify(out[0].notes));
}

/* ── ④ md 뼈대와 「미수집 구간」 지우기 ───────────────────────────── */
section('④ md 뼈대');

{
  const s = channelSkeleton({ name: '사업장가', isPrivate: false, channelId: 'C1', today: '2026-09-01' });
  const L = s.split('\n');
  check('첫 줄이 채널 이름이다', L[0] === '# #사업장가');
  check('메타 블록이 있다', /^> \*\*기간\*\*/m.test(s));
  /* 워크스페이스 줄·채널 ID 줄은 2026-09-21 에 뺐다 (WHK 결정) — 색인이 질문마다
   * 실리는데 37채널에 같은 말이 반복돼 5,069자를 먹었고 읽는 쪽이 0곳이었다.
   * **다시 들어오지 않는 것까지 검사한다** — 뺀 줄은 검사가 없으면 조용히 되살아난다. */
  check('워크스페이스 줄이 없다', !/^> \*\*워크스페이스\*\*/m.test(s));
  check('채널 ID 줄이 없다', !/^> \*\*채널 ID\*\*/m.test(s));
  check('공개 채널에는 자물쇠가 없다', !s.includes('🔒'));

  /* `insert_messages.py` 의 경계 정규식이 이 셋에 걸린다 (57-60행·168행).
   * 하나라도 없으면 「본문 시작 구분선('---')을 찾지 못했습니다」로 죽거나,
   * 더 나쁘게는 블록이 엉뚱한 자리에 들어간다. */
  check('본문 구분선 --- 이 둘이다', L.filter((x) => x.trim() === '---').length === 3,
    `--- ${L.filter((x) => x.trim() === '---').length}개`);
  check('하단 절 머리가 「## 참여 기록」 으로 시작한다', /^## 참여 기록/m.test(s));
  check('출처 줄이 있다', /^_출처: Slack #사업장가 · /m.test(s));

  const p = channelSkeleton({ name: '사업장나', isPrivate: true, channelId: 'C2', today: '2026-09-01' });
  check('비공개 채널은 첫 줄에 자물쇠가 붙는다', p.split('\n')[0].includes('🔒 비공개'));
}

{
  /* verify_archive.py 의 FAIL_MARKS 에 든 문구다. 남아 있으면 pre-commit 이 커밋을 막는다.
   * 마커 모양은 실물에서 가져왔다 — 인용 줄에 굵은 글씨. */
  const MARK = '> ⚠️ **미수집 구간**: 2026-01-05 09:00 이전은 이번 추출에 포함되지 않았습니다.';
  const before = ['# #사업장가', '', '> 메타', '', '## 2026-03', '', MARK, ''].join('\n');
  const { text, removed, unmatched } = stripMissingMarks(before);
  check('마커 줄을 지운다', !text.includes('미수집 구간'));
  check('지운 줄 수를 알려준다', removed === 1, `removed=${removed}`);
  check('나머지는 그대로 둔다', text.includes('## 2026-03') && text.includes('# #사업장가'));
  check('지울 것이 없으면 unmatched 도 비어 있다', unmatched.length === 0);
}

{
  /* **사람이 쓴 본문에 그 낱말이 들어도 지우면 안 된다.** */
  const 본문 = ['**2026-03-15 09:00 · 홍길동**', '미수집 구간이 있는지 확인 부탁드립니다.', ''].join('\n');
  const { text, removed, unmatched } = stripMissingMarks(본문);
  check('사람이 쓴 줄은 안 지운다', text.includes('확인 부탁드립니다'), `removed=${removed}`);
  check('대신 unmatched 로 올린다', unmatched.length === 1 && unmatched[0].includes('미수집 구간'),
    JSON.stringify(unmatched));
}

/* ── ⑤ 재개 — latest 는 「넣은 뒤에만」 내려간다 ────────────────────── */
section('⑤ 재개');

{
  const before = { file: '사업장가', latest: '9999999999', months: [], oldest: null, done: false };
  const after = advance(before, { month: '2026-03', oldestTs: '1772323200' });
  check('넣은 달이 기록된다', after.months.join() === '2026-03');
  check('latest 가 그 달의 가장 오래된 ts 로 내려간다', after.latest === '1772323200');
  check('가장 오래된 날짜가 갱신된다', after.oldest === '1772323200');
  check('원본을 안 고친다', before.months.length === 0 && before.latest === '9999999999');

  const twice = advance(after, { month: '2026-02', oldestTs: '1771545600' });
  check('두 번째 달도 이어 쌓인다', twice.months.join() === '2026-03,2026-02');
  check('가장 오래된 것만 남는다', twice.oldest === '1771545600');

  /* **한 달이 통째로 걸러진 경우.** `batch` 에는 `keepMessage` 를 통과한 것만 들어가므로
   * 참여 알림만 있는 달은 `oldestTs` 가 `null` 이다. 그 `null` 이 그대로 내려가면
   * ① 이어서 돌릴 기준점을 잃어 위에서부터 다시 훑고 ② 「가장 오래된 대화」 표가
   * 멀쩡히 채운 채널에 `없음` 을 찍는다 — 보존기간 경고가 스스로 거짓말을 한다.
   * 둘 다 에러가 안 난다. 2026-09-01 전 채널 시험 로그에 이미 6채널이 그랬다. */
  const filtered = advance(twice, { month: '2026-01', oldestTs: null, oldestSeenTs: '1769900000' });
  check('한 달이 통째로 걸러져도 기준점이 안 비워진다', filtered.latest === '1769900000');
  check('그때도 「가장 오래된 대화」가 안 비워진다', filtered.oldest === '1769900000');

  const nothing = advance(twice, { month: '2026-01', oldestTs: null, oldestSeenTs: null });
  check('둘 다 없으면 앞 값을 지킨다 (null 로 덮지 않는다)',
        nothing.latest === '1771545600' && nothing.oldest === '1771545600');
}

/* ── ⑥ 경계 — 백필은 아카이브가 덮은 자리보다 오래된 것만 읽는다 ────────── */
section('⑥ 경계');

{
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'floor-'));
  const md = (body) => {
    const p = path.join(tmp, `${Math.random().toString(36).slice(2)}.md`);
    fs.writeFileSync(p, body, 'utf8');
    return p;
  };

  const tz = 'Asia/Seoul';

  // 되짚기: stamp() 로 다시 찍었을 때 원래 분이 나와야 한다
  check('분을 epoch 으로 되짚는다',
        stamp(minuteToEpoch('2026-06-09 16:11', tz), tz) === '2026-06-09 16:11');
  check('자정도 되짚는다',
        stamp(minuteToEpoch('2026-01-01 00:00', tz), tz) === '2026-01-01 00:00');

  // 가장 오래된 헤더를 고른다 — 파일 안 순서와 무관하게
  const p1 = md([
    '## 2026-08', '', '**2026-08-20 09:00 · 사람가**', '스무날', '',
    '## 2026-06', '', '**2026-06-09 16:11 · 사람나**', '아흐레', '',
  ].join('\n'));
  const f1 = archiveFloor(p1, tz);
  check('가장 오래된 헤더를 경계로 잡는다', f1 && f1.minute === '2026-06-09 16:11');
  check('경계 epoch 이 그 분의 시작이다',
        f1 && f1.latest === String(minuteToEpoch('2026-06-09 16:11', tz)));

  // 본문에 헤더 흉내 줄이 있어도 속지 않는다 (` · ` 가 없다)
  const p2 = md([
    '## 2026-08', '', '**2026-08-20 09:00 · 사람가**',
    '**2026-01-01 00:00 앞선 회의** 참고', '',
  ].join('\n'));
  check('본문의 헤더 흉내 줄은 경계로 안 잡는다',
        archiveFloor(p2, tz).minute === '2026-08-20 09:00');

  /* **2026-09-02 리뷰가 재현한 사례.** ` · ` 도 있고 닫는 `**` 도 있는 본문 줄 —
   * 예전 판(접두사만 검사)은 이걸 헤더로 잘못 잡아 경계가 실제보다 오래된 값이
   * 됐다(too old). too old 경계는 그 사이 구간을 조용히 통째로 빠뜨리므로
   * (too new 는 눈에 보이는 중복만 남겨 안전하지만 이건 그렇지 않다), 닫는 `**`
   * 뒤에 문장이 더 있으면 걸리지 않아야 한다 — 진짜 헤더(더 최근인 8/20)를 잡아야 한다. */
  const p4 = md([
    '## 2026-08', '', '**2026-08-20 09:00 · 사람가**',
    '**2026-01-01 00:00 · 김철수** 님이 예전에 이렇게 말했습니다', '',
  ].join('\n'));
  check('닫는 ** 뒤에 문장이 이어지는 헤더 흉내 줄도 안 속는다 (진짜 헤더를 잡는다)',
        archiveFloor(p4, tz).minute === '2026-08-20 09:00',
        `실제: ${JSON.stringify(archiveFloor(p4, tz))}`);

  // 새 채널 — 헤더가 하나도 없으면 경계가 없다
  const p3 = md('> **채널**: #사업장가\n\n## 2026-08\n\n');
  check('헤더가 없으면 경계가 없다 (null)', archiveFloor(p3, tz) === null);
  check('파일이 없으면 경계가 없다 (null)',
        archiveFloor(path.join(tmp, '없는파일.md'), tz) === null);

  /* **`writeMonth` 의 `added` — md 헤더 실측 증가가 진짜인지.**
   * 여태 이 값의 유일한 증거는 저장소 밖 스크래치 스크립트(`probe-signal.mjs`·
   * `probe-partial.mjs`)뿐이었다 — `npm run check` 어디에도 안 걸렸다. `writeMonth` 나
   * `insert_block` 을 누가 다시 건드려 이진 판정(「이미 반영됨」 문구 유무)으로 되돌리면
   * 아무 관문도 못 잡는다는 지적을 받았다(2026-09-02, task-4-report.md 「우려」 절이
   * 바로 그 구멍이었다). `countHeaders` 는 export 되지 않은 내부 헬퍼라 여기서 직접
   * 못 부른다 — 대신 우리가 넣는 블록의 헤더 줄을 **문자열 그대로** 세어(정규식을
   * 다시 베끼지 않는다) `added` 와 대본다.
   *
   * **이 검사가 실제로 빨개지는 경우.** ① `writeMonth` 가 `added` 를 안 돌려주게
   * 되돌리면 세 판정 전부 `undefined === n` 이 되어 FAIL. ② 「이미 반영됨」 문구
   * 유무의 이진 판정으로 되돌리면 전부 새 것(2)·전부 중복(0)은 우연히 맞아도
   * **일부만 중복인 세 번째 경우가 2 로 찍혀** FAIL — 이진 판정이 정확히 놓치는
   * 자리다. ③ `insert_block` 이 중복을 걸러내지 못하게 되면 두 번째·세 번째
   * 판정에서 헤더가 중복으로 늘어 실측 카운트가 어긋나 FAIL. */
  const wmPath = md(channelSkeleton({
    name: '검사가', isPrivate: false, channelId: 'C9', today: '2026-09-02',
  }));
  const countLit = (needle) => fs.readFileSync(wmPath, 'utf8').split(needle).length - 1;
  const blockA = '**2026-06-15 10:00 · U1**\n첫 번째 메시지';
  const blockB = '**2026-06-14 09:30 · U2**\n두 번째 메시지';
  const blockC = '**2026-06-13 08:00 · U3**\n세 번째 메시지';

  const wm1 = await writeMonth(wmPath, '2026-06', [blockA, blockB]);
  check('writeMonth added — 전부 새 것이면 넣은 블록 수와 같다', wm1.added === 2, `added=${wm1.added}`);
  check('  실측 헤더 수도 각 1건', countLit('**2026-06-15 10:00 · U1**') === 1
    && countLit('**2026-06-14 09:30 · U2**') === 1);

  const wm2 = await writeMonth(wmPath, '2026-06', [blockA, blockB]);
  check('writeMonth added — 전부 중복이면 0', wm2.added === 0, `added=${wm2.added}`);
  check('  중복으로 다시 안 늘었다', countLit('**2026-06-15 10:00 · U1**') === 1
    && countLit('**2026-06-14 09:30 · U2**') === 1);

  const wm3 = await writeMonth(wmPath, '2026-06', [blockA, blockC]);
  check('writeMonth added — 일부만 중복이면 실제 증가(1)다, 전달한 블록 수(2)가 아니다',
    wm3.added === 1, `added=${wm3.added}`);
  check('  기존 블록은 그대로, 새 블록만 늘었다', countLit('**2026-06-15 10:00 · U1**') === 1
    && countLit('**2026-06-13 08:00 · U3**') === 1);

  fs.rmSync(tmp, { recursive: true, force: true });
}

/* ── ⑦ 백필 실행기 — 씨앗이 saved.latest 로 되돌아가지 않는다 ────────── */
section('⑦ 백필 실행기 씨앗');

{
  /* `run-backfill.js` 는 Slack 에 직접 붙는 스크립트라 ①~⑥ 처럼 가짜 클라이언트로
   * 통째로 돌릴 수 없다(이 파일 자체가 「슬랙에 붙지 않는다」고 위에 적혀 있다).
   * 그래서 **그 스크립트가 실제로 쓰는 소스**에서 씨앗을 만드는 문장만 떼어,
   * `floor.latest` 를 쓰고 `saved.latest` 로 되돌아가지 않는지를 잰다.
   * `usesKeep`(① 절)과 같은 원리 — 판정만 대보면 「함수는 맞는데 실제로는 안
   * 쓴다/다르게 쓴다」가 안 잡힌다.
   *
   * **이 검사가 실제로 빨개지는 경우.** 누가 재개를 「더 빠르게」 만들려고
   * `latest: saved?.latest ?? floor.latest` 로 바꾸면, 상태 파일에 이미 기록이
   * 있는 채널(재개 케이스 전부)은 영원히 md 의 경계를 무시하고 상태 파일의 낡은
   * 값을 쓴다 — 그게 이 과제 전체가 고치려던 문제(오늘부터 겹쳐 읽기)가 재개
   * 경로로 다시 들어오는 자리다. 그 문장은 `latestLine`(`saved` 를 포함)에 그대로
   * 잡히므로 아래 두 번째 check 가 FAIL 로 바뀐다. */
  const src = fs.readFileSync(path.join(ROOT, 'scripts/run-backfill.js'), 'utf8');
  const seedMatch = /const prev = \{([\s\S]*?)\};/.exec(src);
  check('씨앗 블록(const prev = {...})을 찾는다', !!seedMatch);
  const seedBlock = seedMatch ? seedMatch[1] : '';
  const latestLine = (/latest:\s*([^\n,]+)/.exec(seedBlock)?.[1] || '').trim();
  check('씨앗의 latest 가 floor.latest 를 쓴다', latestLine.includes('floor.latest'), latestLine);
  check('씨앗의 latest 가 saved.latest 로 되돌아가지 않는다',
    latestLine.length > 0 && !latestLine.includes('saved'), latestLine);
}

/* ── ⑧ 채널 고르기 · md 이름 되짚기 (최종 검토 F1·F2, 2026-09-02) ────────── */
section('⑧ 채널 고르기');

{
  /* **F1 — 백필이 `digest.skipChannels` 를 안 걸렀다.**
   * 자동 반영(`slack-archive.js`)은 거르고 그 자리 주석이 이유를 적어 뒀다 —
   * 「요약만 빠지고 아카이브에는 쌓이면 설정이 막아준다고 믿는 것과 실제가 어긋난다
   * — 그게 가장 나쁜 상태다」. 백필은 안 걸러서 `--all-channels` 한 번이면 그 채널의
   * md 가 생기고, **봇은 채널을 디스크의 md 목록으로 세므로**(`archive.js` 의
   * listArchivedChannels) 만들어지는 즉시 색인·검색·인용 대상이 된다. */
  const chans = [
    { id: 'C1', name: '사업장가' },
    { id: 'C2', name: 'z_안다루는채널' },
    { id: 'C3', name: '사업장나' },
  ];
  const skip = ['z_안다루는채널'];

  const all = backfillTargets(chans, { skip });
  check('전 채널일 때 skipChannels 를 뺀다',
    all.length === 2 && !all.some((c) => c.name === 'z_안다루는채널'),
    all.map((c) => c.name).join(','));

  check('skip 이 비면 전부 남는다', backfillTargets(chans, { skip: [] }).length === 3);

  /* `--channel` 로 **콕 집어도** 거른다. 「사람이 일부러 시켰으니 통과」로 두면
   * 설정이 막아준다고 믿는 것과 실제가 어긋나는 그 상태가 그대로 생긴다.
   * 넣고 싶으면 skipChannels 에서 지우는 것이 옳은 순서다. */
  check('--channel 로 콕 집어도 skip 은 안 준다',
    backfillTargets(chans, { only: 'z_안다루는채널', skip }).length === 0);
  check('--channel 로 안 막힌 채널은 그것만',
    backfillTargets(chans, { only: '사업장가', skip }).map((c) => c.name).join() === '사업장가');

  /* **개명 되짚기** (2026-09-16). 여기 오는 이름은 슬랙의 현재 이름인데 설정에는 사람이
   * 어느 철자로든 적는다 — 옛 철자인 채로 글자 그대로 대면 skip 줄이 죽어, `--all-channels`
   * 한 번에 그 채널 md 가 생긴다 (F1 과 같은 결말). 가짜 지도(`nameMap`)로 잰다 —
   * 실물 `.sync-state.json` 없이 돈다. */
  const renamed = [
    { id: 'C1', name: '사업장가' },
    { id: 'C2', name: 'z_안다루는채널_새이름' },
  ];
  const nameMap = new Map([['z_안다루는채널', 'z_안다루는채널_새이름']]);
  check('옛 철자를 적어 둔 skip 줄이 개명 뒤에도 산다',
    backfillTargets(renamed, { skip: ['z_안다루는채널'], nameMap }).map((c) => c.name).join() === '사업장가');
  check('새 철자로 고쳐 적어도 산다',
    backfillTargets(renamed, { skip: ['z_안다루는채널_새이름'], nameMap }).map((c) => c.name).join() === '사업장가');
  check('--channel 을 옛 철자로 콕 집어도 skip 은 안 준다',
    backfillTargets(renamed, { only: 'z_안다루는채널', skip: ['z_안다루는채널_새이름'], nameMap }).length === 0);
  check('--channel 을 옛 철자로 줘도 그 채널을 찾는다 (skip 아닐 때)',
    backfillTargets(renamed, { only: 'z_안다루는채널', nameMap }).map((c) => c.name).join() === 'z_안다루는채널_새이름');
  check('되짚기가 관계 없는 채널까지 막지는 않는다',
    backfillTargets(renamed, { skip: ['z_안다루는채널'], nameMap }).some((c) => c.name === '사업장가'));

  /* **F2 — 개명된 채널의 md 를 못 찾아 두 벌을 만들었다.**
   * 개명해도 md 파일명은 안 바꾸므로 옛 이름이 `.sync-state.json` 의 `file` 에 화석으로
   * 남는다(`archive.js` 의 listAllChannels 주석). 백필은 그 파일을 안 보고
   * `.backfill-state.json` 만 봐서, 개명 채널마다 `archiveFloor` 가 null 을 내고
   * **「경계 없음 (아카이브가 비어 있음) — 전부 읽습니다」를 찍은 뒤 전체 이력을 새
   * 이름으로 다시 넣었다.** 그 줄은 거짓말이고, 결과는 같은 사업장이 두 이름으로
   * 봇 색인·검색에 실리는 것이다. 실측 개명 2건에서 둘 다 이 경로를 탔다. */
  const ch = { id: 'C9', name: '사업장다_새이름' };
  const known = [{ id: 'C9', name: '사업장다_새이름', file: '사업장다' }];
  check('개명 화석(.sync-state 의 file)을 md 이름으로 쓴다',
    mdFileFor(ch, { known }) === '사업장다');

  /* **ID 로 맞추는지 이름으로 맞추는지를 가르는 입력.** 위 `known` 은 id 도 name 도 함께
   * 맞아서 둘을 구별하지 못한다 — 실제로 `k.id === ch.id` 를 `k.name === ch.name` 으로
   * 바꿔도 이 절 전체가 통과했다(2026-09-02 돌연변이 시험에서 잡혔다).
   * 갈리는 자리는 **개명 직후**다: 슬랙은 새 이름을 주는데 `.sync-state.json` 은 다음
   * 자동 반영 전까지 옛 이름을 들고 있다. 그때 이름으로 맞추면 화석을 못 찾아 md 를
   * 두 벌 만든다 — 이 결함이 고쳐졌는지를 가르는 것이 바로 이 경우다. */
  check('개명 직후(화석의 name 이 아직 옛 이름)에도 ID 로 찾아낸다',
    mdFileFor(ch, { known: [{ id: 'C9', name: '사업장다', file: '사업장다' }] })
      === '사업장다');
  check('백필 상태의 file 이 우선한다',
    mdFileFor(ch, { savedFile: '백필이쓰던이름', known }) === '백필이쓰던이름');
  check('화석이 없으면 지금 이름을 쓴다',
    mdFileFor(ch, { known: [] }) === '사업장다_새이름');
  check('id 가 다른 화석에는 안 속는다',
    mdFileFor(ch, { known: [{ id: 'CZZZ', name: '남의채널', file: '남의파일' }] })
      === '사업장다_새이름');

  /* **실행기가 실제로 이 둘을 쓰는지** 본다 — ⑦ 절과 같은 이유다. 함수만 맞고
   * 부르는 쪽이 옛 문장을 그대로 두면 위 여덟 검사는 전부 통과한 채 아무것도 안 고쳐진다.
   * 그게 이 두 결함이 **여태 안 잡힌 방식**이기도 하다. */
  const runner = fs.readFileSync(path.join(ROOT, 'scripts/run-backfill.js'), 'utf8');
  check('실행기가 backfillTargets 로 채널을 고른다',
    /const targets = backfillTargets\(/.test(runner));
  check('실행기에 옛 필터(channels.filter 로 직접 고르기)가 안 남아 있다',
    !/const targets = channels\.filter\(/.test(runner));
  check('실행기가 mdFileFor 로 md 이름을 정한다',
    /const file = mdFileFor\(/.test(runner));
  check('실행기에 옛 문장(saved?.file || ch.name)이 안 남아 있다',
    !/const file = saved\?\.file \|\| ch\.name/.test(runner));
}

console.log(ok ? '\n전부 통과' : '\n실패 있음');
process.exit(ok ? 0 : 1);
