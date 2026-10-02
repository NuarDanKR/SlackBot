/**
 * 자동 반영 결과를 본인 DM 으로. 요약과 달리 내부 운영 상태라 항상 DM 이다
 * (요약 채널 `digest.channelId` 에 올리면 팀 전원에게 보인다).
 *
 * **아무 일도 없었으면 보내지 않는다.** 매일 "이상 없음"이 오면 곧 안 읽게 되고,
 * 그때부터는 진짜 경고도 같이 안 읽힌다.
 */
import { config } from '../config.js';
import { chunkForSlack } from '../format.js';

/**
 * 긴 본문은 잘라 보여준다 — DM 은 무엇이 바뀌었는지 알아보는 자리이지 전문을 읽는 자리가 아니다.
 * 답글은 md 줄 그대로 저장돼 있으므로(`> **└ 시각 · 이름** — 본문`) 머리표를 떼고 본문만 보인다.
 * 시각·이름은 바로 윗줄에 이미 있다.
 */
function clip(s, n = 120) {
  const t = String(s || '')
    .replace(/^>\s*\*\*└[^*]*\*\*\s*—\s*/, '')
    .replace(/\s+/g, ' ')
    .trim();
  return t.length > n ? `${t.slice(0, n)}…` : t;
}

export async function ownerDm(client) {
  if (!config.owner?.slackUserId) throw new Error('config.json 의 owner.slackUserId 가 비어 있습니다.');
  const im = await client.conversations.open({ users: config.owner.slackUserId });
  return im.channel.id;
}

/** 알릴 것이 있으면 문장으로, 없으면 null */
export function compose(r) {
  const lines = [];

  const gateFailed = Boolean(r.gate && !r.gate.passed);
  const secondary = (Array.isArray(r.secondaryErrors) ? r.secondaryErrors : [])
    .map((e) => `    [${e?.stage}] ${e?.error}`);

  /* 관문 실패의 상세 — 어느 검사가 왜 깨졌는지. 아래 두 갈래가 같이 쓴다.
   *
   * 예전에는 이것이 관문 갈래에만 있었다. 그런데 관문에서 막히면 되돌리기를 하고,
   * 되돌리기까지 실패하면 `secondaryErrors` 가 찬다 — 그러면 위 갈래가 먼저 잡아
   * **어느 검사가 깨졌는지가 통째로 빠졌다.** 겹칠 때가 하필 가장 알아야 할 때다
   * (2026-09-15). */
  /* 여기서 던지면 `compose` 가 통째로 죽어 **DM 이 아예 안 나간다**(index.js 가 삼킨다).
   * 관문 결과가 깨져 있는 회차가 하필 그것을 가장 알려야 하는 회차라, **모양을 안 믿는다** —
   * 없는 것뿐 아니라 배열이 아닌 것·항목이 null 인 것까지. 값이 이상하면 그 자리에 이상한
   * 글자가 찍히는 편이 낫다. 보고가 안 나가는 것보다는 (2026-09-15). */
  const gateDetail = gateFailed
    ? (Array.isArray(r.gate.failures) ? r.gate.failures : []).flatMap((f) => {
      const detail = Array.isArray(f?.detail) ? f.detail : [];
      return [
        `• *${f?.check}*`,
        ...detail.slice(0, 6).map((d) => `    ${d}`),
        ...(detail.length > 6 ? [`    … 그 밖 ${detail.length - 6}건`] : []),
      ];
    })
    : [];

  /* ── 도중에 멈춘 경우 — 가장 위. 이걸 안 알리면 자동 반영이 조용히 죽는다 ── */
  if (r.fatal) {
    const recovery = r.rollback?.ok
      ? '이번 작업의 변경을 pull 이후 기준으로 되돌렸습니다.'
      : r.rollback?.attempted
        ? '되돌리기에 실패했습니다. 자료 상태를 직접 확인해야 합니다.'
        : r.commitOutcomeUnknown
          ? '커밋·전송 결과를 확인하지 못했습니다. 로컬과 원격을 확인하기 전 자동 복구·재전송하지 않습니다.'
          : r.push?.pushed
            ? `커밋·push는 완료됐습니다 (${r.push.sha}). 이후 처리에서 오류가 났습니다.`
            : r.push?.committed
              ? '로컬 커밋은 보존했습니다. 원격 반영 상태를 확인해야 합니다.'
              : '자료 반영·복구 상태는 아래 단계 결과와 운영 로그를 확인해야 합니다.';
    return [
      '🗂 *아카이브 자동 반영*', '', '🚨 *자동 반영 처리 중 오류가 발생했습니다.*', '',
      ...String(r.fatal).split('\n').map((l) => `    ${l}`),
      ...(gateFailed ? ['    관문 실패', ...gateDetail.map((l) => `    ${l}`)] : []),
      ...secondary,
      '', recovery,
    ].join('\n');
  }

  /* ── 관문에서 막힌 경우 ── */
  if (gateFailed) {
    lines.push(r.rollback?.ok
      ? '🚨 *자동 반영이 관문에서 막혔습니다 — 되돌렸습니다.*'
      : '🚨 *자동 반영이 관문에서 막혔습니다 — 복구 상태 확인이 필요합니다.*', '');
    lines.push(...gateDetail);
    // 줄을 띄우고 이름을 붙인다 — 붙여 놓으면 마지막 검사의 상세 한 줄로 읽힌다.
    if (secondary.length) lines.push('', '그리고 뒷처리에서도 오류가 났습니다:', ...secondary);
    lines.push('', r.rollback?.ok
      ? '아카이브는 pull 이후 기준으로 복구했습니다. 원인을 고친 뒤 다시 돌려야 합니다.'
      : '복구 완료가 확인되지 않았습니다. 자료 상태를 확인한 뒤 조치해야 합니다.');
    return ['🗂 *아카이브 자동 반영*', '', ...lines].join('\n');
  }

  /* ── 비공개 선언 누락 — 새는 쪽이라 맨 위 ── */
  const undeclared = (r.conversations?.channels || []).filter((c) => c.undeclaredPrivate);
  if (undeclared.length) {
    lines.push(
      `🔒 *비공개인데 선언되지 않은 채널 ${undeclared.length}개 — 아카이브하지 않았습니다.*`,
      `    ${undeclared.map((c) => `#${c.channel}`).join(', ')}`,
      '    넣으면 봇이 공개 채널 답변에 인용합니다. `config.json` 의 `privateChannels` 에 넣거나,',
      '    아예 다루지 않을 것이면 `digest.skipChannels` 에 넣으세요. 정할 때까지 매일 다시 알립니다.',
      '',
    );
  }

  /* ── 뒷처리만 실패한 경우 — **본문을 살린다** ──
   *
   * 임시폴더 삭제(`cleanup`)나 비용 기록(`accounting`)은 커밋·push 가 **끝난 뒤**에 돈다.
   * 예전에는 이 한 줄이 맨 위 갈래를 잡아 그날의 새 채널·개명·요약 불일치·수정 목록이
   * 전부 빠졌다 — 반영은 멀쩡히 됐는데 아침 보고에는 오류 문구만 남았다. 알릴 것은
   * 「뒷처리가 실패했다」이지 「아무것도 안 했다」가 아니다 (2026-09-15).
   *
   * **「반영은 끝났다」고 쓰지 않는다** — push 가 안 된 날도, 바뀐 것이 없어 커밋을 아예
   * 안 한 날도 여기로 온다. 무엇이 됐는지는 아래 본문이 말한다. (`--dry` 도 여기로 오는데,
   * 그쪽은 본문 자체가 예전부터 「반영」이라고 쓰므로 이 줄로 고칠 수 있는 것이 아니다.)
   *
   * 자리는 비공개 선언 누락 **아래**다. 그쪽은 자료가 새는 쪽이라 맨 위가 맞다. */
  const bodyStart = secondary.length
    ? lines.push('⚠️ *뒷처리 단계에서 오류가 났습니다 — 반영 결과는 아래 그대로입니다.*', ...secondary, '')
    : 0;

  /* ── 반영 결과 ── */
  if (r.conversations?.totalAdded) {
    const per = r.conversations.channels
      .filter((c) => c.added)
      .map((c) => `#${c.channel} ${c.added}`)
      .join(' · ');
    lines.push(`💬 대화 *${r.conversations.totalAdded}건* 반영 — ${per}`);
  }

  // 나중에 달린 답글(정정 등). 새 대화가 0건인 날에도 이것만 있을 수 있어 따로 센다.
  if (r.conversations?.totalReplies) {
    const per = r.conversations.channels
      .filter((c) => c.threadReplies)
      .map((c) => `#${c.channel} ${c.threadReplies}`)
      .join(' · ');
    lines.push(
      r.dry
        ? `↳ 옛 메시지에 달린 답글 *${r.conversations.totalReplies}건* — 덧붙일 후보입니다 (이미 md 에 있는 것은 실제 반영 때 걸러집니다) — ${per}`
        : `↳ 옛 메시지에 달린 답글 *${r.conversations.totalReplies}건* 을 부모 블록에 덧붙였습니다 — ${per}`,
    );
  }

  /* Hermes 가 올린 글(일일·주간 요약)에 달린 정정. 붙은 자리가 자리표시 블록이라
   * 따로 센다 — 봇 글에는 본문이 없고 사람이 쓴 정정만 들어간다. */
  if (r.conversations?.totalSelfCorrections) {
    const per = r.conversations.channels
      .filter((c) => c.selfCorrections)
      .map((c) => `#${c.channel} ${c.selfCorrections}`)
      .join(' · ');
    lines.push(
      r.dry
        ? `↳ Hermes 글에 달린 \`[정정]\` *${r.conversations.totalSelfCorrections}건* — 넣을 후보입니다 — ${per}`
        : `↳ Hermes 글에 달린 \`[정정]\` *${r.conversations.totalSelfCorrections}건* 을 아카이브에 넣었습니다 — ${per}`,
    );
    /* 문장을 그대로 보인다. 자리표시 블록에는 요약 **본문**이 없으므로 "10/20 입니다" 같은
     * 정정은 무엇의 값인지 알 수 없는 채로 남는다. 그게 이 문장인지 아닌지는 기계가 판정하려
     * 들면 반드시 틀리고(사업장 이름이 없어도 멀쩡한 정정이 있다), 사람은 한 줄 읽으면 안다. */
    const fixes = (r.conversations.channels || []).flatMap((c) => c.selfFixLines || []);
    fixes.slice(0, 5).forEach((x, i) => lines.push(`    ${i + 1}. ${clip(x)}`));
    if (fixes.length > 5) lines.push(`    … 그 밖 ${fixes.length - 5}건`);
    lines.push(
      '    요약 본문은 아카이브에 없습니다. 이 문장만으로 무엇의 값인지 알 수 없으면',
      '    원문이 있는 채널의 그 메시지에 다시 달아 달라고 하세요.',
    );
  }

  /* 다른 봇(`Calendar Bot`·`주간 체크인` 등) 글에 달린 사람 답글. 담기는 자리는 위와 같은
   * 자리표시 블록이지만 **정정이 아니다** — 알림에 대고 사람끼리 주고받은 대화라 `[정정]`
   * 여부와 무관하게 전부 담는다 (WHK 결정 2026-08-12).
   *
   * 위와 한 줄로 합치지 않는다. 2026-08-28 까지 세는 칸이 하나여서 Calendar Bot 글에 달린
   * 인사 답글 2건이 「Hermes 글에 달린 `[정정]` 2건」으로 나갔다. 에러로는 안 드러나고 매일
   * 이 문구로만 나타나는 종류라, 그대로 두면 진짜 정정이 왔을 때 이 줄을 안 믿게 된다.
   *
   * **문장은 안 보인다.** 위쪽은 자리표시 블록에 요약 본문이 없어 정정만으로 뜻이 통하는지
   * 사람이 봐야 하지만, 이쪽은 봐서 할 일이 없다 — 이미 다 담겼고 고칠 것이 없다. */
  if (r.conversations?.totalBotReplies) {
    const per = r.conversations.channels
      .filter((c) => c.botReplies)
      .map((c) => `#${c.channel} ${c.botReplies}`)
      .join(' · ');
    lines.push(
      r.dry
        ? `↳ 봇 글에 달린 답글 *${r.conversations.totalBotReplies}건* — 넣을 후보입니다 — ${per}`
        : `↳ 봇 글에 달린 답글 *${r.conversations.totalBotReplies}건* 을 아카이브에 넣었습니다 — ${per}`,
    );
  }

  /* ── 슬랙에서 고쳐지거나 지워진 것 ──
   * 자동으로 고치지 않는다. 원문을 무인으로 바꾸면 되돌릴 자리가 없고, 슬랙 원문을 따라가는
   * 것과 사람 판단으로 정정하는 것을 기계가 못 가른다. 알리고, 지시를 기다린다. */
  const pending = r.conversations?.pending;
  const fresh = pending?.fresh || [];
  const edits = fresh.filter((x) => x.kind === 'edited');
  const dels = fresh.filter((x) => x.kind === 'deleted');

  if (edits.length) {
    if (lines.length) lines.push('');
    lines.push(`✏️ *슬랙에서 고쳐진 것 ${edits.length}건* — md 는 옛 문장 그대로입니다.`);
    edits.slice(0, 6).forEach((x, i) => {
      lines.push(`${i + 1}. #${x.channel} ${x.key}${x.scope === 'reply' ? ' (답글)' : ''}`
        + (x.manual ? ' — 스크립트로 못 고침, 손으로' : ''));
      lines.push(`    전: ${clip(x.before) || '(없음)'}`);
      lines.push(`    후: ${clip(x.after) || '(비었음)'}`);
    });
    if (edits.length > 6) lines.push(`    … 그 밖 ${edits.length - 6}건`);
  }

  if (dels.length) {
    if (lines.length) lines.push('');
    lines.push(`🗑 *슬랙에서 지워진 것 ${dels.length}건*`);
    dels.slice(0, 6).forEach((x, i) => {
      lines.push(`${i + 1}. #${x.channel} ${x.key}${x.scope === 'reply' ? ' (답글)' : ''}`);
      lines.push(`    "${clip(x.before)}"`);
    });
    if (dels.length > 6) lines.push(`    … 그 밖 ${dels.length - 6}건`);
  }

  if (edits.length || dels.length) {
    // 어디에 대고 말하라는지를 적는다. 여기(슬랙)에서 말하면 나는 읽기만 해서 목록만 돌려준다.
    lines.push('    반영하려면 *Claude Code 에서* "슬랙 수정분 반영해줘" 라고 하시면 됩니다. 그때까지 md 는 안 바뀝니다.');
  }

  /* 같은-분 중복에 수정이 숨은 자리 (갈래 ③, 2026-09-22) — 스크립트가 못 고치는 건이라
   * 위 자동 반영 안내에 안 얹고 블록 안에 자기 지시를 둔다. 매일 같은 문장을 반복하던
   * note 대신 이 항목은 한 번 신규로 나오고, 그 뒤로는 아래 「미반영 N건 · 며칠째」에
   * 실린다 — 렌더를 빼먹으면 항목이 **조용히 안 보이므로** check-report-ambiguous 가 잰다. */
  const ambs = fresh.filter((x) => x.kind === 'ambiguous');
  if (ambs.length) {
    if (lines.length) lines.push('');
    lines.push(`⏸ *같은 1분에 글이 여럿이라 못 짚은 것 ${ambs.length}건* — 그 분의 md 를 `
      + '슬랙 원문과 직접 대보고 고쳐 주세요. 고치면 다음 회차가 저절로 닫습니다.');
    ambs.slice(0, 6).forEach((x, i) => {
      lines.push(`${i + 1}. #${x.channel} ${x.key}${x.scope === 'reply' ? ' (답글)' : ''}`);
    });
    if (ambs.length > 6) lines.push(`    … 그 밖 ${ambs.length - 6}건`);
  }
  /* 안 고친 것은 매일 한 줄로 계속 알린다. 한 번만 알리고 마는 것이 가장 조용히 묻힌다.
   * **며칠째인지를 함께 적는다** — 개수만 적으면 "2건" 이 어제도 오늘도 같은 문장이라
   * 시간이 가는 것이 안 보인다. 실제로 2026-08-10 에 8/8·8/9 자 두 건이 그렇게 남아 있었다. */
  if (pending?.carried) {
    const aged = (pending.carriedItems || [])
      .map((it) => ({ ...it, days: Math.round((Date.parse(pending.today) - Date.parse(String(it.firstSeen))) / 86400000) }))
      .filter((it) => Number.isFinite(it.days))
      .sort((a, b) => b.days - a.days);
    // `oldest` 는 0 일 수 있다 — 07:00 회차가 오늘 처음 잡은 것을 같은 날 오후에 손으로
    // 한 번 더 돌리면 그렇게 된다. 참·거짓으로 보면 그때만 머리줄이 날짜를 빼먹는데,
    // 바로 아래 상세줄은 `0일째` 를 찍어 둘이 어긋난다.
    const oldest = aged[0]?.days;
    lines.push(
      `↳ 이전에 알린 *미반영 ${pending.carried}건* 이 아직 남아 있습니다`
        + (Number.isFinite(oldest) ? ` — 가장 오래된 것은 *${oldest}일째*입니다.` : '.'),
    );
    for (const it of aged.slice(0, 5)) {
      lines.push(`    ${it.days}일째 · #${it.channel} ${it.key}${it.scope === 'reply' ? ' (답글)' : ''}`);
    }
    if (aged.length > 5) lines.push(`    … 그 밖 ${aged.length - 5}건`);
  }

  // stateOnly = 새 대화 없이 동기화 시각만 남긴 커밋. 알릴 것이 아니다 —
  // 매일 "이상 없음"이 오면 그때부터 진짜 경고도 같이 안 읽힌다.
  if (r.push?.pushed && !r.push.stateOnly) {
    lines.push(`   커밋 \`${r.push.sha}\` push 완료 — 봇은 15분 안에 받습니다.`);
  } else if (r.push?.committed && !r.push.pushed) {
    lines.push(`   ⚠️ 커밋 \`${r.push.sha}\` 은 됐지만 *push 가 안 됐습니다* — ${r.push.reason}`);
    lines.push('   push 될 때까지 봇은 옛 자료로 답합니다.');
  }

  /* ── 대화 로그 ──
   * 평상시 갱신은 알리지 않는다. 매일 "로그 남겼습니다"가 오면 곧 안 읽게 되고,
   * 그때부터는 진짜 경고도 같이 안 읽힌다. 원본을 읽지 못한 줄이 있을 때만 알린다 —
   * 그만큼 md 에서 빠졌고, 그 사실이 어디에도 안 드러나면 없었던 대화가 된다. */
  if (r.log?.noSource) {
    /* 이건 「알리지 않는다」의 예외다. 사람이 원본을 옮겨 와야만 풀리고, 그때까지 로그가
     * 하루도 안 쌓인다. 조용히 두면 렌더본이 멀쩡해 보여서 몇 주 뒤에나 드러난다. */
    lines.push(
      '',
      '🗒 *대화 로그 원본이 없습니다* — 렌더를 건너뛰었습니다 (렌더본은 그대로).',
      `    찾은 자리: \`${r.log.rawDir}\``,
      '    원본 jsonl 은 git 밖이라 클론·재배포에 안 따라옵니다. 옛 자리에서 옮겨 오세요.',
    );
  } else if (r.log?.broken) {
    lines.push(
      '',
      `🗒 *대화 로그 원본에서 읽지 못한 줄 ${r.log.broken}개* — 그만큼 md 에 빠져 있습니다.`,
      '    원본은 VM 의 **코드 저장소** 안 `logs/*.jsonl` 입니다.',
    );
  }

  /* ── 파생값 재계산 ──
   * **맞춘 것은 알리지 않는다.** 판단이 0인 값이라 매일 "N곳 맞췄습니다" 가 오면 곧 안 읽게
   * 되고, 그때부터 진짜 경고도 같이 묻힌다 (대화 로그 블록과 같은 이유).
   * 알리는 것은 **못 고친 것**뿐이다 — 조용히 넘기면 index.md 가 소리 없이 벌어지고,
   * 그 파일은 봇 시스템 프롬프트에 통째로 실린다. */
  if (r.derive?.failed) {
    lines.push(
      '',
      `⚠️ *파생값 재계산이 실패했습니다* — ${r.derive.failed}`,
      // **절반만 맞는 말을 하지 않는다.** `sync_index.py` 는 채널 헤더를 **먼저 다 쓰고**
      // 그 뒤에 `index.md` 를 손대는데, 불변식은 `index.md` 쓰기 직전에만 본다.
      // 그래서 「둘 다 낡은 채로 남았다」는 틀리다 — 헤더는 이미 고쳐졌을 수 있다.
      '    `index.md` 는 손대지 않았습니다. **채널 md 헤더는 이미 갱신됐을 수 있습니다** —',
      '    둘이 갈렸는지는 `python .claude/skills/slack-sync/scripts/sync_index.py --all --dry-run` 으로 봅니다.',
    );
  }
  if (r.derive?.unresolved?.length) {
    lines.push('', `📐 *파생값 중 기계가 못 고친 것 ${r.derive.unresolved.length}건*`);
    r.derive.unresolved.slice(0, 8).forEach((u) => {
      lines.push(`    ${u.where} [${u.kind}] ${u.detail}`);
    });
    if (r.derive.unresolved.length > 8) {
      lines.push(`    … 그 밖 ${r.derive.unresolved.length - 8}건`);
    }
  }

  /* ── 목록이 어디 있는지 ──
   * DM 은 알림이고 목록은 파일이다. 여기 적힌 문장은 120자에서 잘리고 근거도 안 붙어서
   * 이 화면에서는 판정할 수 없다 — 어디로 가야 건별로 원문과 함께 볼 수 있는지를 적는다.
   * 파일을 만드는 것은 `ingest/pending-work.js`. */
  if (r.work?.total) {
    lines.push(
      '',
      `📋 *사람이 손대야 하는 것 ${r.work.total}건*`
        + (r.work.fresh ? ` — 오늘 새로 ${r.work.fresh}건` : '')
        + (r.work.oldestDays ? ` · 가장 오래된 것 *${r.work.oldestDays}일째*` : ''),
      '    Claude Code 에서 "아침 보고 처리해줘" 하시면 건별로 원문과 함께 봅니다.',
    );
  }

  /* ── 사람이 해야 하는 것 ── */
  if (r.summary?.findings?.length) {
    lines.push('', `📌 *상단 요약과 다른 것 ${r.summary.findings.length}건* — 원문은 이미 들어갔고, 요약은 사람이 고칩니다.`);
    r.summary.findings.forEach((f, i) => {
      lines.push(`${i + 1}. #${f.channel} [${f.type}] ${f.where || ''}`);
      if (f.was || f.now) lines.push(`    ${f.was || '(없음)'} → *${f.now || '(종결)'}*`);
      if (f.evidence) lines.push(`    근거: ${f.evidence}`);
    });
  }

  if (r.summary?.dropped?.length) {
    lines.push('', `🔍 근거가 원문에서 확인되지 않아 뺀 후보 ${r.summary.dropped.length}건 (조용히 버리지 않고 남깁니다)`);
    for (const d of r.summary.dropped.slice(0, 5)) {
      lines.push(`    #${d.channel} [${d.type}] ${d.now || d.where || ''} — ${d.why}`);
    }
  }

  if (r.conversations?.newChannels?.length) {
    lines.push('', `🆕 새 채널 ${r.conversations.newChannels.length}개 — 기준점만 잡았습니다. 과거 대화는 \`slack-sync\` 로 따로 추출해야 합니다.`);
    lines.push(`    ${r.conversations.newChannels.map((c) => `#${c}`).join(', ')}`);
  }

  if (r.conversations?.renamed?.length) {
    lines.push('', '✏️ 채널 개명 — md 파일명은 그대로 두었습니다. 바꾸려면 사람이 해야 합니다.');
    for (const x of r.conversations.renamed) lines.push(`    ${x.from} → ${x.to}`);
    /* 설정은 채널을 **이름**으로 가리키는데 거르는 자리는 슬랙의 **현재** 이름과 댄다.
     * 그래서 개명 순간 그 줄이 아무것도 안 가리키게 되고 에러도 경고도 안 난다 —
     * 2026-08-10 에 `z_비공개마_옛이름` 줄이 21일간 아무것도 안 막았다. 그날 개명 보고는
     * 났지만 md 파일명 얘기뿐이라, 이 줄이 없어서 아무도 설정을 안 열어 봤다. */
    const hits = r.conversations.renamed.filter((x) => x.inConfig?.length);
    if (hits.length) {
      lines.push('    ⚠️ 옛 이름이 config.json 에 있습니다 — 고치기 전까지 그 줄은 아무것도 안 막습니다.');
      for (const x of hits) lines.push(`       ${x.inConfig.join(' · ')} 의 "${x.from}" → "${x.to}"`);
    }
  }

  /* ── 조용히 빠뜨리지 않기 위한 것들 ── */
  const notes = (r.conversations?.channels || []).flatMap((c) =>
    (c.notes || []).filter((n) => !n.includes('시스템·봇 메시지')).map((n) => `#${c.channel}: ${n}`),
  );
  if (notes.length) {
    lines.push('', '📎 확인할 것');
    for (const n of notes.slice(0, 10)) lines.push(`    ${n}`);
    if (notes.length > 10) lines.push(`    … 그 밖 ${notes.length - 10}건`);
  }

  const errors = [...(r.conversations?.errors || []), ...(r.summary?.failed || [])];
  if (errors.length) {
    lines.push('', '❌ 실패');
    for (const e of errors.slice(0, 10)) lines.push(`    ${e}`);
  }

  /* 뒷처리 오류만 있고 그날 알릴 결과가 하나도 없는 날 — 「아래 그대로」라고 해 놓고
   * 아래가 비어 있으면 뭘 보라는 건지 알 수 없다. 비었다는 것도 말해야 한다. */
  if (bodyStart && lines.length === bodyStart) lines.push('그 밖에 알릴 반영 결과는 없습니다.');

  if (!lines.length) return null;
  return ['🗂 *아카이브 자동 반영*', '', ...lines].join('\n');
}

export async function send(client, text, { dry = false } = {}) {
  if (dry) {
    console.log('\n' + '─'.repeat(52));
    console.log(text);
    console.log('─'.repeat(52));
    console.log('\n(--dry 이므로 슬랙에 보내지 않았습니다)\n');
    return { sent: false, reason: 'dry-run', reportDelivery: { status: 'dry-run', confirmedParts: 0, attemptedParts: 0 } };
  }
  let confirmedParts = 0, attemptedParts = 0;
  try {
    const target = await ownerDm(client);
    for (const part of chunkForSlack(text)) {
      attemptedParts++;
      await client.chat.postMessage({ channel: target, text: part, unfurl_links: false, unfurl_media: false });
      confirmedParts++;
    }
    return { sent: true, reportDelivery: { status: 'sent', confirmedParts, attemptedParts } };
  } catch (cause) {
    const err = cause instanceof Error ? cause : new Error(String(cause));
    // A rejected post may already be visible in Slack. Do not replay the report.
    err.hermesReport = { status: attemptedParts ? 'unknown' : 'not-sent', confirmedParts, attemptedParts };
    throw err;
  }
}
