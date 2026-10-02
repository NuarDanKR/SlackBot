#!/usr/bin/env node
/**
 * **도구** 줄이 쓴 대로 다시 읽히나 — `toolLine` ↔ `parseToolLine` 왕복.
 *
 *     node scripts/check-tool-line-roundtrip.js
 *
 * 종료코드: 0 통과 / 1 실패
 *
 * ── 왜 있나 ──
 *
 * `hermes-log` 의 **도구** 줄은 **사람이 읽으라고 만든 줄**인데, 재보기 때마다 그 줄을
 * 기계가 다시 읽는다(도구 호출 건수·읽은 크기·상한에 걸린 비율). 그런데 그 줄은
 * 되돌리기 어렵게 생겼다 — 인자 **값 안에** 괄호·쉼표가 그대로 들어가고, 80자에서
 * 잘리고, 크기(`N자`)는 있을 수도 없을 수도 있다.
 *
 * 그래서 md 만 보고 새로 쓴 정규식은 **틀려도 에러가 안 난다.** 조각이 조용히 빠지고
 * 숫자만 작아진다. 2026-09-06 재보기에서 실제로 났다 — `\([^)]*\)` 로 인자를 잡아
 * 문서 이름에 괄호가 든 회차를 놓쳤고, 기준선(배포 전 창)을 다시 재서 대보고서야
 * 드러났다. **대조할 기준선을 아는 사람이 있을 때만 드러나는 결함이다.**
 *
 * 그리고 그때 쓴 스크립트는 저장소에 없었다. 다음 사람은 파서를 처음부터 다시 쓴다.
 * 그래서 파서를 `convo-log.js` 의 `toolLine` **바로 옆에** 두고, 이 검사가 둘을
 * 맞대 본다. 렌더가 바뀌면 여기가 빨개진다.
 *
 * **파일도 네트워크도 안 쓴다** — `renderEntry` 에 합성 항목을 직접 먹인다
 * (`check-error-block.js` 와 같은 방식). 실물 로그가 이 기계에 있으면 ③에서 덤으로 훑는다.
 *
 * ── 못 잡는 구간 ──
 *
 *   · 인자 값이 **80자에서 잘린** 것은 원문을 되찾을 수 없다. 되찾은 척하지 않고
 *     `truncated: true` 로 표시만 한다 — 재보기에서 인자로 세면 안 되는 자리다.
 *   · 인자 값 안에 줄바꿈이 들어오면 md 한 줄이 깨져 이 파서로도 못 읽는다.
 *     지금 도구 인자는 전부 한 줄짜리 문자열이라 안 난다.
 *   · 인자 값 안에 `" → 이름("` 이 통째로 들어오면 조각 경계와 구별되지 않는다.
 *     ②의 마지막 항목이 그 자리를 못 박아 둔다 — 갈래가 갈리면 여기가 빨개진다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { renderEntry, parseToolLine, parseEntryHeader, parseCostLine, TOOL_LINE_PREFIX } from '../src/convo-log.js';
import { LOG_DIR } from '../src/config.js';
import { createLogCodec } from '../src/convo-log/codec.js';
import { KIND_LABEL, toolLine } from '../src/convo-log.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

// Codex R3a: real ESM facade and extracted codec must agree, not just test stubs.
const codec = createLogCodec({ KIND_LABEL, num: v => Number(v || 0).toLocaleString('en-US') });
const codecTool = { name: 'search', input: { query: 'fixture(a)' }, chars: 0 };
if (toolLine(codecTool) !== codec.toolLine(codecTool) || TOOL_LINE_PREFIX !== codec.TOOL_LINE_PREFIX) {
  fail('convo-log facade 와 codec 의 렌더/접두 연결이 다릅니다');
}
for (const [name, call, sample] of [
  ['parseToolLine', parseToolLine, TOOL_LINE_PREFIX + toolLine(codecTool)],
  ['parseEntryHeader', parseEntryHeader, '### 12:34 · fixture · channel · 문답'],
  ['parseCostLine', parseCostLine, '> **소요** 1.0초 · 총액 미상'],
]) {
  if (JSON.stringify(call(sample)) !== JSON.stringify(codec[name](sample))) fail(name + ' facade 연결 불일치');
}

/* 한 항목을 렌더해서 **도구** 줄만 꺼낸다. 줄 모양을 여기서 또 적지 않으려고
 * 접두어는 convo-log.js 에서 가져온다 — 여기 베끼면 저쪽이 바뀔 때 조용히 갈린다. */
function toolLineOf(entry) {
  const md = renderEntry(entry);
  for (const line of md.split('\n')) if (line.startsWith(TOOL_LINE_PREFIX)) return line;
  return null;
}

/* ── ① 한 조각씩 — 되찾아야 하는 것과 되찾을 수 없는 것 ──────────────────
 *
 * 사업장·문서 이름은 지어낸 것이다. 이 저장소는 팀끼리 나눠 쓰므로 실제 이름을
 * 픽스처에 넣지 않는다 (`check-business-names.js`).
 *
 * `want` 는 「이 형식이 약속하는 것」이다. `toolLine` 의 조립 규칙을 여기 다시
 * 구현하지 않고 **기대값을 손으로 적는다** — 저쪽이 이어붙이는 방식을 바꾸면
 * 이 표와 어긋나 빨개지는 것이 목적이다. */
const CASES = [
  {
    why: '평범한 것',
    input: { query: '수주 현황' }, chars: 55490,
    want: { name: 'search', args: '수주 현황', chars: 55490, truncated: false },
  },
  {
    why: '문서 이름에 괄호가 든 것 (2026-09-06 에 실제로 놓친 모양)',
    input: { project: '예시사업장', document: '현황보고 (원리금 및 상환재원)' }, chars: 4785,
    want: { name: 'read_document', args: '예시사업장, 현황보고 (원리금 및 상환재원)', chars: 4785, truncated: false },
  },
  {
    why: '인자 값 안에 쉼표가 든 것 — 인자 구분자와 구별이 안 되는 자리',
    input: { project: '예시사업장', document: '산정 내역 (26.09.05 기준, v10)', sheet: '대출이자' }, chars: 31092,
    want: { name: 'read_document', args: '예시사업장, 산정 내역 (26.09.05 기준, v10), 대출이자', chars: 31092, truncated: false },
  },
  {
    why: '크기가 안 적힌 옛 기록 — 0자가 아니라 「모른다」여야 한다',
    input: { query: '내방 예정' }, chars: null,
    want: { name: 'search', args: '내방 예정', chars: null, truncated: false },
  },
  {
    why: '크기가 0 인 것 — 「모른다」와 달라야 한다',
    input: { query: '없는 낱말' }, chars: 0,
    want: { name: 'search', args: '없는 낱말', chars: 0, truncated: false },
  },
  {
    why: '80자에서 잘린 것 — 되찾은 척하지 않고 표시만 한다',
    input: { query: '가'.repeat(120) }, chars: 1234,
    want: { name: 'search', args: `${'가'.repeat(80)}…`, chars: 1234, truncated: true },
  },
  {
    why: '인자가 하나도 없는 것',
    input: {}, chars: 12,
    want: { name: 'list_projects', args: '', chars: 12, truncated: false },
  },
  {
    why: '값이 문자열이 아닌 것 (JSON 으로 실린다)',
    input: { limit: 3 }, chars: 100,
    want: { name: 'search', args: '3', chars: 100, truncated: false },
  },
  {
    why: '값이 숫자·문자로 끝나 「N자」처럼 보이는 것 — 크기로 오인하면 안 된다',
    input: { query: '분양가 1,234자' }, chars: null,
    want: { name: 'search', args: '분양가 1,234자', chars: null, truncated: false },
  },
  { why: '좁힘 꼬리 — 크기와 함께',
    input: { query: '어느 사업장 진행현황' }, chars: 4555,
    narrowedTo: '대화는 #사업장가 · 문서는 사업장가',
    want: { name: 'search', args: '어느 사업장 진행현황', chars: 4555,
            narrowedTo: '대화는 #사업장가 · 문서는 사업장가', widenedFrom: null } },
  { why: '넓힘만 — 크기 없음',
    input: { query: 'ㄱ' }, chars: null, widenedFrom: '대화',
    want: { name: 'search', args: 'ㄱ', chars: null, narrowedTo: null, widenedFrom: '대화' } },
  { why: '꼬리 값의 괄호·대괄호 — 렌더가 지워 파서 경계를 지킨다',
    input: { query: 'ㄴ' }, chars: null, narrowedTo: '문서는 사업장(가칭)[2차]',
    want: { name: 'search', args: 'ㄴ', chars: null, narrowedTo: '문서는 사업장가칭2차', widenedFrom: null } },
  /* **둘이 함께 붙는 것 — 이것이 실제로 나가는 조합이다** (claude.js 의 run 은 대화·문서를
   * 따로 잡아 「대화는 좁히고 문서는 넓혔다」를 한 줄에 낸다). 하나씩만 있는 픽스처는
   * 파서의 두 선택 그룹이 **순서와 무관하게** 맞아서, 렌더 순서를 뒤바꿔도 전부 통과한다 —
   * 그러면 production 의 「둘 다」 줄만 `$` 에서 실패해 조용히 `name: null` 이 된다. */
  { why: '좁힘·넓힘이 함께 붙는 것 — 순서가 뒤바뀌면 이 줄만 빨개진다',
    input: { query: 'ㄷ' }, chars: 777,
    narrowedTo: '대화는 #사업장가', widenedFrom: '문서',
    want: { name: 'search', args: 'ㄷ', chars: 777,
            narrowedTo: '대화는 #사업장가', widenedFrom: '문서' } },
  /* 지우고 나면 빈 꼬리 — `[좁힘: ]` 를 찍으면 파서의 `[^\]]+` 에 안 걸려 조각이 통째로
   * 안 읽힌다. 렌더가 아예 안 붙이는 쪽을 못 박는다 (toolLine 주석). */
  { why: '꼬리 값이 전부 괄호·대괄호 — 빈 꼬리를 찍으면 조각이 통째로 안 읽힌다',
    input: { query: 'ㄹ' }, chars: null, narrowedTo: '()[]',
    want: { name: 'search', args: 'ㄹ', chars: null, narrowedTo: null, widenedFrom: null } },
];

const NAME_OF = {
  search: 'search', read_document: 'read_document', list_projects: 'list_projects',
};
for (const c of CASES) {
  const name = c.want.name;
  if (!NAME_OF[name]) { fail(`픽스처의 도구 이름이 표에 없습니다: ${name}`); continue; }
  const line = toolLineOf({
    at: '2026-08-20T05:00:00.000Z', kind: 'qa', asker: '아무개', origin: '#어느채널',
    /* 좁힘·넓힘은 픽스처에 적은 것을 **그대로** 얹는다 — 없으면 undefined 라 toolLine 이
     * 꼬리를 안 붙인다(값이 있을 때만 붙는 규칙을 여기서 다시 구현하지 않는다). */
    toolCalls: [{
      name, input: c.input, ...(c.chars === null ? {} : { chars: c.chars }),
      narrowedTo: c.narrowedTo, widenedFrom: c.widenedFrom,
    }],
  });
  if (!line) { fail(`${c.why} — **도구** 줄이 렌더에 없습니다`); continue; }

  const got = parseToolLine(line);
  if (!Array.isArray(got) || got.length !== 1) {
    fail(`${c.why} — 조각이 1개여야 하는데 ${got === null ? 'null' : got.length} 입니다\n      ${line}`);
    continue;
  }
  const g = got[0];
  /* 비교할 칸 = 픽스처가 적은 것 + **좁힘·넓힘 둘은 늘**. 그 둘은 안 적었으면 `null` 기대다 —
   * 좁힘이 없던 시절 모양의 옛 픽스처가 그 칸을 조용히 안 재고 지나가면, 「값이 없으면
   * 꼬리를 안 붙인다」는 약속(멱등의 근거)이 아무 데서도 안 지켜진다. */
  for (const k of [...new Set([...Object.keys(c.want), 'narrowedTo', 'widenedFrom'])]) {
    const want = k in c.want ? c.want[k] : null;
    if ((g[k] ?? null) !== want) {
      fail(`${c.why} — ${k} 가 다릅니다: ${JSON.stringify(g[k])} (기대 ${JSON.stringify(want)})\n      ${line}`);
    }
  }
}

/* ── ② 여러 조각 — 경계를 어디서 끊나 ────────────────────────────────────
 *
 * 조각은 ' → ' 로 이어진다. 그런데 그 화살표가 인자 **값 안에도** 들어올 수 있다.
 * 그냥 ' → ' 로 자르면 값이 든 조각 하나가 둘로 쪼개지고, **조각 수가 늘어난 채
 * 에러 없이 통과한다** — 재보기에서는 도구를 더 많이 부른 것으로 읽힌다. */
const MULTI = [
  { name: 'search', input: { query: '가나 수주' }, chars: 55490 },
  { name: 'read_document', input: { project: '예시사업장', document: '현안보고(가나지구)', month: '2026-08' }, chars: 152 },
  { name: 'read_channel', input: { channel: '어느채널' }, chars: 6719 },
];
const multiLine = toolLineOf({
  at: '2026-08-20T05:00:00.000Z', kind: 'qa', asker: '아무개', origin: '#어느채널', toolCalls: MULTI,
});
const multi = parseToolLine(multiLine);
if (!multi || multi.length !== MULTI.length) {
  fail(`조각 ${MULTI.length}개를 넣었는데 ${multi === null ? 'null' : multi.length}개로 읽힙니다\n      ${multiLine}`);
} else {
  MULTI.forEach((t, i) => {
    if (multi[i].name !== t.name) fail(`${i + 1}번째 조각의 이름이 다릅니다: ${multi[i].name} (기대 ${t.name})`);
    if (multi[i].chars !== t.chars) fail(`${i + 1}번째 조각의 크기가 다릅니다: ${multi[i].chars} (기대 ${t.chars})`);
  });
}

/* 값 안에 화살표가 든 것. 경계는 「' → ' 다음에 `이름(` 이 오는 자리」뿐이어야 한다. */
const ARROW = [
  { name: 'search', input: { query: '전문 → 목차 전환' }, chars: 100 },
  { name: 'search', input: { query: '뒤엣것' }, chars: 200 },
];
const arrowLine = toolLineOf({
  at: '2026-08-20T05:00:00.000Z', kind: 'qa', asker: '아무개', origin: '#어느채널', toolCalls: ARROW,
});
const arrow = parseToolLine(arrowLine);
if (!arrow || arrow.length !== 2) {
  fail(`인자 값에 ' → ' 가 들어 조각 수가 갈립니다: ${arrow === null ? 'null' : arrow.length}개 (기대 2)\n      ${arrowLine}`);
} else if (arrow[0].args !== '전문 → 목차 전환') {
  fail(`인자 값 안의 화살표에서 잘렸습니다: ${JSON.stringify(arrow[0].args)}\n      ${arrowLine}`);
}

/* 도구를 안 쓴 회차에는 **도구** 줄 자체가 없다. 그때 parseToolLine 은 null 을 낸다 —
 * 빈 배열로 내면 「도구를 0개 썼다」와 「도구 줄이 없다」가 같은 값이 된다. */
if (parseToolLine('> **근거** #어느채널') !== null) {
  fail('**도구** 줄이 아닌 것을 null 로 안 냅니다 — 0개와 「줄이 없다」가 같아집니다');
}

/* ── ②-2 회차 헤더와 비용 줄 ─────────────────────────────────────────────
 *
 * 재보기는 「어느 날 · 어떤 종류 · 얼마」로 묶어서 센다. 그 셋이 이 두 줄에 있다.
 * 도구 줄과 같은 이유로 여기도 되돌리기가 까다롭다 — 사람·자리 이름에 `·` 가 들어갈
 * 수 있고, 모델이 없는 회차는 머리말 자체가 `**소요**` 로 바뀐다. */
const HEADERS = [
  {
    why: '평범한 문답',
    entry: { at: '2026-08-20T05:00:00.000Z', kind: 'qa', asker: '아무개', origin: '#어느채널' },
    want: { time: '14:00', who: '아무개', where: '#어느채널', kind: '문답', ok: true },
  },
  {
    why: '실패한 정기 발송 — 실패 표시가 종류로 안 읽혀야 한다',
    entry: { at: '2026-08-20T05:00:00.000Z', kind: 'daily', ok: false, target: '#어느채널', origin: '2026-08-20' },
    want: { time: '14:00', who: 'Hermes', where: '#어느채널', kind: '일일 요약', ok: false },
  },
  {
    /* 자리 이름에 가운뎃점이 들면 누가/어디의 경계는 **되찾을 수 없다.** 종류만은
     * 앵커(KIND_LABEL)로 정확히 갈린다 — 재보기가 실제로 쓰는 것이 그것이다.
     * 남는 조각이 `who` 로 붙는 것은 임의로 정한 규칙이라, 조용히 바뀌지 않게 여기 못 박는다. */
    why: '자리 이름에 가운뎃점이 든 것 — 종류는 갈리고, 누가/어디는 못 가른다',
    entry: { at: '2026-08-20T05:00:00.000Z', kind: 'qa', asker: '아무개', origin: '#가 · 나' },
    want: { time: '14:00', who: '아무개 · #가', where: '나', kind: '문답', ok: true },
  },
];
for (const h of HEADERS) {
  const line = renderEntry(h.entry).split('\n').find((l) => l.startsWith('### '));
  const got = parseEntryHeader(line);
  if (!got) { fail(`${h.why} — 회차 헤더를 못 읽었습니다\n      ${line}`); continue; }
  for (const k of Object.keys(h.want)) {
    if (got[k] !== h.want[k]) {
      fail(`${h.why} — 헤더의 ${k} 가 다릅니다: ${JSON.stringify(got[k])} (기대 ${JSON.stringify(h.want[k])})\n      ${line}`);
    }
  }
}

const COSTS = [
  {
    why: '모델이 있는 회차',
    entry: { at: '2026-08-20T05:00:00.000Z', kind: 'qa', asker: '아무개', origin: '#어느채널',
      model: 'some-model', elapsedMs: 12300, costUsd: 0.6153 },
    want: { costUsd: 0.615, knownCostUsd: 0.615, unknownAttempts: 0, seconds: 12.3, model: 'some-model', hasModel: true },
  },
  {
    why: '모델이 없는 회차 — 머리말이 **소요** 로 바뀐다',
    entry: { at: '2026-08-20T05:00:00.000Z', kind: 'qa', asker: '아무개', origin: '#어느채널',
      elapsedMs: 4000, costUsd: 0.25 },
    want: { costUsd: 0.25, knownCostUsd: 0.25, unknownAttempts: 0, seconds: 4, model: null, hasModel: false },
  },
  {
    /* **모델이 있는데 머리말이 `소요` 인 회차.** 미상 시도가 하나라도 섞이면 이 모양이다.
     * 이 경우가 시험에 없던 동안 두 가지가 조용히 틀렸다 — ① 머리말로 모델 유무를 재서
     * 모델이 실렸는데 「없음」으로 나왔고 ② `약 $` 만 찾아 **확인된 지출을 통째로 버렸다.**
     * 합계를 내는 쪽이 아무 표시 없이 적게 찍던 자리다 (2026-09-16). */
    why: '모델이 있는데 미상이 섞인 회차 — 머리말은 **소요** 이고 확인분이 따로 실린다',
    entry: { at: '2026-08-20T05:00:00.000Z', kind: 'qa', asker: '아무개', origin: '#어느채널',
      model: 'some-model', elapsedMs: 12300,
      accounting: { complete: false, knownCostUsd: 0.372, unknownAttempts: 1 } },
    want: { costUsd: null, knownCostUsd: 0.372, unknownAttempts: 1, seconds: 12.3, model: 'some-model', hasModel: true },
  },
  {
    why: '비용을 안 잰 회차 — 0 이 아니라 「모른다」여야 한다',
    entry: { at: '2026-08-20T05:00:00.000Z', kind: 'qa', asker: '아무개', origin: '#어느채널', elapsedMs: 900 },
    want: { costUsd: null, knownCostUsd: null, unknownAttempts: 0, seconds: 0.9, model: null, hasModel: false },
  },
];
for (const c of COSTS) {
  const line = renderEntry(c.entry).split('\n').find((l) => /^> \*\*(비용|소요)\*\*/.test(l));
  const got = parseCostLine(line);
  if (!got) { fail(`${c.why} — 비용 줄을 못 읽었습니다\n      ${line}`); continue; }
  for (const k of Object.keys(c.want)) {
    if (got[k] !== c.want[k]) {
      fail(`${c.why} — 비용 줄의 ${k} 가 다릅니다: ${JSON.stringify(got[k])} (기대 ${JSON.stringify(c.want[k])})\n      ${line}`);
    }
  }
}

/* ── ③ 실물 — 이 기계에 로그가 있으면 한 조각도 안 빠지고 읽히나 ─────────
 *
 * ①②는 우리가 만든 모양만 본다. 실물에는 우리가 아직 생각 못 한 모양이 있다.
 * 없는 기계(코드만 클론한 곳)에서는 **[못잼]** 을 찍는다 — 안 셈을 통과로 읽지 않는다.
 *
 * **걸린 줄의 본문은 화면에 안 낸다.** 도구 인자에는 사업장·문서 이름이 그대로 들어
 * 있어서, 실패할 때마다 그것이 터미널·CI 기록으로 나간다. 자리(파일:줄)와 모양만 내면
 * 사람이 그 줄을 열어 고치기에 충분하다 — 이 저장소가 채널 조회 실패를 낼 때 이미
 * 쓰는 규칙이다(`check-setup.js` 의 「채널 이름(비공개일 수 있다)은 여기 안 넣는다」). */
let lines = 0;
let chunks = 0;
const badLines = [];
let scanned = false;
try {
  for (const f of fs.readdirSync(LOG_DIR)) {
    if (!/^\d{4}-\d{2}\.md$/.test(f)) continue;
    scanned = true;
    fs.readFileSync(path.join(LOG_DIR, f), 'utf8').split('\n').forEach((raw, i) => {
      const line = raw.replace(/\r$/, '');
      if (!line.startsWith(TOOL_LINE_PREFIX)) return;
      lines += 1;
      const at = `${f}:${i + 1}`;
      const got = parseToolLine(line);
      // ' → ' 로 순진하게 자른 개수와 대본다. 파서가 조각을 삼키면 여기서 드러난다.
      const naive = line.slice(TOOL_LINE_PREFIX.length).split(' → ').length;
      if (!got) badLines.push([at, '**도구** 줄인데 읽지 못했습니다']);
      else if (got.some((g) => !g.name)) {
        const which = got.map((g, n) => (g.name ? null : n + 1)).filter(Boolean);
        badLines.push([at, `조각 ${got.length}개 중 ${which.join('·')}번째의 이름이 비었습니다`]);
      } else if (got.length < naive) {
        badLines.push([at, `조각 ${got.length}개 — 순진 분해 ${naive}개보다 적습니다 (삼켜짐)`]);
      } else chunks += got.length;
    });
  }
} catch (e) {
  if (e.code !== 'ENOENT') throw e;
}

if (!scanned) {
  console.log(`[못잼]      · 이 기계엔 렌더된 로그가 없어 실물은 안 댔습니다 (${LOG_DIR})`);
} else if (badLines.length) {
  fail(`실물 로그에서 읽지 못한 **도구** 줄 ${badLines.length}개 (줄 ${lines}개 · 읽어낸 조각 ${chunks}개)`);
  // 줄 본문은 안 낸다 — 인자에 사업장·문서 이름이 들어 있다. 자리만 낸다.
  for (const [at, why] of badLines.slice(0, 10)) console.error(`      ${at} — ${why}`);
  if (badLines.length > 10) console.error(`      … 그 외 ${badLines.length - 10}줄`);
  console.error(`      그 자리를 열어 보세요: ${LOG_DIR}`);
} else {
  // `[보임]` = 통과여도 화면에 올린다. 이 숫자가 조용히 줄어드는 것이 이 검사의 고장 방식이다.
  console.log(`[보임] 실물 **도구** 줄 ${lines}개 · 조각 ${chunks}개를 한 개도 안 빠뜨리고 읽었습니다`);
}

if (!ok) process.exit(1);
console.log('  ✓ **도구** 줄이 쓴 대로 다시 읽힙니다 (괄호·쉼표·화살표·절단·크기 없음)');
