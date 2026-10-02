// 문답 비용 분해 — 로그 비용줄의 in/cache-w/cache-r/out 을 요금표로 다시 계산해
// 「캐시 쓰기 / 새 입력 / 캐시 읽기 / 출력」 몫을 낸다. 읽기 전용.
//
//   node scripts/cost-breakdown.mjs
//
// ── 출처 ──
// 2026-09-10 의 「문답 비용 주범 재측정」(active-todos 102행) 에서 스크래치패드에 쓴
// cost-analysis.mjs 를 옮긴 것이다 (2026-09-11, todo:104 「분해 스크립트를 저장소에 두기」).
//
// **집계 로직(어떤 수를 어떻게 더하는가)은 한 줄도 안 바꿨다.** 원본과 다른 곳은 넷이고,
// 넷 다 숫자를 바꾸지 않는다:
//   ① 경로 — 스크래치패드 판은 저장소 밖이라 코드·로그 경로를 절대경로로 박았다. 여기서는
//      상대 import 와 config 의 `LOG_DIR` 을 쓴다(사용자명이 박힌 경로를 저장소에 안 남긴다).
//   ② 동적 `await import(pathToFileURL(...))` → 정적 `import` (저장소 안이라 상대 경로가 선다)
//   ③ `LOG_ENABLED` 가드 추가 — 로그가 꺼진 설정에서 `readdirSync(null)` 로 죽지 않게.
//   ④ `name.padEnd` → `String(name).padEnd` — `parseToolLine` 이 못 읽은 조각에 `name: null`
//      을 남기므로(그쪽 규약) 그 회차가 섞이면 원본은 던진다.
// (2026-09-11 검토 Minor 3: 「경로 두 줄뿐」이라고 적었던 것이 실제 diff 와 어긋나 고침.)
//
// ── 이 스크립트가 답한 것 (2026-09-10 측정값) ──
// 문답 161건 · 로그 표기 합 $112.50 · 캐시 쓰기 몫 54~66%(5분 캐시 가정 ~ 1시간 캐시 가정).
// 아래를 지금 돌려 나온 숫자가 그것과 다르면, 로그가 그새 늘었거나(9월분 추가) 요금표가
// 바뀐 것이다 — 둘 다 아니면 파서가 조용히 덜 읽은 것을 의심한다.
//
// 파서는 src/convo-log.js 원본을 그대로 쓴다 (여기서 md 를 새로 뜯지 않는다).
import fs from 'node:fs';
import path from 'node:path';
import {
  parseToolLine, parseEntryHeader, parseCostLine, TOOL_LINE_PREFIX, BROADCAST, KIND_LABEL,
} from '../src/convo-log.js';
import { LOG_DIR, LOG_ENABLED } from '../src/config.js';
import { estimateCost } from '../src/format.js';

if (!LOG_ENABLED || !LOG_DIR) {
  console.error('대화 로그가 꺼져 있습니다 (config.json 의 log.enabled · log.path).');
  process.exit(2);
}
const files = fs.readdirSync(LOG_DIR).filter((f) => /^\d{4}-\d{2}\.md$/.test(f)).sort();
const BROADCAST_LABELS = new Set([...BROADCAST].map((k) => KIND_LABEL[k] || k));

/* 줄을 가르고 금액·모델을 읽는 것은 **`parseCostLine` 에 맡긴다** (`src/convo-log/codec.js`).
 *
 * 2026-09-15 에는 여기서 정규식을 따로 썼다. `parseCostLine` 이 그때 **머리말로 모델
 * 유무를 재고**(모델이 실린 `소요` 줄을 「모델 없음」으로 냈다) **확인분 금액을 버려서**
 * 못 믿었기 때문인데, 그렇게 두면 같은 판정이 두 곳이 되고 그게 이 저장소가 `CROSS_CHECKS`
 * 로 경계하는 모양이다. 2026-09-16 에 `parseCostLine` 쪽을 고쳐 여기서 지웠다.
 *
 * 여기 남는 것은 **토큰 내역**뿐이다 — 그건 판정이 아니라 추출이라 갈릴 것이 없다.
 *
 *   > **비용** claude-opus-5 · 56.8초 · in 352 / cache-w 32,101 / cache-r 214,740 / out 2,501 · 약 $0.372
 *   > **소요** claude-opus-5 · 56.8초 · in 352 / … / out 2,501 · 확인분 USD 0.372000 / 미상 1회 (총액 미상)
 */
const TOKENS = /· in ([\d,]+) \/ cache-w ([\d,]+) \/ cache-r ([\d,]+) \/ out ([\d,]+)(?: ·|$)/;

/**
 * 돈이 적힌 줄 하나를 **어떻게 다룰지** 정한다. 아래 훑는 루프는 이 판정을 스스로 하지
 * 않고 이것을 부르기만 한다.
 *
 * **판정을 여기 둔 이유는 검사 때문이다.** 전에는 이 갈래가 훑는 루프 안에 박혀 있었고,
 * `check-usage-accounting.js` 는 이 파일에서 **부품만** 떼어다 봤다. 그래서 루프를
 * 2026-09-15 의 버그로 통째로 되돌려도 검사 32개가 전부 초록이었다 — 확인된 지출이
 * 조용히 사라지는데 아무 신호가 없었다. 판정이 검사가 볼 수 있는 자리에 있어야 한다
 * (2026-09-16 변이 시험에서 드러났다).
 *
 * @returns {null} 돈 줄이 아니다
 * @returns {{kind:'bad'}} 돈 줄인데 못 읽었다 — 숫자를 아예 내지 말아야 한다
 * @returns {{kind:'skip', usd:number|null}} 모델이 없어 못 쪼갠다. 금액은 세어서 말한다
 * @returns {{kind:'count', tok:object}} 모델별로 쪼갤 수 있다
 */
function classifyCostLine(line) {
  const c = parseCostLine(line);
  if (!c) return null;
  if (c.unreadable) return { kind: 'bad' };
  // 사용량 기록과 모델 없는 회차는 쪼갤 수 없다. **그래도 그 돈을 세어서 말한다.**
  if (!c.model) return { kind: 'skip', usd: c.knownCostUsd };
  const m = TOKENS.exec(line);
  if (!m || c.knownCostUsd == null) return { kind: 'bad' };
  const n = (s) => Number(s.replace(/,/g, ''));
  return {
    kind: 'count',
    tok: {
      model: c.model, in: n(m[1]), w: n(m[2]), r: n(m[3]), out: n(m[4]),
      usd: c.knownCostUsd,
      unknown: c.unknownAttempts,
    },
  };
}

/**
 * md 한 파일의 **줄들**을 훑어 회차·못 읽은 줄·못 쪼갠 돈을 모은다.
 *
 * **파일을 안 읽는다.** 검사가 합성 줄로 부를 수 있어야 하기 때문이다 — 전에는 이 갈래가
 * 파일 읽기와 한 덩어리라 저장소의 **어떤 검사도 못 봤고**, 통째로 2026-09-15 의 버그로
 * 되돌려도 검사 32개가 전부 초록이었다 (2026-09-16 변이 시험).
 *
 * @param {string[]} lines md 줄 (개행 제거 전이어도 된다)
 * @param {string} f 어디서 왔는지 — 못 읽은 줄을 사람에게 보일 때만 쓴다
 */
function scanLines(lines, f) {
  const entries = [];
  const bad = [];
  const skipped = [];
  let date = null, cur = null;
  lines.forEach((raw, i) => {
    const line = raw.replace(/\r$/, '');
    const at = `${f}:${i + 1}`;
    const day = /^## (\d{4}-\d{2}-\d{2})$/.exec(line);
    if (day) { date = day[1]; return; }
    if (line.startsWith('### ')) {
      const h = parseEntryHeader(line);
      if (!h || !date) { bad.push(`${at} 헤더`); cur = null; return; }
      cur = { ...h, date, tools: [], tok: null };
      entries.push(cur);
      return;
    }
    /* 회차가 아닌 블록(사용량 기록의 `#### …`)이 시작된다. **앞 회차를 놓는다** —
     * 안 놓으면 그 블록의 금액 줄이 `cur` 을 타고 **앞 회차에 가서 붙는다.** */
    if (line.startsWith('#### ')) { cur = null; return; }
    /* 돈 줄은 `cur` 보다 **먼저** 본다. 회차 밖에도 돈 줄이 있고(사용량 기록),
     * `cur` 이 없다고 건너뛰면 그 돈이 아무 표시 없이 사라진다. */
    const got = classifyCostLine(line);
    if (got) {
      if (got.kind === 'bad') { bad.push(`${at} 비용줄`); return; }
      if (got.kind === 'skip') {
        if (got.usd != null) skipped.push({ where: at, usd: got.usd });
        return;
      }
      // 모델까지 읽었는데 어느 회차 것인지 모른다 — 추측하지 않고 못 읽은 줄로 올린다.
      if (!cur) { bad.push(`${at} 회차 밖 비용줄`); return; }
      cur.tok = got.tok;
      return;
    }
    if (!cur) return;
    if (line.startsWith(TOOL_LINE_PREFIX)) {
      const tools = parseToolLine(line);
      if (!tools) { bad.push(`${at} 도구줄`); return; }
      cur.tools = tools;
    }
  });
  return { entries, bad, skipped };
}

/* 모델이 안 실린 줄에도 **금액은 실릴 수 있다** (`> **소요** 4.0초 · 약 $0.250` 과
 * 사용량 기록 `> **계측 금액** …`). 분해는 모델별 단가가 있어야 하니 그 회차는 못
 * 쪼개지만, **그냥 건너뛰면 그 돈이 아무 표시 없이 사라진다.** 빠뜨린 것을 세어 두었다가
 * 아래에서 합과 함께 말한다 (2026-09-16). */
const skippedUsd = [];
const entries = [];
const bad = [];
for (const f of files) {
  const r = scanLines(fs.readFileSync(path.join(LOG_DIR, f), 'utf8').split('\n'), f);
  entries.push(...r.entries); bad.push(...r.bad); skippedUsd.push(...r.skipped);
}
if (bad.length) { console.error('읽다 만 줄 — 숫자 안 냄:', bad.slice(0, 10)); process.exit(1); }

const qa = entries.filter((e) => !BROADCAST_LABELS.has(e.kind) && e.tok);
console.log(`회차 ${entries.length} · 비용 기록된 문답 ${qa.length}`);

/* 미상 시도가 섞인 회차(`소요` 줄)는 적힌 금액이 **확인분까지**라 바닥값이다. 조용히
 * 더하면 「로그 표기 합」이 아무 표시 없이 적게 찍힌다 — 그 표시를 여기서 낸다. */
const partial = qa.filter((e) => e.tok.unknown);
if (partial.length) {
  const misses = partial.reduce((a, e) => a + e.tok.unknown, 0);
  console.warn(
    `⚠ 문답 ${partial.length}건에 미상 시도가 ${misses}회 섞여 있습니다 — `
    + '「로그 표기 합」은 확인분까지이고 실제 지출은 그보다 큽니다.',
  );
}
if (skippedUsd.length) {
  const sum = skippedUsd.reduce((a, x) => a + x.usd, 0);
  console.warn(
    `⚠ 모델·사용량이 안 실려 분해에서 뺀 회차 ${skippedUsd.length}건 (합 $${sum.toFixed(2)}) — `
    + '아래 어느 숫자에도 안 들어갑니다.',
  );
  console.warn(`  ${skippedUsd.slice(0, 5).map((x) => x.where).join(' · ')}`
    + (skippedUsd.length > 5 ? ` … 그 밖 ${skippedUsd.length - 5}건` : ''));
}

/* ── 1) 비용 분해
 *
 * **단가표를 여기 적지 않는다.** 예전에는 `IN = 5, OUT = 25` 가 박혀 있었고, 그것이
 * 저장소의 **세 번째** 단가표였다 — `src/format.js` 의 RATES 와 `scripts/check-live-llm.js`
 * 의 VERIFIED_RATES 옆에. 단가가 바뀌면 어느 하나는 반드시 안 고쳐지고, 그때 나오는 것은
 * 에러가 아니라 **틀린 돈 숫자**다 (2026-09-15).
 *
 * 그래서 `estimateCost` 에 1e6 토큰을 한 종류씩 넣어 $/M 단가를 되받는다. RATES 가
 * 안 보이는 자리에서도 원본이 하나로 유지되고, 모델이 섞여도 회차마다 제 단가로 센다. */
const rates = new Map();
const rate = (model) => {
  if (!rates.has(model)) {
    const at = (u) => estimateCost(model, u).usd; // 1e6 토큰 = $/M 단가 그대로
    rates.set(model, {
      in: at({ input_tokens: 1e6 }),
      out: at({ output_tokens: 1e6 }),
      read: at({ cache_read_input_tokens: 1e6 }),
      write5: at({ cache_creation_input_tokens: 1e6, cache_creation: { ephemeral_5m_input_tokens: 1e6 } }),
      write1h: at({ cache_creation_input_tokens: 1e6, cache_creation: { ephemeral_1h_input_tokens: 1e6 } }),
      known: estimateCost(model, {}).rateKnown,
    });
  }
  return rates.get(model);
};
const unpriced = [...new Set(qa.map((e) => e.tok.model))].filter((m) => !rate(m).known);
if (unpriced.length) {
  console.warn(`⚠ 단가표에 없는 모델이 섞여 있어 opus 기본 단가로 추정합니다: ${unpriced.join(', ')}`);
  console.warn('  그 회차의 $ 는 틀립니다. src/format.js 의 RATES 에 단가를 넣으세요.');
}
for (const [label, wKey] of [['cache-w 전부 5분(1.25배) 가정', 'write5'], ['cache-w 전부 1시간(2배) 가정', 'write1h']]) {
  let cin = 0, cw = 0, cr = 0, cout = 0, logged = 0;
  for (const e of qa) {
    const p = rate(e.tok.model);
    cin += e.tok.in * p.in / 1e6; cw += e.tok.w * p[wKey] / 1e6;
    cr += e.tok.r * p.read / 1e6; cout += e.tok.out * p.out / 1e6; logged += e.tok.usd;
  }
  const tot = cin + cw + cr + cout;
  const pct = (x) => `${(100 * x / tot).toFixed(1)}%`;
  console.log(`\n[${label}] 재계산 합 $${tot.toFixed(2)} (로그 표기 합 $${logged.toFixed(2)})`);
  console.log(`  새 입력 in    $${cin.toFixed(2)} (${pct(cin)})`);
  console.log(`  캐시 쓰기 w   $${cw.toFixed(2)} (${pct(cw)})`);
  console.log(`  캐시 읽기 r   $${cr.toFixed(2)} (${pct(cr)})`);
  console.log(`  출력 out      $${cout.toFixed(2)} (${pct(cout)})`);
}

// ── 2) 도구별 결과 크기 (크기 기록된 호출만 — run-log-measure 와 같은 규칙)
const byTool = new Map();
for (const e of qa) for (const c of e.tools) {
  if (!byTool.has(c.name)) byTool.set(c.name, { n: 0, sized: 0, chars: 0, list: [] });
  const b = byTool.get(c.name);
  b.n += 1;
  if (c.chars != null) { b.sized += 1; b.chars += c.chars; b.list.push(c.chars); }
}
const med = (xs) => { const s = [...xs].sort((a, b) => a - b); return s.length ? s[s.length >> 1] : 0; };
console.log('\n[도구별 — 문답 회차만] 호출 / 크기기록 / 합계자수 / 평균 / 중앙값');
let allChars = 0;
for (const [name, b] of [...byTool].sort((a, b) => b[1].chars - a[1].chars)) {
  allChars += b.chars;
  console.log(`  ${String(name).padEnd(18)} ${String(b.n).padStart(4)} / ${String(b.sized).padStart(4)} / ${b.chars.toLocaleString().padStart(11)} / ${Math.round(b.chars / (b.sized || 1)).toLocaleString().padStart(7)} / ${med(b.list).toLocaleString().padStart(7)}`);
}
console.log(`  합계 자수 ${allChars.toLocaleString()}`);

// ── 3) 25,134 출처 후보: search 크기기록 호출의 평균 (전 기간 / 기간별)
const searches = [];
for (const e of entries) for (const c of e.tools) if (c.name === 'search' && c.chars != null) searches.push({ date: e.date, chars: c.chars });
const avg = (xs) => Math.round(xs.reduce((a, b) => a + b, 0) / (xs.length || 1));
console.log(`\n[search 크기기록 호출] 전체 ${searches.length}건 · 평균 ${avg(searches.map((s) => s.chars)).toLocaleString()}자 · 중앙값 ${med(searches.map((s) => s.chars)).toLocaleString()}자`);
for (const cut of ['2026-08-28', '2026-09-01', '2026-09-09']) {
  const a = searches.filter((s) => s.date < cut), b = searches.filter((s) => s.date >= cut);
  console.log(`  ${cut} 전: ${a.length}건 평균 ${avg(a.map((s) => s.chars)).toLocaleString()} · 후: ${b.length}건 평균 ${avg(b.map((s) => s.chars)).toLocaleString()}`);
}

// ── 4) 회차당 구조: 도구 호출수·회차수와 캐시쓰기의 관계
const rounds = qa.map((e) => ({ calls: e.tools.length, w: e.tok.w, chars: e.tools.reduce((a, c) => a + (c.chars ?? 0), 0), usd: e.tok.usd }));
const corrTop = [...rounds].sort((a, b) => b.usd - a.usd).slice(0, 8);
console.log('\n[비싼 문답 상위 8] $ / 도구호출 / 도구자수합 / cache-w 토큰');
for (const r of corrTop) console.log(`  $${r.usd.toFixed(3)} / ${String(r.calls).padStart(2)}회 / ${r.chars.toLocaleString().padStart(9)}자 / ${r.w.toLocaleString()}`);
const sum = (xs) => xs.reduce((a, b) => a + b, 0);
console.log(`\n문답 합계: $${sum(qa.map((e) => e.tok.usd)).toFixed(2)} · 도구 0회 문답 ${qa.filter((e) => !e.tools.length).length}건 / ${qa.length}건`);
