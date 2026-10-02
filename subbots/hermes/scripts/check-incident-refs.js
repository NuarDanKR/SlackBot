#!/usr/bin/env node
/**
 * 코드 주석이 가리키는 **자료 저장소 `사고기록.md` 의 절이 실재하나.**
 *
 *     node scripts/check-incident-refs.js
 *
 * 종료코드: 0 다 맞음(또는 잴 것이 없음) / 1 안 맞는 포인터가 있음
 *
 * ── 왜 남겨 두는가 ──
 *
 * 2026-08-31 에 저장소가 갈리면서 **값이 곧 근거인 줄**은 자료 저장소로 옮겼다. 실제
 * 금액·사람 이름·채널 이름이 있어야 뜻이 서는 사고 기록이라 코드 저장소에 둘 수 없었고,
 * 대신 코드 주석이 그 파일의 절 이름을 낫표로 감싸 가리킨다.
 *
 * 저장소가 둘이라 **그 포인터는 아무도 안 지킨다.** 자료 저장소에서 절 제목을 바꾸거나
 * 지워도 코드는 그대로 있고, 에러도 안 난다. 다음 사람이 그 절을 찾다가 못 찾고,
 * 주석은 있는데 근거는 없는 상태가 조용히 굳는다.
 *
 * 2026-09-01 실측으로 두 가지가 실제로 있었다.
 *   · `src/config.js` 가 낫표 안에 파일 이름만 적었는데 **그 이름으로 시작하는 절이
 *     둘**이라 어느 쪽인지 안 갈렸다.
 *   · `src/index.js` 는 절 이름을 아예 안 적어서 확인할 방법 자체가 없었다.
 *
 * ── 무엇을 보나 ──
 *
 * 코드에서 `사고기록.md` 를 언급한 자리마다 뒤따르는 **낫표 안의 절 이름**을 찾아, 그것이
 * `사고기록.md` 의 `##` 제목 **정확히 하나**에 들어 있나 본다.
 *   · 0개면 죽은 포인터 — 절이 없어졌거나 제목이 바뀌었다. **실패다.**
 *   · 2개 이상이면 애매한 포인터 — 사람이 어느 절인지 못 고른다. **실패다.**
 *
 * 낫표로 감싼 절 이름이 없는 언급은 **실패가 아니라 세기만 한다.** 그 파일을 「이런 것이
 * 있다」고 설명하는 문장이 있고(이 파일 머리말이 그렇다), 그것까지 막으면 사람이
 * 아무 절이나 골라 가짜 포인터를 단다 — 그러면 이 검사가 지키려던 것이 사라진다.
 *
 * ── 못 잡는 구간 ──
 *
 * 제목에 **들어 있나**만 본다. 절 내용이 주석이 말하는 것과 맞는지는 안 본다 — 그건
 * 기계가 못 하는 판정이다. 그리고 코드 → 기록 한 방향만 본다: 아무도 안 가리키는 절이
 * 기록에 남아 있는 것은 여기서 실패가 아니다(사고 기록은 코드와 무관하게 가치가 있다).
 *
 * ── 자료 저장소가 없으면 ──
 *
 * 새 팀에는 `사고기록.md` 가 없다. 그때는 **재지 못했다고 말하고 통과**한다 —
 * 남의 팀 사고 기록을 우리가 채워 줄 수는 없고, 영원히 ✗ 를 띄우면 그 팀은 매일
 * 빨간 줄을 보며 무시하는 법을 배운다. 조용히 통과시키지는 않는다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const read = (p) => { try { return fs.readFileSync(p, 'utf8'); } catch { return null; } };

// 자료 저장소 자리 — check-bootstrap.js·check-business-names.js 와 같은 순서로 읽는다.
const DATA_ROOT = (() => {
  if (process.env.HERMES_DATA_ROOT) return path.resolve(process.env.HERMES_DATA_ROOT);
  const env = read(path.join(ROOT, '.env')) || '';
  const m = env.match(/^\s*HERMES_DATA_ROOT\s*=\s*(.+?)\s*$/m);
  if (m) return path.resolve(m[1].replace(/^["']|["']$/g, ''));
  return path.resolve(ROOT, '..', 'hermes-archive');
})();

const RECORD = '사고기록.md';
const recordPath = path.join(DATA_ROOT, RECORD);
const record = read(recordPath);
if (record === null) {
  console.log(`  - 건너뜀: ${RECORD} 가 없어 **재지 못했습니다**`);
  console.log(`      본 곳: ${recordPath}`);
  console.log('      재지 못한 것은 「다 맞다」가 아닙니다.');
  process.exit(0);
}

// `## ` 제목만 모은다. 제목 안의 백틱은 떼고 대본다 — 코드는 「`050` 국번…」 처럼
// 백틱째 적기도 하고 안 적기도 해서, 그 차이로 죽은 포인터가 되면 안 된다.
const plain = (s) => s.replace(/`/g, '').replace(/\s+/g, ' ').trim();
const headings = record.split('\n')
  .filter((l) => l.startsWith('## '))
  .map((l) => plain(l.slice(3)));
if (!headings.length) {
  console.log(`  ✗ ${RECORD} 에 '## ' 절이 하나도 없습니다 — **재지 못했습니다**`);
  console.log(`      본 곳: ${recordPath}`);
  process.exit(1);
}

const files = execFileSync('git', ['-C', ROOT, '-c', 'core.quotePath=false', 'ls-files'], { encoding: 'utf8' })
  .split('\n').filter((f) => f && !f.endsWith('.pyc') && !f.endsWith('.md'));

// 주석은 여러 줄에 걸쳐 접히므로 언급 자리 뒤 240자를 이어 붙여 본다. 줄머리의
// 주석 기호(`*`·`//`·`#`)와 줄바꿈을 지우면 한 문장이 된다.
const AFTER = 240;
let dead = 0; let vague = 0; let none = 0; let good = 0;
const rows = [];
const unread = [];
for (const f of files) {
  const t = read(path.join(ROOT, f));
  if (t === null) { unread.push(f); continue; }
  if (!t.includes(RECORD)) continue;
  const lineOf = (idx) => t.slice(0, idx).split('\n').length;
  let from = 0;
  for (;;) {
    const at = t.indexOf(RECORD, from);
    if (at < 0) break;
    from = at + RECORD.length;
    const tail = t.slice(from, from + AFTER).replace(/\n\s*(\*|\/\/|#)?\s*/g, ' ');
    const m = tail.match(/「(.+?)」\s*절/);
    if (!m) { none += 1; continue; }   // 그냥 언급 — 세기만 한다 (머리말 참조)
    const want = plain(m[1]);
    const hit = headings.filter((h) => h.includes(want));
    if (hit.length === 1) { good += 1; continue; }
    if (hit.length === 0) { dead += 1; rows.push({ f, no: lineOf(at), why: '그런 절이 없음', what: want }); }
    else { vague += 1; rows.push({ f, no: lineOf(at), why: `절 ${hit.length}개에 걸림`, what: want }); }
  }
}

if (unread.length) {
  console.log(`  ✗ 파일 ${unread.length}개를 못 읽어 **재지 못했습니다**`);
  for (const f of unread.slice(0, 5)) console.log(`      ${f}`);
  console.log('      재지 못한 것은 「다 맞다」가 아닙니다.');
  process.exit(1);
}

const 셈 = `절 ${headings.length}개 · 가리키는 주석 ${good + dead + vague}곳 · 그냥 언급 ${none}곳`;
if (rows.length) {
  console.log(`  ✗ ${RECORD} 를 가리키는 주석 ${rows.length}곳이 안 맞습니다`
    + ` (죽음 ${dead} · 애매 ${vague} · 맞음 ${good})`);
  for (const r of rows.slice(0, 10)) {
    console.log(`      ${r.f}:${r.no}  ${r.why} — 「${r.what}」`);
  }
  if (rows.length > 10) console.log(`      … 그 외 ${rows.length - 10}곳`);
  console.log(`\n${RECORD} 의 절 제목과 맞추세요. 제목 안에 들어 있기만 하면 됩니다.`);
  console.log(`      지금 있는 절: ${headings.join(' / ')}`);
  process.exit(1);
}
console.log(`  ✓ ${RECORD} 를 가리키는 주석 ${good}곳이 다 살아 있는 절을 가리킵니다`);
// 「가리키는 주석」이 조용히 0 이 되는 것이 이 검사의 고장 방식이다 — 언급 문구가
// 바뀌어 정규식이 안 물면 아무것도 안 재고도 초록이 난다. 그래서 셈을 화면에 낸다.
console.log(`[보임] 사고 기록 ${셈}`);
