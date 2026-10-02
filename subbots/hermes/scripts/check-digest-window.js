#!/usr/bin/env node
/**
 * 일일 요약 구간이 **못 보낸 데부터 이어지고, 상한에서 정직하게 잘리나.**
 *
 *     node scripts/check-digest-window.js
 *
 * 종료코드: 0 통과 / 1 실패 있음
 *
 * ── 왜 남겨 두는가 ──
 *
 * 2026-09-03 까지 `dailyWindow` 는 「**지금부터** 24시간 전」이었고, 직전 실행 시각을
 * 적어 두는 자리가 저장소 전체에 없었다. 그래서 한 회차가 죽으면 그 24시간이
 * **어떤 요약에도 안 들어갔고**, 팀에게는 끊김 없이 이어진 것처럼 보였다 — 실물로
 * 2026-08-31 17:30 ~ 09-01 17:30 이 지금도 비어 있다(`hermes-log/2026-09.md` 의
 * `17:30 · 일일 요약 · ⚠️ 실패`, `authentication_error`). 실패 DM 이 안내하던 복구
 * 명령마저 창을 「지금」에 다시 붙여서 되메울 수단이 아예 없었다.
 *
 * **상한 48시간은 「평소는 24시간 그대로, 한 회차 실패했을 때만 메운다」는 뜻이다**
 * (WHK 결정 2026-09-03). 이 검사가 지키는 것은 그 문장이다 — 정상 회차의 창이
 * 24시간에서 늘어나지 않아야 하고, 한 회차가 죽으면 48시간이 되어야 하고,
 * **두 회차 연속으로 죽으면 잘리되 그 사실이 조용하지 않아야 한다.**
 *
 * 「조용하지 않다」가 이 검사의 핵심 축이다. 잘린 구간은 **실제로 잃는** 자료라,
 * `cappedFrom` 이 그 사실을 부르는 쪽에 알리고 콘솔에도 한 줄 남아야 한다. 그 둘이
 * 없으면 상한은 「조용히 버리는 장치」가 된다 — 고치려던 결함과 같은 모양이다.
 *
 * **네트워크도 API 도 안 쓴다.** `dailyWindow` 에 상태를 직접 넣어 부르고,
 * `markDailyDigestSent` 는 임시 폴더에만 쓴다(진짜 `logs/` 를 안 건드린다).
 */
process.env.ANTHROPIC_API_KEY ||= 'check-stub-not-used';

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { dailyWindow, markDailyDigestSent } from '../src/slack-live.js';

let bad = 0;
const ok = (m) => console.log(`  ✓ ${m}`);
const fail = (m) => { bad += 1; console.log(`  ✗ ${m}`); };
const eq = (what, got, want) => (got === want ? ok(`${what} — ${got}`)
  : fail(`${what} — ${got} (${want} 여야 합니다)`));

const H = 3600;
const NOW = new Date('2026-09-02T17:30:00+09:00');
const now = Math.floor(NOW.getTime() / 1000);
const hours = (w) => Math.round((w.latest - w.oldest) / H);

// 콘솔 경고를 가로채 「조용하지 않은가」를 실제로 잰다.
function captureWarn(fn) {
  const real = console.warn;
  const lines = [];
  console.warn = (...a) => lines.push(a.join(' '));
  try { return [fn(), lines]; } finally { console.warn = real; }
}

console.log('[1/3] 창이 못 보낸 데부터 이어진다');
{
  eq('  기록이 없으면 24시간 (한 번도 안 나갔거나 상태 파일이 없다)',
    hours(dailyWindow(NOW, 'Asia/Seoul', 24, { sentThrough: null, maxHours: 48 })), 24);
  eq('  정상 회차(직전 성공 24시간 전)는 그대로 24시간',
    hours(dailyWindow(NOW, 'Asia/Seoul', 24, { sentThrough: now - 24 * H, maxHours: 48 })), 24);
  eq('  한 회차 실패(48시간 전)면 48시간으로 메운다',
    hours(dailyWindow(NOW, 'Asia/Seoul', 24, { sentThrough: now - 48 * H, maxHours: 48 })), 48);

  /* **기록이 미래로 튀면 무시한다.** 시계 되돌림·손으로 고친 상태 파일에서 나는데,
   * 그대로 쓰면 창이 음수가 되어 **아무것도 안 실린 요약이 조용히 나간다.** */
  eq('  기록이 미래면 무시하고 24시간으로 돌아간다',
    hours(dailyWindow(NOW, 'Asia/Seoul', 24, { sentThrough: now + 10 * H, maxHours: 48 })), 24);
}

console.log('[2/3] 상한에 걸리면 잘리되 조용하지 않다');
{
  const [w, warns] = captureWarn(() =>
    dailyWindow(NOW, 'Asia/Seoul', 24, { sentThrough: now - 72 * H, maxHours: 48 }));
  eq('  두 회차 연속 실패(72시간)는 48시간으로 잘린다', hours(w), 48);
  if (w.cappedFrom === now - 72 * H) ok('  잘라낸 시작점을 부르는 쪽에 알린다 (cappedFrom)');
  else fail(`  cappedFrom 이 ${w.cappedFrom} 입니다 — 잘라낸 시작점이어야 합니다`);
  if (warns.length === 1 && warns[0].includes('상한')) ok('  잘린 사실이 콘솔에 남는다 (조용히 안 버린다)');
  else fail(`  잘렸는데 콘솔 경고가 ${warns.length}줄입니다 — 정확히 1줄이어야 합니다`);

  // **안 잘렸을 때는 경고가 없어야 한다.** 매번 뜨면 곧 아무도 안 읽는다.
  const [w2, warns2] = captureWarn(() =>
    dailyWindow(NOW, 'Asia/Seoul', 24, { sentThrough: now - 48 * H, maxHours: 48 }));
  if (w2.cappedFrom === null && warns2.length === 0) ok('  안 잘렸으면 경고도 cappedFrom 도 없다');
  else fail(`  안 잘렸는데 cappedFrom=${w2.cappedFrom} · 경고 ${warns2.length}줄입니다`);

  /* 상한을 24 로 낮춰 두면 「되메우기를 끄는」 설정이 된다 — 그때도 하루치는 나가야 한다.
   * `Math.max(maxHours, hours)` 가 그것을 지킨다. */
  const [w3] = captureWarn(() =>
    dailyWindow(NOW, 'Asia/Seoul', 24, { sentThrough: now - 72 * H, maxHours: 24 }));
  eq('  상한을 24로 낮춰도 창이 24시간 아래로 안 내려간다', hours(w3), 24);
}

console.log('[3/3] 성공한 회차만 기록한다 (상태 파일 왕복)');
{
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-digest-'));
  const file = path.join(dir, 'nested', 'digest-state.json');
  try {
    if (markDailyDigestSent(now, file) && fs.existsSync(file)) ok('  없는 폴더에도 기록한다');
    else fail('  기록에 실패했습니다');
    eq('  적은 값을 그대로 읽는다', JSON.parse(fs.readFileSync(file, 'utf8')).dailySentThrough, now);

    // 0·NaN 은 기록하지 않는다 — 적으면 다음 회차 창이 0 부터가 되어 아카이브 전체를 훑는다.
    eq('  0 은 기록하지 않는다', markDailyDigestSent(0, file), false);
    eq('  숫자가 아니면 기록하지 않는다', markDailyDigestSent('어제', file), false);
    eq('  그래도 앞서 적은 값은 안 망가진다',
      JSON.parse(fs.readFileSync(file, 'utf8')).dailySentThrough, now);

    /* **깨진 파일은 조용히 24시간으로 물러선다.** 던지면 요약 자체가 안 나간다 —
     * 하루치를 못 보내는 것보다 이틀치를 못 메우는 쪽이 덜 나쁘다. */
    fs.writeFileSync(file, '{반쪽', 'utf8');
    let threw = false;
    try { dailyWindow(NOW, 'Asia/Seoul', 24, { maxHours: 48 }); } catch { threw = true; }
    eq('  상태 파일이 깨져도 안 던진다', threw, false);

    // 쓰다 죽어도 반쪽 JSON 이 안 남는다 — 임시 파일에 쓰고 이름을 바꾼다.
    markDailyDigestSent(now, file);
    eq('  임시 파일을 안 남긴다', fs.existsSync(`${file}.tmp`), false);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
}

/* **위 [1~3] 은 논리만 잰다 — 값은 전부 인자로 넣어 주기 때문이다.** 그래서 누가 코드의
 * 기본값이나 설정 파일의 숫자를 바꿔도 저기서는 아무 일도 안 일어난다. 실제로
 * 2026-09-03 에 이 검사를 red-green 으로 시험하다 그 구멍을 찾았다 — 기본값을 48 →
 * 999 로 바꿔도 [1~3] 이 전부 통과했다. 아래가 그 자리를 막는다. */
console.log('[4/4] 정한 값이 코드·설정·문서에서 같다');
{
  const CHOSEN = 48;   // WHK 결정 2026-09-03 — 평소 24h · 한 회차 실패 때만 메움
  // 인자를 안 주면 설정을 읽는다. 72시간 밀린 상태에서 창이 설정값만큼 나와야 한다.
  const w = captureWarn(() => dailyWindow(NOW, 'Asia/Seoul', 24, { sentThrough: now - 72 * H }))[0];
  eq(`  설정을 읽었을 때 창이 ${CHOSEN}시간이다 (인자를 안 넘긴 경로)`, hours(w), CHOSEN);

  const src = fs.readFileSync(new URL('../src/slack/live-window.js', import.meta.url), 'utf8');
  const codeDefault = Number(/maxHours = config\.digest\?\.daily\?\.maxHours \?\? (\d+)/.exec(src)?.[1]);
  eq('  코드의 기본값 (설정이 없는 새 설치가 받는 값)', codeDefault, CHOSEN);

  const ex = JSON.parse(fs.readFileSync(new URL('../config.example.json', import.meta.url), 'utf8'));
  eq('  config.example.json 의 값 (새 설치가 복사해 가는 값)', ex?.digest?.daily?.maxHours, CHOSEN);

  /* 문서까지 보는 이유 — 이 저장소가 이미 겪은 실패다. 고치기 전 `config.json` 은
   * 「직전 실행 이후」, `README.md` 는 「지금부터 24시간」이라고 서로 반대를 적고 있었고
   * 코드는 또 달랐다. 문서는 에러를 안 내므로 아무도 안 잡는다. */
  const readme = fs.readFileSync(new URL('../README.md', import.meta.url), 'utf8');
  const inDoc = /상한은 (\d+)시간입니다/.exec(readme)?.[1];
  eq('  README 가 적어 둔 값', Number(inDoc), CHOSEN);
}

console.log(bad ? `\n✗ ${bad}건 실패` : '\n✓ 전부 통과');
process.exit(bad ? 1 : 0);
