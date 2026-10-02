/** Codex R3b: pure failure/cache statistics. No I/O or config imports.
 * Policy sets remain owned by the facade and retain their original references.
 */
export function createLogStats({ BROADCAST, DIAG_FAILED, NO_BASELINE, isQaRound, isBroadcastRound }) {
  /**
   * 실패·재시도 사유 집계. **파일을 안 읽는다** — 검사가 합성 항목으로 부른다
   * (scripts/check-failure-log.js). 렌더 안에 묻어 두면 그 자리를 시험할 방법이 없다.
   *
   * 정기 발송만 센다. 문답은 사람이 앞에서 기다리는 자리라 실패의 뜻이 다르고,
   * 지금 문답 경로는 errorType 을 안 남긴다.
   */
  function failureStats(entries) {
    const failed = new Map();
    const retried = new Map();
    let broadcastTotal = 0;
    let failedTotal = 0;
    let retriedTotal = 0;

    for (const e of entries) {
      if (!isBroadcastRound(e)) continue;
      broadcastTotal += 1;
      if (e.ok === false) {
        failedTotal += 1;
        // 사유가 없어도 세어 둔다 — 조용히 사라지면 「실패가 줄었다」로 잘못 읽힌다.
        const k = e.errorType || '(기타)';
        failed.set(k, (failed.get(k) || 0) + 1);
      } else if (e.attempts?.length > 1) {
        retriedTotal += 1;
        for (const t of e.attempts) {
          if (!t.errorType) continue;
          retried.set(t.errorType, (retried.get(t.errorType) || 0) + 1);
        }
      }
    }

    const rows = [...new Set([...failed.keys(), ...retried.keys()])]
      .map((type) => ({ type, failed: failed.get(type) || 0, retried: retried.get(type) || 0 }))
      .sort((a, b) => b.failed - a.failed || b.retried - a.retried);

    return { broadcastTotal, failedTotal, retriedTotal, rows };
  }

  /**
   * 「실패·재시도 사유」절 — 텍스트만. **파일을 안 읽는다** — 검사가 합성 entries 로
   * 직접 부른다(scripts/check-failure-ratio-wording.js). failureStats 와 나누는 이유는
   * 같다: 렌더 안에 묻어 두면 그 자리를 시험할 방법이 없다.
   *
   * **분모(「N회 중」)를 안 쓴다** (WHK 결정 2026-09-03, 옵션 ㉯). `broadcastTotal` 은
   * "로그에 남은 정기 발송 수"이지 "실제로 돈 횟수"가 아니다 — 자동 반영(kind 'ingest')은
   * 실패만 기록하므로(WHK 결정 2026-08-28 — 「실패만 남긴다」는 안 뒤집는다) 성공한 아침·저녁
   * 회차가 전부 분모에서 빠지고, 그만큼 이 비율이 실제보다 나쁘게 읽힌다.
   *
   * 예약 스케줄에서 기대 회차 수를 계산해 분모로 쓰는 안(옵션 ㉮)도 있었지만 버렸다 —
   * config.json 의 cron 은 이력 내내 바뀌어 왔고 봇이 안 돈 구간(배포·장애)도 있어서, 지금
   * 시점의 cron 으로 과거를 역산하면 **실측처럼 보이는 틀린 값**을 낸다. 계산이 자신 있는
   * 만큼만 「추정」이라고 적어 봐야, 사람은 숫자만 보고 추정인지 실측인지 구분하지 않는다.
   * 그래서 분모를 아예 안 적고 「기록하지 않는다」는 사실 자체를 드러낸다.
   */
  function failureSectionLines(entries) {
    const fs2 = failureStats(entries);
    if (!fs2.rows.length) return [];
    const lines = [
      '',
      '## 실패·재시도 사유',
      '',
      `> 실패 ${fs2.failedTotal}건 (전체 회차 수는 기록하지 않습니다 — 자동 반영은 실패만 ` +
        `남기므로 「N회 중 M회」로 적으면 분모가 실제보다 작게 잡힙니다) · ` +
        `재시도 끝에 나간 것 ${fs2.retriedTotal}건.`,
      '> 사유는 API 가 준 값 그대로입니다 — 한글로 옮기면 새 사유가 생겼을 때 조용히 묻힙니다.',
      '',
      '| 사유 | 실패 | 재시도로 넘김 |',
      '|---|---|---|',
    ];
    for (const r of fs2.rows) lines.push(`| \`${r.type}\` | ${r.failed} | ${r.retried} |`);
    return lines;
  }

  /**
   * 캐시 진단 집계. **파일을 안 읽는다** — 검사가 합성 항목으로 부른다
   * (scripts/check-cache-stats.js). failureStats 와 같은 이유다: 렌더 안에 묻어 두면
   * 그 자리를 시험할 방법이 없다.
   *
   * 가르는 것이 둘이다.
   *
   * ① **분모는 「진단이 붙은 문답」이다.** `cacheMiss` 필드는 커밋 `8468500`
   *    (배포 2026-08-12 09:09) 부터 붙었고 그 전 회차에는 필드 자체가 없다. 전부 세면
   *    **재본 적도 없는 회차가 「맞았다」쪽에 들어간다** — 2026-08-28 에 이 표가 미스율
   *    12%(16/129)로 읽혔는데 잰 구간만 보면 3/64 였다. 모델을 못 부르고 죽은 회차와
   *    빈 질문도 필드가 없어 같이 빠진다(맞다 — 잴 것이 없다).
   *
   * ② **미스와 「못 쟀다」를 같은 칸에 세지 않는다** (DIAG_FAILED, NO_BASELINE). 합쳐 세면
   *    줄일 수 있는 몫이 부풀어 보인다. 「못 쟀다」로 오는 길이 둘이다 — API 가
   *    `previous_message_not_found` 로 답한 것과, **기준이 없어 사유가 아예 안 온 것**
   *    (`cacheBaseline: false`). 뒤엣것은 사유 줄이 비어서 오므로, 세지 않으면 자동으로
   *    「맞았다」쪽에 들어간다. 하필 그 회차가 색인을 다시 쓰는 자리다.
   *
   * 사유별 표(rows)는 정기 발송까지 포함해 전 항목에서 센다 — 지금 정기 발송은 진단을
   * 안 달고 나가지만, 나중에 달았을 때 조용히 빠지지 않게 두는 자리다.
   */
  function cacheStats(entries) {
    const misses = new Map();
    let qaMeasured = 0;
    let qaUnmeasured = 0;
    let withMiss = 0;
    let withUnknown = 0;
    let noBaseline = 0;

    for (const e of entries) {
      /* 캐시 미스는 문답에서만 센다 — 정기 발송은 툴 루프도 색인도 안 쓴다.
       * 사용량만 적으러 온 기록도 분모가 아니다 (파사드의 `isQaRound` 주석이 원본).
       * 전에는 여기만 `accountingOnly` 를 안 뺐다 — 판정이 네 곳에 흩어져 있었다. */
      if (isQaRound(e)) {
        if (Array.isArray(e.cacheMiss)) {
          qaMeasured += 1;
          if (e.cacheMiss.some((m) => !DIAG_FAILED.has(m.type))) withMiss += 1;
          /* 기준이 없던 회차는 사유가 비어서 온다 — `some` 으로는 절대 안 잡힌다.
           * 두 필드는 늘 같이 실리므로(claude.js 의 같은 return) 분모 안에서 함께 센다. */
          const lostBaseline = e.cacheBaseline === false;
          if (lostBaseline) noBaseline += 1;
          if (lostBaseline || e.cacheMiss.some((m) => DIAG_FAILED.has(m.type))) withUnknown += 1;
        } else {
          qaUnmeasured += 1;
        }
      }
      for (const m of e.cacheMiss || []) {
        const cur = misses.get(m.type) || { n: 0, tokens: 0 };
        cur.n += m.count || 0;
        cur.tokens += m.tokens || 0;
        misses.set(m.type, cur);
      }
    }

    /* 기준이 없던 회차도 표에 한 행으로 올린다 — **횟수는 문답 건수다**(회차마다 오는 API
     * 사유와 달리, 기준이 없는 것은 질문당 첫 회차 한 번이다). 놓친 토큰은 「못 잼」으로 둔다. */
    const synthetic = noBaseline ? [{ type: NO_BASELINE, n: noBaseline, tokens: 0 }] : [];

    // 미스를 위로, 그 안에서 놓친 토큰이 큰 것부터. 「못 쟀다」는 아래로 몰아 둔다.
    const rows = [...[...misses].map(([type, v]) => ({ type, n: v.n, tokens: v.tokens })), ...synthetic]
      .map((r) => ({ ...r, unknown: DIAG_FAILED.has(r.type) || r.type === NO_BASELINE }))
      .sort((a, b) => Number(a.unknown) - Number(b.unknown) || b.tokens - a.tokens || b.n - a.n);

    return { qaMeasured, qaUnmeasured, withMiss, withUnknown, noBaseline, rows };
  }

  return { failureStats, failureSectionLines, cacheStats };
}
