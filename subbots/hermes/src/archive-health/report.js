/** 할 일 종류를 사람 말로. `archive-inbox` 의 review_work.py 와 같은 이름을 쓴다. */
const WORK_LABEL = {
  summary: '요약 불일치',
  dropped: '근거 못 찾아 뺀 후보',
  derive: '파생값 — 기계가 못 고침',
  'new-channel': '새 채널',
  renamed: '채널 개명',
  note: '확인할 것',
  'undeclared-private': '비공개인데 선언 안 됨',
};

/**
 * 알릴 내용을 문장으로. 알릴 것이 없으면 null.
 *
 * `preDigest` 는 일일 요약 직전 회차다. **첨부만 본다** — 동기화 밀림은 아침 회차가 알린다.
 * 같은 경고를 하루 두 번 보내면 곧 안 읽게 되고, 그때 진짜 밀린 것도 같이 묻힌다.
 */
export function compose(lag, docs, s, {
  preDigest = false, stale = [], work = [], arch = null, freshness = null,
} = {}) {
  const lines = [];
  // 낡은 상태로 쟀으면 그 사실이 맨 앞이다. **이것만으로도 DM 을 만든다** — 아래
  // 「보류 N건」이 혼자서는 DM 을 못 만드는 것과 일부러 반대다. 보류는 몰라도 그만이지만
  // 「제대로 못 쟀다」가 조용하면 조용함의 뜻이 둘이 된다.
  const stalePut = Boolean(freshness && !freshness.ok);
  if (stalePut) {
    const behindText = freshness.behind === null
      ? '얼마나 뒤처졌는지 확인 못 함입니다.'
      : `origin 에 안 받은 커밋이 ${freshness.behind}개 있습니다.`;
    // 「실패」와 「안 함」은 다르다. 손 실행과 손잡이를 끈 회차는 **일부러** 안 맞춰본 것이라
    // 실패라고 적으면 매번 고장으로 읽힌다. reason 은 실제로 실패한 git 명령이 있을 때만 있다.
    lines.push(
      (freshness.reason
        ? `⚠️ 최신화 실패 (${freshness.reason})`
        : '⚠️ 최신화 안 함 (손 실행이거나 `syncBeforeCheck` 가 꺼져 있습니다)')
        + ` — 아래는 ${freshness.headAt} 기준(HEAD ${freshness.head})이고 ${behindText}`,
      docs.total === 0
        ? '그 뒤에 push 하신 것이 있으면 이 숫자는 어긋납니다 — 문서를 물린 커밋을 아직 안 받았다면 실제로는 **더** 있을 수 있습니다.'
        : '그 뒤에 push 하신 것이 있으면 이 숫자는 어긋납니다 — 대개 실제보다 많습니다.',
    );
  }

  // 요약 전 회차는 동기화 밀림을 건너뛴다 (아침 회차 담당).
  if (!preDigest) {
    const lagLines = [];
    if (lag.missing) {
      lagLines.push('⚠️ `.sync-state.json` 을 읽지 못했습니다 — 대화 동기화 상태를 알 수 없습니다.');
    } else if (lag.days >= s.syncCriticalDays) {
      lagLines.push(
        `🚨 대화 동기화가 *${lag.days}일* 밀렸습니다 (마지막 ${lag.lastSync}).`,
        `실시간 조회는 ${s.liveFetchMaxDays}일까지만 메웁니다. 그 밖으로 넘어간 구간은 ` +
          '아카이브에도 없어서 봇이 아예 못 봅니다. `slack-sync` 를 돌려주세요.',
      );
    } else if (lag.days >= s.syncWarnDays) {
      lagLines.push(
        `대화 동기화가 ${lag.days}일 밀렸습니다 (마지막 ${lag.lastSync}). ` +
          `${s.liveFetchMaxDays}일을 넘기기 전에 \`slack-sync\` 를 돌려주세요.`,
      );
    }
    if (lagLines.length) {
      // 앞에 낡음 경고가 실렸으면 빈 줄로 가른다 — 서로 **무관한** 경고라 붙여 놓으면
      // 한 문단으로 읽혀 「낡아서 밀렸다」처럼 인과가 있는 것처럼 보인다.
      // 아래 첨부 절이 쓰는 것과 같은 방식이다. (한 줄로 모아 두는 이유: 갈래가 셋이라
      // 갈래마다 이 가름을 적으면 하나 빠뜨렸을 때 그 갈래에서만 조용히 붙는다.)
      if (lines.length) lines.push('');
      lines.push(...lagLines);
    }
  }

  if (docs.total >= s.docWarnCount || stalePut) {
    if (lines.length) lines.push('');
    lines.push(
      preDigest
        ? `📎 최근 ${docs.scanDays}일 안에 올라온 첨부 *${docs.total}건* 이 아직 안 들어갔습니다.`
        : `📎 미변환 첨부 *${docs.total}건* — 최근 ${docs.scanDays}일 범위에서 셌습니다.`,
    );
    for (const c of docs.byChannel.slice(0, 8)) {
      lines.push(`• #${c.channel} ${c.count}건 — 최근 \`${c.newest}\``);
    }
    if (docs.byChannel.length > 8) {
      lines.push(`• 그 밖 ${docs.byChannel.length - 8}개 채널`);
    }
    // 0건인데 "지금 돌려 push 하세요" 는 모순이다 — 이 줄은 낡은 상태(stalePut)라
    // 숫자만으로 들어온 것이지 실제로 할 일이 있다는 뜻이 아니다. 위 낡음 줄이
    // 이미 "실제로는 더 있을 수 있다"고 말하므로 여기서 또 행동을 재촉하지 않는다.
    if (!(preDigest && docs.total === 0)) {
      lines.push(
        preDigest
          ? '지금 `doc-archive` 를 돌려 push 하면 오늘 일일 요약이 내용까지 싣습니다. ' +
            '안 하면 요약에는 파일 이름만 나가고, 그 자료를 두고 물으면 봇이 못 읽습니다.'
          : '`doc-archive` 를 돌리면 봇이 내용을 인용할 수 있게 됩니다.',
      );
    }
  }

  // 승인 미반영은 요약 전 회차에서 빼둔다. 그 회차는 2일치만 훑어서(preDigestScanDays)
  // 옛 문서에 오늘 달린 승인을 어차피 못 본다 — 반쪽짜리 숫자를 하루 두 번 보내면 곧 안 읽는다.
  if (!preDigest && docs.approvals?.length) {
    if (lines.length) lines.push('');
    lines.push(`🔓 팀이 \`[공개]\` 로 승인했는데 봇은 아직 막고 있는 자료 *${docs.approvals.length}건*`);
    for (const a of docs.approvals.slice(0, 8)) {
      const how =
        a.state === 'pending'
          ? '아직 변환 전 — `doc-archive` 로 넣을 때 `공개승인` 을 함께 적어주세요'
          : a.state === 'missing'
            ? `상태 파일은 \`${a.doc}\` 를 가리키는데 그 md 가 없습니다 — 확인이 필요합니다`
            : `\`${a.doc}\` 메타에 \`열람: 공개\` + \`공개승인\` 두 줄을 넣고 push`;
      lines.push(`• #${a.channel} \`${a.name}\` — ${how}`);
    }
    if (docs.approvals.length > 8) lines.push(`• 그 밖 ${docs.approvals.length - 8}건`);
    lines.push(
      '`doc-archive` 를 다시 돌려도 이건 안 뜹니다 — 이미 변환된 파일이라 수집이 건너뜁니다. ' +
        '**다시 변환하지 마세요**(같은 문서가 두 벌 쌓입니다). 고칠 것은 메타 두 줄뿐입니다. ' +
        '건별로 열 때는 번호 대신 **이름**으로 고르세요 — `--only "채널/파일명"`. ' +
        '번호는 실행할 때마다 다시 매겨져 그 사이에 승인이 하나 늘면 다른 문서를 엽니다.',
    );
  }

  if (docs.failed?.length) {
    if (lines.length) lines.push('');
    lines.push(`읽지 못한 채널 ${docs.failed.length}개: ${docs.failed.join(', ')}`);
  }

  /* 봇이 못 받는 첨부. 위 「미변환」 숫자에 **안 들어간다** — `doc-archive` 를 아무리
   * 돌려도 안 줄어들어서 함께 세면 매일 같은 값이 오고, 곧 알림 전체를 안 읽게 된다.
   * 그렇다고 조용히 빼면 자료가 없는 것과 구별이 안 되므로 이름을 붙여 남긴다.
   * **끝내는 길을 함께 적는다** — 안 적으면 이 줄이 그 자체로 안 줄어드는 숫자가 된다. */
  if (!preDigest && docs.restricted?.length) {
    if (lines.length) lines.push('');
    lines.push(`🔑 봇이 받지 못하는 첨부 *${docs.restricted.length}건* — 위 미변환 숫자에는 안 들어갑니다.`);
    for (const r of docs.restricted.slice(0, 6)) lines.push(`• #${r.channel} \`${r.name}\``);
    if (docs.restricted.length > 6) lines.push(`• 그 밖 ${docs.restricted.length - 6}건`);
    /* **`--skip` 앞에 매니페스트가 한 단계 필요하다.** 이 파일들은 받아진 적이 없어
     * `.doc-state.json` 에 없고, `decide.py` 의 `resolve` 는 상태 파일과 매니페스트에서만
     * 찾는다 — 매니페스트 없이 치면 「못 찾았습니다」가 나온다(그쪽 `MANIFEST_HINT` 가
     * 그때 같은 안내를 낸다). 수집이 이 파일들을 `skipped` 로 적고 `load_manifest` 가
     * `skipped` 까지 후보로 보므로, 매니페스트만 만들면 지목이 된다 (2026-08-13 리뷰). */
    lines.push(
      '슬랙이 다운로드 주소를 안 줘서(권한 제한) 수집이 건너뛴 파일입니다. ' +
        '필요하면 사람이 열어 그 채널에 다시 올려주세요. 넣지 않기로 정한 것이면 ' +
        '`fetch_slack_files.py --dry-run --manifest /tmp/doc-manifest.json` 을 먼저 돌린 뒤 ' +
        '`decide.py --skip <파일명> --manifest /tmp/doc-manifest.json --reason "…"` 으로 ' +
        '적어야 이 줄이 사라집니다.',
    );
  }

  // ── 반영 안 하고 묵은 슬랙 수정·삭제 ──
  // 07:00 자동 반영 보고에도 「미반영 N건」이 뜨지만 그건 매일 오는 보고라 눈에 안 든다.
  // 09:00 은 이상할 때만 오므로 무게가 다르다. 며칠째인지를 앞에 세워 적는다 —
  // 2026-08-10 에 8/8·8/9 자 두 건이 「미반영 2건」이라는 한 줄로만 알려진 채 남아 있었다.
  if (!preDigest && stale.length) {
    if (lines.length) lines.push('');
    // 「중복」(ambiguous, 2026-09-22 갈래 ③) 건도 이 수에 든다 — 낱말을 좁게 두면
    // 세는 것과 부르는 것이 어긋난다.
    lines.push(`✏️ *반영 안 된 슬랙 수정·삭제·중복 ${stale.length}건 — 가장 오래된 것 ${stale[0].days}일째*`);
    for (const x of stale.slice(0, 6)) {
      lines.push(`• ${x.days}일째 · #${x.channel} ${x.key}${x.scope === 'reply' ? ' (답글)' : ''}`);
    }
    if (stale.length > 6) lines.push(`• 그 밖 ${stale.length - 6}건`);
    lines.push(
      // 「다른 상태」라고 단정하면 안 된다 — 중복(ambiguous) 건은 다른지 **확인을 못 한** 건이다.
      '지금 md 는 슬랙과 다르거나(수정·삭제), 같은-분 중복이라 다른지 확인 못 한(중복) 상태입니다. *Claude Code 에서* "슬랙 수정분 반영해줘" 라고 하세요.',
      // **`--only` 를 빼면 안 된다.** 그것이 없으면 apply_edits.py 가 목록만 찍고 종료코드
      // 0 으로 끝나서(그 스크립트의 `if not args.only:`), 미룬 줄 알았는데 아무것도 안 된다.
      '이번엔 아니면 `apply_edits.py --only <번호> --later 14 --reason "…"` 로 미뤄 두면 만기까지 조용합니다.',
    );
  }

  /* ── 07:00 목록에서 며칠째 안 정한 것 ──
   * 파일에는 남아 있고 07:00 이 매일 개수를 알리지만, 개수만으로는 시간이 가는 것이
   * 안 보인다. 오래된 것만 여기에 한 번 더 올린다 (위 수정·삭제와 같은 이유). */
  if (!preDigest && work.length) {
    if (lines.length) lines.push('');
    lines.push(`📋 *며칠째 안 정한 할 일 ${work.length}건 — 가장 오래된 것 ${work[0].days}일째*`);
    for (const x of work.slice(0, 6)) {
      const label = WORK_LABEL[x.kind] || x.kind;
      const where = x.channel ? `#${x.channel}` : (x.where || '');
      lines.push(`• ${x.days}일째 · [${label}] ${where} ${x.where && x.channel ? x.where : ''}`.trimEnd());
    }
    if (work.length > 6) lines.push(`• 그 밖 ${work.length - 6}건`);
    lines.push(
      '*Claude Code 에서* "아침 보고 처리해줘" 라고 하시면 건별로 원문과 함께 봅니다.',
      // **`--only` 를 빼면 안 된다** — decide_work.py 가 거절한다 (apply_edits.py 와 같은 규약).
      '이번엔 아니면 `decide_work.py --only <번호> --later 14 --reason "…"` 로 미뤄 두면 만기까지 조용합니다.',
    );
  }

  // ── 아카이브 md 구조 결함 ──
  // 계산으로 못 고치는 것들이라 사람이 봐야 한다. 파일별로 묶어 보여준다.
  if (!preDigest && arch?.failed) {
    if (lines.length) lines.push('');
    lines.push(`⚠️ *아카이브 검사를 돌리지 못했습니다* — ${arch.failed}`);
  } else if (!preDigest && arch?.total) {
    if (lines.length) lines.push('');
    const withProblems = arch.files.filter((f) => f.problems?.length);
    lines.push(`🩹 *아카이브 구조 결함 ${arch.total}건* (${withProblems.length}개 파일)`);
    for (const f of withProblems.slice(0, 6)) {
      lines.push(`• \`${f.file}\``);
      for (const p of f.problems.slice(0, 2)) lines.push(`    ${p}`);
      if (f.problems.length > 2) lines.push(`    … 그 밖 ${f.problems.length - 2}건`);
    }
    if (withProblems.length > 6) lines.push(`• 그 밖 ${withProblems.length - 6}개 파일`);
    lines.push('전부 보려면 VM 에서 `python .claude/skills/slack-sync/scripts/verify_archive.py --all`');
  }

  // ── 보류는 **어긋남이 아니라 정한 상태**다 ─────────────────────────────
  // 그래서 이것만으로는 DM 을 만들지 않는다 (아래 `!lines.length` 앞에서 붙이는 이유).
  // 매일 "보류 1건" 이 오면 곧 안 읽게 되고, 그때 진짜 밀린 것도 같이 묻힌다.
  // 만기가 지나면 미변환으로 다시 세어지므로 그때 저절로 소리가 난다.
  if (!preDigest && lines.length && docs.deferred?.length) {
    const until = String(docs.deferred[0].until || '').slice(5).replace('-', '/');
    lines.push('', `⏸ 보류 ${docs.deferred.length}건 (가장 이른 만기 ${until}) — 만기가 지나면 위 숫자로 돌아옵니다.`);
  }

  if (!lines.length) return null;
  // 여기부터는 이미 보낼 것이 있다. 그래서 이 줄은 혼자 DM 을 만들지 않는다.
  if (freshness?.ok) lines.push('', `기준: HEAD ${freshness.head} · ${freshness.checkedAt} 최신화`);
  return [preDigest ? '🗂 *요약 전 마감 점검*' : '🗂 *아카이브 위생 점검*', '', ...lines].join('\n');
}
