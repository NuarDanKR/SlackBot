#!/usr/bin/env node
/**
 * 「전사 종합 포인터 카드」 검사 — 사업장으로 좁힌 문서 검색이 빈손일 때
 * `companyWideCards`·`shouldOfferCards`(searchDocuments 래퍼)가 지키는 13가지.
 *
 *   node scripts/check-doc-cards.js
 *
 * 종료코드: 0 전부 통과 / 1 실패 있음
 *
 * ── 무엇을 재나 ──
 *
 * 좁힌 문서 검색이 「봇이 자료가 없다고 닫을 자리」일 때만, 여러 사업장을 한 문서에
 * 담는 전사 종합 폴더의 문서를 **발췌 없는 카드**로 별도 구역에 싣는다(Task 4 계획).
 * [1]~[7]은 카드가 붙는/안 붙는 자리와 카드 모양을, [8]~[10]은 Task 3 이 확정한 C7
 * 발동 조건(확정0 그리고 (부분일치 3건 이하 또는 로컬에 없는 낱말 1개 이상))을 잰다.
 * [11]~[13]은 고침 회차 1(검토 지적)이 더한 것 — [11]은 별칭·부분 이름으로 불러도
 * `resolveProjectFor` 를 실제로 거치는지(빼면 ✗ 가 나야 한다), [12]는 중복 낱말 질의에서
 * 「전부 로컬에 있다」판정이 살아 있는지, [13]은 못 푸는/막힌 사업장 이름에 카드가 안
 * 붙는지(`r.error` 관문이 죽어 있던 자리, `searchDocuments` 래퍼가 직접 막는다)를 잰다.
 *
 * **실물 아카이브를 안 건드린다** — `HERMES_DATA_ROOT` 를 임시 폴더로 돌려놓고
 * 그 아래에 가짜 자료 저장소를 만들어 돈다. `src/config.js`·`src/documents.js` 를
 * 정적 import 하면 안 되고(모듈 최상위가 먼저 돌아 실물 경로가 굳는다) 아래에서
 * `await import()` 로 늦게 가져온다 (check-thread-loss.js 방식).
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

/* ── 가짜 자료 저장소 ─────────────────────────────────────────────── */

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-doc-cards-'));
const DATA = path.join(TMP, 'data');
fs.mkdirSync(DATA, { recursive: true });

/* config.example.json 을 그대로 베이스로 쓰고 search 키만 더한다 — 실물 값을
 * 하드코딩하지 않는다. companyWideDocProjects 는 브리프가 준 중립 이름 하나뿐이다. */
const exampleConfig = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf8'));
exampleConfig.search = { companyWideDocProjects: ['전사폴더A'] };
fs.writeFileSync(path.join(DATA, 'config.json'), JSON.stringify(exampleConfig, null, 2), 'utf8');

/** fixture 가 만든 자리 이름 — 마지막에 개명 지도로 한 번에 적는다 (writeSyncState). */
const projectNames = new Set();

function writeDoc(project, file, lines) {
  projectNames.add(project);
  const dir = path.join(DATA, 'documents', 'projects', project);
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, file), lines.join('\n'), 'utf8');
}

/* 개명 지도(`slack-export/.sync-state.json`). **없으면 안 된다** — `src/config.js` 는
 * 아카이브에 자료(대화 md 또는 문서 폴더)가 있는데 지도를 못 읽으면 「죽음」으로 보고
 * 전체 권한이 아닌 접근을 전부 닫는다(fail-closed). 빈 지도로 때워도 같은 결과다 —
 * 그때는 모든 이름이 「고아」가 되어 똑같이 닫힌다. 그래서 fixture 가 만든 문서 폴더
 * 이름을 전부 현재 이름으로 등록한다(`file` == `name` = 개명 안 한 채널). */
function writeSyncState() {
  const channels = {};
  let i = 0;
  for (const name of projectNames) {
    i += 1;
    channels[`C${String(i).padStart(9, '0')}`] = { name, file: name };
  }
  fs.mkdirSync(path.join(DATA, 'slack-export'), { recursive: true });
  fs.writeFileSync(
    path.join(DATA, 'slack-export', '.sync-state.json'),
    JSON.stringify({ channels }, null, 2), 'utf8',
  );
}

/** 회차 여러 개를 만든다 — 회차마다 지정한 낱말 하나만 담아, 두 낱말짜리 질의에서
 * 그 낱말들이 서로 다른 회차에만 나뉘어 있게 한다(확정 히트는 0, 부분 일치만 는다). */
function entries(word, count, startDay) {
  const out = [];
  for (let i = 0; i < count; i += 1) {
    const day = String(startDay + i).padStart(2, '0');
    out.push(`**2026-08-${day} · 잡보${startDay + i}.pdf**`, '', `${word} 관련 잡보 내용입니다.`, '');
  }
  return out;
}

// 전사 종합 폴더 — 공개 문서(절 2개) + 비공개 문서(검사 [5]).
writeDoc('전사폴더A', '20260731-종합보고.md', [
  '# [종합] 전사폴더A 종합보고', '',
  '> **사업장**: 전사폴더A · **종류**: 종합보고',
  '> **열람**: 공개', '',
  '---', '',
  '## 2026-07', '',
  '**2026-07-31 · 종합보고.pdf**', '',
  '### 1. 사업개요', '',
  '지표 항목 관련 세부 내용이 여기 있습니다. 설명이 이어집니다.', '',
  '### 2. 재무현황', '',
  '다른 절의 내용입니다. 참고 낱말: 알파 베타 감마 델타 입실론.',
]);
writeDoc('전사폴더A', '20260805-비공개철.md', [
  '# [비공개] 전사폴더A 비공개철', '',
  '> **사업장**: 전사폴더A · **종류**: 비공개철',
  '> **열람**: 비공개', '',
  '---', '',
  '## 2026-08', '',
  '**2026-08-05 · 비공개철.pdf**', '',
  '### 1. 절머리', '',
  '지표 항목 관련 비공개 내용입니다.', '',
  '### 2. 절꼬리', '',
  '비공개 절 내용입니다.',
]);

// 현장나 — 로컬 확정 히트가 있는 문서 하나 + 부분 일치를 만드는 잡보 문서 하나.
writeDoc('현장나', '20260801-현장보고.md', [
  '# [현황] 현장나 현황보고', '',
  '> **사업장**: 현장나 · **종류**: 현황보고',
  '> **열람**: 공개', '',
  '---', '',
  '## 2026-08', '',
  '**2026-08-01 · 현장보고.pdf**', '',
  '일반 낱말 관련 안내입니다.',
]);
writeDoc('현장나', '20260802-현장잡보.md', [
  '# [잡보] 현장나 현장잡보', '',
  '> **사업장**: 현장나 · **종류**: 잡보',
  '> **열람**: 공개', '',
  '---', '',
  '## 2026-08', '',
  ...entries('알파', 5, 2),    // [8] 부분일치 5건 — '베타' 와 합쳐 8건
  ...entries('베타', 3, 7),    // [8]
  ...entries('델타', 2, 10),   // [10] 부분일치 2건 — '입실론' 과 합쳐 3건
  ...entries('입실론', 1, 12), // [10]
]);

writeSyncState();

process.env.HERMES_DATA_ROOT = DATA;

const { searchDocuments, companyWideCards } = await import('../src/documents.js');
const { PUBLIC_ACCESS } = await import('../src/config.js');

const A = PUBLIC_ACCESS;

/* ── [1]·[2] 사고 회복 자리 — 로컬 0건(확정·부분 모두)일 때 카드가 붙고 모양이 맞다 ── */
{
  const r = searchDocuments({ query: '지표 항목', project: '현장나', access: A });
  const cards = r.cards || [];
  console.log('[1/13] project=현장나, query=지표 항목 (확정 0 · 부분일치도 0) → 카드에 전사폴더A/종합보고');
  if (cards.length !== 1 || cards[0].title !== '[종합] 전사폴더A 종합보고') {
    bad(`카드: ${JSON.stringify(cards)}`);
  } else ok(`카드 1건: ${cards[0].project} / ${cards[0].title}`);

  if ((r.hits || []).length !== 0) bad(`hits 가 비어 있지 않습니다: ${JSON.stringify(r.hits)}`);
  else ok('hits 는 빈 채로');

  if (!r.note || !r.note.includes("'현장나' 에서 '지표 항목' 로는 0건입니다")) {
    bad(`note 가 0건 안내 문구가 아닙니다: ${r.note}`);
  } else if (/전사폴더A|종합보고|카드/.test(r.note)) {
    bad(`note 에 카드 관련 문구가 섞여 들어왔습니다(불변이어야 합니다): ${r.note}`);
  } else ok('note 불변 (0건 안내 그대로)');

  console.log('[2/13] 카드 조각이 {project, title, date, chars, sections} 를 다 갖고 sections=2');
  const c = cards[0];
  if (!c || typeof c.project !== 'string' || typeof c.title !== 'string' || typeof c.date !== 'string'
    || typeof c.chars !== 'number' || c.chars <= 0 || c.sections !== 2) {
    bad(`카드 모양이 어긋납니다: ${JSON.stringify(c)}`);
  } else ok(`${JSON.stringify(c)}`);
}

/* ── [3] 로컬 확정 히트가 있으면 카드를 안 낸다 ── */
{
  const r = searchDocuments({ query: '일반 낱말', project: '현장나', access: A });
  console.log('[3/13] project=현장나, query=일반 낱말 (로컬 확정 있음) → 카드 없음');
  if (r.cards?.length) bad(`카드가 붙었습니다: ${JSON.stringify(r.cards)}`);
  else ok('카드 없음 (확정 히트가 있어 안 낸다)');
}

/* ── [4] 전사 폴더 자체를 검색하면 카드가 자기 자신을 안 가리킨다 ── */
{
  const r = searchDocuments({ query: '지표 항목', project: '전사폴더A', access: A });
  console.log('[4/13] project=전사폴더A (전사 폴더 자체 검색) → 카드 없음');
  if (r.cards?.length) bad(`카드가 붙었습니다: ${JSON.stringify(r.cards)}`);
  else ok('카드 없음 (자기 자신은 안 가리킨다)');
}

/* ── [5] 비공개 문서는 공개 권한 카드에 안 실린다 ── */
{
  const cards = companyWideCards({ query: '지표 항목', project: '현장나', access: A });
  console.log('[5/13] 비공개철은 공개 권한 카드에 안 실린다 (canSeeDoc)');
  if (cards.some((c) => c.title.includes('비공개철'))) bad(`비공개 문서가 실렸습니다: ${JSON.stringify(cards)}`);
  else if (!cards.length) bad('카드 자체가 비어 있습니다 — 공개 문서까지 걸러진 것으로 보입니다');
  else ok(`카드 ${cards.length}건, 비공개철 없음`);
}

/* ── [6] document 를 함께 준 호출 → 카드 없음 ── */
{
  const r = searchDocuments({
    query: '지표 항목', project: '현장나', document: '현장보고', access: A,
  });
  console.log('[6/13] document 를 함께 준 호출 → 카드 없음 (「이 문서를 보라」와 어긋난다)');
  if (r.cards?.length) bad(`카드가 붙었습니다: ${JSON.stringify(r.cards)}`);
  else ok('카드 없음');
}

/* ── [7] project 없는 넓은 검색 → 카드 없음 ── */
{
  const r = searchDocuments({ query: '지표 항목', access: A });
  console.log('[7/13] project 없는 넓은 검색 → 카드 없음');
  if (r.cards?.length) bad(`카드가 붙었습니다: ${JSON.stringify(r.cards)}`);
  else ok('카드 없음');
}

/* ── [8]~[10] Task 3 이 확정한 C7 발동 조건 ── */
{
  const r = searchDocuments({ query: '알파 베타', project: '현장나', access: A });
  console.log(`[8/13] 확정0 · 부분일치 ${r.hits?.length ?? 0}건(≥4) · 질의 낱말이 전부 로컬에 있음 → 카드 없음 (C7 이 막는 자리)`);
  if ((r.hits || []).some((h) => typeof h.score !== 'number')) bad('확정 히트가 섞여 있어 전제가 깨졌습니다');
  else if ((r.hits || []).length < 4) bad(`부분일치가 4건 미만입니다: ${r.hits?.length}`);
  else if (r.cards?.length) bad(`카드가 붙었습니다(C7 이 막아야 합니다): ${JSON.stringify(r.cards)}`);
  else ok(`부분일치 ${r.hits.length}건, 카드 없음`);
}
{
  const r = searchDocuments({ query: '알파 감마', project: '현장나', access: A });
  console.log(`[9/13] 확정0 · 부분일치 ${r.hits?.length ?? 0}건(≥4) · 질의 낱말 중 로컬 어디에도 없는 것 1개(감마) → 카드 있음`);
  if ((r.hits || []).some((h) => typeof h.score !== 'number')) bad('확정 히트가 섞여 있어 전제가 깨졌습니다');
  else if ((r.hits || []).length < 4) bad(`부분일치가 4건 미만입니다: ${r.hits?.length}`);
  else if (!r.cards?.length) bad('카드가 안 붙었습니다');
  else ok(`부분일치 ${r.hits.length}건, 카드 ${r.cards.length}건`);
}
{
  const r = searchDocuments({ query: '델타 입실론', project: '현장나', access: A });
  console.log(`[10/13] 확정0 · 부분일치 ${r.hits?.length ?? 0}건(≤3) · 질의 낱말이 전부 로컬에 있음 → 카드 있음 (C7 의 「또는」 앞쪽 가지)`);
  if ((r.hits || []).some((h) => typeof h.score !== 'number')) bad('확정 히트가 섞여 있어 전제가 깨졌습니다');
  else if ((r.hits || []).length > 3) bad(`부분일치가 3건을 넘습니다: ${r.hits?.length}`);
  else if (!r.cards?.length) bad('카드가 안 붙었습니다');
  else ok(`부분일치 ${r.hits.length}건, 카드 ${r.cards.length}건`);
}

/* ── [11] 별칭·부분 이름으로 불러도 resolveProjectFor 를 실제로 거친다 ──
 * [8]과 같은 질의·같은 사업장이지만 폴더 이름을 글자 그대로 안 쓰고 부분 이름('현장나'의
 * 부분인 '현장')으로 부른다. fold('현장') !== fold('현장나') 라, `resolveProjectFor` 를
 * 안 거치고 opts.project 원문을 그대로 listDocuments 에 넘기면 빈 목록이 나와
 * 「로컬에 아무 낱말도 없다」로 읽혀 카드가 항상 켜진다 — [8]의 fixture(project: '현장나'
 * 글자 그대로)는 이 경로를 한 번도 안 밟아 그 함정을 못 잡았다. */
{
  const r = searchDocuments({ query: '알파 베타', project: '현장', access: A });
  console.log(`[11/13] 별칭(부분 이름 '현장') · 확정0 · 부분일치 ${r.hits?.length ?? 0}건(≥4) · 질의 낱말이 전부 로컬에 있음 → 카드 없음`);
  if ((r.hits || []).some((h) => typeof h.score !== 'number')) bad('확정 히트가 섞여 있어 전제가 깨졌습니다');
  else if ((r.hits || []).length < 4) bad(`부분일치가 4건 미만입니다: ${r.hits?.length}`);
  else if (r.cards?.length) bad(`카드가 붙었습니다(resolveProjectFor 를 안 거친 것으로 보입니다): ${JSON.stringify(r.cards)}`);
  else ok(`부분일치 ${r.hits.length}건, 카드 없음`);
}

/* ── [12] 중복 낱말 질의에서도 「전부 로컬에 있다」 판정이 산다 ──
 * '알파 베타 알파' → splitTerms 는 3개('알파' 두 번)를 내지만 고유 낱말은 2개다.
 * `need` 를 `terms.length`(3)로 재면 seen.size 가 고유 2 를 못 넘어 이 판정이 영영 못
 * 걸리고, 실제로는 둘 다 로컬에 있는데도 카드가 켜진다(2026-09-11 실측 버그 — 되돌림
 * 회귀). `need = new Set(terms).size` 여야 여기서 카드 없음이 나온다. */
{
  const r = searchDocuments({ query: '알파 베타 알파', project: '현장나', access: A });
  console.log(`[12/13] 중복 낱말('알파' 두 번) · 확정0 · 부분일치 ${r.hits?.length ?? 0}건(≥4) · 고유 낱말은 전부 로컬에 있음 → 카드 없음`);
  if ((r.hits || []).some((h) => typeof h.score !== 'number')) bad('확정 히트가 섞여 있어 전제가 깨졌습니다');
  else if ((r.hits || []).length < 4) bad(`부분일치가 4건 미만입니다: ${r.hits?.length}`);
  else if (r.cards?.length) bad(`카드가 붙었습니다(need 가 고유 낱말 수가 아닌 것으로 보입니다): ${JSON.stringify(r.cards)}`);
  else ok(`부분일치 ${r.hits.length}건, 카드 없음`);
}

/* ── [13] 못 푸는 이름 · 막힌 비공개 사업장 → 카드 없음 ──
 * `scanDocuments` 는 이름 풀기 실패를 `error` 가 아니라 `note` 로 돌려준다 — 래퍼의
 * 기존 `r.error` 검사는 이 경로에서 영원히 안 걸려, 못 찾은 사업장 이름에도 hits.length
 * 가 0(≤3) 이라 카드가 붙었다(고침 전). `searchDocuments` 가 `resolveProjectFor(...).ok`
 * 를 직접 확인해 막는다. */
{
  const r = searchDocuments({ query: '알파 감마', project: '존재안함사업장', access: A });
  console.log('[13/13] 못 푸는 사업장 이름 → hits 빈 채로(note 로만), 카드 없음');
  if ((r.hits || []).length !== 0) bad(`hits 가 비어 있지 않습니다: ${JSON.stringify(r.hits)}`);
  else if (!r.note) bad('이름 풀기 실패인데 note 가 없습니다');
  else if (r.cards?.length) bad(`카드가 붙었습니다(resolveProjectFor.ok 관문이 안 막은 것으로 보입니다): ${JSON.stringify(r.cards)}`);
  else ok(`note="${r.note}", 카드 없음`);
}

fs.rmSync(TMP, { recursive: true, force: true });

console.log(failed ? `\n실패 ${failed}건` : '\n전부 통과');
process.exit(failed ? 1 : 0);
