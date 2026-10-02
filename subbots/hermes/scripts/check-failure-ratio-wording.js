#!/usr/bin/env node
/**
 * hermes-log index.md 의 「실패·재시도 사유」절 — 분모가 정직한가.
 *
 *   node scripts/check-failure-ratio-wording.js
 *
 * 종료코드: 0 통과 / 1 실패
 *
 * ── 왜 필요한가 ──
 *
 * 이 절은 「정기 발송 N회 중 M회 실패」로 분수를 적었다. N(`broadcastTotal`)은 **로그에
 * 남은** 정기 발송 수인데, 자동 반영(kind 'ingest')은 실패만 기록한다(WHK 결정
 * 2026-08-28). 그래서 성공한 아침·저녁 회차가 전부 분모에서 빠지고, 실패율이 실제보다
 * 나쁘게 읽힌다. 「N회 중 M회」 표현을 버리고 분모를 모른다는 사실이 드러나는 문구로
 * 바꿨다(WHK 결정 2026-09-03 — 예약 스케줄로 기대 회차를 역산하는 안은 config 변경·
 * 다운타임을 못 반영해 실측처럼 보이는 틀린 값을 낼 위험이 있어 버렸다).
 *
 * **파일도 네트워크도 안 쓴다.** failureSectionLines 에 합성 entries 를 직접 먹인다.
 */
import { failureSectionLines } from '../src/convo-log.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };

// 자동 반영(ingest) 실패 둘 + 성공한 daily 하나. 성공 회차가 있어도 ingest 는 로그에
// 안 남으므로, 옛 문구라면 「정기 발송 2회 중 2회 실패」(=100%)처럼 실제보다 나쁘게 나온다.
const entries = [
  { kind: 'ingest', ok: false, errorType: 'ingest_fatal' },
  { kind: 'ingest', ok: false, errorType: 'gate_failed' },
  { kind: 'daily', ok: true, attempts: [{ model: 'm', waitMs: 0, errorType: null }] },
];
const lines = failureSectionLines(entries).join('\n');

if (/\d+\s*회\s*중\s*\d+\s*회/.test(lines)) {
  fail(`"N회 중 M회" 분수 표현이 남아 있습니다 — 자동 반영은 실패만 기록해 분모가 실제보다 작습니다.\n${lines}`);
}
if (!lines.includes('실패 2건')) fail(`실패 건수가 안 보입니다.\n${lines}`);
if (!/기록하지 않|모름|알 수 없/.test(lines)) {
  fail(`분모를 모른다는 사실이 문구에 안 드러납니다.\n${lines}`);
}

// 실패가 하나도 없으면 절 자체를 안 낸다 — 좋은 상태를 빈 표로 보여줄 이유가 없다(기존 규칙).
const empty = failureSectionLines([{ kind: 'daily', ok: true }]);
if (empty.length) fail(`실패·재시도가 없는데 절이 나왔습니다.\n${empty.join('\n')}`);

if (!ok) process.exit(1);
console.log('  ✓ 「N회 중 M회」 대신 분모를 모른다는 사실이 드러납니다');
