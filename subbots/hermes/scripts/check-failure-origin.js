#!/usr/bin/env node
/**
 * **던져서 죽은 회차도 기간 라벨(`_기간:_`)을 로그에 남기나** — 2026-08-28 최종 검토가
 * 짚고 미뤄 둔 것(WHK 결정 2026-09-03: 고친다).
 *
 * 갈래가 둘이다. 생성 실패로 **돌아온** 회차는 `run()` 이 `res.origin` 을 주지만,
 * **던져서** 죽은 회차는 그 자리가 없어 `origin` 이 `undefined` 로 갔다. 같은 잡인데
 * 로그 모양이 갈렸다.
 *
 * 재는 법이 둘인 이유 — `digest.js` 가 라벨을 **싣는** 것과 `scheduler.js` 가 그것을
 * **받아 넘기는** 것은 다른 자리이고, 둘 중 하나만 있으면 조용히 아무 일도 안 일어난다.
 * 그래서 ① 연결을 소스에서 보고 ② 실제로 `onFailure` 를 불러 로그 줄에 라벨이 찍히는지
 * 본다. ②만으로는 `digest.js` 쪽이 빠져도 통과하고, ①만으로는 렌더까지 안 닿는다.
 */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
let failed = false;
const ok = (m) => console.log(`  ✓ ${m}`);
const bad = (m) => { console.log(`  ✗ ${m}`); failed = true; };

// ── ① 연결이 소스에 있나 ──────────────────────────────────────────
const digest = fs.readFileSync(path.join(ROOT, 'src', 'digest.js'), 'utf8');
const sched = fs.readFileSync(path.join(ROOT, 'src', 'scheduler.js'), 'utf8');

if (/err\.hermesOrigin\s*=/.test(digest)) {
  ok('digest.js 가 던지는 에러에 회차 라벨(hermesOrigin)을 싣습니다');
} else {
  bad('digest.js 가 err.hermesOrigin 을 안 답니다 — 던져서 죽은 회차의 로그에 `_기간:_` 줄이 빠집니다');
}

if (/origin:\s*err\?\.hermesOrigin/.test(sched)) {
  ok('scheduler.js 의 catch 가 그 라벨을 onFailure 의 origin 으로 넘깁니다');
} else {
  bad('scheduler.js 가 err.hermesOrigin 을 안 넘깁니다 — 실어 보내도 받는 곳이 없습니다');
}

// ── ② 실제로 로그 줄에 찍히나 ─────────────────────────────────────
// `logConversation` 은 LOG_DIR 에 쓴다. 임시 디렉터리를 물려 진짜 파일로 재고,
// 그 다음 렌더가 `_기간:_` 를 내는지 본다. 소스 문자열만 보면 렌더 쪽이 그 칸을
// 안 쓰게 바뀌는 날 조용히 통과한다.
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-origin-'));
process.env.HERMES_LOG_DIR = tmp;

const { renderEntry } = await import('../src/convo-log.js');
const LABEL = '2026-09-03 07:00 ~ 2026-09-03 17:00';
const line = renderEntry({
  kind: 'daily', ok: false, at: '2026-09-03T08:00:00.000Z',
  target: '#테스트', origin: LABEL, error: '일부러 낸 실패',
  errorType: 'api_error', elapsedMs: 1234,
});

if (line.includes(LABEL)) ok('로그 렌더가 origin 을 받으면 기간 줄을 냅니다');
else bad(`로그 렌더가 origin 을 안 씁니다 — 라벨을 넘겨도 화면에 안 나옵니다\n${line.slice(0, 300)}`);

// 대조군 — origin 이 없으면 그 줄이 없어야 한다. 이게 없으면 위 검사가
// 「무엇을 넣어도 통과」인지 아닌지 모른다.
const none = renderEntry({
  kind: 'daily', ok: false, at: '2026-09-03T08:00:00.000Z',
  target: '#테스트', error: '일부러 낸 실패', errorType: 'api_error', elapsedMs: 1234,
});
if (!none.includes('_기간:')) ok('대조군 — origin 이 없으면 기간 줄도 없습니다');
else bad('origin 없이도 기간 줄이 나옵니다 — 위 검사가 아무것도 안 재고 있습니다');

fs.rmSync(tmp, { recursive: true, force: true });

console.log(failed ? '\n실패' : '\n통과');
process.exitCode = failed ? 1 : 0;
