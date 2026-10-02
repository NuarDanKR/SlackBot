/** Daily, weekly and recent Slack windows; state reading stays in the facade. */
export function createSlackLiveWindow({ config, stamp, readDailySentThrough, DEFAULT_LIVE_FETCH_MAX_DAYS, console = globalThis.console }) {
  function tzOffsetSeconds(date, tz) {
    const dtf = new Intl.DateTimeFormat('en-US', {
      timeZone: tz, hour12: false,
      year: 'numeric', month: '2-digit', day: '2-digit',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
    const p = Object.fromEntries(dtf.formatToParts(date).map((x) => [x.type, x.value]));
    const asUTC = Date.UTC(+p.year, +p.month - 1, +p.day, p.hour === '24' ? 0 : +p.hour, +p.minute, +p.second);
    // formatToParts 는 초 단위까지만 준다. 밀리초가 남은 원본과 빼면 오프셋에 소수점이 섞이고,
    // 그 잔차가 하루 경계(oldest)를 최대 1초 뒤로 밀어 자정 직후 메시지를 놓친다. 초로 잘라 맞춘다.
    return (asUTC - Math.floor(date.getTime() / 1000) * 1000) / 1000;
  }

  function startOfLocalDay(date, tz, daysBack = 0) {
    const off = tzOffsetSeconds(date, tz);
    const local = new Date(date.getTime() + off * 1000);
    const dayStartUTC = Date.UTC(local.getUTCFullYear(), local.getUTCMonth(), local.getUTCDate()) / 1000;
    return dayStartUTC - off - daysBack * 86400;
  }

  /**
   * **직전에 실제로 나간 일일 요약 이후 ~ 지금.** 한 번도 안 나갔으면 24시간.
   *
   * 예전에는 '오늘 00:00 ~ 지금' 이었다. 요약이 17:30 에 나가므로 **매일 17:30 이후 대화는
   * 그날 요약에도, 0시부터 다시 시작하는 다음 날 요약에도 안 들어갔다.** 월~목 저녁은
   * 주간요약(월 00:00~금 16:00)이 주워 담았지만 금·토·일 저녁은 아무 데도 안 실렸다.
   * (2026-08-05 실측: 915건 중 53건이 어떤 요약에도 안 들어가는 자리에 있었다.)
   *
   * 그 뒤로는 '지금부터 24시간 전' 이었는데, 그것도 하루를 잇지 못했다 — **한 회차가 실패하면
   * 그 24시간은 영영 아무 요약에도 안 들어간다.** 다음 날 회차는 자기 24시간만 싣고, 팀에게는
   * 끊김 없이 이어진 것처럼 보인다. 2026-09-01 17:30 회차가 `authentication_error` 로 죽어
   * 2026-08-31 17:30 ~ 09-01 17:30 이 지금도 비어 있다 (`hermes-log/2026-09.md`, 9월 로그
   * 전체에 재발송 없음). `config.json` 은 그동안에도 「구간은 '직전 실행 이후 24시간' 이라
   * 하루가 빈틈없이 이어진다」고 적고 있었다 — **문서가 약속한 것을 코드가 안 지키고 있었다.**
   *
   * 그래서 **실제로 나간 회차의 끝**(`markDailyDigestSent`)을 다음 창의 시작점으로 쓴다.
   * 실패한 회차의 구간은 다음 회차가 함께 싣고, 실패 DM 이 안내하는 `npm run digest:daily` 도
   * 같은 구간을 집는다 — 이제 그 명령이 「지금부터 24시간」이 아니라 「못 보낸 데부터」다.
   *
   * **상한(`maxHours`)을 둔다.** 오래 멈춰 있다가 살아나면 한 회차가 무한정 커진다 — 모델
   * 입력이 상한을 넘거나 비용이 튀고, 그 회차마저 실패하면 더 커진다. 상한에 걸려 **잘라낸
   * 구간은 아무 요약에도 안 실리므로** 조용히 넘기지 않고 콘솔에 남긴다.
   * **기본 48시간은 「평소는 24시간 그대로, 한 회차 실패했을 때만 메운다」는 뜻이다**
   * (WHK 결정 2026-09-03). 정상이면 창은 정확히 24시간이고, 한 회차가 죽으면 48시간이
   * 되어 빠진 하루를 함께 싣는다. **두 회차 연속으로 죽으면 가장 오래된 24시간은 잘려 잃는다**
   * — 그 대신 한 요약이 며칠치로 부풀어 그 회차마저 죽는 것을 막는다. `digest.daily.maxHours`
   * 로 바꾼다.
   *
   * @param {{sentThrough?:number|null, maxHours?:number}} opts 시험에서 상태를 직접 주려고 열어 둔다
   */
  function dailyWindow(now = new Date(), tz = config.timezone, hours = 24, {
    sentThrough = readDailySentThrough(),
    maxHours = config.digest?.daily?.maxHours ?? 48,
  } = {}) {
    const latest = Math.floor(now.getTime() / 1000);
    const floor = latest - Math.max(maxHours, hours) * 3600;
    let oldest = latest - hours * 3600;
    let cappedFrom = null;
    // 기록이 미래로 튀어 있으면(시계 되돌림 등) 무시한다 — 창이 음수가 되면 아무것도 안 실린다.
    if (sentThrough && sentThrough < latest) {
      if (sentThrough < floor) cappedFrom = sentThrough;
      oldest = Math.max(sentThrough, floor);
    }
    if (cappedFrom !== null) {
      console.warn(
        `[digest] 못 보낸 구간이 상한(${maxHours}시간)을 넘어 ${stamp(cappedFrom, tz)} ~ ` +
          `${stamp(oldest, tz)} 는 이번 요약에 안 실립니다 (digest.daily.maxHours).`,
      );
    }
    return {
      oldest,
      latest,
      // 실패 뒤 이어 붙였는지·상한에 잘렸는지를 부르는 쪽이 알 수 있게 함께 준다.
      sentThrough: sentThrough ?? null,
      cappedFrom,
      label: `${stamp(oldest, tz)} ~ ${stamp(latest, tz)}`,
    };
  }

  /** 이번 주 월요일 00:00 ~ 지금 */
  function weeklyWindow(now = new Date(), tz = config.timezone) {
    const off = tzOffsetSeconds(now, tz);
    const local = new Date(now.getTime() + off * 1000);
    const dow = local.getUTCDay(); // 0=일
    const back = dow === 0 ? 6 : dow - 1;
    const oldest = startOfLocalDay(now, tz, back);
    return {
      oldest,
      latest: Math.floor(now.getTime() / 1000),
      label: `${stamp(oldest, tz).slice(0, 10)} ~ ${stamp(Math.floor(now.getTime() / 1000), tz).slice(0, 10)}`,
    };
  }

  /** 최근 N일 (fetch_recent_slack 툴용) */
  function recentWindow(days, now = new Date(), tz = config.timezone) {
    const capped = Math.min(Math.max(1, days), config.limits?.liveFetchMaxDays ?? DEFAULT_LIVE_FETCH_MAX_DAYS);
    return { oldest: startOfLocalDay(now, tz, capped - 1), latest: Math.floor(now.getTime() / 1000), days: capped };
  }

  return { dailyWindow, weeklyWindow, recentWindow };
}
