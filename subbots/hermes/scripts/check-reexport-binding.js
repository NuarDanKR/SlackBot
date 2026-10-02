#!/usr/bin/env node
/**
 * **재수출만 해 놓고 그 이름을 자기 파일에서 쓰는 자리**를 잡는다.
 *
 *   node scripts/check-reexport-binding.js
 *
 * 종료코드: 0 통과 / 1 그런 자리 있음
 *
 * ── 왜 필요한가 (2026-09-11 리뷰 Critical) ──
 *
 * `export { X } from './y.js'` 는 **재수출 전용**이다 — 밖으로 내보내기만 하고 그 파일
 * 자신의 스코프에는 `X` 라는 이름을 **안 만든다.** 그래서 같은 파일이 `X` 를 맨
 * 식별자로 쓰면 그 줄이 실행되는 순간 `ReferenceError` 로 죽는다.
 *
 * 실제로 났다: `BOT_ANSWER_MARK` 를 `config.js` 로 옮기며 `slack-live.js` 에 재수출 한
 * 줄만 남겼는데, 그 파일이 세 곳에서 그 이름을 맨 식별자로 쓰고 있었다 —
 * **이어 묻기 스레드의 실시간 조회가 광범위하게 크래시**하는 자리였다. 사람이 읽어서
 * 잡았고, `npm run check` 의 어떤 검사도 이 종류를 안 봤다.
 *
 * 고친 모양은 `import { X } from './y.js'` + `export { X };` 다 — 그러면 로컬 바인딩이
 * 생기고 재수출도 그대로 산다.
 *
 * ── 어떻게 재나 ──
 *
 * [1/3] 합성 픽스처 — 일부러 그 모양으로 적은 소스 여섯 벌을 가려내는지 본다. **지금
 *       실물에는 그런 자리가 하나도 없어서**(고쳐 놨다), 이 검사가 정말 무언가를
 *       잡을 수 있는지는 픽스처로만 증명된다. 양성 대조 없이 실물 훑기만 두면 「아무것도
 *       안 잡는 검사가 늘 초록」인 자리가 된다.
 * [2/3] 지우기가 갈피를 잃었을 때 **신고가 올라오는지** — 아래 「조용한 초록」 절 참고.
 * [3/3] 실물 — `src/`·`scripts/` 전체를 훑어 그런 자리가 없는지 본다.
 *
 * 이름이 **주석·문자열 안**에만 나오는 것은 사용이 아니다(이 저장소는 주석에서 이름을
 * 자주 부른다). 그래서 `_code-only.js` 로 주석·문자열·정규식 속을 지운 사본에서 센다.
 *
 * ── 이 검사가 조용히 초록이 될 수 있는 자리 (2026-09-11 회의적 검증이 찾음) ──
 *
 * 지우기가 틀려 **코드가 지워지면 잡을 대상이 사라져 「그런 자리 없음」**이 된다 —
 * 다른 검사들과 달리 여기서는 오작동이 **조용한 초록** 쪽이다. 실측으로, 사고 모양을
 * 되살린 `slack-live.js` 에 평범한 한 줄(`for (const x of p) /['"]/.test(x);`)을 끼우면
 * 끼운 자리에 따라 **최대 51%** 에서 진짜 위반을 놓쳤다.
 *
 * 그래서 **`codeOnlyChecked` 의 `suspicious` 를 반드시 보고, 있으면 빨갛게 낸다.**
 * 판정 근거는 문법상 불가능한 상태(줄바꿈을 품은 정규식·따옴표 문자열)라 취향이 아니다.
 * 훑을 파일이 0개인 것도 실패로 낸다 — 「아무것도 안 재고 초록」이 같은 모양이라서다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { codeOnlyChecked } from './_code-only.js';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

/** `export { a, b as c } from '…'` — **줄 맨 앞**의 것만 본다.
 *
 * 줄 맨 앞으로 못 박는 이유: 이 저장소는 주석 안에서 이 모양을 **글로 설명한다**
 * (`config.js` 의 `BOT_ANSWER_MARK` 주석이 그렇다). 주석 줄은 ` * ` 로 시작하므로
 * 줄 맨 앞 조건이 그것들을 걸러 준다 — 지우기(사본)에 기대지 않고 걸러 내는 것이 요점이다. */
const REEXPORT_RE = /^[ \t]*export\s*\{([^}]*)\}\s*from\s*['"][^'"]+['"]/gm;
/** `export * as ns from '…'` — `ns` 도 **로컬에 안 생긴다**. 2026-09-11 2차 검증이
 * 이 모양을 통째로 안 보던 것을 잡았다(실제로 `ReferenceError` 가 난다). */
const STAR_RE = /^[ \t]*export\s*\*\s*as\s+([A-Za-z_$][\w$]*)\s+from\s*['"][^'"]+['"]/gm;
/** `import … from '…'` 로 **로컬에 생기는** 이름들 — 기본·이름·네임스페이스 전부. */
const IMPORT_RE = /import\s+([^'"]+?)\s+from\s*['"][^'"]+['"]/g;

function localNames(code) {
  const names = new Set();
  for (const m of code.matchAll(IMPORT_RE)) {
    const clause = m[1];
    const braced = clause.match(/\{([^}]*)\}/);
    if (braced) {
      for (const part of braced[1].split(',')) {
        const t = part.trim();
        if (!t) continue;
        const as = t.split(/\s+as\s+/);
        names.add((as[1] || as[0]).trim());   // `a as b` 면 로컬 이름은 b
      }
    }
    const rest = clause.replace(/\{[^}]*\}/, '').replace(/^\s*,|,\s*$/g, '').trim();
    for (const t of rest.split(',')) {
      const n = t.trim();
      if (!n) continue;
      const ns = n.match(/^\*\s+as\s+(\S+)$/);
      names.add(ns ? ns[1] : n);              // `* as ns` · 기본 import
    }
  }
  return names;
}

/** 재수출 문장이 **로컬에 안 만드는** 이름들과 그 문장이 차지한 자리. */
function reexportedNames(source) {
  const out = [];
  for (const m of source.matchAll(REEXPORT_RE)) {
    for (const part of m[1].split(',')) {
      const t = part.trim();
      if (!t) continue;
      // **뒤 이름**이 로컬에 없어서 크래시하는 쪽이다 — `export { MARK as M } from '…'` 는
      // 밖으로 `M` 을 내보낼 뿐 `M` 도 `MARK` 도 이 파일 스코프에 안 만든다. 그 파일이
      // 쓰는 이름은 `M` 이다. (2026-09-11 회의적 검증이 앞 이름을 보던 것을 잡았다 —
      // `export { default as X } from` 까지 통째로 놓치고 있었다.)
      const name = t.split(/\s+as\s+/).pop().trim();
      // `as default` 는 이름이 예약어라 **식별자로 쓸 수 없다** — 쓰는 일이 없으니 안 본다.
      // 안 거르면 `switch` 의 `default:` 를 사용으로 세어 멀쩡한 코드를 빨갛게 낸다.
      if (!/^[A-Za-z_$][\w$]*$/.test(name) || name === 'default') continue;
      out.push({ name, start: m.index, end: m.index + m[0].length });
    }
  }
  for (const m of source.matchAll(STAR_RE)) {
    out.push({ name: m[1], start: m.index, end: m.index + m[0].length });
  }
  return out;
}

/**
 * 그 파일이 재수출만 해 놓고 쓰는 이름들.
 *
 * **판정은 원본(raw)에서 한다.** 지운 사본에서 판정하면 지우기가 틀렸을 때 대상이 사라져
 * **조용한 초록**이 된다 — 2026-09-11 검증이 두 번(1차·2차) 그 길을 실제로 뚫었고,
 * 두 번째는 진짜 `ReferenceError` 가 나는 파일을 초록으로 통과시키는 것까지 보였다.
 * 그래서 사본은 **판정에서 빼고 문구에만 쓴다**: 원본에 그 이름이 또 나오면 무조건 빨강,
 * 사본에도 있으면 「실행되는 자리」, 사본엔 없으면 「주석·문자열로 보이나 사람이 확인」.
 * **지우기가 아무리 틀려도 판정이 초록으로 뒤집히지 않는다** — 그게 이 구조의 요점이다.
 *
 * @returns {{hits: Array<{name:string, index:number, inCode:boolean}>}}
 */
function offenders(source) {
  const { code } = codeOnlyChecked(source);
  const local = localNames(source);
  const out = [];
  for (const { name, start, end } of reexportedNames(source)) {
    if (local.has(name)) continue;
    const use = new RegExp(`(^|[^\\w$.'"\`])${name}(?![\\w$])`, 'g');
    let first = null;
    for (const u of source.matchAll(use)) {
      if (u.index >= start && u.index < end) continue;
      // 사본에서도 그 자리에 같은 이름이 남아 있나 — 「코드에서 쓴다」의 근거다.
      const inCode = code.slice(u.index, u.index + u[0].length) === u[0];
      // **코드 자리를 먼저 고른다** — 주석이 코드보다 위에 있는 것이 보통이라, 처음 걸린
      // 것을 그냥 쓰면 진짜 사용처를 두고 주석을 가리키게 된다.
      if (inCode) { first = { name, index: u.index, inCode }; break; }
      if (!first) first = { name, index: u.index, inCode };
    }
    if (first) out.push(first);
  }
  return { hits: out };
}

const lineOf = (s, i) => s.slice(0, i).split('\n').length;

/* ── [1/3] 합성 픽스처 — 양성 대조 ───────────────────────────────── */

console.log('[1/3] 재수출만 한 이름을 쓰는 파일을 실제로 잡는가 (합성 픽스처)');
{
  // 픽스처는 **글자로만** 있으면 된다 — 이 검사는 소스 문자열을 재지 파일을 읽지 않는다.
  // 임시 폴더를 만들 이유가 없다.
  const bad1 = [
    "export { MARK } from './config.js';",
    '',
    '// MARK 를 주석에서 부르는 것은 사용이 아니다',
    'export function render(x) {',
    '  return `${x} ${MARK}`;',   // ← 크래시하는 자리
    '}',
  ].join('\n');
  const good = [
    "import { MARK } from './config.js';",
    '',
    'export { MARK };',
    'export function render(x) {',
    '  return `${x} ${MARK}`;',
    '}',
  ].join('\n');
  const onlyComment = [
    "export { MARK } from './config.js';",
    '',
    '/* MARK 는 여기서 안 쓴다 — 이름만 지나간다. */',
    "export const OTHER = 'MARK';",
  ].join('\n');
  // `as` 별칭 — 크래시하는 쪽은 **뒤 이름**이다(2026-09-11 회의적 검증이 놓침을 잡았다).
  const alias = [
    "export { MARK as M } from './config.js';",
    '',
    'export function render(x) {',
    '  return `${x} ${M}`;',
    '}',
  ].join('\n');
  const defaultAlias = [
    "export { default as MARK } from './config.js';",
    '',
    'export function render(x) {',
    '  return x + MARK;',
    '}',
  ].join('\n');
  // 앞 이름만 로컬에 있고 **뒤 이름**을 쓰는 모양 — 앞 이름을 보던 옛 판은 여기서 조용했다.
  const aliasFrontBound = [
    "import { MARK } from './config.js';",
    "export { MARK as M } from './config.js';",
    '',
    'export const use = M;',
  ].join('\n');

  // `export * as ns from` — 2026-09-11 2차 검증이 통째로 안 보던 것을 잡았다
  const star = [
    "export * as cfg from './config.js';",
    '',
    'export function get() { return cfg.MARK; }',
  ].join('\n');
  // `as default` 는 식별자로 쓸 수 없다 — `switch` 의 `default:` 를 사용으로 세면 안 된다
  const asDefault = [
    "export { MARK as default } from './config.js';",
    '',
    "export function pick(x) { switch (x) { case 1: return 'a'; default: return 'b'; } }",
  ].join('\n');

  const b = offenders(bad1);
  if (b.hits.length !== 1 || b.hits[0].name !== 'MARK') bad(`재수출만 한 이름을 쓰는 파일을 못 잡았습니다: ${JSON.stringify(b.hits)}`);
  else if (!b.hits[0].inCode) bad('코드에서 쓰는데 「주석으로 보임」으로 셌습니다');
  else ok('재수출 전용 + 맨 식별자 사용 → 잡음 (코드 자리)');

  const g = offenders(good);
  if (g.hits.length) bad(`import 로 로컬 바인딩이 있는 정상 파일을 잘못 잡았습니다: ${JSON.stringify(g.hits)}`);
  else ok('import + export {…} (로컬 바인딩 있음) → 안 잡음');

  /* 주석에만 나오는 이름도 **잡는다(빨강)** — 다만 문구가 다르다.
   * 2026-09-11 2차 검증이, 사본을 판정에 쓰면 지우기가 틀릴 때 **진짜 크래시가 초록으로
   * 지나가는 것**을 실물로 보였다. 그래서 판정은 원본에서 하고, 사본은 「코드 자리인가」를
   * 알려 주는 데만 쓴다. 재수출이 있는 파일에서 그 이름이 또 보이면 **사람이 한 번 보는
   * 쪽**을 고른 것이다 — 조용히 놓치는 것보다 낫다. 오늘 실물에 그런 파일은 0개다. */
  const c = offenders(onlyComment);
  if (c.hits.length !== 1) bad(`주석에만 있는 이름을 아예 안 봤습니다 — 지우기가 틀리면 그대로 놓칩니다: ${JSON.stringify(c.hits)}`);
  else if (c.hits[0].inCode) bad('주석 안의 이름을 「코드 자리」로 셌습니다');
  else ok('주석 안의 이름 → 빨강이되 「사람이 확인」 문구 (판정을 사본에 안 맡긴다)');

  const s1 = offenders(star);
  if (s1.hits.length !== 1 || s1.hits[0].name !== 'cfg') bad(`export * as ns from … 를 못 잡았습니다: ${JSON.stringify(s1.hits)}`);
  else ok('export * as ns from … + ns 사용 → 잡음');

  const d1 = offenders(asDefault);
  if (d1.hits.length) bad(`\`as default\` 를 switch 의 default: 와 엮어 잘못 잡았습니다: ${JSON.stringify(d1.hits)}`);
  else ok('as default + switch default: → 안 잡음 (식별자로 쓸 수 없는 이름)');

  const a1 = offenders(alias);
  if (a1.hits.length !== 1 || a1.hits[0].name !== 'M') bad(`\`as\` 별칭(뒤 이름)을 못 잡았습니다: ${JSON.stringify(a1.hits)}`);
  else ok('export { X as Y } from … + Y 사용 → 잡음 (뒤 이름)');

  const a2 = offenders(defaultAlias);
  if (a2.hits.length !== 1 || a2.hits[0].name !== 'MARK') bad(`\`default as\` 별칭을 못 잡았습니다: ${JSON.stringify(a2.hits)}`);
  else ok('export { default as X } from … + X 사용 → 잡음');

  const a3 = offenders(aliasFrontBound);
  if (a3.hits.length !== 1 || a3.hits[0].name !== 'M') bad(`앞 이름이 import 돼 있다고 뒤 이름을 놓쳤습니다: ${JSON.stringify(a3.hits)}`);
  else ok('앞 이름만 로컬에 있고 뒤 이름을 쓰는 모양 → 잡음');
}

/* ── [2/3] **지우기가 갈피를 잃어도 판정이 안 뒤집힌다** ─────────────
 *
 * 아래 둘은 2026-09-11 회의적 검증이 실제로 뚫었던 입력이다. 그때는 지운 사본에서
 * 판정해서, 지우기가 코드를 삼키면 대상이 사라져 **초록**이 됐다 — 둘째 것은 `node` 로
 * 돌리면 진짜 `ReferenceError` 가 나는 파일인데도 통과했다. 판정을 원본으로 옮긴 지금은
 * 지우기가 어떻게 틀리든 **빨강**이어야 한다. */

console.log('[2/3] 지우기가 갈피를 잃어도 판정이 안 뒤집힌다 (1·2차 검증이 뚫었던 입력)');
{
  // ① 정규식 오독 + 줄 끝 주석의 아포스트로피 — 따옴표 수가 짝수라 신고조차 안 뜨던 모양
  const exploit1 = [
    "export { MARK } from './config.js';",
    '',
    'export function tag(t) {',
    "  if (t) /['\"]/.test(t) || String(MARK);  // don't ask",
    '  return t;',
    '}',
  ].join('\n');
  // ② `}` 뒤 나눗셈(ASI) — 뒤 코드가 통째로 지워지던 모양
  const exploit2 = [
    "export { MARK } from './config.js';",
    'const g = {}',
    '/ 3;',
    'export const use = MARK;',
  ].join('\n');

  for (const [name, src] of [['정규식 오독 + 아포스트로피', exploit1], ['} 뒤 나눗셈(ASI)', exploit2]]) {
    const r = offenders(src);
    if (r.hits.length !== 1 || r.hits[0].name !== 'MARK') bad(`${name}: 진짜 크래시를 놓쳤습니다 — ${JSON.stringify(r.hits)}`);
    else ok(`${name} → 잡음 (판정이 원본에서 난다)`);
  }
}

/* ── [3/3] 실물 src/·scripts/ ─────────────────────────────────────────────── */

function jsFiles(dir) {
  const out = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, e.name);
    if (e.isDirectory()) out.push(...jsFiles(p));
    else if (e.isFile() && e.name.endsWith('.js')) out.push(p);
  }
  return out;
}

console.log('[3/3] src/ · scripts/ 에 그런 자리가 없다');
{
  const files = [...jsFiles(path.join(ROOT, 'src')), ...jsFiles(path.join(ROOT, 'scripts'))];
  const hits = [];
  for (const f of files) {
    const src = fs.readFileSync(f, 'utf8');
    for (const o of offenders(src).hits) {
      const where = o.inCode ? '코드에서 씀 — 실행되면 ReferenceError' : '주석·문자열로 보임 — 사람이 한 번 보세요';
      hits.push(`${path.relative(ROOT, f)}:${lineOf(src, o.index)} — ${o.name} (${where})`);
    }
  }

  // **훑을 것이 0개면 실패다** — 「아무것도 안 재고 초록」이 이 검사가 막으려는 모양과 같다.
  if (!files.length) {
    bad('훑을 파일이 0개입니다 — 검사 전제가 깨졌습니다(경로가 틀렸을 수 있습니다)');
  } else if (hits.length) {
    bad(`재수출만 하고 그 이름이 또 나오는 자리 ${hits.length}건:\n      ${hits.join('\n      ')}`);
    console.error("      고치는 법: `import { 이름 } from '…'` 을 더하고 `export { 이름 };` 으로 남기세요.");
  } else {
    ok(`src/·scripts/ 파일 ${files.length}개 — 그런 자리 없음`);
  }
}

console.log(failed ? `\n실패 ${failed}건` : '\n전부 통과');
process.exit(failed ? 1 : 0);
