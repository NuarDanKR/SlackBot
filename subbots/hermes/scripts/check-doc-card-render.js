#!/usr/bin/env node
/**
 * 「전사 종합 포인터 카드」 **렌더** 검사 — 봇이 실제로 받는 글자를 잰다.
 *
 *   node scripts/check-doc-card-render.js
 *
 * 종료코드: 0 통과 / 1 실패 있음
 *
 * ── 왜 필요한가 ──
 *
 * `check-doc-cards.js` 는 `companyWideCards`·`searchDocuments` 가 **카드 배열을 만드는지**
 * 만 잰다. 그 카드를 실제로 프롬프트에 그리는 자리(`src/llm/tools.js` 의 search 도구
 * run 안 카드 render — 구역 제목·안내문·`- ` 한 줄 렌더)는 어느 검사도 안 본다
 * (2026-09-11 최종 검토 Important 4) —
 * `check-outside-hits.js` 도 `buildTools` 를 부르지만 카드가 아니라 outside 구역만 잰다.
 * 그래서 `tools.js` 의 그 블록을 통째로 지우거나 렌더 모양을 바꿔도 어떤 검사도 안
 * 빨개졌다.
 *
 * `buildTools` 의 `search` 도구를 실제로 불러(`check-outside-hits.js` 와 같은 방식),
 * 돌아온 문자열 안에서 카드 구역만 본다.
 *
 * **[5/5] — 2026-09-11 성능·비용 검토 Important 1.** 전사 폴더 문서 중 절이 나뉘지 않은
 * 것(실측 16건 중 15건)을 카드로 안내하면서 「read_document 로 절 목차를 열어
 * 확인하세요」라고 하면 ⓐ 열 절이 없는데 절을 열라고 지시하고 ⓑ 봇이 그대로 따르면
 * `read_document` 가 본문을 통째로 돌려준다(실측 최악 51,154자=38,268토큰=$0.24). 그래서
 * 안내는 카드마다 `sections` 유무로 갈려야 한다 — fixture 에 절이 있는 문서 하나·없는
 * 문서 하나를 함께 둬서 그 갈림을 잰다.
 *
 * **실물 아카이브를 안 건드린다** — `check-doc-cards.js` 와 같은 가짜 자료 저장소 방식.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

let failed = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { failed += 1; console.error(`  ✗ ${m}`); };

/* ── 가짜 자료 저장소 (check-doc-cards.js 의 [1] 시나리오와 같은 모양) ─────── */

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-doc-card-render-'));
const DATA = path.join(TMP, 'data');
fs.mkdirSync(DATA, { recursive: true });

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

// 절이 나뉜 문서 — 1번 칸(구조)으로 뽑힌다. 카드 안내는 「절 목차를 열어 확인하세요」여야 한다.
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
  '다른 절의 내용입니다. 참고 낱말: 알파 베타 감마.',
]);
// 절이 안 나뉜 문서 — 관련성 칸(2번)에 뽑힌다. 카드 안내는 「통째로 옵니다」로 달라야 한다.
writeDoc('전사폴더A', '20260810-부속자료.md', [
  '# [부속] 전사폴더A 부속자료', '',
  '> **사업장**: 전사폴더A · **종류**: 부속자료',
  '> **열람**: 공개', '',
  '---', '',
  '## 2026-08', '',
  '**2026-08-10 · 부속자료.pdf**', '',
  '지표와 항목 관련 내용이 절 구분 없이 쭉 이어지는 문서입니다. 여러 문단이 있습니다.',
]);
writeDoc('현장나', '20260801-현장보고.md', [
  '# [현황] 현장나 현황보고', '',
  '> **사업장**: 현장나 · **종류**: 현황보고',
  '> **열람**: 공개', '',
  '---', '',
  '## 2026-08', '',
  '**2026-08-01 · 현장보고.pdf**', '',
  '일반 낱말 관련 안내입니다.',
]);

// slack-export 는 `only:'documents'` 로 안 건드리지만, listArchivedChannels 가 존재 확인만
// 해서 없어도 죽지 않는다 — 그래도 assertArchive() 가 다른 경로에서 불릴 수 있어 최소한을 둔다.
fs.mkdirSync(path.join(DATA, 'slack-export', 'channels'), { recursive: true });
fs.writeFileSync(path.join(DATA, 'slack-export', 'index.md'), '# 색인\n', 'utf8');
writeSyncState();

process.env.HERMES_DATA_ROOT = DATA;

const { PUBLIC_ACCESS } = await import('../src/config.js');
const { buildTools } = await import('../src/claude.js');

const A = PUBLIC_ACCESS;
const search = buildTools({ access: A, touched: new Set() }).find((t) => t.name === 'search');

const rendered = await search.run({ query: '지표 항목', where: '현장나', only: 'documents' });

/* 카드 구역만 뜯어낸다 — 다음 `## ` 구역 시작 전까지. */
function extractSection(text, headerMarker) {
  const idx = text.indexOf(headerMarker);
  if (idx === -1) return null;
  const rest = text.slice(idx);
  const nextIdx = rest.indexOf('\n## ', 1);
  return nextIdx === -1 ? rest : rest.slice(0, nextIdx);
}

const HEADER = '## 전사 종합 문서 (발췌 없음 — 카드만)';
const section = extractSection(rendered, HEADER);

console.log('[1/5] 카드 구역 제목이 그대로 실린다');
if (!section) bad(`카드 구역을 못 찾았습니다 — 전문:\n${rendered}`);
else ok(`구역 발견: "${HEADER}"`);

if (section) {
  console.log('[2/5] 절 있는 카드 줄이 「- 사업장 / 제목 · 날짜 · 글자수자 · 절 N개」 모양이고 (발췌 없음), 절 목차를 열라고 안내한다');
  const withSectionsRe = /^- 전사폴더A \/ \[종합\] 전사폴더A 종합보고 · 2026-07-31 · [\d,]+자 · 절 2개 — read_document 로 절 목차를 열어 확인하세요\.$/m;
  if (!withSectionsRe.test(section)) bad(`절 있는 카드 줄 모양이 다릅니다:\n${section}`);
  else ok('절 있는 카드 줄 모양 일치 (절 목차 안내)');

  // 발췌(본문 문장)가 새면 안 된다 — 카드는 목록 한 줄만이다.
  if (section.includes('사업개요') || section.includes('세부 내용이 여기 있습니다')) {
    bad('카드 구역에 문서 본문 발췌가 섞여 있습니다 — 카드는 발췌 없이 목록만이어야 합니다');
  } else {
    ok('본문 발췌 없음 확인');
  }

  console.log('[3/5] 안내문이 「전사 종합 보고서」· read_document 를 말한다 (구역 공통 note)');
  if (!/전사 종합 보고서/.test(section) || !/read_document/.test(section)) {
    bad(`안내문이 기대한 요소를 다 담지 않습니다:\n${section}`);
  } else {
    ok('안내문 확인');
  }

  console.log('[4/5] partial=false — 카드가 여럿이어도 「일부만 맞은 결과」 표시가 안 붙는다');
  if (section.includes('일부만 맞은 결과')) {
    bad('카드 구역에 부분 일치 표시가 붙었습니다 — 카드는 발췌 없는 안내지 추측이 아닙니다');
  } else {
    ok('부분 일치 표시 없음');
  }

  console.log('[5/5] 절 없는 카드는 「절 목차」 대신 「본문이 통째로 옵니다」로 다르게 안내한다 (2026-09-11 성능·비용 검토 Important 1)');
  const withoutSectionsRe = /^- 전사폴더A \/ \[부속\] 전사폴더A 부속자료 · 2026-08-10 · [\d,]+자(?! · 절) — 절이 나뉘어 있지 않아 read_document 로 열면 본문 [\d,]+자가 통째로 옵니다\.$/m;
  if (!withoutSectionsRe.test(section)) {
    bad(`절 없는 카드 줄이 없거나 안내가 다릅니다(「절 목차」로 잘못 안내했을 수 있습니다):\n${section}`);
  } else if (/부속자료[\s\S]*?절 목차를 열어 확인하세요/.test(section)) {
    bad('절 없는 카드에 「절 목차를 열어 확인하세요」가 붙었습니다 — 열 절이 없는데 절을 열라고 지시합니다');
  } else {
    ok('절 없는 카드는 「통째로 옵니다」로 다르게 안내됨');
  }
} else {
  console.log('[2/5]~[5/5] 건너뜀 — 카드 구역 자체를 못 찾았습니다');
}

fs.rmSync(TMP, { recursive: true, force: true });

console.log(failed ? `\n실패 ${failed}건` : '\n전부 통과');
process.exit(failed ? 1 : 0);
