#!/usr/bin/env node
/**
 * `month` 로 좁혔는데도 그 달이 상한을 넘을 때, **주 단위로 다시 나뉘나.**
 *
 *   node scripts/check-doc-week.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 있나 (2026-09-20) ──
 *
 * `month` 로 이미 좁혀 부른 호출은 목차 갈래를 안 타서, **그 한 달이 상한을 넘으면
 * 앞 60,000자만 오고 뒤는 도구로 못 봤다.** 잘림 안내가 붙긴 하지만 봇이 그것을 안
 * 읽고 앞부분만으로 답하면 **조용히 부분만 보고 답한 것**이 된다.
 *
 * 갈래 셋 중 **주 단위 재분할**을 채택했다 (WHK 결정 2026-09-19). 「뒤에서부터 주기」는
 * md 가 최신순이라 역효과가 확정이었고, 「프롬프트로 당부」는 준수율 문제가 남는다.
 * 실물 측정(2026-09-20): 상한 넘는 월 13건 → 조각 44개 · 잘리는 조각 0개.
 *
 * ── 무엇을 재나 ──
 *
 * 크기를 재는 시험이 아니라 **행동**을 잰다. 상한을 낮춘 fixture 로 돌려야 60,000자짜리
 * 가짜 문서를 만들지 않고도 같은 갈래에 닿는다 — `_outline-probe.js` 는 상한을 못
 * 낮추므로 진짜 크기로 만드는 [3]만 그쪽을 쓰고, 나머지는 팩토리를 직접 세워
 * 상한을 작게 준다 (`check-doc-read.js` 와 같은 방식).
 */
import { createDocumentRead } from '../src/documents/read.js';
import { splitMessages, preambleOf, fold } from '../src/archive.js';
import { SHEET_RE, sectionsOf } from '../src/documents/parse.js';
import { readDocumentInTmp } from './_outline-probe.js';

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

/** 상한을 마음대로 줄 수 있는 readDocument 하나. 파일도 설정도 안 건드린다. */
function readerWithCap(text, cap) {
  const doc = { slug: '시험문서', project: '시험', abs: '시험문서', title: '시험문서', meta: {}, entries: [] };
  const { readDocument } = createDocumentRead({
    fs: { readFileSync: () => '{}' },
    path: { join: (...a) => a.join('/') },
    DOCS_DIR: 'synthetic',
    DOC_READ_MAX_CHARS: cap,
    DOC_HIT_MAX_CHARS: 100,
    SECTION_OUTLINE_MIN_COVER: 0.7,
    DOC_PREAMBLE_MARK: '[머리말]',
    DIGEST_DOC_MAX_CHARS: 15000,
    DIGEST_DOC_PER_DOC_CHARS: 3000,
    DIGEST_DOC_MIN_CHARS: 400,
    hasDocuments: () => true,
    resolveProjectFor: (p) => ({ ok: true, name: p }),
    resolveDocumentFor: () => ({ ok: true, doc }),
    canSeeDoc: () => true,
    BLOCKED_NOTE: 'BLOCKED',
    readCached: () => text,
    redactPrivateMentions: (s) => s,
    preambleOf,
    splitMessages,
    SHEET_RE,
    sectionsOf,
    // **실물 fold 를 그대로 쓴다.** 가짜 fold(공백만 제거)는 대시를 남겨 부분 일치
    // 의미가 실물과 달랐고, 그래서 순번 가로채기 결함을 이 시험이 못 봤다 (2026-09-20).
    fold,
    loadDocument: () => doc,
    maskProject: (p) => p,
    clip: (s) => s,
  });
  return (args) => readDocument({ project: '시험', document: '시험문서', access: { full: true }, ...args });
}

/** `## 2026-08` 한 달에 회차를 날짜별로 채운 fixture. 회차마다 `fill` 자를 넣는다. */
function monthFixture(dates, fill) {
  const body = dates.map((d) => `**${d} · 보고.hwp**\n\n${d} 본문 ${'가'.repeat(fill)}`).join('\n\n');
  return `## 2026-07\n\n**2026-07-01 · 보고.hwp**\n\n칠월본문\n\n## 2026-08\n\n${body}`;
}

// 8월 — 3주에 걸친 9일. 회차 하나가 290여 자라 **한 주(3회차)는 상한 1,000자 안에 들고
// 한 달(9회차)은 넘는다.** 이 사이가 재려는 자리다 — 주도 넘으면 [5] 의 날짜 갈래로 간다.
const AUG = ['2026-08-03', '2026-08-05', '2026-08-07', '2026-08-10', '2026-08-12',
  '2026-08-14', '2026-08-17', '2026-08-19', '2026-08-21'];

console.log('[1/9] 그 달이 상한을 넘으면 주 목차가 온다');
{
  const read = readerWithCap(monthFixture(AUG, 250), 1000);
  const r = read({ month: '2026-08' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.outline !== 'week') bad(`outline 이 '${r.outline}' 입니다 — 'week' 여야 합니다 (truncated=${r.truncated})`);
  else if (r.truncated) bad('목차를 주면서 truncated=true 로 표시했습니다');
  else if (!/주 3개/.test(r.text)) bad(`주가 3개로 안 잡혔습니다: ${r.text.split('\n')[1]}`);
  else if (!/week 로 하나를 지정해 다시 부르세요 \(month 와 함께\)/.test(r.text)) {
    bad(`「month 와 함께」 안내가 없습니다: ${r.text}`);
  } else ok(`주 목차가 온다 — ${r.text.split('\n')[1].trim()}`);
}

console.log('\n[2/9] 그 주만 열린다 (이름·월요일 날짜·순번 셋 다)');
{
  const read = readerWithCap(monthFixture(AUG, 250), 1000);
  for (const [label, week] of [['이름', '2026-08-10~08-16'], ['월요일 날짜', '2026-08-10'], ['순번', '2']]) {
    const r = read({ month: '2026-08', week });
    if (r.error) { bad(`${label}(${week}) → 오류: ${r.error}`); continue; }
    const has = (d) => r.text.includes(`${d} 본문`);
    if (!has('2026-08-10') || !has('2026-08-12') || !has('2026-08-14')) {
      bad(`${label}(${week}) — 그 주 회차가 빠졌습니다`);
    } else if (has('2026-08-03') || has('2026-08-17')) {
      bad(`${label}(${week}) — 다른 주 회차까지 왔습니다 (경계가 틀렸습니다)`);
    } else if (r.week !== '2026-08-10~08-16') {
      bad(`${label}(${week}) — 돌려준 week 가 '${r.week}' 입니다`);
    } else ok(`${label}(${week}) 로 그 주만 열린다`);
  }
}

console.log('\n[3/9] 월 헤딩이 그 주 글 앞에 다시 붙는다 (어느 달인지 안 사라진다)');
{
  const read = readerWithCap(monthFixture(AUG, 250), 1000);
  const r = read({ month: '2026-08', week: '2026-08-10' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (!r.text.includes('## 2026-08')) bad(`월 헤딩이 없습니다: ${r.text.slice(0, 120)}`);
  else ok('주 글 앞에 `## 2026-08` 이 붙는다');
}

console.log('\n[4/9] 잘못 부르면 조용히 무시하지 않고 막는다');
{
  const read = readerWithCap(monthFixture(AUG, 250), 1000);
  const cases = [
    ['month 없이 week 만', { week: '2026-08-10' }, /month 와 함께/],
    ['week + section', { month: '2026-08', week: '2026-08-10', section: '1' }, /함께 쓸 수 없습니다/],
    ['없는 주', { month: '2026-08', week: '2026-12-25' }, /특정하지 못했습니다/],
  ];
  for (const [label, args, re] of cases) {
    const r = read(args);
    if (!r.error) bad(`${label} — 에러 없이 통과했습니다 (조용히 좁혀진 것으로 믿게 됩니다)`);
    else if (!re.test(r.error)) bad(`${label} — 문구가 다릅니다: ${r.error}`);
    else ok(`${label} → 막고 이유를 말한다`);
  }
}

console.log('\n[5/9] 한 주가 또 상한을 넘으면 그 주만 날짜로 더 나뉜다');
{
  // 한 주(8/3·8/5·8/7)에만 큰 회차를 몰아 둔다 — 그 주가 상한을 넘는다.
  const dense = ['2026-08-03', '2026-08-05', '2026-08-07'].map((d) => `**${d} · 보고.hwp**\n\n${d} 본문 ${'가'.repeat(900)}`).join('\n\n');
  const light = ['2026-08-10', '2026-08-12'].map((d) => `**${d} · 보고.hwp**\n\n${d} 본문 ${'가'.repeat(100)}`).join('\n\n');
  const read = readerWithCap(`## 2026-07\n\n**2026-07-01 · 보고.hwp**\n\n칠월\n\n## 2026-08\n\n${dense}\n\n${light}`, 1000);
  const r = read({ month: '2026-08' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.outline !== 'week') bad(`outline 이 '${r.outline}' 입니다`);
  else if (!r.text.includes('2026-08-03 (') || !r.text.includes('2026-08-05 (')) {
    bad(`넘치는 주가 날짜로 안 나뉘었습니다: ${r.text.split('\n')[1]}`);
  } else if (!/날짜별로 더 나눴습니다/.test(r.text)) {
    bad('날짜로 나눈 사실을 목차가 안 밝힙니다 — 봇이 그 주가 통째로 없는 것으로 읽습니다');
  } else {
    const one = read({ month: '2026-08', week: '2026-08-05' });
    if (one.error) bad(`날짜 하나로 못 엽니다: ${one.error}`);
    else if (!one.text.includes('2026-08-05 본문') || one.text.includes('2026-08-03 본문')) {
      bad('날짜 하나를 열었는데 그 날 회차가 아니거나 다른 날이 섞였습니다');
    } else ok('넘치는 주만 날짜로 나뉘고, 날짜 하나로도 열린다');
  }
}

console.log('\n[6/9] 안 나눠야 할 때는 안 나눈다 (대조군)');
{
  // ① 상한 안에 드는 달 — 목차 없이 본문이 와야 한다
  const small = readerWithCap(monthFixture(AUG, 10), 100000);
  const r1 = small({ month: '2026-08' });
  if (r1.error) bad(`작은 달 → 오류: ${r1.error}`);
  else if (r1.outline) bad(`작은 달인데 목차가 왔습니다 (outline=${r1.outline})`);
  else if (!r1.text.includes('2026-08-03 본문')) bad('작은 달인데 본문이 안 왔습니다');
  else ok('상한 안에 드는 달은 예전처럼 본문이 온다');

  /* ② 본문에 날짜가 없어도 **회차 머리줄**의 날짜로 나눈다.
   * 「날짜를 못 읽는 회차가 하나라도 있으면 안 나눈다」는 방어는 여기서 못 잰다 —
   * 실물 `splitMessages` 는 머리줄이 날짜로 시작하는 블록만 내므로 날짜 없는 블록이
   * 나올 수 없다. 그 방어는 **다른 `splitMessages` 를 주입하는 쪽**(Clio)에서만 걸린다.
   * 그래서 이 줄은 방어가 아니라 **대조군**이다. */
  const nodate = `## 2026-07\n\n**2026-07-01 · 보고.hwp**\n\n칠월\n\n## 2026-08\n\n`
    + `**2026-08-03 · 보고.hwp**\n\n${'가'.repeat(900)}\n\n`
    + `**2026-08-10 · 보고.hwp**\n\n${'나'.repeat(900)}`;
  const r2 = readerWithCap(nodate, 1000)({ month: '2026-08' });
  if (r2.error) bad(`오류: ${r2.error}`);
  else if (r2.outline !== 'week') bad(`대조군이 무너졌습니다 — 날짜가 읽히는 fixture 인데 outline=${r2.outline}`);
  else ok('날짜가 읽히면 나눈다 (대조군)');

  // ③ 주까지 좁힌 호출에는 목차를 또 주지 않는다
  const r3 = readerWithCap(monthFixture(AUG, 250), 1000)({ month: '2026-08', week: '1' });
  if (r3.error) bad(`오류: ${r3.error}`);
  else if (r3.outline) bad(`주까지 좁혔는데 목차를 또 줬습니다 (outline=${r3.outline})`);
  else ok('주까지 좁힌 호출에는 목차를 다시 안 준다');
}

console.log('\n[7/9] 목차가 어느 달의 것인지·머리말이 어디서 온 것인지 (표시 계층)');
{
  // 머리말은 첫 월 헤딩보다 **위**라, 월로 자른 뒤에 뽑으면 월 헤딩 줄이 머리말로 잡힌다.
  const pre = `사람이 적어 둔 머리말입니다 ${'말'.repeat(60)}`;
  const read = readerWithCap(`${pre}\n\n${monthFixture(AUG, 250)}`, 1000);
  const r = read({ month: '2026-08' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.outline !== 'week') bad(`outline 이 '${r.outline}' 입니다`);
  else if (!r.text.startsWith('[머리말]')) bad(`머리말이 목차 앞에 안 붙었습니다: ${r.text.slice(0, 60)}`);
  else if (!r.text.includes('사람이 적어 둔 머리말')) bad('붙은 것이 진짜 머리말이 아닙니다 (월 헤딩을 머리말로 잡았을 수 있습니다)');
  else if (r.text.includes('## 2026-08 ·')) bad('월 헤딩 줄이 머리말 자리에 왔습니다');
  else if (!/시험문서 · 2026-08 · 전체/.test(r.text)) bad(`목차 제목에 그 달이 없습니다: ${r.text.split('\n')[0]}`);
  else if (r.month !== '2026-08') bad(`주 목차인데 돌려준 month 가 '${r.month}' 입니다 — 봇이 어느 달인지 잃습니다`);
  else ok('주 목차에 그 달과 진짜 머리말이 실린다');
}

console.log('\n[8/9] 못 나누는 모양은 예전처럼 잘리고 안내가 붙는다');
{
  /* ① 상한 넘는 달인데 회차가 **한 주에 다 몰려** 있다 — 주로는 하나지만 **날짜로는
   * 여럿**이다. 처음에는 「주가 하나면 안 나눈다」로 뒀는데, 그러면 이 달만 예전처럼
   * 앞부분만 오고 뒤는 못 본다 — 고치려던 증상이 그대로 남는다 (2026-09-20 회의적 검증). */
  const oneWeek = ['2026-08-03', '2026-08-04', '2026-08-05'].map((d) => `**${d} · 보고.hwp**\n\n${d} 본문 ${'가'.repeat(400)}`).join('\n\n');
  const r1 = readerWithCap(`## 2026-07\n\n**2026-07-01 · 보고.hwp**\n\n칠월\n\n## 2026-08\n\n${oneWeek}`, 1000)({ month: '2026-08' });
  if (r1.error) bad(`한 주에 몰린 달 → 오류: ${r1.error}`);
  else if (r1.outline !== 'week') bad(`한 주에 몰렸다고 안 나눴습니다 (outline=${r1.outline}) — 그 달은 예전처럼 앞부분만 옵니다`);
  else if (!r1.text.includes('2026-08-04 (')) bad(`날짜로 안 나뉘었습니다: ${r1.text.split('\n')[1]}`);
  else ok('한 주에 다 몰린 달도 날짜로 나뉜다');

  // ①-b 회차가 하나뿐인데 그것이 상한을 넘으면 더 나눌 축이 없다 — 예전처럼 잘린다.
  const single = `## 2026-07\n\n**2026-07-01 · 보고.hwp**\n\n칠월\n\n## 2026-08\n\n**2026-08-03 · 보고.hwp**\n\n${'가'.repeat(1500)}`;
  const r1b = readerWithCap(single, 1000)({ month: '2026-08' });
  if (r1b.error) bad(`회차 하나짜리 달 → 오류: ${r1b.error}`);
  else if (r1b.outline) bad(`회차가 하나인데 목차를 냈습니다 (outline=${r1b.outline}) — 고를 것이 없습니다`);
  else if (!r1b.truncated) bad('상한을 넘는데 truncated=false 입니다');
  else if (!/이 조각만도/.test(r1b.hint || '')) bad(`잘림 안내가 예전 문구가 아닙니다: ${r1b.hint}`);
  else ok('회차 하나짜리 큰 달은 예전처럼 잘리고 안내가 붙는다');

  /* ② 날짜를 못 읽는 회차가 섞이면 **부분만 나누지 않는다.** 실물 splitMessages 로는 그런
   * 블록이 안 나오지만, 이 파일은 그 함수를 주입받으므로 여기서 직접 잴 수 있다
   * (Clio 가 이 파일을 무수정 복사해 가므로 저쪽 계약이 다르면 거기서 걸린다).
   *
   * **방어가 두 겹이라 한 겹을 빼도 이 시험은 안 떨어진다** — `dated.some(...)` 을
   * 지워도 `mondayOf` 가 null 을 내는 자리에서 다시 멈춘다(2026-09-20 돌연변이로 확인).
   * 여기서 고정하는 것은 두 겹 중 어느 한 줄이 아니라 **「안 나눈다」는 행동**이다. */
  const text = monthFixture(AUG, 250);
  const dateless = (s) => splitMessages(s).map((b, i) => (i === 1 ? { text: b.text.replace(/\d{4}-\d{2}-\d{2}/g, '날짜없음') } : b));
  const doc = { slug: '시험문서', project: '시험', abs: '시험문서', title: '시험문서', meta: {}, entries: [] };
  const { readDocument } = createDocumentRead({
    fs: { readFileSync: () => '{}' }, path: { join: (...a) => a.join('/') },
    DOCS_DIR: 'synthetic', DOC_READ_MAX_CHARS: 1000, DOC_HIT_MAX_CHARS: 100,
    SECTION_OUTLINE_MIN_COVER: 0.7, DOC_PREAMBLE_MARK: '[머리말]',
    DIGEST_DOC_MAX_CHARS: 15000, DIGEST_DOC_PER_DOC_CHARS: 3000, DIGEST_DOC_MIN_CHARS: 400,
    hasDocuments: () => true, resolveProjectFor: (p) => ({ ok: true, name: p }),
    resolveDocumentFor: () => ({ ok: true, doc }), canSeeDoc: () => true, BLOCKED_NOTE: 'BLOCKED',
    readCached: () => text, redactPrivateMentions: (s) => s, preambleOf,
    splitMessages: dateless, SHEET_RE, sectionsOf,
    fold,
    loadDocument: () => doc, maskProject: (p) => p, clip: (s) => s,
  });
  const r2 = readDocument({ project: '시험', document: '시험문서', month: '2026-08', access: { full: true } });
  if (r2.error) bad(`날짜 없는 블록 → 오류: ${r2.error}`);
  else if (r2.outline === 'week') bad('날짜를 못 읽는 회차가 있는데 나눴습니다 — 그 회차는 어느 주에도 안 들어 조용히 못 닿게 됩니다');
  else ok('날짜를 못 읽는 회차가 하나라도 있으면 안 나눈다');
}

console.log('\n[9/9] 실물 모양 — 최신순·월을 걸친 주·모호한 입력');
{
  // 실물 md 는 **최신순(내림차순)** 이다. 위 fixture 들은 오름차순이라 여기서 뒤집어 잰다.
  // 7/27(월)~8/2(일) 주가 8월 조각에 8/1 하루만으로 선다 — 월을 걸친 주의 이름이 그 모양이다.
  const desc = ['2026-08-10', '2026-08-05', '2026-08-01'];
  const read = readerWithCap(monthFixture(desc, 400), 1000);
  const r = read({ month: '2026-08' });
  if (r.error) bad(`오류: ${r.error}`);
  else if (r.outline !== 'week') bad(`outline 이 '${r.outline}' 입니다`);
  else if (!r.text.includes('2026-07-27~08-02')) bad(`월을 걸친 주 이름이 없습니다: ${r.text.split('\n')[1]}`);
  else if (!/1\. 2026-08-10~08-16/.test(r.text)) bad(`조각 순서가 문서 등장순이 아닙니다: ${r.text.split('\n')[1]}`);
  else {
    const one = read({ month: '2026-08', week: '2026-07-27' });
    if (one.error) bad(`월을 걸친 주를 못 엽니다: ${one.error}`);
    else if (!one.text.includes('2026-08-01 본문') || one.text.includes('2026-08-05 본문')) {
      bad('월을 걸친 주를 열었는데 든 회차가 틀립니다');
    } else ok('최신순 문서에서 월을 걸친 주도 이름·순서·열기가 맞다');
  }

  /* **숫자는 목차 순번으로만 읽는다** (2026-09-20 회의적 검증이 실물에서 잡은 결함).
   * 주 이름이 전부 날짜라 한 자리 숫자가 이름에 부분 일치한다 — 실측에서 `week:"4"` 가
   * 목차 4번이 아니라 `2026-08-24~08-30` 을 열었고(이름에 `4` 가 있다), 실물 문서
   * 5개·달 13개에서 오배달 7건이었다. **에러가 안 나서 봇은 엉뚱한 주로 답한다.**
   * 아래 fixture 는 그 모양을 그대로 만든다 — 3번 조각 이름에 `1` 이 들어 있다. */
  const dates = ['2026-08-24', '2026-08-17', '2026-08-10', '2026-08-03'];
  const ord = readerWithCap(monthFixture(dates, 400), 1000);
  const byNum = ord({ month: '2026-08', week: '1' });
  if (byNum.error) bad(`순번 1 → 오류: ${byNum.error}`);
  else if (byNum.week !== '2026-08-24~08-30') {
    bad(`순번 1 이 목차 1번이 아니라 '${byNum.week}' 를 열었습니다 — 이름 부분 일치에 가로채였습니다`);
  } else ok('숫자는 목차 순번으로만 읽는다 (이름 부분 일치에 안 가로채인다)');

  const outOf = ord({ month: '2026-08', week: '9' });
  if (!outOf.error) bad(`조각이 4개인데 순번 9 로 '${outOf.week}' 가 열렸습니다 — 범위 밖 순번은 막아야 합니다`);
  else if (!/특정하지 못했습니다/.test(outOf.error)) bad(`문구가 다릅니다: ${outOf.error}`);
  else ok('범위 밖 순번은 아무것도 안 연다');

  // 여럿에 걸리는 입력은 **아무것도 안 연다** — 조용히 첫 주를 열면 봇은 좁혀졌다고 믿는다.
  const amb = read({ month: '2026-08', week: '2026-08' });
  if (!amb.error) bad(`모호한 week('2026-08')로 무언가 열렸습니다 — 어느 주인지 모른 채 답하게 됩니다`);
  else if (!/특정하지 못했습니다/.test(amb.error)) bad(`문구가 다릅니다: ${amb.error}`);
  else ok('여럿에 걸리는 week 는 아무것도 안 연다');
}

console.log('\n[보조] 실물 경로로도 한 번 — 임시 아카이브의 진짜 readDocument');
{
  const r = await readDocumentInTmp(monthFixture(AUG, 250), { project: '시험', document: '산정내역', month: '2026-08' });
  if (r.error) bad(`실물 경로 오류: ${r.error}`);
  else if (r.outline) bad(`상한(60,000자) 안에 드는 fixture 인데 목차가 왔습니다 (outline=${r.outline})`);
  else if (!r.text.includes('2026-08-03 본문')) bad('실물 경로에서 8월 본문이 안 왔습니다');
  else ok('실물 경로에서 작은 달은 본문 그대로 온다 (배선이 살아 있다)');
}

console.log(failed ? `\n✗ ${failed}건 어긋남` : '\n✓ 주 단위 재분할이 규약대로 돕니다');
process.exit(failed ? 1 : 0);
