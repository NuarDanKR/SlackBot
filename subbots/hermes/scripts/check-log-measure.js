#!/usr/bin/env node
/**
 * 재보기(`run-log-measure.js`)가 md 를 **어느 회차 것으로** 읽나.
 *
 *     node scripts/check-log-measure.js
 *
 * 종료코드: 0 통과 / 1 실패
 *
 * ── 왜 있나 ──
 *
 * 2026-09-16 까지 `run-log-measure.js` 에는 **시험이 하나도 없었다** (검사 카탈로그에도
 * 없었다). 그 파일의 훑는 갈래를 통째로 되돌려도 저장소의 어느 검사도 안 빨개졌다.
 * 그런데 그 갈래는 **어느 회차에 얼마를 매기나**를 정하는 자리다 — 틀리면 에러가 아니라
 * 돈 숫자가 조용히 다른 회차로 간다.
 *
 * 특히 사용량 기록(`#### 요약 대조 비용 계측 …` + `> **계측 금액** …`)은 **회차 헤더가
 * 없다.** 그 블록에서 앞 회차를 안 놓으면 그 금액 줄이 앞 회차의 값을 덮어쓴다.
 * 그 기록은 `8c3957f` 에서 생겨 아직 배포 전이라 실물 로그에는 0건이고, **배포하면
 * 그때부터 매일 난다.** 실물이 없으니 합성 줄이 유일한 방어다.
 *
 * **파일도 설정도 안 쓴다** — 그 스크립트의 `scanLines` 본문만 떼어 와서 합성 줄로 몬다.
 * 파서는 `createLogCodec` 으로 진짜를 만들어 넣는다(검사가 자기 파서를 흉내 내면 진짜가
 * 갈려도 조용히 통과한다).
 *
 * ── 여기서 안 보는 것 ──
 *
 * 렌더한 줄이 파서로 그대로 되읽히나는 **`check-usage-accounting.js`** 가 본다.
 * 여기는 읽은 값을 **어느 회차에 얹나**만 본다. 그래서 아래 합성 줄의 모양은 그쪽이
 * 렌더와 맞대 본 것을 그대로 쓴다.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { compileFunction } from 'node:vm';
import { createLogCodec } from '../src/convo-log/codec.js';

const KIND_LABEL = { qa: '문답', empty: '빈 질문', daily: '일일 요약', weekly: '주간 요약', health: '위생 점검', ingest: '자동 반영' };
const num = (v) => Number(v || 0).toLocaleString('en-US');
const codec = createLogCodec({ KIND_LABEL, num });

/* 그 스크립트의 본문을 그대로 쓴다 — 파일 읽기 없이. 경계가 어긋나면 잘라 온 것이
 * 엉뚱한 조각이므로 **조용히 통과하지 않고 여기서 멈춘다.** */
const src = fs.readFileSync(new URL('run-log-measure.js', import.meta.url), 'utf8');
const start = src.indexOf('function scanLines');
const end = src.indexOf('\nfunction readRendered', start);
assert.ok(start >= 0 && end > start, 'run-log-measure.js 의 scanLines 를 못 찾았습니다');
const { scanLines } = compileFunction(
  src.slice(start, end) + '\nreturn { scanLines };',
  ['parseEntryHeader', 'parseToolLine', 'parseCostLine', 'TOOL_LINE_PREFIX'],
)(codec.parseEntryHeader, codec.parseToolLine, codec.parseCostLine, codec.TOOL_LINE_PREFIX);

let passed = 0;
const check = (name, fn) => { fn(); passed += 1; console.log('  ✓ ' + name); };

const DAY = '## 2026-01-01';
const HEAD = '### 12:34 · 아무개 · #어느채널 · 문답';
const COST = '> **비용** claude-opus-5 · 12.3초 · in 100 / cache-w 40 / cache-r 50 / out 10 · 약 $0.615';
const ACCT_HEAD = '#### 요약 대조 비용 계측 (운영 회차·실패 건수에서 제외)';
const ACCT = '> **계측 금액** 2026-01-01T00:05:00Z · 확인분 USD 0.492665 / 미상 1회';

check('평범한 회차 — 금액과 소요 시간이 그 회차에 붙는다', () => {
  const { entries, bad } = scanLines([DAY, '', HEAD, '', COST], 'f.md');
  assert.deepEqual(bad, []);
  assert.equal(entries.length, 1);
  assert.equal(entries[0].costUsd, 0.615);
  assert.equal(entries[0].seconds, 12.3);
  assert.equal(entries[0].date, '2026-01-01');
});

check('사용량 기록은 회차가 아니고, 그 금액이 앞 회차를 덮지 않는다', () => {
  const { entries, bad } = scanLines([DAY, '', HEAD, '', COST, '', ACCT_HEAD, '', ACCT], 'f.md');
  assert.deepEqual(bad, []);
  assert.equal(entries.length, 1, '사용량 기록을 회차로 세면 안 됩니다');
  assert.equal(entries[0].costUsd, 0.615, '앞 회차의 금액이 사용량 기록으로 덮였습니다');
});

check('회차가 아닌 블록 안의 비용 줄도 앞 회차에 붙지 않는다', () => {
  /* 지금 나가는 사용량 기록에는 모델이 없어 `cur` 을 건드릴 일이 없지만, 그건 **오늘의
   * 우연**이지 계약이 아니다. 그 블록에 모델이 실린 줄이 한 번만 들어오면 앞 회차의
   * 금액이 통째로 바뀐다 — 두 회차의 돈이 섞이고 아무 표시가 없다. */
  const other = '> **비용** claude-opus-5 · 1.0초 · in 1 / cache-w 1 / cache-r 1 / out 1 · 약 $9.999';
  const { entries } = scanLines([DAY, '', HEAD, '', COST, '', ACCT_HEAD, '', other], 'f.md');
  assert.equal(entries.length, 1);
  assert.equal(entries[0].costUsd, 0.615, '회차 밖 비용 줄이 앞 회차에 붙었습니다');
});

check('못 읽은 비용 줄은 덮어쓰지 않고 말한다', () => {
  // 덮어쓰면 그 회차가 `null` 이 되어 「공짜였다」와 「못 쟀다」가 같은 칸에 들어간다.
  const { entries, bad } = scanLines([DAY, '', HEAD, '', COST, '', '> **비용** claude opus 5 · 약 $9.999'], 'f.md');
  assert.equal(entries[0].costUsd, 0.615);
  assert.equal(bad.length, 1, '못 읽은 비용 줄을 조용히 넘기면 안 됩니다');
});

check('날짜 헤딩 없이 시작한 회차는 「모른다」로 통과시키지 않는다', () => {
  const { entries, bad } = scanLines([HEAD, '', COST], 'f.md');
  assert.equal(entries.length, 0);
  assert.equal(bad.length, 1);
});

check('도구 줄이 삼켜지면 말한다', () => {
  const line = codec.TOOL_LINE_PREFIX + codec.toolLine({ name: 'search', input: { query: '어느 낱말' }, chars: 10 });
  const ok = scanLines([DAY, '', HEAD, '', line], 'f.md');
  assert.deepEqual(ok.bad, []);
  assert.equal(ok.entries[0].tools.length, 1);
  const torn = scanLines([DAY, '', HEAD, '', codec.TOOL_LINE_PREFIX + 'search(가 → read_document(나'], 'f.md');
  assert.equal(torn.bad.length, 1);
});

console.log(`\n${passed} log-measure checks passed.`);
