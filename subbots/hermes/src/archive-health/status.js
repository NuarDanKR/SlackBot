/** Archive age and pending-item status. Shared decision rules are injected unchanged. */
export function createHealthStatus({ config, path, ARCHIVE_DIR, readJson, DEFAULTS, activeDeferred, suppress }) {
  /**
   * 대화 동기화가 며칠 밀렸는지.
   * @returns {{days:number, lastSync:string}|{missing:true}}
   */
  function syncLag(now = Date.now()) {
    const state = readJson(path.join(ARCHIVE_DIR, '.sync-state.json'));
    const last = state?.last_sync;
    if (!last) return { missing: true };
    const t = Date.parse(last);
    if (Number.isNaN(t)) return { missing: true };
    return { days: Math.floor((now - t) / 86400000), lastSync: String(last).slice(0, 10) };
  }

  /**
   * 슬랙에서 고쳐지거나 지워졌는데 **N일 넘게 반영 안 된 것**.
   *
   * 자동 반영이 매일 찾아 `.pending-edits.json` 에 적고 07:00 DM 으로 「미반영 N건」이라고
   * 알린다. 그런데 그 줄은 **며칠째인지를 안 말해서** 2026-08-10 에 8/8·8/9 자 두 건이
   * 그대로 남아 있었다 — `firstSeen` 은 파일에 저장돼 있는데 아무도 읽지 않았다.
   * 여기서 나이를 세어 오래된 것만 09:00 에 한 번 더 올린다.
   *
   * **읽기만 한다.** 그 파일은 자동 반영이 회차마다 통째로 다시 쓰므로 여기서 손대면 지워진다.
   * 사람이 「나중에」로 미룬 것은 `.sync-state.json` 의 `deferred` 에 있고, 만기 판정은
   * 문서 쪽과 같은 `activeDeferred` 를 그대로 쓴다.
   */
  function stalePendingEdits(now = Date.now(), minDays = DEFAULTS.pendingEditDays) {
    const items = readJson(path.join(ARCHIVE_DIR, '.pending-edits.json'))?.items || [];
    if (!items.length) return [];

    const state = readJson(path.join(ARCHIVE_DIR, '.sync-state.json'));
    const held = new Set(activeDeferred(state, now).map(([id]) => id));

    const today = new Intl.DateTimeFormat('sv-SE', { timeZone: config.timezone }).format(new Date(now));
    const t0 = Date.parse(today);

    return items
      .filter((it) => !held.has(it.id))
      .map((it) => ({ ...it, days: Math.round((t0 - Date.parse(String(it.firstSeen))) / 86400000) }))
      .filter((it) => Number.isFinite(it.days) && it.days >= minDays)
      .sort((a, b) => b.days - a.days);
  }

  /**
   * 07:00 목록에 올라왔는데 **N일 넘게 안 정한 것**.
   *
   * 07:00 보고는 매일 오지만 09:00 은 이상할 때만 온다. 개수만 매일 흘러가면 곧 안 읽히고,
   * 그때부터 진짜 경고도 같이 묻힌다 — 수정·삭제 쪽이 2026-08-10 에 정확히 그래서
   * 위(`stalePendingEdits`)가 생겼다. 같은 이유로 이쪽도 오래된 것만 한 번 더 올린다.
   *
   * **읽기만 한다.** 그 파일은 자동 반영이 회차마다 다시 쓴다.
   *
   * **거르는 판정은 `pending-work.js` 의 `suppress` 를 그대로 부른다.** 전에는 여기서
   * `deferred` 만 걸렀는데, 그 근거가 「「빼·나중에」는 이미 suppress 가 파일에서 빼 뒀다」
   * 였다 — 맞지만 **그것은 다음 07:00 회차가 파일을 다시 쓴 뒤의 이야기다.** 사람이 정한
   * 시점부터 그 아침까지는 파일에 그대로 있고, 그 구간에서 「빼」로 정한 것이 계속
   * 세어졌다. 방금 처리한 건이 두 시간 뒤에 「N일째」로 다시 오면 사람은 곧 이 알림을
   * 안 읽게 된다 — 이 함수가 막으려던 바로 그 일이다.
   * (2026-09-04 실물 2건. 「나중에」는 걸러지고 「빼」만 안 걸러지는 비대칭이 증거였다.)
   */
  function stalePendingWork(now = Date.now(), minDays = DEFAULTS.pendingEditDays) {
    const items = readJson(path.join(ARCHIVE_DIR, '.pending-work.json'))?.items || [];
    if (!items.length) return [];

    const state = readJson(path.join(ARCHIVE_DIR, '.sync-state.json'));

    const today = new Intl.DateTimeFormat('sv-SE', { timeZone: config.timezone }).format(new Date(now));
    const t0 = Date.parse(today);

    // **「반영」도 여기서 걸러진다** (2026-09-07 부터). 그 기록이 로컬 전용
    // `.decision-stamp.json` 에만 있던 동안은 이 함수에 안 보여서, 반영 당일 09:00·16:00
    // 점검이 같은 건을 한 번 더 셌다 — 지금은 「반영·빼·나중에」 셋 다 `.sync-state.json`
    // 이라 `suppress` 한 곳에서 갈린다.
    return suppress(items, state, now)
      .map((it) => ({ ...it, days: Math.round((t0 - Date.parse(String(it.firstSeen))) / 86400000) }))
      .filter((it) => Number.isFinite(it.days) && it.days >= minDays)
      .sort((a, b) => b.days - a.days);
  }

  return { syncLag, stalePendingEdits, stalePendingWork };
}
