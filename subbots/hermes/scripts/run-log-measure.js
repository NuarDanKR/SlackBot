#!/usr/bin/env node
/**
 * `hermes-log` 를 다시 재는 자리 — 배포 전후를 대볼 때 쓴다.
 *
 *     npm run log:measure
 *     npm run log:measure -- --구간 2026-08-05~2026-08-27=배포전 --구간 2026-08-28T03:09~=배포후
 *
 * `--구간 시작~끝=이름` 은 여러 번 줄 수 있다. 양끝을 포함하고, 비우면 열린 끝이다
 * (`2026-08-28~=배포후` = 8/28 부터 끝까지). 하나도 안 주면 로그 전체를 한 구간으로 낸다.
 * 끝은 **시각까지** 적을 수 있다(`2026-08-28T03:09`) — 배포는 하루 중 어느 시각에 일어난다.
 *
 * 종료코드: 0 잼 / 1 못 잼(읽다 만 줄이 있음) / 2 로그가 없음
 *
 * ── 왜 저장소에 있나 ──
 *
 * 2026-08-28 문서 목차 배포의 효과를 2026-09-06 에 쟀는데, **재는 스크립트가 스크래치패드에
 * 있었고 세션과 함께 사라졌다.** 다음 사람은 파서를 처음부터 다시 쓰고, 그때 한 번 틀렸다 —
 * `\([^)]*\)` 로 인자를 잡아 문서 이름에 괄호가 든 조각을 놓쳤다. 기준선(배포 전 창)을 다시
 * 재서 대보고서야 드러났고, **대조할 기준선을 아는 사람이 없었으면 안 드러났다.**
 *
 * 그래서 파서는 `src/convo-log.js` 의 렌더 **바로 옆에** 있고
 * (`parseToolLine`·`parseEntryHeader`·`parseCostLine`),
 * `scripts/check-tool-line-roundtrip.js` 가 렌더와 파서를 맞대 본다. 이 파일은 그것을
 * 불러 쓰기만 한다 — **여기서 md 를 직접 정규식으로 뜯지 않는다.**
 *
 * ── 이 표가 지키는 규칙 ──
 *
 *   · **「크기 미기록」을 따로 낸다.** 도구 결과 크기(`chars`)는 나중에 생긴 필드라 그전
 *     기록에는 없다. 옛 파서는 `N자` 를 앵커로 삼아 **그 호출을 아예 안 셌다** — 2026-08-28
 *     설계 문서의 「read_document 54건」이 그것이고, 같은 창의 실제 호출은 104건이다
 *     (50건이 크기 미기록). 비율(10/54)은 「잰 것 중에서」로 읽으면 성립하지만, 건수를
 *     나란히 놓는 자리에서는 절반이 빠진 값이 된다. 그래서 칸을 가른다.
 *   · **못 읽은 줄이 하나라도 있으면 숫자를 안 낸다.** 조각이 조용히 빠진 표는 작아진
 *     숫자로 보이지 에러로 보이지 않는다. 이 파일의 고장 방식이 그것이라 아예 막는다.
 *   · **평균과 중앙값을 같이 낸다.** 6만 자에 걸린 몇 건이 평균을 끌어올린다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { parseToolLine, parseEntryHeader, parseCostLine, TOOL_LINE_PREFIX, BROADCAST, KIND_LABEL } from '../src/convo-log.js';
import { DOC_READ_MAX_CHARS } from '../src/documents.js';
import { LOG_DIR, LOG_ENABLED } from '../src/config.js';

if (!LOG_ENABLED || !LOG_DIR) {
  console.error('대화 로그가 꺼져 있습니다 (config.json 의 log.enabled · log.path).');
  process.exit(2);
}

/* 정기 발송의 한글 이름. 「문답만」 기준을 낼 때 쓴다 — `BROADCAST` 는 원래 kind 값
 * (daily 등)이고 렌더된 md 에는 한글 라벨만 남으므로, 그 둘을 여기서 잇는다.
 * 두 값 다 convo-log.js 가 원본이라 목록을 새로 적지 않는다. */
const BROADCAST_LABELS = new Set([...BROADCAST].map((k) => KIND_LABEL[k] || k));

/* ── 읽기 ─────────────────────────────────────────────────────────────── */

/**
 * md 한 파일의 **줄들**을 훑어 회차를 모은다.
 *
 * **파일을 안 읽는다** — 검사가 합성 줄로 부를 수 있어야 하기 때문이다.
 * 전에는 이 갈래가 파일 읽기와 한 덩어리였고, 그래서 이 파일에는 시험이 **하나도**
 * 없었다. 갈래를 통째로 되돌려도 아무 검사가 안 빨개졌다 (2026-09-16 변이 시험 M14).
 * `scripts/check-log-measure.js` 가 이 함수를 몰아 본다.
 *
 * @param {string[]} lines md 줄
 * @param {string} f 어디서 왔는지 — 못 읽은 줄을 사람에게 보일 때만 쓴다
 */
function scanLines(lines, f) {
  const entries = [];
  const bad = [];
  let date = null;
  let cur = null;
  lines.forEach((raw, i) => {
    const line = raw.replace(/\r$/, '');
    const at = `${f}:${i + 1}`;
    const day = /^## (\d{4}-\d{2}-\d{2})$/.exec(line);
    if (day) { date = day[1]; return; }
    if (line.startsWith('### ')) {
      const h = parseEntryHeader(line);
      if (!h) { bad.push([at, '회차 헤더를 못 읽었습니다']); cur = null; return; }
      // 날짜 헤딩보다 회차가 먼저 나오면 어느 날인지 모른다. 「모른다」를 통과시키지 않는다.
      if (!date) { bad.push([at, '앞에 `## 날짜` 헤딩이 없어 어느 날인지 모릅니다']); cur = null; return; }
      cur = { ...h, date, file: f, tools: [], costUsd: null, seconds: null };
      entries.push(cur);
      return;
    }
    /* 회차가 아닌 블록(사용량 기록의 `#### …`)이 시작된다. **앞 회차를 놓는다** —
     * 안 놓으면 그 블록의 `> **계측 금액**` 줄이 아래 `cur.costUsd` 를 타고 **앞 회차의
     * 금액을 덮어쓴다.** 재보기는 회차별 숫자만 보므로 그 줄 자체는 안 센다. */
    if (line.startsWith('#### ')) { cur = null; return; }
    if (!cur) return;
    if (line.startsWith(TOOL_LINE_PREFIX)) {
      const got = parseToolLine(line);
      const naive = line.slice(TOOL_LINE_PREFIX.length).split(' → ').length;
      if (!got) { bad.push([at, '**도구** 줄인데 읽지 못했습니다']); return; }
      if (got.some((g) => !g.name)) { bad.push([at, `조각 ${got.length}개 중 이름이 빈 것이 있습니다`]); return; }
      if (got.length < naive) { bad.push([at, `조각이 삼켜졌습니다 (${got.length} < 순진 분해 ${naive})`]); return; }
      cur.tools = got;
      return;
    }
    const cost = parseCostLine(line);
    if (!cost) return;
    /* 돈 줄인데 못 읽었다. **덮어쓰지 않는다** — 그대로 얹으면 그 회차의 금액이 `null` 이
     * 되어 「공짜였다」와 「못 쟀다」가 같은 칸에 들어간다. 못 읽었다고 말하고 둔다. */
    if (cost.unreadable) { bad.push([at, '비용 줄인데 읽지 못했습니다']); return; }
    cur.costUsd = cost.costUsd;
    cur.seconds = cost.seconds;
  });
  return { entries, bad };
}

function readRendered() {
  const entries = [];
  const bad = [];
  let files = [];
  try {
    files = fs.readdirSync(LOG_DIR).filter((f) => /^\d{4}-\d{2}\.md$/.test(f)).sort();
  } catch (e) {
    if (e.code !== 'ENOENT') throw e;
  }
  for (const f of files) {
    const r = scanLines(fs.readFileSync(path.join(LOG_DIR, f), 'utf8').split('\n'), f);
    entries.push(...r.entries);
    bad.push(...r.bad);
  }
  return { entries, bad, files };
}

/* ── 세기 ─────────────────────────────────────────────────────────────── */

const median = (xs) => {
  if (!xs.length) return null;
  const s = [...xs].sort((a, b) => a - b);
  const m = s.length >> 1;
  return s.length % 2 ? s[m] : Math.round((s[m - 1] + s[m]) / 2);
};
const round = (n, d = 0) => (n == null ? null : Number(n.toFixed(d)));
const num = (n) => (n == null ? '(못 잼)' : Number(n).toLocaleString('en-US'));
const pct = (a, b) => (b ? `${((a / b) * 100).toFixed(1)}%` : '—');

function measure(entries) {
  const qa = entries.filter((e) => !BROADCAST_LABELS.has(e.kind));
  const withTools = entries.filter((e) => e.tools.length);
  const chunks = entries.flatMap((e) => e.tools);
  const sized = chunks.filter((c) => c.chars != null);
  /* **「회차당 도구 자료」의 분모는 「크기가 기록된 회차」다.** 도구를 쓴 회차 전부로 나누면
   * 크기를 모르는 회차가 0자로 섞여 값이 낮아진다 — 2026-08-28 설계 문서가 이 분모를 썼고
   * (「크기가 기록된 42회차, 213호출, 합계 3,387,071자(회차당 80,644자)」), 그래야 그때
   * 기준선이 재현된다. `cacheStats` 가 「진단이 붙은 문답」을 분모로 쓰는 것과 같은 규칙이다. */
  const sizedRounds = entries.filter((e) => e.tools.some((c) => c.chars != null));

  const rd = chunks.filter((c) => c.name === 'read_document');
  const rdSized = rd.filter((c) => c.chars != null);
  const rdBig = rdSized.filter((c) => c.chars >= DOC_READ_MAX_CHARS);

  const costed = entries.filter((e) => e.costUsd != null);
  const qaCosted = qa.filter((e) => e.costUsd != null);
  const sum = (xs) => xs.reduce((a, b) => a + b, 0);

  return {
    days: new Set(entries.map((e) => e.date)).size,
    span: entries.length ? [entries.map((e) => e.date).sort()[0], entries.map((e) => e.date).sort().at(-1)] : null,
    회차: entries.length,
    문답: qa.length,
    정기발송: entries.length - qa.length,
    도구쓴회차: withTools.length,
    크기기록회차: sizedRounds.length,
    조각: chunks.length,
    조각크기기록: sized.length,
    조각크기미기록: chunks.length - sized.length,
    read_document: rd.length,
    rd크기기록: rdSized.length,
    rd크기미기록: rd.length - rdSized.length,
    상한걸림: rdBig.length,
    읽은크기중앙값: median(rdSized.map((c) => c.chars)),
    읽은크기평균: rdSized.length ? Math.round(sum(rdSized.map((c) => c.chars)) / rdSized.length) : null,
    회차당도구자료: sizedRounds.length ? Math.round(sum(sized.map((c) => c.chars)) / sizedRounds.length) : null,
    회차당비용: costed.length ? round(sum(costed.map((e) => e.costUsd)) / costed.length, 3) : null,
    문답회차당비용: qaCosted.length ? round(sum(qaCosted.map((e) => e.costUsd)) / qaCosted.length, 3) : null,
    비용미기록: entries.length - costed.length,
    도구10회이상: withTools.filter((e) => e.tools.length >= 10).length,
    최다도구: withTools.length ? Math.max(...withTools.map((e) => e.tools.length)) : 0,
  };
}

/* ── 구간 ─────────────────────────────────────────────────────────────── */

/* 끝을 **날짜가 아니라 시각**으로 자를 수 있어야 한다. 배포 전후를 대보는 것이 이
 * 도구의 쓰임인데, 배포는 하루 중 어느 시각에 일어난다 — 날짜로만 자르면 배포 당일
 * 새벽의 옛 회차가 「배포 후」에 섞인다. 2026-09-06 재보기가 03:09 로 잘랐고, 날짜로만
 * 자르면 그 회차 4건(그중 상한에 걸린 것 2건)이 넘어와 값이 달라진다.
 *
 * `시각`을 안 적으면 시작은 그날 00:00, 끝은 그날 23:59 로 본다 (양끝 포함).
 * 구분자가 `:` 라서 시각의 `:` 와 겹치므로 날짜·시각은 `2026-08-28T03:09` 로 붙여 적는다. */
const bound = (v, end) => {
  if (!v) return '';
  if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return `${v}T${end ? '23:59' : '00:00'}`;
  if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(v)) return v;
  console.error(`구간의 날짜 모양이 아닙니다: ${v} (YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM)`);
  process.exit(1);
};

const args = process.argv.slice(2);
const windows = [];
for (let i = 0; i < args.length; i += 1) {
  if (args[i] !== '--구간') continue;
  const spec = args[i + 1];
  if (!spec) { console.error('`--구간` 뒤에 `시작~끝=이름` 이 없습니다'); process.exit(1); }
  /* 구분자가 `~` 와 `=` 인 이유: 시각의 `:` 와 안 부딪히게. `:` 로 가르면
   * `2026-08-28T03:09` 이 세 조각으로 쪼개진다. */
  const eq = spec.indexOf('=');
  const range = eq < 0 ? spec : spec.slice(0, eq);
  const label = eq < 0 ? '' : spec.slice(eq + 1);
  const [from = '', to = ''] = range.split('~');
  if (!range.includes('~')) {
    console.error(`구간에 \`~\` 가 없습니다: ${spec} (모양은 \`시작~끝=이름\`)`);
    process.exit(1);
  }
  windows.push({
    from: bound(from, false),
    to: bound(to, true),
    label: label || `${from || '처음'} ~ ${to || '끝'}`,
  });
}

const { entries, bad, files } = readRendered();

if (!files.length) {
  console.error(`렌더된 로그가 없습니다: ${LOG_DIR}`);
  process.exit(2);
}

/* **못 읽은 줄이 있으면 숫자를 안 낸다.** 조각이 빠진 표는 「작아진 숫자」로 보이지
 * 에러로 보이지 않는다 — 이 파일이 막으려는 것이 바로 그 모양이다.
 * 줄 본문은 안 낸다 (도구 인자에 사업장·문서 이름이 들어 있다). 자리만 낸다. */
if (bad.length) {
  console.error(`읽다 만 줄이 ${bad.length}개 있어 **숫자를 내지 않습니다.**\n`);
  for (const [at, why] of bad.slice(0, 20)) console.error(`  ${at} — ${why}`);
  if (bad.length > 20) console.error(`  … 그 외 ${bad.length - 20}줄`);
  console.error(`\n  그 자리: ${LOG_DIR}`);
  console.error('  렌더가 바뀐 것이면 src/convo-log.js 의 파서를 함께 고치고');
  console.error('  node scripts/check-tool-line-roundtrip.js 로 왕복을 다시 확인하세요.');
  process.exit(1);
}

if (!windows.length) {
  windows.push({ from: '', to: '', label: '전체' });
}

const rows = windows.map((w) => {
  // 회차의 자리는 `날짜T시각` 이다 — 구간 양끝과 같은 모양으로 맞춰 대본다.
  const sel = entries.filter((e) => {
    const at = `${e.date}T${e.time}`;
    return (!w.from || at >= w.from) && (!w.to || at <= w.to);
  });
  return { ...w, n: sel.length, m: measure(sel) };
});

/* ── 내기 ─────────────────────────────────────────────────────────────── */

console.log(`\nHermes 대화 로그 재보기 — 파일 ${files.length}개 · 회차 ${entries.length}개 · 읽다 만 줄 0개`);
console.log(`상한 기준: documentReadMaxChars = ${num(DOC_READ_MAX_CHARS)}자 (config.json)\n`);

const LINES = [
  ['기간', (m) => (m.span ? `${m.span[0]} ~ ${m.span[1]} (${m.days}일)` : '—')],
  ['회차 (문답 / 정기 발송)', (m) => `${m.회차} (${m.문답} / ${m.정기발송})`],
  ['도구 쓴 회차 · 조각', (m) => `${m.도구쓴회차}회차 · ${m.조각}조각`],
  ['  크기 기록된 회차 · 호출', (m) => `${m.크기기록회차}회차 · ${m.조각크기기록}호출`],
  ['read_document 호출', (m) => `${m.read_document}`],
  ['  그중 크기 기록됨', (m) => `${m.rd크기기록}`],
  ['  그중 크기 미기록', (m) => `${m.rd크기미기록}${m.rd크기미기록 ? ' ← 세는 자리에서 빠집니다' : ''}`],
  [`  ${num(DOC_READ_MAX_CHARS)}자 이상`, (m) => `${m.상한걸림} (크기 기록된 것의 ${pct(m.상한걸림, m.rd크기기록)})`],
  ['읽은 크기 중앙값', (m) => `${num(m.읽은크기중앙값)}자`],
  ['읽은 크기 평균', (m) => `${num(m.읽은크기평균)}자`],
  ['회차당 도구 자료', (m) => `${num(m.회차당도구자료)}자`],
  ['  (분모: 크기 기록된 회차)', (m) => `${m.크기기록회차}`],
  ['회차당 비용 (전체)', (m) => (m.회차당비용 == null ? '(못 잼)' : `$${m.회차당비용.toFixed(3)}`)],
  ['회차당 비용 (문답만)', (m) => (m.문답회차당비용 == null ? '(못 잼)' : `$${m.문답회차당비용.toFixed(3)}`)],
  ['  비용 미기록 회차', (m) => `${m.비용미기록}`],
  ['도구 10회 이상 회차', (m) => `${m.도구10회이상}`],
  ['최다 도구 호출', (m) => `${m.최다도구}회`],
];

/* 칸 너비는 **라벨이 아니라 실제로 찍히는 값**에서 잰다. 라벨만 보고 정하면 값이 더 길 때
 * 칸끼리 딱 붙어 「10 (…18.5%)1 (…3.6%)」 처럼 두 구간이 한 덩어리로 읽힌다 —
 * 표가 틀린 것은 아닌데 사람이 잘못 읽고, 기계로 대볼 때도 갈린다.
 *
 * 한글·한자는 터미널에서 두 칸을 먹으므로 그렇게 센다. 글자 수로 세면 한글이 든 줄만
 * 밀린다. */
const wide = (c) => {
  const u = c.codePointAt(0);
  return (u >= 0x1100 && u <= 0x115f) || (u >= 0x2e80 && u <= 0xa4cf)
    || (u >= 0xac00 && u <= 0xd7a3) || (u >= 0xf900 && u <= 0xfaff)
    || (u >= 0xfe30 && u <= 0xfe6f) || (u >= 0xff00 && u <= 0xff60)
    || (u >= 0xffe0 && u <= 0xffe6);
};
const width = (s) => [...String(s)].reduce((a, c) => a + (wide(c) ? 2 : 1), 0);
const cell = (s, w) => String(s) + ' '.repeat(Math.max(0, w - width(s)));

const values = LINES.map(([, get]) => rows.map((r) => (r.n ? get(r.m) : '(회차 없음)')));
const W = Math.max(22, ...LINES.map(([l]) => width(l))) + 2;
const colW = Math.max(
  ...rows.map((r) => width(r.label)),
  ...values.flat().map((v) => width(v)),
) + 2;

console.log(`${cell('', W)}${rows.map((r) => cell(r.label, colW)).join('')}`);
console.log('─'.repeat(W + colW * rows.length));
LINES.forEach(([label], i) => {
  console.log(`${cell(label, W)}${values[i].map((v) => cell(v, colW)).join('')}`);
});

const 미기록 = rows.reduce((a, r) => a + r.m.rd크기미기록, 0);
if (미기록) {
  console.log(`\n「크기 미기록」 ${미기록}건은 도구 결과 크기(chars)가 로그에 안 실린 호출입니다.`);
  console.log('그 필드는 나중에 생겼습니다 — 0자가 아니라 **크기를 모르는 것**이라,');
  console.log('중앙값·평균·회차당 자료·상한 비율에서 전부 빠져 있습니다.');
  console.log('건수를 나란히 놓고 대볼 때는 「read_document 호출」 줄을 보세요.');
}
console.log('');
console.log('비용 평균은 총액이 기록된 회차만 대상으로 합니다. 미상 회차의 확인분은 평균에서 제외합니다.');
