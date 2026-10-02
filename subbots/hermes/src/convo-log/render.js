/**
 * Log markdown rendering with injected storage and policy boundaries.
 * Factory creation performs no I/O; the facade owns storage and source guards.
 */
export function createLogRenderer({
  localParts, BROADCAST, KIND_LABEL, num, toolLine,
  readRaw, path, LOG_DIR, writeIfChanged, months,
  cacheStats, failureSectionLines, NO_BASELINE,
  isOperational, isQaRound, isBroadcastRound,
}) {
  /** 한 건 → md 조각.
   *
   * **검사가 부르므로 내보낸다**(`scripts/check-failure-log.js`). 판정을 두 벌 만들지
   * 않으려는 것 — 검사가 자기 렌더를 흉내 내면 진짜 렌더가 갈려도 조용히 통과한다. */
  function renderEntry(e) {
    if (e.accountingOnly) {
      const a = e.accounting;
      return [
        '#### 요약 대조 비용 계측 (운영 회차·실패 건수에서 제외)',
        '',
        /* **돈 적는 모양은 비용 줄과 똑같이 쓴다** (`확인분 USD X / 미상 N회` — 빗금).
         * 전에는 여기만 가운뎃점이라, 같은 사실이 두 가지로 적혀 있었고 `parseCostLine` 이
         * 이 줄을 아예 못 읽었다. 그래서 분해 도구도 재보기도 이 돈을 못 봤는데, 월 머리말
         * 합계(`usd`)는 포함해서 **같은 저장소의 두 숫자가 아무 표시 없이 어긋났다.**
         * `accountingOnly` 는 `8c3957f` 에서 생겨 아직 배포 전이라 이 줄이 실물에 0건일 때
         * 모양을 맞췄다 — 배포 뒤였다면 옛 줄이 영영 안 읽히는 채로 남았다 (2026-09-16).
         *
         * `?` 는 그대로 둔다. 값이 없다는 뜻이고, 그러면 파서가 「못 읽음」으로 올려
         * **시끄럽게** 멈춘다 — 0 으로 적어 「미상 없음」이라고 거짓말하는 것보다 낫다. */
        `> **계측 금액** ${e.at} · 확인분 USD ${Number(a?.knownCostUsd || 0).toFixed(6)} / 미상 ${a?.unknownAttempts ?? '?'}회`,
        '',
      ].join('\n');
    }
    const { time } = localParts(e.at);
    const isBroadcast = BROADCAST.has(e.kind);
    const who = e.asker || (isBroadcast ? 'Hermes' : '(알 수 없음)');
    const where = (isBroadcast ? e.target : e.origin) || e.origin || '';
    const label = KIND_LABEL[e.kind] || e.kind;

    const out = [`### ${time} · ${who} · ${where} · ${label}${e.ok === false ? ' · ⚠️ 실패' : ''}`, ''];

    if (isBroadcast && e.origin) out.push(`_기간: ${e.origin}_`, '');
    if (e.question) out.push('**질문**', '', e.question, '');
    if (e.answer) out.push(isBroadcast ? '**보낸 내용**' : '**답변**', '', e.answer, '');
    if (e.error) {
      const errText = String(e.error);
      // 인라인 백틱 하나는 줄바꿈을 못 담는다 — 여러 줄 사유(예: 자동 반영 중단의
      // err.message, ingest/index.js)가 오면 백틱 밖으로 새어 md 가 깨진다. 한 줄이면
      // 인라인이 더 가볍다(펜스 두 줄을 안 늘린다) — 대부분이 한 줄이라 매 건 펜스를
      // 두르면 로그가 불필요하게 불어난다. 되돌리면 scripts/check-error-block.js 가 잡는다.
      out.push('**오류**', '', ...(errText.includes('\n') ? ['```', errText, '```'] : [`\`${errText}\``]), '');
    }
    if (e.stats) out.push(`_${e.stats}_`, '');

    /* 메타는 인용 블록으로 둔다. 답변이 목록으로 끝나는 일이 흔한데, 그냥 목록으로 붙이면
     * 답변의 마지막 항목들과 한 덩어리로 읽힌다 — 어디까지가 봇의 말인지 흐려진다. */
    const meta = [];
    /* 실패한 회차가 어디까지 갔나. **값이 없으면 줄을 안 쓴다** — 빈 줄은 「아무 데도 못
     * 갔다」와 「못 쟀다」를 같은 글자로 만든다. 이 한 줄이 「재료는 멀쩡한데 모델 호출만
     * 죽었다」와 「슬랙을 못 읽어 재료가 비었다」를 가른다 (2026-08-28). */
    if (e.context) meta.push(`**어디까지** ${e.context}`);
    // 아카이브 채널에는 # 를 붙인다. 문서는 이미 📄 가 붙어 온다 (claude.js 의 touched).
    const evidence = (e.channels || []).map((c) => (c.startsWith('📄') ? c : `#${c.replace(/^#/, '')}`));
    if (evidence.length) meta.push(`**근거** ${evidence.join(' · ')}`);
    else if (e.kind === 'qa' && e.ok !== false) meta.push('**근거** (아카이브를 열지 않고 답함)');
    if (e.toolCalls?.length) meta.push(`**도구** ${e.toolCalls.map(toolLine).join(' → ')}`);

    const cost = [];
    if (e.elapsedMs != null) cost.push(`${(e.elapsedMs / 1000).toFixed(1)}초`);
    if (e.usage) {
      cost.push(
        `in ${num(e.usage.input_tokens)} / cache-w ${num(e.usage.cache_creation_input_tokens)} / ` +
          `cache-r ${num(e.usage.cache_read_input_tokens)} / out ${num(e.usage.output_tokens)}`,
      );
    }
    if (e.accounting?.complete === false) {
      cost.push(`확인분 USD ${Number(e.accounting.knownCostUsd).toFixed(6)} / 미상 ${e.accounting.unknownAttempts}회 (총액 미상)`);
    }
    if (e.accounting) meta.push('**계측** 앱 관측분 추정 (SDK 내부 재시도 미관측)');
    if (e.costUsd != null) cost.push(`약 $${Number(e.costUsd).toFixed(3)}`);
    if (e.model) meta.push(`**${e.accounting?.complete === false ? '소요' : '비용'}** ${[e.model, ...cost].join(' · ')}`);
    else if (cost.length) meta.push(`**소요** ${cost.join(' · ')}`);

    /* 캐시가 안 맞은 사유. **맞은 회차에는 줄을 안 쓴다** — 대부분이 맞아야 정상이고,
     * 0을 매 건 적으면 눈이 미끄러져 정작 미스가 난 건이 안 보인다.
     * 사유는 API 가 준 값 그대로 둔다(system_changed · previous_message_not_found 등) —
     * 한글로 옮기면 API 문서와 대조가 안 되고, 새 사유가 생겼을 때 조용히 '기타'로 묻힌다. */
    const cacheNote = [];
    /* 대조 기준이 없던 회차. **여기도 「없으면 줄을 안 쓴다」이지만 뜻이 반대다** — 위의 미스는
     * 없는 것이 정상이고, 이 줄은 있으면 그 회차의 첫 진단이 통째로 비었다는 표시다. 값이
     * `false` 일 때만 적는다(옛 기록은 필드 자체가 없어 `undefined` 다 — 모르는 것을 아는 척
     * 하지 않고, 옛 md 도 예전 그대로 다시 렌더된다). */
    if (e.cacheBaseline === false) cacheNote.push('대조 기준 없음 (첫 회차는 진단 자체가 안 옴)');
    if (e.cacheMiss?.length) {
      cacheNote.push(`미스 ${e.cacheMiss.map((m) => `${m.type} ${m.count}회 (${num(m.tokens)}토큰)`).join(' · ')}`);
    }
    if (cacheNote.length) meta.push(`**캐시** ${cacheNote.join(' · ')}`);

    /* 재시도 끝에 나간 회차. **한 번에 성공했으면 줄을 안 쓴다** — 대부분이 그래야 정상이고,
     * 매 건 `0회` 를 적으면 눈이 미끄러져 정작 재시도가 난 건이 안 보인다(캐시 미스와 같은 이유).
     * 사유는 API 가 준 값 그대로다 — 한글로 옮기면 새 사유가 조용히 묻힌다. */
    if (e.attempts?.length > 1) {
      const failedTries = e.attempts.filter((a) => a.errorType);
      const waitedMs = e.attempts.reduce((a, t) => a + (t.waitMs || 0), 0);
      const types = [...new Set(failedTries.map((a) => a.errorType))];
      meta.push(
        `**재시도** ${types.join(' · ')} ${failedTries.length}회 → ` +
          `${e.attempts.length}번째 ${e.ok === false ? '실패' : '성공'}` +
          (waitedMs ? ` · ${(waitedMs / 60000).toFixed(1)}분 지연` : ''),
      );
    }

    const tail = [];
    // privateAccess 는 열람 권한을 채널 이름으로 적은 것("#비공개가" · "공개만" · "전체").
    // allowPrivate 는 2026-08-05 이전의 불린 필드다 — 옛 jsonl 도 계속 렌더해야 하므로 남긴다.
    //
    // ⚠️ **이 줄은 비공개 채널 이름을 자료 저장소에 글자로 남긴다** (50-resources/hermes-log/,
    // 매일 커밋). 그래도 되는 이유는 **하나뿐이다 — 자료 저장소를 WHK 혼자 Admin 으로
    // 관리한다**(WHK 판정 2026-09-16). 근거가 그것뿐이므로 **clone·열람 범위가 넓어지는
    // 순간 다시 판정해야 한다.** 그때는 이름 대신 건수만 적거나 가려서 적어야 한다.
    if (e.privateAccess != null) tail.push(`비공개 근거 ${e.privateAccess}`);
    else if (e.allowPrivate != null) tail.push(`비공개 근거 ${e.allowPrivate ? '허용' : '금지'}`);
    // 스레드에서 이어 물은 경우. 봇 답변은 맥락에 안 들어가므로 여기 숫자는 사람 발언 수다.
    if (e.threadContext) tail.push(`스레드 앞 대화 ${e.threadContext}건`);
    if (e.refused) tail.push('모델 거절');
    if (e.truncated) tail.push('⚠️ 상한에 걸려 잘림');
    if (e.toolLimit) tail.push('⚠️ 도구 상한에 걸려 멈춤');
    // 과부하로 죽은 회차를 Anthropic 에 물을 때 이 값이 필요하다.
    if (e.requestId) tail.push(`요청 ID ${e.requestId}`);
    if (e.permalink) tail.push(`[슬랙에서 열기](${e.permalink})`);
    if (tail.length) meta.push(tail.join(' · '));

    if (meta.length) out.push(...meta.map((l) => `> ${l}`), '');
    return out.join('\n');
  }

  /**
   * 한 달치 md 를 다시 쓴다. 최근 것이 위 (아카이브와 같은 순서).
   * @returns {{ym:string, file:string, qa:number, broadcast:number, failed:number, usd:number, broken:number, changed:boolean}}
   */
  function renderMonth(ym, { write = true } = {}) {
    const { entries, broken } = readRaw(ym);
    const file = path.join(LOG_DIR, `${ym}.md`);

    const operational = entries.filter(isOperational);
    const qa = entries.filter(isQaRound).length;
    const broadcast = entries.filter(isBroadcastRound).length;
    const failed = operational.filter((e) => e.ok === false).length;
    /* 머리말 괄호가 "문답" 바로 뒤에 붙어 있어서, 종류 구분 없이 세면 정기 발송의 실패가
     * "문답이 실패했다"로 잘못 읽힌다(2026-08-28 F2 — 정기 발송도 실패로 기록되기 시작하며
     * 드러난 문제다. 전에는 정기 발송이 실패로 기록된 적이 없어 우연히 맞았다).
     * `failed`(전체 개수)는 index.md 표·run-log-render.js 가 그대로 쓰므로 손대지 않는다. */
    const qaFailed = entries.filter((e) => isQaRound(e) && e.ok === false).length;
    const broadcastFailed = entries.filter((e) => isBroadcastRound(e) && e.ok === false).length;
    const usd = entries.reduce((a, e) => a + (Number(e.accounting?.knownCostUsd ?? e.costUsd) || 0), 0);
    const unknownCost = entries.filter((e) => e.accounting?.complete === false || (e.costUsd == null && e.usage)).length;

    const sorted = [...entries].sort((a, b) => String(b.at).localeCompare(String(a.at)));
    const days = new Map();
    for (const e of sorted) {
      const { date } = localParts(e.at);
      if (!days.has(date)) days.set(date, []);
      days.get(date).push(e);
    }

    const span = sorted.length
      ? `${localParts(sorted[sorted.length - 1].at).date} ~ ${localParts(sorted[0].at).date}`
      : '기록 없음';

    const head = [
      `# Hermes 대화 로그 — ${ym}`,
      '',
      `> **문답 ${qa}건**${qaFailed ? ` (실패 ${qaFailed}건)` : ''} · **정기 발송 ${broadcast}건**` +
        `${broadcastFailed ? ` (실패 ${broadcastFailed}건)` : ''} · **추정 $${usd.toFixed(2)}** · ${span}`,
      `> 원본: **코드 저장소**의 \`logs/qa-${ym}.jsonl\` (VM, git 밖). 이 파일은 그것을 렌더한 것이라 손으로 고쳐도 다음 렌더에 덮어씁니다.`,
      '> **Hermes 의 근거가 아닙니다** — 봇은 이 폴더를 읽지 않습니다. 최근 것이 위.',
    ];
    if (unknownCost) head.push(`> 비용은 확인분 합계입니다. 총액 미상 ${unknownCost}회차는 추가 비용이 있을 수 있습니다.`);
    if (broken) {
      head.push(`> ⚠️ 원본에서 읽지 못한 줄 ${broken}개 — 그만큼 아래에 빠져 있습니다.`);
    }
    head.push('');

    const body = [];
    for (const [date, list] of days) {
      body.push('---', '', `## ${date}`, '');
      for (const e of list) body.push(renderEntry(e));
    }
    if (!body.length) body.push('---', '', '_이 달에는 기록이 없습니다._', '');

    // 답변 본문은 손대지 않는다 — 빈 줄을 줄이는 정리조차 하지 않는다.
    // (코드 블록 안의 빈 줄까지 건드리면 그건 원문이 아니다)
    const text = [...head, ...body].join('\n') + '\n';
    const changed = writeIfChanged(file, text, write);
    return { ym, file, qa, broadcast, failed, usd, unknownCost, broken, changed, text };
  }

  /** 목록 페이지. 월별 표 + 누가 물었나 + 자주 나온 자리. */
  function renderIndex(stats, write) {
    const askers = new Map();
    const places = new Map();
    const allEntries = [];
    let lastAt = null;

    for (const ym of months()) {
      for (const e of readRaw(ym).entries) {
        allEntries.push(e);
        if (!lastAt || String(e.at) > lastAt) lastAt = String(e.at);
        if (isQaRound(e)) {
          const k = e.asker || '(알 수 없음)';
          const cur = askers.get(k) || { n: 0, usd: 0 };
          cur.n += 1;
          cur.usd += Number(e.accounting?.knownCostUsd ?? e.costUsd) || 0;
          askers.set(k, cur);
        }
        for (const c of e.channels || []) places.set(c, (places.get(c) || 0) + 1);
      }
    }
    const cs = cacheStats(allEntries);

    const lines = [
      '# Hermes 대화 로그',
      '',
      '> Hermes 와 오간 대화를 전부 남깁니다 — 채널 멘션·DM·정기 발송(일일·주간 요약, 위생 점검, 자동 반영).',
      '> 원본은 VM 의 **코드 저장소** 안 `logs/*.jsonl` (git 밖)이고, 이 트리는 그것을 렌더한 것입니다.',
      '> **Hermes 의 근거가 아닙니다** — 봇은 이 폴더를 읽지 않습니다. 넣으면 자기 답변을 원본 근거로 삼습니다.',
      '',
      '## 달별',
      '',
      '> 비용은 확인분 합계이며 누락분·미상 비용·SDK 내부 재시도는 포함하지 못할 수 있습니다.',
      '| 달 | 문답 | 정기 발송 | 실패 | 추정 비용 |',
      '|---|---|---|---|---|',
    ];
    for (const s of [...stats].reverse()) {
      lines.push(`| [${s.ym}](${s.ym}.md) | ${s.qa} | ${s.broadcast} | ${s.failed} | $${s.usd.toFixed(2)}${s.unknownCost ? ` + 미상 ${s.unknownCost}회차` : ''} |`);
    }

    if (askers.size) {
      lines.push('', '## 누가 물었나', '', '| 사람 | 문답 | 추정 비용 |', '|---|---|---|');
      for (const [k, v] of [...askers].sort((a, b) => b[1].n - a[1].n)) {
        lines.push(`| ${k} | ${v.n} | $${v.usd.toFixed(2)} |`);
      }
    }

    /* 캐시 미스 사유. **줄이려면 사유별 몫을 봐야 한다** — 시스템 색인은 2.7만 토큰이라
     * 한 번 다시 쓰면 약 $0.27 이고, 줄어드는 것은 `system_changed`(색인이 실제로 바뀜)뿐이다.
     * 사유가 하나도 없으면 절을 통째로 뺀다 — 좋은 상태를 빈 표로 보여줄 이유가 없다.
     * 세는 규칙은 cacheStats 주석에 있다. */
    if (cs.rows.length) {
      lines.push(
        '',
        '## 캐시 미스 사유',
        '',
        `> 진단이 붙은 문답 ${cs.qaMeasured}건 중 ${cs.withMiss}건에서 캐시가 실제로 안 맞았습니다` +
          `${cs.qaUnmeasured ? ` (그 밖의 ${cs.qaUnmeasured}건은 진단이 붙기 전이거나 모델을 부르지 않은 회차라 잰 적이 없습니다)` : ''}.`,
        '> `system_changed` 는 색인이 실제로 바뀐 것(커밋이 들어왔다)이라 **줄일 여지가 있습니다**.',
        `> **「진단 실패」 ${cs.withUnknown}건은 미스가 아니라 못 잰 것**입니다 — 캐시가 맞았는지를 알 수 없어,`,
        '> 그 줄의 「놓친 토큰」도 손해가 0이라는 뜻이 아닙니다. 못 잰 길이 둘입니다.',
        '> `previous_message_not_found` 는 대조할 앞 요청을 못 찾은 것이고,',
        `> \`${NO_BASELINE}\`(${cs.noBaseline}건)은 **봇을 다시 켠 뒤 첫 질문**이라 기준 자체가 없어 API 가 사유를`,
        '> 아예 안 보낸 것입니다 — 색인을 다시 쓰는 자리라, 사유가 비었다고 맞은 쪽에 세면 안 됩니다.',
        '',
        '| 사유 | 판정 | 횟수 | 놓친 토큰 |',
        '|---|---|---|---|',
      );
      for (const r of cs.rows) {
        lines.push(
          `| \`${r.type}\` | ${r.unknown ? '진단 실패' : '미스'} | ${r.n} | ` +
            `${r.unknown ? '(못 잼)' : r.tokens.toLocaleString('en-US')} |`,
        );
      }
    }

    /* 실패·재시도 사유. **사유가 하나도 없으면 절을 통째로 뺀다** — 좋은 상태를 빈 표로
     * 보여줄 이유가 없다(캐시 미스 절과 같은 규칙). 사유는 API 가 준 값 그대로 둔다.
     * 텍스트 조립은 failureSectionLines 로 뗐다 — 분모를 왜 안 쓰는지는 그 함수 주석 참조. */
    lines.push(...failureSectionLines(allEntries));

    if (places.size) {
      lines.push('', '## 근거로 열린 자리 (상위 15)', '', '| 자리 | 횟수 |', '|---|---|');
      for (const [k, n] of [...places].sort((a, b) => b[1] - a[1]).slice(0, 15)) {
        lines.push(`| ${k} | ${n} |`);
      }
    }

    lines.push('', `_마지막 기록: ${lastAt ? localParts(lastAt).date + ' ' + localParts(lastAt).time : '없음'}_`);

    const file = path.join(LOG_DIR, 'index.md');
    const text = lines.join('\n') + '\n';
    return { file, text, changed: writeIfChanged(file, text, write) };
  }
  return { renderEntry, renderMonth, renderIndex };
}
