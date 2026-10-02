#!/usr/bin/env node
/**
 * **제목이 통째로 가려진 전사 종합 문서가 카드 상한을 갉아먹지 않는지** 잰다.
 *
 *   node scripts/check-doc-card-masked-title.js
 *
 * 종료코드: 0 통과 / 1 실패 있음
 *
 * ── 왜 필요한가 (2026-09-11 이월 Minor 고침 + 회의적 검증의 지적) ──
 *
 * 카드는 제목이 전부인 안내다. 그래서 `redactPrivateMentions` 가 제목 줄을 통째로
 * 지운 카드(= 봇이 `read_document` 로 부를 이름이 없는 막다른 카드)는 뺀다(선례
 * `projectDocInventory` 와 같은 규칙).
 *
 * 그런데 **거르기가 자르기보다 뒤에** 있으면, 그 문서가 1번 칸에 앉았을 때 그 자리가
 * 빈 채로 잘려 나가 카드가 상한(2장)보다 **적게** 나간다. 방향은 안전하지만(적게 나감)
 * 그만큼 볼 수 있는 다음 후보가 조용히 버려진다.
 *
 * **이것이 이번 이월 Minor 고침 중 봇 동작을 실제로 바꾸는 유일한 자리인데, 되돌려도
 * 카드 검사 넷이 전부 초록이었다**(2026-09-11 회의적 검증). 그래서 이 검사를 만든다.
 *
 * 드문 경우가 아니다 — 실물에서 **전사 폴더 안에 제목이 가려지는 문서가 1건** 있고,
 * 「그 폴더는 보되 그 문서가 가리키는 비공개 채널은 못 보는」 부분 권한에서 실제로
 * 도는 경로다(같은 검증의 실측: 그 권한으로 재면 카드 결과 **152건**이 달라진다).
 *
 * ── 어떻게 재나 ──
 *
 * 실물 아카이브를 안 건드린다 — `check-doc-cards.js` 와 같은 가짜 자료 저장소 방식이고,
 * 사업장·채널 이름은 전부 중립 이름이다. 전사 폴더에 문서 셋을 둔다.
 *   ① 제목에 비공개 채널 이름이 든 문서(공개 권한에서 제목이 통째로 지워진다) —
 *      날짜가 가장 늦어 **1번 칸(구조 칸)**에 앉는다
 *   ②③ 제목이 멀쩡하고 질의 낱말을 가진 문서 둘
 * 공개 권한에서 상한 2로 부르면 **②③ 두 장**이 나와야 한다. 거르기가 뒤에 있으면
 * ①이 잘린 자리를 채우지 못해 **한 장**만 나온다.
 *
 * 대조군도 함께 둔다 — 그 비공개 채널을 볼 수 있는 권한에서는 ①이 1번 칸에 그대로
 * 나온다(제목이 안 지워지므로). 빠지는 이유가 **가리기 때문**임을 그것이 못 박는다.
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

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-doc-card-masked-'));
const DATA = path.join(TMP, 'data');
fs.mkdirSync(DATA, { recursive: true });

const HIDDEN = '숨은채널';   // 중립 이름 — 실물 채널 이름을 쓰지 않는다
const exampleConfig = JSON.parse(fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf8'));
exampleConfig.search = { companyWideDocProjects: ['전사폴더A'] };
exampleConfig.privateChannels = [HIDDEN];
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
 * 이름을 전부 현재 이름으로 등록한다(`file` == `name` = 개명 안 한 채널). 비공개
 * 판정은 그대로다 — 지도에 있는 것과 `privateChannels` 에 적힌 것은 별개다. */
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

/** 절 둘을 가진 전사 종합 문서 하나. `절` 이 있어야 구조 칸(1번) 후보가 된다. */
function wideDoc(slugDate, title, word) {
  return [
    `# ${title}`, '',
    '> **사업장**: 전사폴더A · **종류**: 종합보고',
    '> **열람**: 공개', '',
    '---', '',
    `## ${slugDate.slice(0, 4)}-${slugDate.slice(4, 6)}`, '',
    `**${slugDate.slice(0, 4)}-${slugDate.slice(4, 6)}-${slugDate.slice(6, 8)} · 종합보고.pdf**`, '',
    '### 1. 사업개요', '',
    `${word} 관련 내용이 여기 있습니다.`, '',
    '### 2. 재무현황', '',
    `${word} 에 대한 설명이 이어집니다.`,
  ];
}

// ① 제목에 비공개 채널 이름 — 공개 권한에서 제목 줄이 통째로 지워진다. 날짜가 가장 늦다.
writeDoc('전사폴더A', '20260903-가려질보고.md', wideDoc('20260903', `[종합] ${HIDDEN} 종합보고`, '지표'));
// ②③ 제목이 멀쩡한 문서 둘
writeDoc('전사폴더A', '20260902-둘째보고.md', wideDoc('20260902', '[종합] 둘째 종합보고', '지표'));
writeDoc('전사폴더A', '20260901-셋째보고.md', wideDoc('20260901', '[종합] 셋째 종합보고', '지표'));

// 카드를 받을 사업장 — 질의 낱말이 여기엔 없다
writeDoc('현장나', '20260801-현장보고.md', [
  '# [현황] 현장나 현황보고', '',
  '> **사업장**: 현장나 · **종류**: 현황보고',
  '> **열람**: 공개', '',
  '---', '',
  '## 2026-08', '',
  '**2026-08-01 · 현장보고.pdf**', '',
  '일반 낱말 관련 안내입니다.',
]);

fs.mkdirSync(path.join(DATA, 'slack-export', 'channels'), { recursive: true });
fs.writeFileSync(path.join(DATA, 'slack-export', 'index.md'), '# 색인\n', 'utf8');
writeSyncState();

process.env.HERMES_DATA_ROOT = DATA;

const { companyWideCards } = await import('../src/documents.js');
const { PUBLIC_ACCESS, FULL_ACCESS } = await import('../src/config.js');

const call = (access) => companyWideCards({ query: '지표', project: '현장나', access, max: 2 });

/* ── [1/3] 전제 확인 — 가려짐이 실제로 일어나고, 그 문서가 1번 칸 후보다 ── */

console.log('[1/3] 전제: 그 문서 제목이 공개 권한에서 통째로 지워지고, 전체 권한에서는 1번 칸에 온다');
{
  const full = call(FULL_ACCESS);
  if (!full.length || !full[0].title.includes(HIDDEN)) {
    bad(`전체 권한에서 1번 칸이 그 문서가 아닙니다 — 픽스처 전제가 깨졌습니다: ${JSON.stringify(full)}`);
  } else if (full.length !== 2) {
    bad(`전체 권한에서 카드가 2장이 아닙니다: ${JSON.stringify(full)}`);
  } else {
    ok(`전체 권한: 1번 칸 = ${full[0].title} · 2장`);
  }
}

/* ── [2/3] 본론 — 가려진 카드가 상한을 갉아먹지 않는다 ── */

console.log('[2/3] 공개 권한: 제목이 가려진 1번 칸이 빠져도 카드가 상한(2장)만큼 나온다');
{
  const pub = call(PUBLIC_ACCESS);
  const titles = pub.map((c) => c.title);
  if (pub.length !== 2) {
    bad(`카드가 ${pub.length}장입니다(2장이어야 합니다) — 거르기가 자르기보다 **뒤**에 있으면 `
      + `가려진 1번 칸이 잘려 나간 자리를 못 채웁니다: ${JSON.stringify(titles)}`);
  } else if (titles.some((t) => !t.trim())) {
    bad(`빈 제목 카드가 실렸습니다(봇이 부를 이름이 없습니다): ${JSON.stringify(titles)}`);
  } else if (titles.some((t) => t.includes(HIDDEN))) {
    bad(`가려져야 할 문서 제목이 그대로 실렸습니다: ${JSON.stringify(titles)}`);
  } else {
    ok(`카드 2장 · 빈 제목 없음: ${titles.join(' · ')}`);
  }
}

/* ── [3/3] 방향 — 못 보는 문서가 그 자리를 대신 채우지는 않는다 ── */

console.log('[3/3] 빈자리를 채우는 것은 **볼 수 있는** 다음 후보뿐이다');
{
  const pub = call(PUBLIC_ACCESS).map((c) => c.title);
  const full = call(FULL_ACCESS).map((c) => c.title);
  // 공개 권한 결과는 전체 권한 결과에서 가려진 것만 빠진 모양이어야 한다 —
  // 권한 때문에 못 보던 문서가 새로 끼어들면 안 된다.
  const extra = pub.filter((t) => !full.includes(t) && t !== '[종합] 셋째 종합보고');
  if (extra.length) bad(`전체 권한에도 없던 카드가 공개 권한에서 새로 나왔습니다: ${JSON.stringify(extra)}`);
  else ok('새로 끼어든 카드 없음 (빈자리는 다음 후보가 채운다)');
}

fs.rmSync(TMP, { recursive: true, force: true });

console.log(failed ? `\n실패 ${failed}건` : '\n전부 통과');
process.exit(failed ? 1 : 0);
