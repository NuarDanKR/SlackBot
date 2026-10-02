#!/usr/bin/env node
/**
 * 캐시 진단 집계가 「안 맞았다」와 「못 쟀다」를 가르나.
 *
 *   node scripts/check-cache-stats.js
 *
 * 종료코드: 0 통과 / 1 어긋남 (stderr 에 사유)
 *
 * ── 왜 필요한가 ──
 *
 * 이 표는 2026-08-28 까지 셋을 한 칸에 세고 있었다.
 *
 *   ① 진단이 붙기 전 회차(그때는 필드 자체가 없다)를 「맞았다」쪽에 넣었다
 *   ② `previous_message_not_found`(대조할 앞 요청을 못 찾음 = 못 쟀다)를 미스로 셌다
 *   ③ 그래서 8월 표가 「129건 중 16건」으로 읽혔는데 실측은 「64건 중 3건」이었다
 *
 * 셋 다 **에러를 내지 않는다** — 숫자만 조용히 틀리고, 그 숫자를 보고 줄일 것이
 * 있다고 판단하게 된다. 그래서 검사가 필요하다.
 *
 * **파일도 네트워크도 안 쓴다.** cacheStats 에 합성 항목을 직접 먹인다.
 */
import { cacheStats } from '../src/convo-log.js';

let ok = true;
const fail = (m) => { console.error(`  ✗ ${m}`); ok = false; };
const eq = (what, got, want) => {
  if (got !== want) fail(`${what}: ${got} (기대 ${want})`);
};

const AT = '2026-08-28T08:30:00.000Z';
const qa = (cacheMiss) => (cacheMiss === undefined ? { at: AT, kind: 'qa' } : { at: AT, kind: 'qa', cacheMiss });
const MISS = { type: 'system_changed', count: 1, tokens: 26_873 };
const UNKNOWN = { type: 'previous_message_not_found', count: 1, tokens: 0 };

/* ① 필드가 없는 회차는 분모에서 빠진다.
 *
 * 여기가 갈리면 「재본 적 없는 회차」가 맞은 쪽에 들어가 미스율이 실제보다 낮게 나온다.
 * 옛 기록만이 아니라 **빈 질문·모델을 못 부르고 죽은 회차**도 이 갈래로 온다. */
{
  const s = cacheStats([qa(undefined), qa(undefined), qa([]), qa([MISS])]);
  eq('진단이 붙은 문답', s.qaMeasured, 2);
  eq('잰 적 없는 문답', s.qaUnmeasured, 2);
  eq('실제 미스 회차', s.withMiss, 1);
}

/* ② `previous_message_not_found` 는 미스로 세지 않는다 — 이 검사의 본체다.
 *
 * 합쳐 세면 줄일 수 있는 몫이 부풀어 보인다(8월 실측 3건 → 16건). */
{
  const s = cacheStats([qa([UNKNOWN]), qa([UNKNOWN]), qa([MISS]), qa([])]);
  eq('실제 미스 회차', s.withMiss, 1);
  eq('진단 실패 회차', s.withUnknown, 2);
  eq('진단이 붙은 문답', s.qaMeasured, 4);

  const row = s.rows.find((r) => r.type === 'previous_message_not_found');
  if (!row) fail('진단 실패 사유가 표에서 통째로 사라졌습니다 — 안 보이면 없는 것이 됩니다.');
  else if (!row.unknown) fail("`previous_message_not_found` 가 미스로 판정됐습니다 (unknown=false).");
}

/* ③ 한 회차가 둘 다 가지면 양쪽에 다 센다.
 *
 * 툴 루프는 회차마다 진단이 오므로 한 질문 안에서 사유가 섞일 수 있다. 한쪽으로만
 * 세면 그 질문의 실제 미스가 「못 쟀다」에 가려진다. */
{
  const s = cacheStats([qa([MISS, UNKNOWN])]);
  eq('둘 다 든 회차 — 미스', s.withMiss, 1);
  eq('둘 다 든 회차 — 진단 실패', s.withUnknown, 1);
  eq('둘 다 든 회차 — 분모', s.qaMeasured, 1);
}

/* ④ 새 사유는 기본이 「미스」다.
 *
 * 모르는 것을 「못 쟀다」쪽에 넣으면 표 아래로 밀려 조용히 묻힌다. 이 표는 묻히면
 * 안 되는 것을 보여주려고 있는 자리라, 모르면 눈에 띄는 쪽에 둔다. */
{
  const s = cacheStats([qa([{ type: '내일_생길_사유', count: 1, tokens: 12_345 }])]);
  eq('새 사유 — 미스로 셈', s.withMiss, 1);
  eq('새 사유 — 진단 실패 아님', s.withUnknown, 0);
  if (s.rows[0]?.unknown) fail('새 사유가 「진단 실패」로 판정됐습니다 — 조용히 묻힙니다.');
}

/* ⑤ 정기 발송은 분모에 안 든다 (툴 루프도 색인도 안 쓴다).
 *
 * 들면 문답 미스율이 정기 발송 건수만큼 희석된다. 다만 **사유 표에는 실린다** —
 * 나중에 정기 발송에 진단을 달았을 때 조용히 빠지지 않게 두는 자리다. */
{
  const s = cacheStats([
    { at: AT, kind: 'daily', cacheMiss: [MISS] },
    { at: AT, kind: 'health', cacheMiss: [] },
    qa([]),
  ]);
  eq('정기 발송을 뺀 분모', s.qaMeasured, 1);
  eq('정기 발송은 문답 미스로 안 셈', s.withMiss, 0);
  if (!s.rows.some((r) => r.type === 'system_changed')) {
    fail('정기 발송의 사유가 표에서 빠졌습니다 — 진단을 달아도 안 보이게 됩니다.');
  }
}

/* ⑥ 미스가 위, 「못 쟀다」가 아래. 사람이 위에서부터 읽으므로 순서가 곧 무게다. */
{
  const s = cacheStats([qa([UNKNOWN, UNKNOWN, UNKNOWN]), qa([MISS])]);
  if (s.rows[0]?.type !== 'system_changed') {
    fail(`횟수가 적어도 실제 미스가 위여야 합니다 — 지금 첫 행: ${s.rows[0]?.type}`);
  }
}

/* ⑦ 항목이 하나도 없으면 빈 표가 아니라 절 자체가 빠져야 한다 (renderIndex 가 rows 로 판정). */
{
  const s = cacheStats([qa([]), qa(undefined)]);
  eq('사유가 없을 때 행 수', s.rows.length, 0);
}

/* ⑧ 대조 기준이 없던 회차는 「맞았다」가 아니다 — 이 검사가 가장 조용히 틀리는 자리다.
 *
 * 봇을 다시 켜면 기준(직전 응답 id)이 사라지고, 그러면 API 가 첫 회차 사유를 **아예 안
 * 보낸다.** 사유가 빈 배열로 오므로 `some(...)` 류의 판정에는 **절대 안 걸리고**, 그래서
 * 세지 않으면 자동으로 맞은 쪽에 들어간다. 하필 그 회차가 색인(약 2.7만 토큰)을 반드시
 * 다시 쓰는 자리다. `cacheBaseline: false` 를 claude.js 가 남기고 여기서 가른다. */
{
  const s = cacheStats([
    { at: AT, kind: 'qa', cacheMiss: [], cacheBaseline: false },
    { at: AT, kind: 'qa', cacheMiss: [], cacheBaseline: true },
    qa([]),
  ]);
  eq('기준 없던 회차', s.noBaseline, 1);
  eq('기준 없음 — 진단 실패로 셈', s.withUnknown, 1);
  eq('기준 없음 — 미스로는 안 셈', s.withMiss, 0);
  eq('기준 없음 — 분모에는 든다', s.qaMeasured, 3);

  const row = s.rows.find((r) => r.type === '(대조 기준 없음)');
  if (!row) fail('기준 없던 회차가 표에서 통째로 빠졌습니다 — 안 보이면 없는 것이 됩니다.');
  else if (!row.unknown) fail('「대조 기준 없음」이 미스로 판정됐습니다 — 사유는 못 잰 것입니다.');
}

/* ⑨ 옛 기록에는 필드가 없다 (`undefined`). 그것을 「기준이 없었다」로 읽으면 안 된다 —
 * 우리가 아는 것은 「모른다」이고, 8월 이전 전 회차가 한꺼번에 이 칸으로 들어온다. */
{
  const s = cacheStats([qa([]), qa([MISS])]);
  eq('필드 없는 옛 기록', s.noBaseline, 0);
  if (s.rows.some((r) => r.type === '(대조 기준 없음)')) {
    fail('필드가 없는 옛 기록이 「기준 없음」으로 세어졌습니다.');
  }
}

/* ⑩ 기준도 없고 뒤 회차에서 실제 미스도 난 질문은 양쪽에 다 센다.
 *
 * 첫 회차만 기준이 없고 2회차부터는 그 질문 안에서 대조되므로 실제로 섞여 온다. 한쪽으로만
 * 세면 그 질문의 진짜 미스가 「못 쟀다」에 가려진다 (③ 과 같은 이유). */
{
  const s = cacheStats([{ at: AT, kind: 'qa', cacheMiss: [MISS], cacheBaseline: false }]);
  eq('섞인 회차 — 미스', s.withMiss, 1);
  eq('섞인 회차 — 진단 실패', s.withUnknown, 1);
  eq('섞인 회차 — 기준 없음', s.noBaseline, 1);
}

process.exit(ok ? 0 : 1);
