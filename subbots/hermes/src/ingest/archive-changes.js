/** Extracted ingest boundary; dependencies are supplied by slack-archive.js. */
export function createArchiveChanges({ parseBlocks, stamp, renderText, renderReply, isBotMessage }) {
/**
 * 대조용 키 — **시각만. 작성자 이름은 넣지 않는다.**
 *
 * 이름을 키에 넣으면 슬랙에서 **표시 이름을 바꾸는 순간** 그 사람의 md 블록이 전부 짝을 잃어
 * "지워졌다"가 된다. 30일치가 통째로 그렇게 되고, 그걸 믿고 반영하면 멀쩡한 원문을 지운다.
 * 같은 이유로 `Claude (앱)` 대 `Claude` 같은 표기 차이도 전부 오탐이 됐다 (2026-08-05 실측 2건).
 *
 * 아카이브 헤더는 **분 단위까지만** 적는다(`**2026-06-15 14:46 · 박민수**`). 그래서 이름을
 * 빼면 같은 1분 안에 올라온 메시지끼리 구별이 안 되어 대조에서 빠진다 — 아카이브 915건 중
 * 49건(5.4%)에서 103건(11.3%)으로 는다. 빠진다는 것은 "지워졌다·고쳐졌다"고 말하지 않고
 * 손을 뗀다는 뜻이라, 놓치는 쪽으로만 늘어난다. 그래서 이 값을 받는다.
 *
 * 초 단위로 맞추면 겹침이 없어지지만 헤더 모양을 바꿔야 하고, 그 모양은 봇이 메시지를 잘라
 * 읽는 기준이라 바꾸면 **에러 없이** 검색이 0건이 된다 (`archive.js` 의 splitMessages).
 */
function minuteOf(key) {
  return key.slice(0, 16); // 'YYYY-MM-DD HH:MM'
}

function tally(list, get) {
  const c = new Map();
  for (const x of list) { const k = get(x); c.set(k, (c.get(k) || 0) + 1); }
  return c;
}

/**
 * md 와 슬랙을 대조해 고쳐진 것·지워진 것을 찾는다.
 *
 * - **삭제**: md 에 있는데 같은 구간 슬랙 조회 결과에 없을 때. 이쪽은 아무 기록도 필요 없어
 *   매번 처음부터 다시 판정된다 — 반영 전까지 계속 잡히고 반영하면 저절로 빠진다.
 *
 * - **수정**: 슬랙이 `edited` 로 표시했고, 그 편집이 **지난 반영 이후**에 났고, md 본문과
 *   실제로 다를 때. 세 조건이 다 필요하다.
 *   · 표시만 보면 안 되는 이유: 초기 수기 추출분은 사람이 옮겨 적은 것이라 슬랙 원문과
 *     표기가 조금씩 다르다(`-` 를 `•` 로, `@이름` 뒤 띄어쓰기, 첨부를 본문 줄에 붙여
 *     적은 것). 그 메시지가 예전에 한 번 편집된 적 있으면 전부 "고쳐졌다"로 나온다.
 *     2026-08-05 실측으로 9건이 그렇게 잡혔고 대부분이 이 표기 차이였다.
 *   · 시각으로 거르면 "반영 전까지 계속 잡힌다"가 깨진다. 그래서 **한 번 잡힌 것은
 *     `.pending-edits.json` 에 남아 있는 동안 시각과 무관하게 다시 대조한다**(`pendingKeys`).
 *     고치면 본문이 같아져 저절로 빠지고, 안 고치면 계속 남는다.
 */
function detectChanges({
  md, windowMsgs, repliesByTs, userMap, tz, selfId, oldest, lastTs, editedSince, pendingKeys,
  maxFindings = 10, requireComplete = false,
}) {
  const edited = [];
  const deleted = [];
  const notes = [];
  /* 같은 1분에 수정이 숨은 자리 — note+기준동결 대신 **항목**으로 남긴다 (2026-09-22
   * 갈래 ③, 설계는 워크스페이스 10-projects/260922_수집래치-갈래③_설계.md).
   * 열쇠는 블록이 아니라 **그 1분**이다 — 어느 블록인지 못 고르는 것이 이 항목의
   * 존재 이유라, 블록에 붙이는 순간 추측이 된다. `.pending-edits.json` 에 실리면
   * `pendingKeys` 가 기준 시각과 무관하게 매 회차 다시 대조하므로 기준을 얼릴
   * 필요가 없고, 항목이라서 「며칠째」가 붙고 사람이 md 를 고치면 저절로 빠진다. */
  const ambiguous = [];
  const seenAmb = new Set();
  const pushAmbiguous = (scope, min, before) => {
    if (seenAmb.has(`${scope}|${min}`)) return;
    seenAmb.add(`${scope}|${min}`);
    ambiguous.push({
      scope, key: min, header: String(before).split('\n')[0],
      replyKey: scope === 'reply' ? min : null, before, manual: true,
    });
  };
  /* 래치가 둘이고, **켜지는 조건이 다르다.**
   *
   * - `incomplete` — 판정하지 못한 후보가 있다 → 위(`slack-archive.js`)가 후보 목록을 보존한다
   * - `retry` — **되돌아보기 기준(`editedSince`)을 붙잡아 두면 다음에 더 볼 수 있다**
   *
   * 뒤의 것을 아껴 쓴다. `editedSince` 를 쓰는 곳은 **수정 대조 한 곳뿐**이고
   * (`recent = m.edited.ts > editedSince`), 삭제 대조는 매번 md 대 슬랙 개수로 처음부터
   * 판정한다. 그래서 삭제 쪽 미판정에 기준을 붙잡으면 얻는 것 없이 그 채널이 영영
   * 재시도로 돌고, 그 재시도가 `requireComplete` 를 켜서 note 를 하나 더 만든다 —
   * 래치가 스스로를 먹여 살리는 모양이 된다 (2026-09-15).
   *
   * 그리고 **판정이 끝난 것을 알리기만 하는 note 는 둘 다 안 켠다.** */
  let incomplete = false;
  let retry = false;
  const unresolved = (msg, { retry: pin = false } = {}) => {
    notes.push(msg);
    incomplete = true;
    if (pin) retry = true;
  };
  const blocks = parseBlocks(md);

  // 구간 경계는 분 단위라 양 끝에서 오탐이 난다. 1분씩 좁혀서 본다.
  const from = stamp(String(Number(oldest) + 60), tz);
  const to = stamp(String(Number(lastTs) - 60), tz);
  const inWindow = (key) => key.slice(0, 16) >= from && key.slice(0, 16) <= to;

  /* 슬랙에 **지금 있는 것 전부**. 참여 로그·봇 글도 거르지 않는다 — 여기서 거르면 그것이
   * "슬랙에 없다"가 되어 삭제로 오탐한다. 초기 수기 추출분에는 `Claude (앱) 채널 참여` 같은
   * 줄이 실제로 md 에 들어가 있다 (2026-08-05 실측 2건). */
  const live = windowMsgs.map((m) => ({ m, key: stamp(m.ts, tz) }));

  const inBlocks = blocks.filter((b) => inWindow(b.key));
  const mdCount = tally(inBlocks, (b) => minuteOf(b.key));
  const liveCount = tally(live, (x) => x.key);
  const liveByKey = new Map(live.map((x) => [x.key, x]));

  /**
   * 이 분에 **아직 못 본 것이 숨어 있을 수 있나.**
   *
   * `requireComplete` 는 지난 회차가 못 보고 온 것이 있을 때 켜지고, 그러면 후보로 기록된
   * 것이 없어도 중복된 시각을 알린다. 그런데 「같은 1분에 글 둘」은 아카이브에 그대로 남아
   * **매 회차 다시 걸린다** — 그것만으로 래치하면 한 번 걸린 채널이 영영 안 풀린다
   * (실측 2026-09-15: 최근 30일 안에 그런 채널이 43개 중 9개, 전체로는 26개).
   *
   * 숨어 있을 수 있는 것은 둘뿐이다 — ① 기준 시각 이후에 편집된 슬랙 글 ② md 보다 슬랙이
   * 적어 삭제가 의심되는 경우. 둘 다 아니면 이 분에는 판정할 것이 없다.
   */
  const recentlyEdited = (m) => Boolean(m.edited) && Number(m.edited.ts) > editedSince;
  const countShort = (min) => (liveCount.get(min) || 0) < mdCount.get(min);
  /* 이 분에 **수정이 숨어 있나.** 부모 글 자체의 수정만 보면 안 된다 — 부모 시각이 중복이면
   * 아래 답글 루프가 통째로 건너뛰므로(`mdCount !== 1 || liveCount !== 1`) 그 스레드의
   * 답글 수정도 같이 못 본다. 답글은 자기 `edited` 를 갖고 있고 부모는 안 갖고 있어서,
   * 부모만 보면 그 답글 수정이 아무 표시 없이 사라진다 (2026-09-15). */
  const editHides = (min) => live.some((x) => x.key === min
    && (recentlyEdited(x.m) || (repliesByTs.get(x.m.ts) || []).some(recentlyEdited)));

  /* 같은 1분에 **수정이 숨은** 분 — requireComplete·hasPending 과 무관하게 즉시 항목으로.
   * 이 무조건성이 「래치가 아직 없는 채널에서 수정이 중복 1분에 떨어지면 DM 한 줄 없이
   * 영영 유실되는」 갈래를 닫는다 (2026-09-20 회의적 검증이 찾은 자리). 이미 항목이 있는
   * 분은 중복이 남아 있는 동안 다시 실어 firstSeen 이 이어지게 한다. */
  for (const min of new Set(inBlocks.map((b) => minuteOf(b.key)))) {
    if ((mdCount.get(min) || 0) <= 1 && (liveCount.get(min) || 0) <= 1) continue;
    if (editHides(min) || pendingKeys.has(`ambiguous|message|${min}`)) {
      const before = inBlocks.filter((b) => minuteOf(b.key) === min)
        .map((b) => b.headerLine).join('\n') || min;
      pushAmbiguous('message', min, before);
    }
  }

  // Ambiguous parents also make their replies uncheckable. Absence (zero)
  // still participates in deletion detection; multiple matches are not proof
  // that a previously pending edit/deletion has resolved.
  for (const b of inBlocks) {
    const hasPending = ['edited', 'deleted'].some((kind) =>
      pendingKeys.has(`${kind}|message|${b.key}`)
      || b.replies.some((r) => pendingKeys.has(`${kind}|reply|${r.key}`)));
    const min = minuteOf(b.key);
    /* 수정이 숨은 분은 위에서 ambiguous 항목이 됐다 — 여기는 **기존 수정·삭제 후보가
     * 걸린 분**(hasPending)과, 재시도 회차의 **삭제 의심 분**(countShort)만 알린다.
     * 기준은 어느 쪽도 안 붙잡는다 — 붙잡으면 조건이 얼어붙어 매일 같은 줄이 나가고
     * 사람이 md 를 고치기 전까지 풀 길이 없었다 (갈래 ③ 이 그 자리를 대신한다). */
    if ((hasPending || (requireComplete && countShort(min)))
      && (mdCount.get(min) !== 1 || liveCount.get(min) > 1)) {
      unresolved(
        `부모 시각(${min})이 중복되어 수정·삭제를 짚지 못했습니다 — 그 시각의 md 블록을 `
        + '슬랙 원문과 직접 대보고 고쳐 주세요. 고칠 때까지 매일 다시 알립니다.',
      );
    }
  }

  /* 지워진 것 — 같은 분의 md 블록 수가 슬랙 메시지 수보다 많으면 그 차이만큼 지워진 것이다.
   *
   * "md 에 있는데 슬랙에 없다"로 단순 비교하면 안 된다. md 가 1건인데 슬랙이 같은 1분 안에 2건인
   * 경우(참여 로그가 함께 있는 등)가 흔하고, 그건 지워진 것이 아니라 애초에 안 담은 것이다.
   * 개수로 보면 그 경우가 저절로 걸러진다. */
  for (const b of inBlocks) {
    const min = minuteOf(b.key);
    if ((liveCount.get(min) || 0) >= mdCount.get(min)) continue;
    if (mdCount.get(min) > 1) {
      /* 같은 1분 안에 블록이 여럿이라 어느 것이 지워졌는지 못 고른다. 추측하지 않는다.
       *
       * **이 분은 수정 대조에서도 빠지지만**, 여기 수정이 숨어 있으면 위 갈래 ③ 이
       * ambiguous 항목으로 이미 남겼다 — 항목이 `pendingKeys` 로 매 회차 다시 대조를
       * 부르므로, 예전처럼 기준을 얼려 둘(2026-09-15 방식) 필요가 없다. 삭제 쪽은
       * 매번 개수로 처음부터 재판정되어 이 note 만으로 잃지 않는다. */
      unresolved(
        `같은 1분(${min}) 안에 메시지가 여럿이라 어느 것이 지워졌는지 판정하지 못했습니다 — `
        + '그 시각의 md 블록을 슬랙 원문과 직접 대보고 없는 것을 지워 주세요. 고칠 때까지 매일 다시 알립니다.',
      );
      continue;
    }
    deleted.push({
      scope: 'message', key: b.key, header: b.headerLine, replyKey: null,
      before: b.compressed ? b.headerLine : b.body,
    });
  }

  /* 고쳐진 것 — 그 분에 md 도 슬랙도 딱 하나일 때만 짝지을 수 있다. */
  for (const b of inBlocks) {
    const min = minuteOf(b.key);
    if (mdCount.get(min) !== 1 || liveCount.get(min) !== 1) continue;
    const x = liveByKey.get(min);
    // 봇 글은 애초에 md 에 없다 — 짝이 그것이면 md 쪽이 다른 것이라는 뜻이라 건너뛴다
    if (!x || isBotMessage(x.m, selfId)) continue;
    const m = x.m;
    if (!m.edited) continue;
    const recent = Number(m.edited.ts) > editedSince;
    // ambiguous 열쇠도 본다 — 중복이 풀린 회차에 숨어 있던 수정이 「최근 것」이
    // 아니어도 여기서 짚인다 (갈래 ③ 의 해소 지점).
    if (!recent && !pendingKeys.has(`edited|message|${b.key}`)
      && !pendingKeys.has(`ambiguous|message|${min}`)) continue;
    const after = renderText(m.text, userMap);
    if (b.compressed) {
      /* 본문이 헤더 줄에 있어 `--replace-body` 로는 못 고친다(그 모드는 헤더 아래만 건드린다).
       * 그렇다고 알리고 마는 것으로 끝내면 그날 DM 을 놓쳤을 때 영영 묻힌다. `manual` 을 달아
       * 목록에 남겨 두고, 사람이 헤더 줄을 정상 형태로 풀어 쓰면 다음 대조에서 저절로 빠진다. */
      edited.push({
        scope: 'message', key: b.key, header: b.headerLine, replyKey: null,
        before: b.headerLine, after, manual: true,
      });
      notes.push(`한 줄로 줄여 적은 메시지가 슬랙에서 수정됐습니다 — 스크립트로 못 고칩니다 (${b.key})`);
      continue;
    }
    if (after === b.body) continue;
    edited.push({ scope: 'message', key: b.key, header: b.headerLine, replyKey: null, before: b.body, after });
  }

  /* 스레드 답글 — 부모 블록 안의 `> **└ …**` 줄 */
  /* 이번 회차에 답글 대조가 실제로 닿은 **블록** — 부모가 1:1 로 짚여 아래 루프에 들어온
   * 것만. 분 단위로 표시하면 안 된다: 같은 분에 답글을 가진 스레드가 둘일 때 깨끗한
   * 쪽만 대조돼도 「그 분은 검증됐다」가 되어, 모호한 부모 밑의 이월 항목이 무음으로
   * 증발한다 (2026-09-22 회의적 검증이 재현으로 잡음). 보전 패스는 그 분에 답글을 가진
   * **모든** 블록이 대조됐을 때만 종결로 읽는다. */
  const checkedReplyBlocks = new Set();
  for (const [ts, replies] of repliesByTs) {
    const parent = live.find((x) => x.m.ts === ts);
    // 부모가 md 에서 1:1 로 짚여야 그 블록 안의 답글을 대조할 수 있다
    if (!parent || mdCount.get(parent.key) !== 1 || liveCount.get(parent.key) !== 1) continue;
    const b = inBlocks.find((x) => minuteOf(x.key) === parent.key);
    if (!b) continue;

    // 답글도 같은 이유로 **시각만** 본다. 존재 대조에는 봇 답변까지 넣는다 —
    // 빼면 md 에 남아 있는 옛 기록이 삭제로 잡힌다.
    const liveAll = tally(replies, (r) => stamp(r.ts, tz));
    const liveHuman = new Map(
      replies
        .filter((r) => !isBotMessage(r, selfId))
        .map((r) => [stamp(r.ts, tz), { r, line: renderReply(r, userMap, tz) }]),
    );
    checkedReplyBlocks.add(b);
    const mdReplyCount = tally(b.replies, (x) => minuteOf(x.key));
    const replyEditHides = (min) => replies.some((r) => stamp(r.ts, tz) === min && recentlyEdited(r));
    const replyCountShort = (min) => (liveAll.get(min) || 0) < mdReplyCount.get(min);
    for (const min of new Set(b.replies.map((x) => minuteOf(x.key)))) {
      // 부모와 같은 갈래 ③ — 수정이 숨은 답글 분은 조건 없이 항목으로 (위 메시지 쪽 주석).
      if ((mdReplyCount.get(min) || 0) <= 1 && (liveAll.get(min) || 0) <= 1) continue;
      if (replyEditHides(min) || pendingKeys.has(`ambiguous|reply|${min}`)) {
        pushAmbiguous('reply', min, b.headerLine);
      }
    }
    for (const x of b.replies) {
      const min = minuteOf(x.key);
      const hasPending = ['edited', 'deleted'].some((kind) => pendingKeys.has(`${kind}|reply|${x.key}`));
      // 부모 쪽과 같은 좁힘 — 수정이 숨은 분은 위 갈래 ③ 항목이 맡고, 기준은 안 붙잡는다.
      if ((hasPending || (requireComplete && replyCountShort(min)))
        && (mdReplyCount.get(min) !== 1 || liveAll.get(min) > 1)) {
        unresolved(
          `답글 시각(${min})이 중복되어 수정·삭제를 짚지 못했습니다 — 그 시각의 답글 줄을 `
          + '슬랙 스레드와 직접 대보고 고쳐 주세요. 고칠 때까지 매일 다시 알립니다.',
        );
      }
    }

    for (const x of b.replies) {
      const min = minuteOf(x.key);
      if ((liveAll.get(min) || 0) >= mdReplyCount.get(min)) continue;
      if (mdReplyCount.get(min) > 1) {
        // 부모와 같은 이유로 기준은 안 붙잡는다 — 숨은 수정은 위 갈래 ③ 항목이 맡는다.
        unresolved(
          `같은 1분(${min}) 안에 답글이 여럿이라 어느 것이 지워졌는지 판정하지 못했습니다 — `
          + '그 시각의 답글 줄을 슬랙 스레드와 직접 대보고 없는 것을 지워 주세요. 고칠 때까지 매일 다시 알립니다.',
        );
        continue;
      }
      deleted.push({ scope: 'reply', key: x.key, header: b.headerLine, replyKey: x.key, before: x.line });
    }

    for (const x of b.replies) {
      const min = minuteOf(x.key);
      if (mdReplyCount.get(min) !== 1 || liveAll.get(min) !== 1) continue;
      const lr = liveHuman.get(min);
      if (!lr || !lr.r.edited) continue;
      const recent = Number(lr.r.edited.ts) > editedSince;
      // 답글 자신의 ambiguous 열쇠와 **부모 분의 message 열쇠**를 둘 다 본다 — 중복된
      // 부모 밑에 숨어 있던 답글 수정은 부모 분의 항목으로 잡혔으므로, 부모 중복이
      // 풀린 회차에 그 열쇠가 이 답글을 짚게 해야 유실되지 않는다 (갈래 ③).
      if (!recent && !pendingKeys.has(`edited|reply|${x.key}`)
        && !pendingKeys.has(`ambiguous|reply|${min}`)
        && !pendingKeys.has(`ambiguous|message|${minuteOf(b.key)}`)) continue;
      if (x.line === lr.line) continue;
      edited.push({
        scope: 'reply', key: x.key, header: b.headerLine, replyKey: x.key,
        before: x.line, after: lr.line,
      });
    }
  }

  /* 스레드가 통째로 사라진 경우 — 답글이 0 이면 읽을 일이 없어 위 루프에 안 들어온다.
   * md 에 날짜 있는 답글이 남아 있는데 슬랙 쪽 스레드가 없으면 그 답글들은 지워진 것이다. */
  for (const b of inBlocks) {
    if (!b.replies.length) continue;
    const min = minuteOf(b.key);
    if (mdCount.get(min) !== 1 || liveCount.get(min) !== 1) continue;
    const x = liveByKey.get(min);
    // 읽기에 실패한 스레드는 건너뛴다 — 못 읽은 것을 없어진 것으로 세면 안 된다
    if (!x || x.m.reply_count || repliesByTs.has(x.m.ts)) continue;
    for (const r of b.replies) {
      deleted.push({ scope: 'reply', key: r.key, header: b.headerLine, replyKey: r.key, before: r.line });
    }
  }

  /* 이월 보전 — 부모가 모호해 이번 회차에 답글 대조가 못 닿은 reply-scope 항목은
   * 지운 것이 아니라 **못 본 것**이다. 넓은 쪽으로 유지한다 (repliesByTs 에 그 스레드가
   * 없거나 부모가 1:1 로 안 짚인 회차가 여기 온다). */
  for (const key of pendingKeys) {
    const parts = key.split('|');
    if (parts[0] !== 'ambiguous' || parts[1] !== 'reply') continue;
    const min = parts.slice(2).join('|').slice(0, 16);
    if (min < from || min > to) continue;          // 창 밖은 아래 패스가 맡는다
    if (seenAmb.has(`reply|${min}`)) continue;      // 이번 회차에 다시 잡혔다
    const owners = blocks.filter((b) => b.replies.some((x) => minuteOf(x.key) === min));
    if (!owners.length) continue;                   // md 쪽이 사라졌다 — 좇을 대상이 없다
    // 그 분에 답글을 가진 블록 **전부**가 대조됐을 때만 종결이다 — 하나라도 못 닿았으면
    // (부모 모호·스레드 조회 실패·부모가 창 밖) 항목이 가리키던 자리일 수 있다.
    const unchecked = owners.find((b) => !checkedReplyBlocks.has(b));
    if (unchecked) pushAmbiguous('reply', min, unchecked.headerLine);
  }

  /* 창 밖으로 나간 ambiguous — 슬랙을 다시 못 보므로 **md 쪽 중복만** 본다.
   * 중복이 그대로면 항목을 유지하고, 사람이 풀었으면 빼되 숨은 수정의 내용은 이제
   * 자동 대조가 못 본다고 한 번 알린다. 이 note 는 판정을 끝내며 내는 것이라
   * incomplete 를 안 켠다 (unresolved 를 일부러 안 쓴다). */
  const allMdMin = tally(blocks, (b) => minuteOf(b.key));
  const allReplyMin = tally(blocks.flatMap((b) => b.replies), (x) => minuteOf(x.key));
  for (const key of pendingKeys) {
    const parts = key.split('|');
    if (parts[0] !== 'ambiguous') continue;
    const scope = parts[1];
    const min = parts.slice(2).join('|').slice(0, 16);
    if (min >= from && min <= to) continue;        // 창 안은 위에서 판정했다
    if (((scope === 'reply' ? allReplyMin : allMdMin).get(min) || 0) > 1) {
      const owner = scope === 'reply'
        ? blocks.find((b) => b.replies.some((x) => minuteOf(x.key) === min))
        : blocks.find((b) => minuteOf(b.key) === min);
      pushAmbiguous(scope, min, owner?.headerLine || min);
    } else {
      notes.push(`같은 1분(${min}) 중복은 풀렸지만 그 구간이 대조 범위(30일) 밖이라 `
        + '숨어 있던 수정은 자동으로 못 좇습니다 — 슬랙 원문과 직접 대조해 주세요');
    }
  }

  /* 한꺼번에 많이 잡히면 그건 대개 진짜 삭제가 아니다 — 마지막 그물.
   *
   * 표시 이름 변경은 위에서 키에 이름을 안 넣어 막았지만, 원인은 그것 말고도 있다:
   * 슬랙 보존 기간이 대조 구간보다 짧아지거나(옛 메시지가 통째로 사라져 보인다),
   * 아카이브 헤더 포맷이 어긋나거나(md 쪽이 통째로 안 짚인다) 하면 같은 모양이 된다.
   * 그대로 알리면 사람이 멀쩡한 원문을 지우게 되므로 목록 대신 경고를 낸다. */
  /* 잡은 것을 버리는 자리다. **수정 쪽만 기준을 붙잡는다** — 버린 수정은 다음 회차에
   * 「최근 것」이 아니게 되어 아예 안 잡히지만, 삭제는 매번 개수로 처음부터 다시
   * 판정하므로 기준과 무관하게 그대로 다시 잡힌다. */
  const overflow = (list, what, pin = false) => {
    if (list.length <= maxFindings) return list;
    unresolved(
      `${what} ${list.length}건이 한꺼번에 잡혀 목록을 내지 않았습니다 — 하나씩 지워진 것이 아니라 ` +
        '슬랙 보존 기간이 짧아졌거나 아카이브 포맷이 어긋났을 가능성이 큽니다. ' +
        '확인 전에는 반영하지 마세요.',
      { retry: pin },
    );
    return [];
  };

  const outEdited = overflow(edited, '수정', true);
  const outDeleted = overflow(deleted, '삭제');
  return {
    edited: outEdited,
    deleted: outDeleted,
    // 갈래 ③ — overflow 그물에 안 넣는다. 분 단위로 겹침 없이 세어져 폭주 모양이
    // 다르고, 많다는 것 자체가 사람이 봐야 할 정보다.
    ambiguous,
    notes: [...new Set(notes)],
    incomplete,
    retry,
  };
}

/**
 * 감지된 수정·삭제를 사람이 반영할 수 있게 파일로 남긴다.
 *
 * 매번 **현재 상태로 통째 덮어쓴다.** 감지가 md 와 슬랙을 그때그때 대조하는 방식이라
 * 아직 안 고친 것은 다음 실행에도 그대로 다시 잡히고, 고친 것은 저절로 빠진다.
 * 그러니 누적할 것이 없고, "처리했음" 표시를 관리할 일도 없다.
 *
 * `firstSeen` 만 이전 파일에서 이어받는다 — 오늘 새로 생긴 것과 예전에 알렸는데 아직 안
 * 고친 것을 DM 에서 갈라 보여주기 위해서다.
 *
 * 이 파일은 봇이 읽지 않는다 (`archive.js` 는 `index.md` 와 `channels/*.md` 만 읽는다).
 */
function buildPending(results, prev, today) {
  const before = new Map((prev?.items || []).map((it) => [it.id, it]));
  const items = [];
  for (const r of results) {
    for (const kind of ['edited', 'deleted', 'ambiguous']) {
      for (const x of r[kind] || []) {
        const id = `${r.id}|${kind}|${x.scope}|${x.key}`;
        items.push({
          id,
          kind,
          scope: x.scope,
          channel: r.channel,
          file: r.file,
          key: x.key,
          header: x.header,
          replyKey: x.replyKey,
          before: x.before,
          ...(kind === 'edited' ? { after: x.after } : {}),
          // 스크립트로 못 고치는 것 (본문이 헤더 줄에 있는 압축 1줄 메시지). 사람이 Edit 으로.
          ...(x.manual ? { manual: true } : {}),
          firstSeen: before.get(id)?.firstSeen || today,
        });
      }
    }
  }
  // A failed or incomplete scan is not evidence that an old finding resolved.
  // Fresh observations win by identity; carry the remaining records verbatim.
  const incomplete = new Set(results.filter((r) => r.error || r.editScanIncomplete).map((r) => r.id));
  /* ambiguous 만 예외 — detectChanges 가 끝까지 돈 채널(editScanComplete)은 유지·종결을
   * 그 안에서 스스로 정했다(재방출·창-밖 md 판정·이월 보전). 여기 이월이 그 종결을
   * 되살리면 「자동으로 못 좇습니다」 note 가 매 incomplete 회차마다 반복되고 항목은
   * 영영 안 닫힌다 (2026-09-22 회의적 검증이 재현으로 잡음). 스캔 자체가 못 돈
   * 채널(error·md 부재·감지 예외)은 종전대로 보존한다. */
  const scanned = new Set(results.filter((r) => r.editScanComplete).map((r) => r.id));
  const observed = new Set(items.map((it) => it.id));
  for (const [id, it] of before) {
    const cid = id.split('|')[0];
    if (!incomplete.has(cid) || observed.has(id)) continue;
    if (it.kind === 'ambiguous' && scanned.has(cid)) continue;
    items.push({ ...it });
  }
  return { items, before };
}

/**
 * 창 밖 pending 이 있어 되돌아보기 기준을 붙잡아야 하나 (slack-archive.js 의 래치 자리).
 *
 * `ambiguous` 는 **뺀다** — 창 밖 ambiguous 는 detectChanges 의 md-쪽 판정이 유지·해소를
 * 맡으므로, 여기 걸면 갈래 ③ 이 없앤 기준-동결이 뒷문으로 돌아온다 (2026-09-22).
 */
function pendingOutOfWindow(keys, from, to) {
  return [...(keys || [])].some((key) => {
    if (key.startsWith('ambiguous|')) return false;
    const minute = key.split('|').slice(2).join('|').slice(0, 16);
    return minute < from || minute > to;
  });
}

  return { detectChanges, buildPending, pendingOutOfWindow };
}
