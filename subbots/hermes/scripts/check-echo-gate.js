#!/usr/bin/env node
/**
 * 봇 질문 반향이 대화 안전망을 잠그지 않나.
 *
 *   node scripts/check-echo-gate.js
 *
 * 종료코드: 0 통과 / 1 어긋남
 *
 * ── 왜 필요한가 ──
 *
 * 봇에게 한 질문은 채널 md 에 일반 회차로 남는다(답변만 「미수록」). 그래서 같은 낱말의
 * 좁힘 검색은 항상 확정 히트 ≥1(과거 질문)을 받고, outside 안전망(확정 0건일 때만)이
 * 영영 안 켜졌다 — 같은 질문을 할수록 굳는 자기강화다 (2026-09-11 실측: 전 채널 51건).
 *
 * 히트에서 지우지 않는다 — 질문 회차의 스레드에 사람의 답이 붙는 실물이 있다.
 * 빼는 곳은 「발동 판정」과 「outside 내용물」 둘뿐이다.
 *
 * [1]~[7]은 그 판정 함수(`isEchoEntry`·`hasRealConfirmed`) 자체를 잰다. **이것만으로는
 * 부족했다** — `src/archive.js:1002`(발동 관문)·`1014`(outside 필터)를 옛 줄로 되돌려도
 * [1]~[7]은 전부 초록이었다(2026-09-11 최종 검토 Important 4). 판정 함수는 맞아도
 * 그 함수를 부르는 배선(`searchArchive`)이 끊기면 아무 의미가 없다.
 *
 * [8]은 그 배선을 **실물 아카이브 대신 임시 자료 저장소**로 잰다 — `searchArchive` 를
 * 직접 불러, 반향뿐인 채널에서 진짜로 outside 가 켜지는지 · 다른 채널의 반향은
 * outside 에 안 섞이는지를 함께 본다. `1002`·`1014` 어느 쪽을 되돌려도 이 자리가
 * 빨개진다(둘 다 일부러 되돌려 확인함, task 보고 참고).
 *
 * **실물 아카이브를 안 건드린다** — `HERMES_DATA_ROOT` 를 임시 폴더로 돌려놓고 그
 * 아래에 가짜 자료 저장소를 만들어 돈다. `src/config.js`·`src/archive.js` 를 정적
 * import 하면 안 되고(모듈 최상위가 먼저 돌아 실물 경로가 굳는다) 이 파일 전체가
 * `await import()` 로 늦게 가져온다(check-doc-cards.js·check-thread-loss.js 와 같은 이유
 * — [1]~[7]은 순수 함수라 아카이브 경로와 무관하지만, 한 파일에서 함수를 두 번 다른
 * 경로로 import 하면 헷갈리므로 전부 이 시점에서 함께 가져온다).
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const pass = (m) => console.log(`  ✓ ${m}`);

/* ── 가짜 자료 저장소 (isEchoEntry·hasRealConfirmed 를 가져오기 전에 만든다) ──── */

const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-echo-gate-'));
const DATA = path.join(TMP, 'data');
fs.mkdirSync(path.join(DATA, 'slack-export', 'channels'), { recursive: true });
fs.writeFileSync(
  path.join(DATA, 'config.json'),
  fs.readFileSync(path.join(ROOT, 'config.example.json'), 'utf8'),
  'utf8',
);
fs.writeFileSync(path.join(DATA, 'slack-export', 'index.md'), '# 색인\n', 'utf8');

const MARK = '(봇 답변 — 미수록)';

/* 개명 되짚기 지도(`.sync-state.json`)에 넣을 줄들. **없으면 config.js 가 이 가짜
 * 아카이브를 「지도가 죽었다」로 보고(md 는 있는데 지도가 없다) 전체 권한이 아닌
 * 접근을 전부 닫는다** — 그러면 [8] 의 검색 히트가 0이 되어 전제부터 깨진다.
 * 여기 채널들은 개명한 적이 없으므로 `name` 만 있으면 된다. */
const syncStateChannels = {};

function writeChannel(name, lines) {
  fs.writeFileSync(
    path.join(DATA, 'slack-export', 'channels', `${name}.md`),
    lines.join('\n'),
    'utf8',
  );
  syncStateChannels[`C${String(Object.keys(syncStateChannels).length + 1).padStart(9, '0')}`] =
    { name, file: name };
}

// 채널가 — 질의 낱말을 다 맞춘 회차가 **반향뿐**이다(사람 확정 히트가 하나도 없다).
writeChannel('채널가', [
  '# #채널가', '',
  '**2026-09-02 14:40 · 질문자**',
  '@봇이름 지표 확인 부탁',
  '',
  `> 💬 스레드 1건 ${MARK}`,
]);
// 채널나 — 같은 낱말을 다 맞춘 **진짜** 회차(반향이 아니다). 밖 안전망이 켜지면 이게 잡혀야 한다.
writeChannel('채널나', [
  '# #채널나', '',
  '**2026-09-03 09:00 · 담당자**',
  '지표 확인 결과 안내합니다. 오늘 오전 집계입니다.',
]);
// 채널다 — 같은 낱말을 다 맞춘 **또 다른 반향**. outside 필터가 죽으면 이게 섞여 든다.
writeChannel('채널다', [
  '# #채널다', '',
  '**2026-09-04 10:00 · 질문자2**',
  '@봇이름 지표 확인 문의',
  '',
  `> 💬 스레드 1건 ${MARK}`,
]);

fs.writeFileSync(
  path.join(DATA, 'slack-export', '.sync-state.json'),
  JSON.stringify({ channels: syncStateChannels }, null, 2),
  'utf8',
);

process.env.HERMES_DATA_ROOT = DATA;

const { isEchoEntry, hasRealConfirmed, searchArchive } = await import('../src/archive.js');
const { PUBLIC_ACCESS } = await import('../src/config.js');

/* ── [1]~[7] 판정 함수 자체 ─────────────────────────────────────── */

const echo = `**2026-09-02 14:40 · 질문자**\n@봇이름 현장 지표 총액?\n\n> 💬 스레드 1건 ${MARK}`;
const normal = `**2026-09-02 14:00 · 담당자**\n지표 IM입니다. 진행중입니다.\n\n> 💬 **스레드 (7)**`;
const threadOnly = `**2026-08-24 11:00 · 담당자**\n실데이터 공유합니다.\n\n> 💬 스레드 3건 ${MARK}`;

// [1] 반향 회차 판정
if (!isEchoEntry(echo)) fail('[1] 반향 회차를 못 알아본다');
else pass('[1] 반향 회차 판정');

// [2] 일반 회차는 반향이 아니다
if (isEchoEntry(normal)) fail('[2] 일반 회차를 반향으로 오인');
else pass('[2] 일반 회차 무해');

// [3] 사람 본문 + 스레드 안 봇 질문(THREAD-ONLY)은 반향이 아니다 — 본문이 사람 것이라 지우면 안 된다
if (isEchoEntry(threadOnly)) fail('[3] 사람 본문 회차를 반향으로 오인 — 정보 손실이 난다');
else pass('[3] THREAD-ONLY 회차 보존');

// [4] CRLF 에서도 같게
if (!isEchoEntry(echo.replace(/\n/g, '\r\n'))) fail('[4] CRLF 반향을 못 알아본다');
else pass('[4] CRLF 무관');

// [5] 관문: 확정이 반향뿐이면 「진짜 확정 없음」
{
  const hits = [{ text: echo }];           // score 없음 = 확정
  if (hasRealConfirmed(hits)) fail('[5] 반향뿐인데 확정 있음으로 판정 — 안전망이 안 켜진다');
  else pass('[5] 반향뿐이면 안전망이 켜질 수 있다');
}

// [6] 진짜 확정이 하나라도 있으면 그대로 「있음」
{
  const hits = [{ text: echo }, { text: normal }];
  if (!hasRealConfirmed(hits)) fail('[6] 진짜 확정을 놓친다 — 기존 경로가 바뀌면 안 된다');
  else pass('[6] 진짜 확정 보존');
}

// [7] 부분 일치(score 있음)는 원래 확정이 아니다
{
  const hits = [{ text: normal, score: 2 }];
  if (hasRealConfirmed(hits)) fail('[7] 부분 일치를 확정으로 셌다');
  else pass('[7] 부분 일치 무관');
}

/* ── [8] 배선 — 실물 searchArchive 를 가짜 자료 저장소로 잰다 ─────────── */
{
  const r = searchArchive({ query: '지표 확인', channel: '채널가', access: PUBLIC_ACCESS });
  console.log('[8] searchArchive 배선 — 반향뿐인 채널에서 outside 가 실제로 켜지고, 다른 반향은 안 섞인다');

  if ((r.hits || []).length !== 1) {
    fail(`[8-a] 채널가의 확정 히트가 1건이 아닙니다(전제 확인 실패): ${JSON.stringify(r.hits)}`);
  } else {
    pass('[8-a] 채널가 확정 히트 1건(반향) — 히트에서는 안 지운다');
  }

  // 1002행: hasRealConfirmed 를 옛 줄(`base.hits.some(h => typeof h.score !== 'number')`)로
  // 되돌리면 반향도 확정으로 세어 여기서 base 를 그대로 돌려준다 — outside 가 undefined 로
  // 남는다. 반향뿐인 채널에서 안전망이 영영 안 켜지는 원래 사고 그대로다.
  if (!r.outside?.length) {
    fail('[8-b] 반향뿐인데 outside 가 안 켜졌습니다 — archive.js:1002 를 확인하세요(옛 hasRealConfirmed 판정으로 돌아간 것으로 보입니다)');
  } else {
    pass(`[8-b] outside 켜짐 (${r.outside.length}건)`);
  }

  // 1014행: !isEchoEntry(h.text) 필터를 지우면 채널다의 반향이 여기 섞여 outside.length 가
  // 2가 되고, 반향 문구(MARK)가 outside 본문에 나타난다.
  const leaked = (r.outside || []).filter((h) => String(h.text).includes(MARK));
  if (leaked.length) {
    fail(`[8-c] 다른 채널의 반향이 outside 에 섞였습니다 — archive.js:1014 의 isEchoEntry 필터를 확인하세요: ${JSON.stringify(leaked)}`);
  } else if (!(r.outside || []).some((h) => h.channel === '채널나')) {
    fail(`[8-c] outside 에 채널나(진짜 확정)가 없습니다 — fixture 전제가 깨졌거나 필터가 과하게 걸렀습니다: ${JSON.stringify(r.outside)}`);
  } else {
    pass('[8-c] outside 에 진짜 확정(채널나)만 있고, 다른 채널의 반향(채널다)은 안 섞임');
  }
}

fs.rmSync(TMP, { recursive: true, force: true });

process.exit(ok ? 0 : 1);
