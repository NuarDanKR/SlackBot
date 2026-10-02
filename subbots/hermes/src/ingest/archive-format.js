/** Extracted ingest boundary; dependencies are supplied by slack-archive.js. */
export function createArchiveFormat({ renderText, stamp, isBotMessage, BOT_ANSWER_MARK, BOT_AUTHOR_LABEL, BOT_BLOCK_BODY, permalink }) {
/* ── 아카이브 md 모양으로 쓰기 ──────────────────────────────────── */

function authorOf(m, userMap) {
  const name = m.user ? userMap.get(m.user) || m.user : m.username || m.bot_profile?.name || '봇';
  return m.bot_id && !m.user ? `${name} (봇)` : name;
}

function filesOf(m) {
  return (m.files || []).map((f) => f.name).filter(Boolean);
}

/**
 * 스레드 답글 한 줄. 새로 넣을 때와 나중에 덧붙일 때가 **같은 모양**이어야 한다 —
 * 두 벌이 되면 나중에 붙은 정정만 모양이 달라져 사람 눈에도 봇 눈에도 이물이 된다.
 *
 * **반응(이모지)은 싣지 않는다** (WHK 결정 2026-09-03). 본문 블록도 마찬가지다.
 * 값어치가 없고, 나중에 붙는 반응이 아카이브와 슬랙을 영영 어긋나게 한다 — 반응이 붙어도
 * 슬랙은 `edited` 를 안 달아 수정 감지에 안 걸린다. 되살아나면 `check-thread-loss.js` ① 이 막는다.
 */
function renderReply(r, userMap, tz) {
  const rt = renderText(r.text, userMap);
  const rf = filesOf(r);
  const tail = rf.length ? ` 📎 첨부: ${rf.map((f) => `\`${f}\``).join(', ')}` : '';
  // 답글 본문에 줄바꿈이 있으면 둘째 줄부터도 인용 안에 둔다 — `>` 가 없으면 md 에서
  // 인용 블록이 거기서 끊겨 그 줄이 사람 발언 본문처럼 떠 버린다.
  const body = `${rt}${tail}`.split('\n').join('\n> ');
  return `> **└ ${stamp(r.ts, tz)} · ${authorOf(r, userMap)}** — ${body}`;
}

/**
 * 답글을 **읽지 못했다**는 표식. 봇 답변 자리(`BOT_ANSWER_MARK`)와 같은 모양이다.
 *
 * `slack-live.js` 가 아니라 여기 두는 이유: 이 문구를 쓰는 곳도 읽는 곳도 아카이브
 * 렌더뿐이다(`BOT_ANSWER_MARK` 는 스레드 맥락·프롬프트 설명도 함께 본다).
 */
const UNREAD_REPLIES_MARK = '(읽지 못함 — 미수록)';

/**
 * 메시지 한 건 + 스레드를 아카이브 블록으로.
 *
 * 봇의 답변은 넣지 않는다 — Hermes 답변은 아카이브를 읽어 만든 2차 가공물이라 넣으면 다음번에
 * 봇이 자기 요약을 원본 근거로 삼고 한 번 잘못 요약한 숫자가 원문처럼 굳는다. 다른 봇의 글도
 * 같이 뺀다 (WHK 결정 2026-08-12).
 * 사람이 호출한 메시지는 남기고, 답변 자리에는 건수만 적는다.
 *
 * `opts.unreadReplies` 는 **답글을 못 읽었을 때 남기는 흔적**이다. `replies` 가 비면 이 함수는
 * 스레드 줄을 아예 안 쓰므로, 읽기에 실패한 스레드의 부모가 「원래 답글이 없던 글」과
 * 구별되지 않는다 — `verify_archive.py` 의 검사 ① 도 볼 머리줄이 없어 조용하다. 지금
 * 넘기는 곳은 **백필뿐이다**(`backfill.js`). 자동 반영은 못 읽은 스레드를 상태에 적어
 * 다음 회차에 다시 읽으므로 흔적 대신 재시도로 막는다.
 *
 * 문구는 봇 답변 자리(`BOT_ANSWER_MARK`)와 같은 모양이다. **머리줄(`> 💬 **스레드 (N)**`)
 * 모양을 쓰지 않는 것이 요점이다** — 그 모양이면 `verify_archive.py` ① 이 「머리줄 N건인데
 * 답글 0개」로 커밋을 막고(몇 시간짜리 백필이 통째로 커밋 불가가 되는데, 백필은 경계 아래만
 * 읽으므로 다시 돌려도 그 스레드를 안 읽는다), `insert_messages.py` 가 나중에 답글을
 * 덧붙일 때 그 숫자를 함께 올린다. 같은 이유로 `FAIL_MARKS` 의 낱말도 안 쓴다.
 */
function renderMessage(m, replies, userMap, tz, selfId, { unreadReplies = 0 } = {}) {
  const lines = [`**${stamp(m.ts, tz)} · ${authorOf(m, userMap)}**`];

  const text = renderText(m.text, userMap);
  if (text) lines.push(text);

  const files = filesOf(m);
  if (files.length) lines.push(`📎 첨부: ${files.map((f) => `\`${f}\``).join(', ')}`);

  const human = replies.filter((r) => !isBotMessage(r, selfId));
  const bot = replies.length - human.length;

  if (human.length) {
    lines.push('');
    lines.push(`> 💬 **스레드 (${human.length})**`);
    for (const r of human) lines.push(renderReply(r, userMap, tz));
    if (bot) lines.push(`> 💬 스레드 ${bot}건 ${BOT_ANSWER_MARK}`);
  } else if (bot) {
    lines.push('');
    lines.push(`> 💬 스레드 ${bot}건 ${BOT_ANSWER_MARK}`);
  }

  if (unreadReplies) {
    if (!human.length && !bot) lines.push('');
    lines.push(`> 💬 스레드 ${unreadReplies}건 ${UNREAD_REPLIES_MARK}`);
  }

  return lines.join('\n');
}

/**
 * 봇이 올린 글의 **자리표시 블록** — 헤더와 표식뿐, 본문은 없다.
 *
 * 봇 글은 아카이브에 안 넣지만(renderMessage 주석), 거기 달린 사람 답글은 넣어야 한다.
 * 자리만 만들고 내용은 비워 두는 것이 그 둘을 함께 지키는 방법이다. 표식 문구와 작성자 이름은
 * `slack-live.js` 의 상수를 쓴다 — `ingest/verify.js` 의 관문이 같은 문자열로 이 블록을
 * 알아보고, 어긋나면 "봇 글이 아카이브에 들어갔다"로 읽어 매일 되돌린다.
 *
 * 헤더에 **어느 봇인지는 적지 않는다.** 슬랙 표시 이름은 바뀌는데 나중에 답글을 덧붙일 때
 * 이 헤더 줄로 부모를 찾으므로, 이름을 넣으면 이름이 바뀐 날 같은 글에 블록이 하나 더 생긴다.
 * 어느 봇이었는지는 아래 `[원문]` 링크로 연다.
 *
 * 표식 옆에 **그 봇 글의 슬랙 링크**를 단다. 정정 문장만 남으면 무엇을 고치는 것인지 알 수
 * 없는 경우가 있어("10/20 입니다"), 나중에 이 블록을 보는 사람이 원문을 열어 볼 수 있어야
 * 한다. 봇의 문장이 아니라 **주소**라 원칙에 걸리지 않는다.
 *
 * @param {string} channelId 봇 글이 올라간 채널
 * @param {string} ts 봇 글의 Slack ts
 * @param {string[]} replyLines `renderReply` 로 만든 답글 줄들
 */
function renderBotPlaceholder(channelId, ts, replyLines, tz) {
  const link = permalink(channelId, ts);
  return [
    botHeader(ts, tz),
    BOT_BLOCK_BODY + (link ? ` · [원문](${link})` : ''),
    '',
    `> 💬 **스레드 (${replyLines.length})**`,
    ...replyLines,
  ].join('\n');
}

/** 자리표시 블록의 헤더 줄. 나중에 답글을 덧붙일 때도 이 모양으로 부모를 찾는다. */
function botHeader(ts, tz) {
  return `**${stamp(ts, tz)} · ${BOT_AUTHOR_LABEL}**`;
}

/** Slack ts → 'YYYY-MM' (config.timezone 기준) */
function monthOf(ts, tz) {
  return stamp(ts, tz).slice(0, 7);
}

/* ── 수정·삭제 감지 ─────────────────────────────────────────────── */

/**
 * 슬랙에서 메시지를 고치거나 지워도 md 는 그대로다. `conversations.history` 를
 * `oldest: last_ts` 로 부르는데 본문을 고쳐도 `ts` 는 그대로라, 이미 반영된 메시지는
 * 두 번 다시 조회 범위에 들어오지 않는다. 삭제는 "사라졌다"는 신호를 볼 자리가 없다.
 *
 * 그래서 **편집 시각을 어디 적어 두고 비교하는 대신, md 에 있는 것과 슬랙에 있는 것을
 * 그때그때 대조한다.** 이렇게 하면 반영하기 전까지 매일 다시 잡히고(알림을 놓쳐도 안
 * 사라진다), 반영하면 저절로 조용해진다. `.sync-state.json` 에 "처리했음" 표시를 만들지
 * 않는 이유이기도 하다 — `summary_reviewed_ts` 가 대조한 적 없는 것을 대조 완료로 적어 둬
 * 아무도 다시 묻지 않게 된 사고가 이미 한 번 났다.
 *
 * **고치지는 않는다.** 찾아서 알리기만 하고, md 반영은 사람이 지시할 때 `slack-sync` 가 한다.
 */

/** md 메시지 헤더. 뒤에 내용이 붙은 압축 1줄 메시지도 잡도록 줄끝 앵커를 두지 않는다. */
const MD_HEADER = /^\*\*(\d{4}-\d{2}-\d{2} \d{2}:\d{2} · [^*]+)\*\*(.*)$/;
/** 스레드 답글 줄. 날짜 없는 초기 수기 추출분(`> **└ WHK** —`)은 안 잡힌다 — 대조에서 뺀다. */
const MD_REPLY = /^>\s*\*\*└\s*(\d{4}-\d{2}-\d{2} \d{2}:\d{2} · [^*]+)\*\*/;
/** 답글이 **시작되는** 줄. 경계를 볼 때만 쓴다 — 날짜 없는 옛 답글도 경계이긴 하다. */
const MD_REPLY_ANY = /^>\s*\*\*└/;
/** 정규식 안에 문자열을 글자 그대로 넣기 위한 이스케이프 */
const reEscape = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

/**
 * 본문이 끝나는 자리. `renderMessage` 가 쌓는 순서(본문 → 첨부 → 스레드)와 짝이다.
 * `>` 로 시작하는 줄을 전부 경계로 보면 안 된다 — 슬랙 본문에도 인용문이 있다.
 * 백틱 한 줄을 받는 갈래는 **본문이 백틱으로 끝나는 경우**를 위한 것이다 — 반응 줄을
 * 받던 자리이기도 했지만 반응은 2026-09-03 에 아카이브에서 걷어냈다.
 *
 * **표식 문구는 `BOT_ANSWER_MARK` 에서 만든다 — 손으로 적지 않는다.**
 * 2026-09-03 까지 여기는 `\(Hermes` 라고 적혀 있었다. 정본은 `slack-live.js` 의
 * `BOT_ANSWER_MARK`(`(봇 답변 — 미수록)`)라서 **이 갈래는 한 번도 안 맞았고**, 표식 줄이
 * 본문에 빨려 들어간 블록이 실물에 7건 있었다(2026-09-03 실측 — 아래 짝인
 * `insert_messages.py` 의 `BODY_TAIL_RE` 는 맞게 적혀 있어서 두 쪽이 서로 다른 자리에서
 * 본문을 끊고 있었다). 그 블록 하나가 슬랙에서 수정되면 `apply_edits.py --apply` 가
 * 표식 줄을 본문의 일부로 보고 함께 지운다.
 *
 * 파이썬 쪽과는 값을 공유할 수 없어(언어가 다르다) **어긋나면 빨개지는 교차검사**를 뒀다 —
 * `scripts/check-archive-contract.js` [6/7] 이 같은 줄들을 양쪽에 먹여 답을 대본다.
 */
const BODY_TAIL = new RegExp(
  '^(?:`[^`]*`\\s*$|📎 첨부: |> 💬 |> \\*\\*└|💬 스레드 \\d+건 '
  + `${reEscape(BOT_ANSWER_MARK)})`,
);

/** 채널 md 를 메시지 블록으로 자른다 (검증용으로 밖에서도 부른다) */
function parseBlocks(md) {
  // 채널 md 는 CRLF 와 LF 가 섞여 있다(35개 중 30개가 CRLF). `\n` 으로만 자르면 줄 끝에 `\r`
  // 이 남아 줄끝 앵커가 있는 정규식이 **에러 없이 하나도 안 맞는다.**
  const lines = md.split(/\r?\n/);
  const heads = [];
  lines.forEach((l, i) => { if (MD_HEADER.test(l)) heads.push(i); });

  return heads.map((start, n) => {
    let end = n + 1 < heads.length ? heads[n + 1] : lines.length;
    for (let i = start + 1; i < end; i += 1) {
      if (lines[i].startsWith('## ')) { end = i; break; }
    }
    let bodyEnd = start + 1;
    while (bodyEnd < end && !BODY_TAIL.test(lines[bodyEnd])) bodyEnd += 1;

    /* 답글은 **한 줄이 아니다.** 본문에 줄바꿈이 있으면 둘째 줄부터도 `> ` 안에 들어간다
     * (renderReply). 첫 줄만 들면 두 가지가 어긋난다: 슬랙 원문과 견줄 때 늘 다르다고
     * 나오고(`x.line === lr.line`), `before` 로 넘긴 것이 반쪽이라 반영이 나머지 줄을
     * 남겨 둔다. 아카이브에 여러 줄 답글이 36건 있다 (2026-08-10 실측). */
    const replies = [];
    for (let i = start + 1; i < end; i += 1) {
      const r = MD_REPLY.exec(lines[i]);
      if (!r) continue;
      let j = i + 1;
      while (j < end && lines[j].startsWith('>')
             && !MD_REPLY_ANY.test(lines[j]) && !/^>\s*💬/.test(lines[j])) j += 1;
      replies.push({ key: r[1].trim(), line: lines.slice(i, j).join('\n') });
    }

    const m = MD_HEADER.exec(lines[start]);
    return {
      key: m[1].trim(),
      headerLine: lines[start],
      // 한 줄로 줄여 적은 메시지(`**…** — 7/29 일일업무일지 · 💬 스레드 1건`). 본문이 헤더
      // 줄에 있어 대조도 교체도 못 한다. 아카이브에 실제로 38건 있다.
      compressed: !!m[2].trim(),
      body: lines.slice(start + 1, bodyEnd).join('\n').trim(),
      replies,
    };
  });
}

/**
 * `insert_messages.py --report` 가 낸 마지막 줄에서 **실제로 써 넣은 것**을 읽는다.
 *
 * 파이썬의 평상시 문구(`OK 스레드 덧붙임 (…)`)는 **어디에** 넣었는지만 말한다. 넣으려던
 * 다섯 중 하나만 새것이어도 그 문구가 같아서, 「이미 반영됨 이 아니면 전부 들어갔다」로
 * 읽으면 부분 성공이 전량으로 세어진다. 봇 글에 달린 답글 갈래는 개수만이 아니라
 * **어느 문장이 쓰였는지 목록까지** DM 에 싣기 때문에, 개수를 md 로 다시 세는 것만으로는
 * 못 가른다 — 그래서 파이썬이 신원을 돌려준다 (WHK 결정 2026-09-03, ㉰안).
 *
 * **못 읽으면 던진다.** 조용히 빈 배열을 돌려주면 「하나도 안 썼다」가 되어 정정이 md 에는
 * 들어갔는데 DM 에는 안 뜨는, 지금 막으려는 것과 방향만 반대인 같은 사고가 난다.
 *
 * @param {string} stdout 파이썬 표준출력 전체
 * @param {string[]} lines 넣으려고 넘긴 줄들 (`renderReply` 한 건이 한 칸)
 * @returns {string[]} `lines` 중 이번에 실제로 들어간 것 (원래 순서)
 */
function writtenFrom(stdout, lines) {
  const at = String(stdout).split(/\r?\n/).filter((l) => l.startsWith('REPORT '));
  if (!at.length) {
    throw new Error('insert_messages.py 가 REPORT 줄을 내지 않았습니다 (--report 를 줬는지 확인하세요)');
  }
  const report = JSON.parse(at[at.length - 1].slice('REPORT '.length));
  if (!Array.isArray(report.written)) {
    throw new Error(`insert_messages.py 의 REPORT 에 written 목록이 없습니다: ${at[at.length - 1].slice(0, 120)}`);
  }
  /* 덧붙이기(`append-thread`)면 `written` 이 곧 답글 덩이라 그대로 짝이 맞고, 자리표시
   * 블록을 새로 만드는 삽입(`insert`)이면 `written` 이 **블록 통째**라 그 안에 답글이
   * 들어 있다. 두 모양을 한 규칙으로 다루려고 「쓴 것 안에 있나」로 본다 — 답글 줄에는
   * 시각이 박혀 있어 같은 줄이 둘일 수 없다. */
  const blob = report.written.join('\n');
  return lines.filter((l) => blob.includes(l));
}

  return { authorOf, renderReply, renderMessage, renderBotPlaceholder, botHeader, monthOf, UNREAD_REPLIES_MARK, BODY_TAIL, MD_REPLY_ANY, parseBlocks, writtenFrom };
}
