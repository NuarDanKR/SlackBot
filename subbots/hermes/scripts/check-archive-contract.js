#!/usr/bin/env node
/**
 * 아카이브 md 의 **파싱 계약**을 봇이 실제로 쓰는 파서로 잰다.
 *
 *   node scripts/check-archive-contract.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (사유는 화면에)
 *
 * ── 왜 필요한가 ──
 *
 * 채널 md 의 모양은 계약이고, 깨져도 **에러가 안 난다** — 검색이 조용히 0건이 되고 봇은
 * "아카이브에 없습니다"라고 정상적으로 답한다. 지금 그 계약을 지키는 것은 사람의 기억뿐인데,
 * **같은 규칙이 이미 여러 벌로 갈려 있다.**
 *
 * | 무엇 | 갈린 모양 |
 * |---|---|
 * | 메시지 헤더 | `archive.js`(`splitMessages`)·`insert_messages.py` 는 **날짜만** · `backfill.js` 는 `HH:MM` 요구 · `slack-archive.js`(`parseBlocks`)는 `HH:MM` + `· ` 요구 |
 * | 월 헤딩 | `archive.js`(`readChannel`·`MONTH_HEADING`)·`documents.js` 는 공백 **한 칸** 고정 · 나머지 여섯(`summary.js`·`ingest/verify.js`·`insert_entry.py`·`verify_format.py`·`insert_messages.py`·`review_work.py`)은 `^##\s+` |
 *
 * 그래서 `##  2026-08`(두 칸)은 **쓰는 쪽·검사 쪽 관문을 전부 통과하면서** 봇의 월 단위
 * 읽기만 막는다. 사람이 알아챌 자리가 없다.
 *
 * **이 검사는 정규식을 통일하지 않는다** — 통일은 파일 여럿을 건드리는 일이라 따로 한다.
 * 여기서 하는 것은 **갈림이 실물에서 실제로 벌어졌는지**를 재는 것이다. 그래서 규칙을
 * 여기에 다시 적는 대신 **봇이 쓰는 함수를 그대로 부른다**(`splitMessages`·`parseBlocks`·
 * `readChannel`·`metaBlock`·`checkInserted`). 규칙을 베끼면 그 사본이 또 한 벌이 된다.
 *
 * ── 무엇을 안 재나 ──
 *
 * `digest.skipChannels` 에 적힌 채널은 뺀다 — 봇이 아예 안 읽기로 정한 자리라
 * `readChannel` 이 내용 대신 거절 문구를 돌려준다. 그것을 「못 읽음」으로 세면 매번
 * 빨개진다. 몇 개를 뺐는지는 화면에 적는다(통과와 못 잰 것은 다르다).
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { config, CHANNELS_DIR, FULL_ACCESS } from '../src/config.js';
import { splitMessages, readChannel, readCached, metaBlock } from '../src/archive.js';
import { parseBlocks, BODY_TAIL, writtenFrom } from '../src/ingest/slack-archive.js';
import { BOT_ANSWER_MARK } from '../src/slack-live.js';
import { checkInserted } from '../src/ingest/verify.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);
const note = (m) => console.log(`  · ${m}`);

/* ── 파이썬 쪽을 부르는 자리 ─────────────────────────────────────────────
 * [6/8]~[8/8] 은 같은 규칙의 **파이썬 판**(`insert_messages.py`)과 대본다. 값을 공유할 수
 * 없는 두 언어라 「어긋나면 빨개지는 검사」가 공유를 대신한다.
 *
 * `python` 이라는 이름이 없는 곳이 있다 — VM(리눅스)에는 `python3` 만 깔린 경우가 흔하다.
 * 이름 하나만 보면 거기서는 늘 「부르지 못했습니다」로 끝나는데, 그 실패는 진짜 어긋남과
 * 구별되지 않아서 곧 이 검사 전체를 안 읽게 된다. 훅과 같은 순서로 되짚는다.
 * (`scripts/check-shared-rules.js` 가 같은 이유로 같은 순서를 쓴다.) */
const HERE = path.dirname(fileURLToPath(import.meta.url));
const SYNC_PY = path.join(HERE, '..', '.claude', 'skills', 'slack-sync', 'scripts');
const INSERT_PY = path.join(SYNC_PY, 'insert_messages.py');
const PY_NAMES = process.env.PYTHON ? [process.env.PYTHON] : ['python', 'python3'];

/** 파이썬을 한 번 부른다. 못 부르면 `{ err }` — **못 잰 것을 통과로 내지 않으려고** 나눈다. */
function runPy(args, opts = {}) {
  let last = '';
  for (const name of PY_NAMES) {
    try {
      return { out: execFileSync(name, args, { encoding: 'utf-8', ...opts }) };
    } catch (err) {
      last = `${name}: ${(err.stderr || err.message || '').toString().split('\n')[0]}`;
    }
  }
  return { err: last };
}

/** 파이썬에서 JSON 을 받아 온다 (한글이 오가므로 아스키로만 주고받는다). */
function pyJson(code, input) {
  const r = runPy(['-c', [
    'import json,sys',
    `sys.path.insert(0, ${JSON.stringify(SYNC_PY)})`,
    'import insert_messages as M',
    "data = json.loads(sys.stdin.buffer.read().decode('utf-8'))",
    code,
    "sys.stdout.buffer.write(json.dumps(out).encode('ascii'))",
  ].join('\n')], { input: JSON.stringify(input) });
  if (r.err) return { err: r.err };
  return { val: JSON.parse(r.out) };
}

/* ── 계약 두 벌. **판정에 쓰지 않는다** — 아래 ①에서 「둘이 갈린다」를 보이는 데만 쓴다.
 * 실물 판정은 전부 봇 함수를 불러서 한다. */
const READER_MONTH = /^## (\d{4}-\d{2})\s*$/;    // archive.js — 공백 한 칸
const WRITER_MONTH = /^##\s+(\d{4}-\d{2})\s*$/;  // 나머지 여섯 — `\s+`

const SKIP = new Set(config?.digest?.skipChannels || []);

// ══ ① 어긋내기 — 이 검사에 이빨이 있나 ═══════════════════════════════════════
//
// 통과하는 것은 재고 있다는 증거가 아니다. 그래서 **일부러 어긋낸 입력**을 먼저 먹여
// 판정이 갈리는지 본다. 여기서 안 갈리면 아래 실물 결과는 아무 뜻이 없다.
console.log('[1/8] 어긋내기 — 일부러 어긋낸 입력에서 판정이 갈리나');
{
  const good = '## 2026-08\n\n**2026-08-01 09:00 · 김**\n본문\n';
  const bad = good.replace('## 2026-08', '##  2026-08');   // 공백 두 칸

  const rd = (t) => t.split('\n').filter((l) => READER_MONTH.test(l)).length;
  const wr = (t) => t.split('\n').filter((l) => WRITER_MONTH.test(l)).length;

  if (rd(good) === 1 && wr(good) === 1) pass('한 칸짜리는 읽는 쪽·쓰는 쪽이 똑같이 센다');
  else fail(`한 칸짜리에서 이미 갈립니다 (읽는 쪽 ${rd(good)} · 쓰는 쪽 ${wr(good)})`);

  if (rd(bad) === 0 && wr(bad) === 1) {
    pass('두 칸짜리는 쓰는 쪽만 세고 읽는 쪽은 못 센다 — 이 검사가 잡을 것');
  } else {
    fail(`두 칸짜리에서 갈림이 안 드러납니다 (읽는 쪽 ${rd(bad)} · 쓰는 쪽 ${wr(bad)}) `
       + '— 아래 [2/8] 는 아무것도 못 잡습니다');
  }

  // 헤더 뒤에 본문을 붙인 「압축 1줄」은 두 파서가 갈리는 자리다.
  const one = '**2026-08-01 09:00 · 김** — 7/29 일일업무일지\n';
  const sm = splitMessages(one).length;
  const pb = parseBlocks(one);
  if (sm === 0 && pb.length === 1 && pb[0].compressed) {
    pass('압축 1줄은 parseBlocks 만 보고 splitMessages 는 못 본다 — 이 검사가 셀 것');
  } else {
    fail(`압축 1줄에서 갈림이 안 드러납니다 (splitMessages ${sm} · parseBlocks ${pb.length})`);
  }

  // 건1 — CRLF 로 저장된 파일에서 여러 줄 답글 프로브가 맞나.
  //   `renderReply` 는 `'\n> '` 로 잇고 작업 트리 md 는 CRLF 라, 관문이 줄 끝을 안
  //   맞추면 **여러 줄 답글은 원리상 절대 안 맞는다**. 여기서는 `checkInserted` 가 쓰는
  //   것과 같은 대조(블록 안에 그 문자열이 있나)를 CRLF 판에 걸어 본다.
  const parentLf = '**2026-08-01 09:00 · 김**\n\n> 💬 **스레드 (1)**\n> **└ 2026-08-01 09:30 · 박** — 첫 줄\n> 둘째 줄\n';
  const replyLf = '> **└ 2026-08-01 09:30 · 박** — 첫 줄\n> 둘째 줄';
  const parentCrlf = parentLf.replace(/\n/g, '\r\n');
  if (!parentCrlf.includes(replyLf) && parentCrlf.replace(/\r\n/g, '\n').includes(replyLf)) {
    pass('CRLF 판은 줄 끝을 맞추기 전에는 여러 줄 답글을 못 찾는다 — [5/8] 가 잡을 것');
  } else {
    fail('CRLF 판에서 갈림이 안 드러납니다 — [5/8] 가 아무것도 안 잽니다');
  }
}

// ══ 실물 아카이브 ════════════════════════════════════════════════════════════
if (!fs.existsSync(CHANNELS_DIR)) {
  // **못 잰 것을 통과로 내지 않는다.**
  console.error(`\n채널 아카이브가 없습니다: ${CHANNELS_DIR}`);
  console.error('아래 넷은 재지 못했습니다 — 통과가 아닙니다.');
  process.exit(1);
}

const files = fs.readdirSync(CHANNELS_DIR).filter((f) => f.endsWith('.md'));
const targets = files.filter((f) => !SKIP.has(f.replace(/\.md$/, '')));
const skipped = files.length - targets.length;

console.log(`\n[2/8] 월 헤딩 — 읽는 쪽(archive.js)과 쓰는 쪽이 같은 것을 세나  (채널 ${targets.length}개)`);
{
  let bad = 0;
  for (const f of targets) {
    const lines = readCached(path.join(CHANNELS_DIR, f)).split('\n');
    const rd = lines.filter((l) => READER_MONTH.test(l));
    const wr = lines.filter((l) => WRITER_MONTH.test(l));
    if (rd.length !== wr.length) {
      bad += 1;
      const only = wr.filter((l) => !READER_MONTH.test(l)).slice(0, 3);
      fail(`${f} — 쓰는 쪽 ${wr.length}개 · 읽는 쪽 ${rd.length}개. `
         + `봇이 못 세는 줄: ${only.map((l) => JSON.stringify(l)).join(' ')}`);
    }
  }
  if (!bad) pass('두 규칙이 같은 줄을 센다 (관문만 통과하는 월 헤딩이 없다)');
}

console.log('\n[3/8] 메시지 헤더 — splitMessages 와 parseBlocks 가 같은 줄을 헤더로 보나');
{
  let bad = 0;
  let compressed = 0;
  for (const f of targets) {
    const text = readCached(path.join(CHANNELS_DIR, f));
    const smHeads = new Set(splitMessages(text).map((b) => b.text.split('\n')[0]));
    const blocks = parseBlocks(text);
    const pbHeads = new Set(blocks.map((b) => b.headerLine));
    // parseBlocks 만 보는 줄 중 **압축 1줄이 아닌 것**은 계약이 갈린 자리다.
    // 압축 1줄은 `verify_archive.py` ⑥ 이 기준선으로 따로 세므로 여기서는 개수만 적는다.
    for (const b of blocks) {
      if (smHeads.has(b.headerLine)) continue;
      if (b.compressed) { compressed += 1; continue; }
      bad += 1;
      fail(`${f} — parseBlocks 만 헤더로 보는 줄: ${JSON.stringify(b.headerLine.slice(0, 60))}`);
    }
    for (const h of smHeads) {
      if (pbHeads.has(h)) continue;
      bad += 1;
      fail(`${f} — splitMessages 만 헤더로 보는 줄: ${JSON.stringify(h.slice(0, 60))} `
         + '(수정·삭제 대조가 이 메시지를 통째로 못 봅니다)');
    }
  }
  if (!bad) pass('두 파서가 같은 줄을 헤더로 본다');
  if (compressed) {
    note(`압축 1줄 ${compressed}건 — splitMessages 가 안 자릅니다. `
       + '기준선 판정은 verify_archive.py ⑥ 담당입니다');
  }
}

console.log('\n[4/8] 봇의 월 단위 읽기 — 메시지가 있는 달마다 readChannel 이 그 달을 찾나');
{
  let bad = 0;
  for (const f of targets) {
    const name = f.replace(/\.md$/, '');
    const text = readCached(path.join(CHANNELS_DIR, f));
    const months = [...new Set(splitMessages(text).map((b) => b.date.slice(0, 7)))].sort();
    for (const m of months) {
      const r = readChannel({ channel: name, month: m, access: FULL_ACCESS });
      if (r.error) {
        bad += 1;
        fail(`${f} — ${m} 메시지가 있는데 봇은 그 달을 못 엽니다: ${r.error.split('\n')[0].slice(0, 80)}`);
      }
    }
    if (!metaBlock(path.join(CHANNELS_DIR, f)).trim()) {
      bad += 1;
      fail(`${f} — 상단 인용(>) 메타가 비었습니다 (색인에 이 채널이 한 줄도 안 실립니다)`);
    }
  }
  if (!bad) pass('메시지가 있는 달은 전부 봇이 열 수 있고, 색인에 실릴 메타가 있다');
}

console.log('\n[5/8] 관문 프로브 — 실물의 여러 줄 답글을 checkInserted 가 찾나');
{
  // `slack-archive.js` 의 `renderReply` 가 만드는 것과 같은 모양(LF 로 이음)으로
  // 프로브를 만들어, 관문이 그것을 부모 블록 안에서 찾는지 본다. 실물이 CRLF 라
  // 줄 끝을 안 맞추면 여기서 걸린다 (2026-09-03 이전에는 실제로 걸렸다).
  const MD_REPLY = /^>\s*\*\*└\s*\d{4}-\d{2}-\d{2} \d{2}:\d{2} · [^*]+\*\*/;
  const HDR = /^\*\*(\d{4}-\d{2}-\d{2})[^*]*\*\*\s*$/;
  const probes = [];
  for (const f of targets) {
    const lines = fs.readFileSync(path.join(CHANNELS_DIR, f), 'utf8').split(/\r?\n/);
    for (let i = 0; i < lines.length; i += 1) {
      if (!MD_REPLY.test(lines[i])) continue;
      let j = i + 1;
      while (j < lines.length && lines[j].startsWith('>')
             && !MD_REPLY.test(lines[j]) && !/^>\s*💬/.test(lines[j])) j += 1;
      if (j === i + 1) continue;                       // 한 줄짜리는 이 함정에 안 걸린다
      let h = i;
      while (h >= 0 && !HDR.test(lines[h])) h -= 1;
      if (h < 0) continue;
      probes.push({ file: f.replace(/\.md$/, ''), header: lines[h].trim(), reply: lines.slice(i, j).join('\n') });
      break;                                            // 채널마다 하나면 된다
    }
  }
  if (!probes.length) {
    // **없는 것과 통과한 것을 가른다.**
    note('여러 줄 답글이 있는 채널이 없어 재지 못했습니다 (통과가 아닙니다)');
  } else {
    const misses = checkInserted({ threadReplies: probes });
    if (misses.length) {
      for (const m of misses.slice(0, 5)) fail(m);
      fail(`실물 여러 줄 답글 ${probes.length}건 중 ${misses.length}건을 관문이 못 찾습니다`);
    } else {
      pass(`실물 여러 줄 답글 ${probes.length}건을 전부 부모 블록 안에서 찾는다`);
    }
    // **이빨 확인** — 없는 답글은 반드시 걸려야 한다. 안 걸리면 위 통과는 헛것이다.
    const bogus = probes.slice(0, 1).map((p) => ({
      ...p, reply: '> **└ 9999-01-01 00:00 · 없는사람** — 아카이브에 없는 답글',
    }));
    if (checkInserted({ threadReplies: bogus }).length !== 1) {
      fail('없는 답글도 통과했습니다 — 이 검사는 아무것도 안 재고 있습니다');
    } else {
      pass('없는 답글은 걸린다 (검사가 실제로 대조하고 있다)');
    }
  }
}

// ══ [6/8] 봇 답변 표식 — 두 언어가 같은 문자열·같은 경계를 쓰나 ══════════════
//
// 표식(`(봇 답변 — 미수록)`)의 정본은 `src/slack-live.js` 의 `BOT_ANSWER_MARK` 다. 그
// 문자열이 세 곳에서 쓰인다 — 렌더(`renderMessage`) · JS 의 본문 끝 판정(`BODY_TAIL`) ·
// 파이썬의 본문 끝 판정(`insert_messages.py` 의 `BODY_TAIL_RE`·`BOT_TAIL_RE`).
//
// 2026-09-03 까지 JS 쪽 `BODY_TAIL` 만 `(Hermes` 라고 적혀 있어서 **한 번도 안 맞았다.**
// 어긋나도 에러가 안 난다 — 표식 줄이 사람 발언 본문으로 빨려 들어갈 뿐이고, 그 블록이
// 슬랙에서 수정되면 `apply_edits.py --apply` 가 표식 줄을 본문의 일부로 보고 함께 지운다.
// 실물에 그런 블록이 7건 있었다(2026-09-03 실측).
console.log('\n[6/8] 봇 답변 표식 — JS 와 파이썬이 같은 자리에서 본문을 끊나');
{
  /* 본문 끝 판정의 갈래를 하나씩 가르는 입력. **표식 줄이 이 검사의 핵심**이라 정본을
   * 그대로 쓰고(`BOT_ANSWER_MARK`), 옛 오타판(`(Hermes …`)도 함께 넣어 「한쪽만 맞는」
   * 상태가 실제로 드러나는지 본다. */
  const TAIL_CASES = [
    ['표식 줄 (정본)', `💬 스레드 1건 ${BOT_ANSWER_MARK}`],
    ['표식 줄 (옛 오타판)', '💬 스레드 1건 (Hermes 답변 — 미수록)'],
    ['인용 안의 표식 줄', `> 💬 스레드 1건 ${BOT_ANSWER_MARK}`],
    ['스레드 머리줄', '> 💬 **스레드 (2)**'],
    ['답글 줄', '> **└ 2026-08-01 09:30 · 박** — 네'],
    ['첨부 줄', '📎 첨부: `보고서.pdf`'],
    ['백틱 한 줄', '`코드 한 줄`'],
    ['평범한 본문', '오늘 회의는 3시입니다'],
    ['본문 속 인용문', '> 원문에 그렇게 적혀 있습니다'],
    ['', ''],
  ];

  const r = pyJson(
    'out = {"mark": M.BOT_ANSWER_MARK,'
    + ' "bodyTail": [bool(M.BODY_TAIL_RE.match(s)) for s in data],'
    + ' "botTail": [bool(M.BOT_TAIL_RE.match(s)) for s in data]}',
    TAIL_CASES.map(([, s]) => s),
  );
  if (r.err) {
    // **못 잰 것은 「같다」가 아니다.**
    fail(`insert_messages.py 의 본문 끝 판정을 부르지 못했습니다 — ${r.err}`);
  } else {
    if (r.val.mark !== BOT_ANSWER_MARK) {
      fail(`표식 문구가 두 언어에서 갈립니다 — JS ${JSON.stringify(BOT_ANSWER_MARK)}`
         + ` · 파이썬 ${JSON.stringify(r.val.mark)}. 정본은 src/slack-live.js 의 BOT_ANSWER_MARK 입니다.`);
    } else {
      pass(`표식 문구가 두 언어에서 같다 (${JSON.stringify(BOT_ANSWER_MARK)})`);
    }

    let bad = 0;
    TAIL_CASES.forEach(([label, s], i) => {
      const js = BODY_TAIL.test(s);
      if (js !== r.val.bodyTail[i]) {
        bad += 1;
        fail(`본문 끝 판정이 갈립니다 — ${label}: JS ${js} · 파이썬 ${r.val.bodyTail[i]}`);
      }
    });
    if (!bad) pass(`본문 끝 판정이 ${TAIL_CASES.length}가지 줄에서 두 언어가 같다`);

    // **이빨 확인** — 표식 줄에서 양쪽이 실제로 「끊는다」고 답해야 이 대조가 뜻이 있다.
    // 둘 다 `false` 를 내도 「갈리지 않음」이라 위 대조는 조용히 통과한다 (2026-09-03 이전이
    // 딱 그 반대 방향이었다 — JS 만 false).
    const i0 = 0;
    const i1 = 1;
    if (!BODY_TAIL.test(TAIL_CASES[i0][1]) || !r.val.bodyTail[i0]) {
      fail('정본 표식 줄을 양쪽 다 본문 끝으로 안 봅니다 — 이 대조는 아무것도 안 지킵니다.');
    } else if (BODY_TAIL.test(TAIL_CASES[i1][1]) || r.val.bodyTail[i1]) {
      fail('옛 오타판 표식(`(Hermes …`)까지 본문 끝으로 봅니다 — 판정이 표식을 안 보고 있습니다.');
    } else {
      pass('정본 표식만 본문 끝으로 보고 옛 오타판은 안 본다 (판정이 표식을 실제로 본다)');
    }
  }

  /* 실물 — 표식 줄이 본문에 빨려 들어간 블록이 남아 있나.
   * 후보 줄만 파이썬에 보낸다: 두 정규식의 어느 갈래든 첫 글자가 이 넷 중 하나다
   * (백틱 · `📎` · `>` · `💬`). 나머지는 원리상 어느 쪽도 안 맞는다. */
  const HEAD_CHARS = new Set(['`', '📎', '>', '💬']);
  const byFile = new Map();
  const uniq = new Set();
  for (const f of targets) {
    const lines = readCached(path.join(CHANNELS_DIR, f)).split('\n').map((l) => l.replace(/\r$/, ''));
    byFile.set(f, lines);
    for (const l of lines) if (HEAD_CHARS.has([...l][0])) uniq.add(l);
  }
  const cand = [...uniq];
  const rr = cand.length ? pyJson('out = [bool(M.BODY_TAIL_RE.match(s)) for s in data]', cand) : { val: [] };
  if (rr.err) {
    fail(`실물 줄을 파이썬 판정에 먹이지 못했습니다 — ${rr.err}`);
  } else {
    const pyHit = new Map(cand.map((s, i) => [s, rr.val[i]]));
    const MD_H = /^\*\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2} · [^*]+)\*\*/;   // 블록 경계 — 판정이 아니라 자르기용
    let swallowed = 0;
    const seen = [];
    for (const [f, lines] of byFile) {
      const heads = [];
      lines.forEach((l, i) => { if (MD_H.test(l)) heads.push(i); });
      heads.forEach((start, n) => {
        let end = n + 1 < heads.length ? heads[n + 1] : lines.length;
        for (let i = start + 1; i < end; i += 1) if (lines[i].startsWith('## ')) { end = i; break; }
        let a = start + 1;
        while (a < end && !BODY_TAIL.test(lines[a])) a += 1;
        let b = start + 1;
        while (b < end && !(pyHit.get(lines[b]) ?? false)) b += 1;
        if (a !== b) {
          swallowed += 1;
          if (seen.length < 5) seen.push(`${f} ${lines[start].slice(0, 34)} (JS ${a - start}줄 · 파이썬 ${b - start}줄)`);
        }
      });
    }
    if (swallowed) {
      for (const s of seen) fail(s);
      fail(`두 판정이 본문을 다른 자리에서 끊는 블록 ${swallowed}건 — 표식 줄이 한쪽에서 본문에 빨려 들어갑니다.`);
    } else {
      pass(`실물 ${targets.length}개 채널에서 두 판정이 같은 자리에서 본문을 끊는다 (갈리는 블록 0건)`);
    }
  }
}

// ══ [7/8] 메시지 헤더 계약 — 파이썬 쪽까지 같은 줄을 고르나 ══════════════════
//
// **정본으로 고른 모양**: `**YYYY-MM-DD HH:MM · 이름**` 이 **줄 전체**를 차지한다
// (닫는 `**` 뒤에는 공백만). 새로 쓰는 헤더는 전부 이 모양이다.
//
// 예외가 **하나** 있고 그것은 옛 기록을 **찾아내기 위한** 것이다 — `parseBlocks`
// (`slack-archive.js`)와 `BLOCK_START_RE`(`insert_messages.py`)는 뒤에 본문이 붙은
// 「압축 1줄」도 헤더로 본다. 안 그러면 교체·삭제가 그 줄을 앞 블록에 흡수시켜 **이웃
// 메시지까지 함께 지운다.** `parseBlocks` 는 그런 블록에 `compressed: true` 를 단다.
//
// 갈리면 백필이 경계를 못 찾아 이력을 통째로 다시 넣거나(too old), 수정·삭제 대조가
// 그 메시지를 통째로 못 본다. 실물 대조는 아래에서 하고 `archive.js`·`backfill.js` 쪽은
// [3/8] 과 그 파일들의 주석이 맡는다.
console.log('\n[7/8] 메시지 헤더 계약 — archive.js·slack-archive.js·insert_messages.py 가 같은 줄을 고르나');
{
  const HEADER_CASES = [
    ['정본 — 줄 전체가 헤더', '**2026-08-01 09:00 · 김철수**', 'header'],
    ['정본 + 줄끝 공백', '**2026-08-01 09:00 · 김철수**  ', 'header'],
    ['압축 1줄 (옛 기록)', '**2026-08-01 09:00 · 김철수** — 7/29 일일업무일지', 'compressed'],
    ['본문 흉내 줄 (닫는 ** 뒤에 문장)', '**2026-01-01 00:00 · 김철수** 님이 예전에 말하길', 'compressed'],
    ['시각이 없는 것', '**2026-08-01 · 김철수**', 'loose'],
    ['가운뎃점이 없는 것', '**2026-08-01 09:00 김철수**', 'loose'],
    ['헤더가 아닌 줄', '오늘 회의는 **2026-08-01 09:00** 입니다', 'none'],
  ];
  /* 갈래마다 넷이 어떻게 답해야 하는가. `loose` 는 **정본이 아닌 모양**이다 —
   * 날짜만 보는 둘(archive.js·MSG_HEADER_RE)은 헤더로 보고 시각·가운뎃점을 요구하는
   * 둘(parseBlocks·BLOCK_START_RE)은 못 본다. 실물에 있으면 봇은 그 메시지를 자르는데
   * 수정·삭제 대조는 통째로 못 보는 상태가 된다. 그래서 **합성 입력으로만 두고 실물에서는
   * 0건이어야 한다.** */
  const WANT = {
    header:     { sm: true,  pb: true,  compressed: false, msg: true,  block: true },
    compressed: { sm: false, pb: true,  compressed: true,  msg: false, block: true },
    loose:      { sm: true,  pb: false, compressed: false, msg: true,  block: false },
    none:       { sm: false, pb: false, compressed: false, msg: false, block: false },
  };

  const r = pyJson(
    'out = {"msg": [bool(M.MSG_HEADER_RE.match(s)) for s in data],'
    + ' "block": [bool(M.BLOCK_START_RE.match(s)) for s in data]}',
    HEADER_CASES.map(([, s]) => s),
  );
  if (r.err) {
    fail(`insert_messages.py 의 헤더 판정을 부르지 못했습니다 — ${r.err}`);
  } else {
    let bad = 0;
    HEADER_CASES.forEach(([label, s, kind], i) => {
      const blocks = parseBlocks(s + '\n');
      const got = {
        sm: splitMessages(s + '\n').length === 1,
        pb: blocks.length === 1,
        compressed: blocks.length === 1 && !!blocks[0].compressed,
        msg: r.val.msg[i],
        block: r.val.block[i],
      };
      const want = WANT[kind];
      for (const k of Object.keys(want)) {
        if (got[k] !== want[k]) {
          bad += 1;
          fail(`${label} — ${k} 가 ${got[k]} 입니다 (계약은 ${want[k]}). `
             + `정본은 '**YYYY-MM-DD HH:MM · 이름**' 이 줄 전체를 차지하는 모양입니다.`);
        }
      }
    });
    if (!bad) {
      pass(`정본·압축 1줄·느슨한 모양·비헤더 ${HEADER_CASES.length}가지에서 네 판정이 계약대로 답한다`);
    }
  }

  /* 실물 — 파이썬 쪽 둘이 JS 쪽 둘과 같은 줄을 고르나.
   * 후보는 `**` 로 시작하는 줄뿐이다 (두 파이썬 정규식이 다 그것을 요구한다). */
  const cand = new Set();
  const perFile = new Map();
  for (const f of targets) {
    const lines = readCached(path.join(CHANNELS_DIR, f)).split('\n').map((l) => l.replace(/\r$/, ''));
    perFile.set(f, lines);
    for (const l of lines) if (l.startsWith('**')) cand.add(l);
  }
  const list = [...cand];
  const rr = list.length
    ? pyJson('out = {"msg": [bool(M.MSG_HEADER_RE.match(s)) for s in data],'
           + ' "block": [bool(M.BLOCK_START_RE.match(s)) for s in data]}', list)
    : { val: { msg: [], block: [] } };
  if (rr.err) {
    fail(`실물 줄을 파이썬 헤더 판정에 먹이지 못했습니다 — ${rr.err}`);
  } else {
    const pyMsg = new Map(list.map((s, i) => [s, rr.val.msg[i]]));
    const pyBlock = new Map(list.map((s, i) => [s, rr.val.block[i]]));
    let bad = 0;
    let loose = 0;
    let heads = 0;
    for (const [f, lines] of perFile) {
      const text = lines.join('\n');
      const sm = new Set(splitMessages(text).map((b) => b.text.split('\n')[0]));
      const pb = new Set(parseBlocks(text).map((b) => b.headerLine));
      for (const l of new Set(lines.filter((x) => x.startsWith('**')))) {
        const g = { sm: sm.has(l), pb: pb.has(l), msg: !!pyMsg.get(l), block: !!pyBlock.get(l) };
        if (!g.sm && !g.pb && !g.msg && !g.block) continue;
        heads += 1;
        if (g.sm !== g.msg) {
          bad += 1;
          fail(`${f} — archive.js 와 insert_messages.py 의 MSG_HEADER_RE 가 갈립니다: `
             + `${JSON.stringify(l.slice(0, 60))} (봇 ${g.sm} · 파이썬 ${g.msg})`);
        }
        if (g.pb !== g.block) {
          bad += 1;
          fail(`${f} — slack-archive.js 의 parseBlocks 와 insert_messages.py 의 BLOCK_START_RE 가 갈립니다: `
             + `${JSON.stringify(l.slice(0, 60))} (parseBlocks ${g.pb} · 파이썬 ${g.block})`);
        }
        if (g.sm && !g.pb) loose += 1;   // 정본이 아닌 모양 — [3/8] 이 같은 줄을 사유까지 적어 낸다
      }
    }
    if (!bad) pass(`실물 헤더 ${heads}줄에서 네 판정이 같은 줄을 고른다`);
    if (loose) {
      fail(`정본이 아닌 헤더(시각·가운뎃점이 없는 모양) ${loose}줄 — 봇은 자르는데 수정·삭제 대조는 못 봅니다.`);
    }
  }
}

// ══ [8/8] 덧붙이기 보고 — 「무엇을 실제로 썼나」가 부르는 쪽까지 오나 ═════════
//
// `insert_messages.py` 의 평상시 문구는 **어디에** 넣었는지만 말한다. 넣으려던 다섯 중
// 하나만 새것이어도 문구가 같아서, 「'이미 반영됨' 이 아니면 전부 들어갔다」로 읽으면
// 부분 성공이 전량으로 세어진다 — 봇 글에 달린 정정 갈래는 개수만이 아니라 **어느 문장이
// 쓰였는지 목록까지** DM 에 싣기 때문에, 쓰이지도 않은 정정 문장이 사람 앞에 나온다.
// 그래서 `--report` 가 신원을 돌려주고 `writtenFrom` 이 그것을 읽는다 (WHK 결정 2026-09-03).
console.log('\n[8/8] 덧붙이기 보고 — 부분 성공을 전량으로 안 세나');
{
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-report-'));
  const md = path.join(dir, '시험채널.md');
  const OLD = '> **└ 2026-08-01 09:30 · 김** — [정정] 이미 들어간 정정';
  const NEW = '> **└ 2026-08-01 09:40 · 박** — [정정] 오늘 새로 달린 정정';
  fs.writeFileSync(md, [
    '# #시험채널', '', '> **채널 ID**: C0TEST', '', '---', '',
    '## 2026-08', '',
    '**2026-08-01 09:00 · 봇**', '(봇 발신 — 본문 미수록)', '',
    '> 💬 **스레드 (1)**', OLD, '',
    '---', '', '## 참여 기록 (요약)', '', '- 시험', '',
  ].join('\n'), 'utf-8');
  const content = path.join(dir, 'block.md');
  fs.writeFileSync(content, `${OLD}\n${NEW}\n`, 'utf-8');

  const r = runPy([INSERT_PY, '--file', md, '--append-thread', '**2026-08-01 09:00 · 봇**',
    '--content-file', content, '--report']);
  if (r.err) {
    fail(`insert_messages.py --report 를 부르지 못했습니다 — ${r.err}`);
  } else {
    // ① 옛 판정("이미 반영됨" 이 아니면 전부") 이 실제로 부풀리는가 — 이빨 확인.
    //    여기서 부풀지 않으면 아래 통과는 아무것도 안 지킨다.
    const naive = r.out.includes('이미 반영됨') ? 0 : 2;
    if (naive !== 2) {
      fail('옛 판정이 이 입력에서 안 부풉니다 — 부분 성공을 만드는 데 실패했습니다 (검사가 헛돕니다).');
    } else {
      pass('옛 판정("이미 반영됨"이 아니면 전부)은 이 입력에서 2건이라고 말한다 — 부풀림이 재현된다');
    }
    // ② 새 길: 파이썬이 돌려준 신원을 읽으면 **새로 쓴 하나만** 나와야 한다.
    let wrote = null;
    try {
      wrote = writtenFrom(r.out, [OLD, NEW]);
    } catch (err) {
      fail(`writtenFrom 이 보고를 못 읽었습니다 — ${err.message}`);
    }
    if (wrote) {
      if (wrote.length === 1 && wrote[0] === NEW) {
        pass('writtenFrom 은 이번에 실제로 들어간 1건만 돌려준다 (정정 문장 목록도 그만큼)');
      } else {
        fail(`writtenFrom 이 ${wrote.length}건을 돌려줬습니다 (1건이어야 하고 새 정정이어야 합니다): `
           + JSON.stringify(wrote.map((x) => x.slice(0, 40))));
      }
    }
    // ③ md 에 실제로 늘어난 답글 머리줄 수와 맞나 — 세는 규칙은 ④-b 와 같은 하나다.
    const grew = fs.readFileSync(md, 'utf-8').split(/\r?\n/).filter((l) => /^>\s*\*\*└/.test(l)).length;
    if (grew !== 2) fail(`md 의 답글이 ${grew}줄입니다 (기존 1 + 새것 1 = 2 여야 합니다)`);
    else pass('md 에도 새 답글 한 줄만 늘었다 (보고와 파일이 같은 말을 한다)');
  }

  // ④ 하나도 새것이 없으면 빈 목록이어야 한다 — 「안 썼는데 썼다고 말하는」 반대 방향.
  const r2 = runPy([INSERT_PY, '--file', md, '--append-thread', '**2026-08-01 09:00 · 봇**',
    '--content-file', content, '--report']);
  if (r2.err) {
    fail(`insert_messages.py --report 두 번째 호출이 실패했습니다 — ${r2.err}`);
  } else {
    let again = null;
    try {
      again = writtenFrom(r2.out, [OLD, NEW]);
    } catch (err) {
      fail(`writtenFrom 이 두 번째 보고를 못 읽었습니다 — ${err.message}`);
    }
    if (again && again.length) {
      fail(`두 번 돌렸는데 ${again.length}건을 또 썼다고 말합니다 — 매일 같은 정정이 DM 에 실립니다.`);
    } else if (again) {
      pass('두 번째 회차는 0건이라고 말한다 (같은 정정이 날마다 다시 안 실린다)');
    }
  }

  // `--report` 를 안 주면 **조용히 0건**이 아니라 던져야 한다 (fail closed).
  const r3 = runPy([INSERT_PY, '--file', md, '--append-thread', '**2026-08-01 09:00 · 봇**',
    '--content-file', content]);
  if (r3.err) {
    fail(`--report 없는 호출이 실패했습니다 — ${r3.err}`);
  } else {
    let threw = false;
    try {
      writtenFrom(r3.out, [OLD, NEW]);
    } catch {
      threw = true;
    }
    if (threw) pass('보고가 없으면 writtenFrom 이 던진다 (0건으로 조용히 넘어가지 않는다)');
    else fail('보고가 없는데도 writtenFrom 이 답을 냈습니다 — 조용한 0건이 됩니다.');
  }

  fs.rmSync(dir, { recursive: true, force: true });

  /* 위 넷은 **재료**(파이썬의 보고 · `writtenFrom`)만 잰다. 부르는 자리
   * (`ingestChannel` 의 ④-c)가 그 재료를 실제로 쓰는지는 `check-bot-reply-count.js` 가
   * 가짜 슬랙으로 잰다 — 거기는 `HERMES_DATA_ROOT` 를 임시 폴더로 돌려놓고 돌아야 해서
   * (모듈 최상위에서 실물 경로가 굳는다) 이 파일 안에서는 못 돌린다. 그래서 **자식
   * 프로세스로 부른다** — `npm run check` 의 목록(`check-setup.js` 의 CROSS_CHECKS)에
   * 직접 넣지 못하는 상태라, 여기서 부르지 않으면 아무 정기 절차도 그 검사를 안 돌린다. */
  const child = path.join(HERE, 'check-bot-reply-count.js');
  if (!fs.existsSync(child)) {
    fail('check-bot-reply-count.js 가 없습니다 — ④-c 가 실제로 실물을 쓰는지 재지 못했습니다.');
  } else {
    // 실패하면 종료코드 1 이라 `execFileSync` 가 던진다 — 그때도 화면을 읽어야 하므로 받는다.
    let r4;
    try {
      r4 = execFileSync(process.execPath, [child], { encoding: 'utf-8' });
    } catch (err) {
      r4 = `${err.stdout || ''}\n${err.stderr || ''}`;
    }
    const fails = r4.split('\n').filter((l) => l.includes('FAIL '));
    if (fails.length) {
      for (const l of fails.slice(0, 5)) fail(l.trim());
      fail(`check-bot-reply-count.js 가 ${fails.length}건 실패했습니다 — 위 재료를 부르는 쪽이 안 쓰고 있습니다.`);
    } else {
      const passes = r4.split('\n').filter((l) => l.includes('PASS ')).length;
      pass(`ingestChannel ④-c 가 그 보고를 실제로 쓴다 (check-bot-reply-count.js ${passes}건 통과)`);
    }
  }
}

if (skipped) {
  console.log('');
  note(`안 다루기로 한 채널 ${skipped}개는 뺐습니다 (digest.skipChannels)`);
}

if (ok) {
  console.log('\n[check-archive-contract] OK — 봇이 쓰는 파서가 실물 아카이브를 읽어냅니다.');
} else {
  console.error('\n고칠 곳: 위에 적힌 채널 md. 규칙의 원본은');
  console.error('  월 헤딩   src/archive.js 의 MONTH_HEADING · readChannel');
  console.error('  메시지 헤더 src/archive.js 의 splitMessages · src/ingest/slack-archive.js 의 parseBlocks');
  process.exitCode = 1;
}
