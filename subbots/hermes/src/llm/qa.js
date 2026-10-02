/**
 * Q&A runner extracted by Codex. Dependencies are supplied by the claude facade.
 * One factory instance owns cross-question diagnostics; no archive or Slack I/O
 * occurs on import. Defaults preserve the production clock and console.
 */
import { createUsageCollector, attachUsage } from './usage.js';

export function createQuestionAnswerer({
  config, toolSession, buildTools, systemBlocks, lastSyncedAt, privateQuoteLine,
  BOT_ANSWER_MARK, logUsage, estimateCost, now = () => new Date(), console = globalThis.console,
}) {
  /* ── Q&A ──────────────────────────────────────────────────────── */

  // 툴 루프는 반복(iteration)마다 API 를 한 번씩 부른다. 마지막 메시지의 usage 는
  // 그 마지막 1회분뿐이라, 검색을 서너 번 도는 질문이면 실제 비용의 1/3 만 찍힌다.
  // 루프를 돌면서 전부 더한다.
  const emptyUsage = () => ({
    input_tokens: 0,
    cache_creation_input_tokens: 0,
    cache_read_input_tokens: 0,
    output_tokens: 0,
    // 캐시 쓰기는 5분·1시간 단가가 달라서(1.25배 vs 2배) 나눠 더한다. format.js 의 estimateCost 가 읽는다.
    cache_creation: { ephemeral_5m_input_tokens: 0, ephemeral_1h_input_tokens: 0 },
  });

  /**
   * 캐시 미스를 사유별로 묶는다. 로그에 남는 모양이라 회차별 원본 대신 이 요약만 싣는다.
   * @returns {Array<{type:string, count:number, tokens:number}>} 사유 없으면 빈 배열
   */
  function summarizeCacheMiss(list) {
    const by = new Map();
    for (const m of list) {
      const cur = by.get(m.type) || { type: m.type, count: 0, tokens: 0 };
      cur.count += 1;
      cur.tokens += m.tokens || 0;
      by.set(m.type, cur);
    }
    return [...by.values()].sort((a, b) => b.tokens - a.tokens);
  }

  /* 캐시 미스 사유를 **질문과 질문 사이에서도** 받으려고 마지막 응답 ID 를 들고 있는다.
   *
   * API 는 `diagnostics.previous_message_id` 로 준 요청과 이번 요청을 대조해 사유를 낸다.
   * 예전에는 첫 회차에 null 을 줬는데, **색인을 다시 쓰는 것이 바로 그 첫 회차라** 정작 알고
   * 싶은 것에는 사유가 한 번도 안 붙었다 (2026-08-11 실측: 도구를 8회 도는 질문도 사유가 빈
   * 배열이었다). 앞 질문의 응답 ID 를 주면 그 자리에서 사유가 나온다 — 별개 요청끼리도
   * 대조된다는 것을 작은 요청으로 확인했다.
   *
   * **넘기는 값은 실제로 받은 id 뿐이다.** 모양이 틀린 값(빈 문자열 포함)은 400 이고, 모양이
   * 맞는데 없는 id 는 정상 응답에 `previous_message_not_found` 로 돌아온다(실측). 그래서
   * 낡아서 못 찾는 것은 안전하지만 지어낸 값은 안전하지 않다.
   *
   * 질문이 겹쳐 들어오면 남의 질문 것이 기준이 될 수 있다. 사유는 관찰용이고 답변에 영향이
   * 없으므로 그대로 둔다 — 정확히 맞추려면 요청마다 들고 다녀야 하는데 그 값어치가 없다.
   */
  let lastMessageId = null;

  function addUsage(acc, u) {
    if (!u) return acc;
    for (const k of Object.keys(acc)) {
      if (k === 'cache_creation') continue;
      acc[k] += u[k] || 0;
    }
    for (const k of Object.keys(acc.cache_creation)) {
      acc.cache_creation[k] += u.cache_creation?.[k] || 0;
    }
    return acc;
  }

  /* 도구 호출에 결과 크기를 붙인다. 크기 칸은 도구 래퍼(tools.js 의 buildTools 끝)가
   * tool_use 의 id 와 함께 밀고, 여기서는 **그 id 로 짝짓는다.** 순서로 짝지으면(예전:
   * 같은 이름끼리 앞에서부터) 칸을 안 남긴 호출 — 던진 호출, refusal break 로 실행
   * 자체가 안 된 호출 — 이 하나라도 있을 때 뒤의 크기가 전부 한 칸씩 앞으로 밀렸다.
   * id 짝에서는 그런 호출이 그냥 크기 없이 남는다 — 없는 값을 지어내지 않는다. */
  function attachSizes(calls, sizes) {
    const byId = new Map(sizes.filter((s) => s.id != null).map((s) => [s.id, s]));
    return calls.map((c) => {
      const s = c.id != null ? byId.get(c.id) : undefined;
      return s ? { ...c, chars: s.chars } : c;
    });
  }

  /* search 호출에 좁힘 정보를 붙인다 — attachSizes 와 같은 id 짝짓기. 좁힘 칸은 래퍼가
   * run 본문 진입 전에 밀어서 던진 호출도 칸(빈 칸)을 남기고, 실행 자체가 안 된 호출은
   * 칸이 없어도 id 짝이라 남의 칸을 못 가져간다. 여기가 짝짓기의 마지막 단이라
   * **짝짓기용 id 를 여기서 뗀다** — convo-log 의 store 가 toolCalls 를 jsonl 로 그대로
   * 저장하므로, 남기면 원본 로그에 로그 필드가 아닌 값이 실린다. */
  function attachNarrows(calls, narrows) {
    const byId = new Map(narrows.filter((n) => n.id != null).map((n) => [n.id, n]));
    return calls.map(({ id, ...call }) => {
      const { id: _paired, ...slot } = (id != null ? byId.get(id) : undefined) ?? {};
      return { ...call, ...slot };
    });
  }

  /**
   * @param {{question:string, access:object, origin:string, asker:string, slackClient?:object, threadContext?:string}} opts
   *   access 는 config.js 의 열람 권한. 비공개 채널은 여기 적힌 것만 열린다.
   *   threadContext 는 이 질문이 달린 스레드의 앞 대화 (slack-live.js 의 fetchThreadContext).
   * @returns {Promise<{text:string, channels:string[], refused:boolean, toolCalls:object[], usage:object, model:string, costUsd:number}>}
   *
   * channels·toolCalls·usage 는 대화 로그(convo-log.js)가 "이 답이 무엇을 근거로 나왔나"를
   * 남기는 데 쓴다. 예전에는 touched 도 툴 호출도 여기서 만들어 놓고 버렸다.
   */
  async function answerQuestion({ question, access, origin, asker, slackClient, threadContext }) {
    const touched = new Set();
    const toolSizes = [];
    const toolNarrows = [];
    const tools = buildTools({ access, slackClient, touched, sizes: toolSizes, narrows: toolNarrows });
    const today = new Intl.DateTimeFormat('sv-SE', { timeZone: config.timezone }).format(now());

    const userText = [
      '[요청 정보]',
      `- 오늘: ${today}`,
      `- 아카이브 마지막 동기화: ${lastSyncedAt()}`,
      `- 이 질문이 온 곳: ${origin}`,
      `- 인용해도 되는 비공개 채널: ${privateQuoteLine(access)}`,
      `- 질문자: ${asker}`,
      '',
      // 스레드 앞 대화. **봇의 발언은 빠져 있고** 그 자리에 `BOT_ANSWER_MARK` 만
      // 있다. 여기서 한 번 더 못박는 이유는, 자기표시를 본 모델이 "내가 아까 뭐라고 했을 텐데"
      // 하고 값을 지어내는 것이 가장 그럴듯한 실패 모양이기 때문이다.
      //
      // **문구를 손으로 적지 않는다** — 여기 리터럴을 두면 상수를 고쳤을 때 모델에게만 옛
      // 문구를 설명하게 되고, 그러면 프롬프트가 아카이브에 없는 표식을 가리킨다(에러 없음).
      // `config.js` 의 그 상수 주석이 「claude.js 도 같은 문자열을 본다」고 적는 근거가
      // 이 import 다 (2026-09-11 리뷰 Minor — 그전까지는 주석만 그렇게 적혀 있었다).
      ...(threadContext
        ? [
            '[이 스레드의 앞 대화]',
            '지시어("그럼", "거기", "그 사업장")가 무엇을 가리키는지 푸는 데만 쓰세요.',
            `\`${BOT_ANSWER_MARK}\` 은 봇이 앞서 말한 자리입니다. **그 내용은 주어지지 않았고,`,
            '무슨 값이었을지 짐작해서도 안 됩니다** — 필요한 숫자·날짜·이름은 전부 도구로 다시 확인하세요.',
            '',
            threadContext,
            '',
          ]
        : []),
      '[질문]',
      question,
    ].join('\n');

    /* 시스템 블록 둘 중 **앞 것에만 캐시를 건다.**
     *
     * 앞 블록은 모든 질문자에게 같아서 캐시가 곧 절감이다. **크기는 아카이브와 함께 자란다** —
     * API 가 알려준 실측(캐시 미스 `system_changed` 의 토큰 수)이 2026-08-14 23,594 → 08-20
     * 24,804 → 08-25 26,873 이고 하루 약 300토큰씩 늘었다(2026-09 중순 추정 3.3만). 여기 한
     * 숫자를 박아 두면 몇 주 뒤 조용히 틀린다 — 지금 값이 궁금하면 hermes-log 에서 그
     * `system_changed` 줄을 본다. 뒤 블록은 권한별로
     * 다르지만 최대 2,700토큰이라, 캐시로 얻는 것(회당 약 $0.012)보다 **표시 하나를 아껴 두는
     * 편이 낫다.** 표시는 한 요청에 4개가 상한인데 툴 루프가 대화 쪽에서 2개를 쓰고 있어
     * (rollCacheMarks 가 남기는 직전 것 + withCacheTail 의 새 것), 여기에 하나를 더 걸면
     * 딱 4개로 여유가 0이 된다. 그 상태에서 표시를 하나라도 더하면 400 이 나는데 그건
     * 팀 앞에서 답이 안 나가는 실패다.
     *
     * 캐시를 안 걸어도 뒤 블록이 앞 블록의 캐시를 깨지는 않는다 — 캐시는 표시 지점까지의
     * 앞부분만 보고, 그 뒤는 그냥 정가 입력이다. 툴 루프 둘째 회차부터는 대화 쪽 표시가
     * 이 블록까지 함께 담는다. */
    const sys = systemBlocks(access);

    const req = {
      system: [
        {
          type: 'text',
          text: sys.common,
          cache_control: { type: 'ephemeral', ttl: '1h' },
        },
        ...(sys.extra ? [{ type: 'text', text: sys.extra }] : []),
      ],
      messages: [{ role: 'user', content: userText }],
      tools,
      maxIterations: config.limits.maxToolIterations,
      // 대조 기준은 **앞 질문의 마지막 응답**이다 — 색인을 다시 쓰는 것이 첫 회차라, 여기에
      // null 을 주면 정작 알고 싶은 자리에 사유가 안 붙는다 (lastMessageId 주석).
      // 쓸 수 있는 모양인지는 창구가 판정한다 — 여기서는 **받은 값 그대로** 넘긴다.
      previousMessageId: lastMessageId,
    };

    // 단가 계산기는 꽂아서 쓴다 (digest·요약 대조와 같은 모양). 안 꽂으면 원장이 제 기본값
    // 으로 도는데 그 기본값이 마침 같은 계산기라 **금액은 맞고 배선만 안 재지는** 상태가 된다.
    const ledger = createUsageCollector({ estimate: estimateCost });
    let activeModel = config.models.qa.id;
    let final;
    let usage = emptyUsage();
    const toolCalls = [];
    const cacheMiss = [];
    // Only an iterator.next() failure represents an unobserved model attempt.
    // Tool execution and rendering errors retain known usage without adding one.
    async function* observedMessages(runner, model) {
      const iterator = runner[Symbol.asyncIterator]();
      try {
        while (true) {
          let step;
          try { step = await iterator.next(); }
          catch (err) { ledger.failed(model); throw err; }
          if (step.done) return;
          yield step.value;
        }
      } finally {
        await iterator.return?.();
      }
    }
    try {
    const session = toolSession({ ...config.models.qa }).open(req);

    /* 이번 질문의 대조 기준. **null 이었는지를 답에 실어 보낸다** (convo-log.js 가 센다).
     *
     * 기준이 없으면 API 는 첫 회차에 사유를 **아예 안 보낸다** — 미스가 없어서가 아니라 견줄
     * 것이 없어서다. 그런데 그 첫 회차가 바로 색인(약 2.7만 토큰)을 다시 쓰는 자리다. 남기지
     * 않으면 표에서 「사유가 없다 = 맞았다」로 세어지고, 그렇게 세면 줄일 몫이 실제보다 작게
     * 보인다. 8월 실측으로 직전 문답이 1시간을 넘겼는데 사유 줄이 없는 회차가 14건 있었고,
     * 그중 몇이 이 경우인지 가릴 방법이 없었다.
     *
     * **창구가 실제로 보낸 값을 되읽는다** — 모양 판정을 여기서 한 벌 더 하면 규칙이 두 곳이
     * 되고, 한쪽만 고쳐졌을 때 그 차이는 에러 없이 흐른다. */
    const baseline = session.previousMessageId;

    for await (const turn of observedMessages(session, activeModel)) {
      final = turn;
      addUsage(usage, turn.usage);
      ledger.record(turn.raw, activeModel);
      // 다음 **질문**이 이 응답과 대조되게 한다 (같은 질문 안의 회차 대조는 창구가 한다).
      if (turn.messageId) lastMessageId = turn.messageId;
      for (const u of turn.toolUses) {
        // id 는 크기·좁힘 짝짓기용이다 (attachSizes·attachNarrows) — 마지막 단에서 뗀다.
        toolCalls.push({ id: u.id, name: u.name, input: u.input });
        console.log(`  -> ${u.name}(${JSON.stringify(u.input).slice(0, 120)})`);
      }
      /* 캐시가 **왜** 안 맞았는지를 모아 둔다. 예전에는 찍고 버려서 VM 콘솔에만 남았고,
       * 그래서 「1시간 만료 때문인가 · 권한 조합 때문인가」를 며칠 뒤 로그를 뒤져야 알 수
       * 있었다. 대화 로그에 실으면 그 판정이 그냥 보인다 (convo-log.js). */
      if (turn.cacheMiss) {
        cacheMiss.push(turn.cacheMiss);
        console.log(`  · 캐시 미스: ${turn.cacheMiss.type}${turn.cacheMiss.tokens ? ` (${turn.cacheMiss.tokens} 토큰)` : ''}`);
      }

      /* 대화를 잇는 일을 창구가 가져가면 runner 는 자기가 assistant 메시지를 붙이지 않고
       * 거절 처리도 건너뛴다. 그래서 거절은 그 전에 우리가 끊는다. */
      if (turn.stopReason === 'refusal') break;

      /* 도구 실행은 창구에게 그대로 맡긴다 — 스키마 검증도 에러 감싸기도 그쪽 것이다. */
      const toolMessage = await session.generateToolResponse();
      if (!toolMessage) break; // 도구를 안 불렀다 = 이 답이 최종이다

      session.continueWith(toolMessage, turn);
    }

    // 안전 분류기가 거절한 경우(내부 재무 질의에서는 드물다) 한 번만 다른 모델로 재시도.
    if (final?.stopReason === 'refusal') {
      // 모델이 바뀌면 단가도 달라지므로 1차 시도분을 먼저 정산해 둔다.
      logUsage('qa', final?.model || config.models.qa.id, usage);
      console.warn('  ! refusal — fallback 모델로 재시도');
      activeModel = config.models.fallback;
      const rerun = toolSession({ ...config.models.qa, id: config.models.fallback }).open(req);
      usage = emptyUsage();
      for await (const turn of observedMessages(rerun, activeModel)) {
        final = turn;
        addUsage(usage, turn.usage);
        ledger.record(turn.raw, activeModel);
        if (turn.messageId) lastMessageId = turn.messageId;
        for (const u of turn.toolUses) toolCalls.push({ id: u.id, name: u.name, input: u.input });
        if (turn.cacheMiss) cacheMiss.push(turn.cacheMiss);
      }
      if (final?.stopReason === 'refusal') {
        const model = final?.model || config.models.fallback;
        logUsage('qa', model, usage);
        return {
          text: '이 질문에는 답변할 수 없습니다.',
          channels: [],
          refused: true,
          toolCalls: attachNarrows(attachSizes(toolCalls, toolSizes), toolNarrows),
          usage,
          model,
          costUsd: ledger.snapshot().costUsd,
          accounting: ledger.snapshot(),
          cacheMiss: summarizeCacheMiss(cacheMiss),
          cacheBaseline: Boolean(baseline),
        };
      }
    }

    const model = final?.model || config.models.qa.id;
    logUsage('qa', model, usage);

    const text = final?.text ?? '';

    /* **잘린 답을 완성본처럼 내보내지 않는다.** 요약 쪽은 2026-08-10 에 같은 사고로 검사를
     * 넣었는데(아래 runDigest) Q&A 만 `refusal` 만 보고 있었다. 여기 걸리면 문장이 중간에서
     * 끊기는데 받는 사람에게는 그냥 짧은 답으로 보인다 — 2026-08-28 10:20 어느 사업장 신용조회
     * 질문이 본문 66자에서 끊긴 채 채널에 나갔고, 로그에도 `ok: true` 로 남아 아무 데서도
     * 드러나지 않았다.
     *
     * **이 검사가 그 사고의 본체다.** 상한(`models.qa.maxTokens`)을 올린 것은 여유일 뿐
     * 원인 수정이 아니다 — 같은 질문을 상한 8,000 그대로 다시 돌리면 끝까지 나온다(실측
     * 3회 중 2회). 마지막 회차가 얼마나 생각할지가 흔들려서 **가끔** 넘고, 넘었을 때
     * 조용히 나가는 것이 문제였다. 그러니 상한을 올렸다고 이 검사를 지우면 안 된다.
     *
     * 요약과 달리 **던지지 않고 표시만 붙여 내보낸다** — Q&A 는 사람이 앞에서 기다리는
     * 자리라, 통째로 안 보내면 여기까지 나온 답까지 함께 잃는다 (WHK 결정 2026-08-28). */
    const truncated = final?.stopReason === 'max_tokens';
    const cut =
      `⚠️ *여기서 잘렸습니다* — 답변이 상한(${config.models.qa.maxTokens}토큰)에 걸려 끝까지 쓰지 못했습니다. ` +
      '위 내용은 그대로 쓰실 수 있지만 **뒷부분이 빠져 있습니다.** 질문을 좁혀 다시 물어 주세요.';

    /* **도구 반복 상한에 걸린 회차도 같이 붙잡는다** (2026-09-03). 위 검사는 `max_tokens` 만
     * 봐서 이쪽이 통째로 빠져 있었다.
     *
     * 툴 루프는 `max_iterations` 에서 끊기는데, 그 끊김은 **SDK 안에서 예외도 표시도 없이**
     * 일어난다 — `BetaToolRunner` 반복자 맨 앞의
     * `if (max_iterations && iterationCount >= max_iterations) break;` 한 줄이라
     * 위 `for await` 이 그냥 조용히 끝난다. 그때 마지막 응답은 도구를 더 부르려던 중간
     * 상태라 본문이 없거나 토막인데, `truncated`·`refused` 가 둘 다 false 라
     * **정상 답변과 구별이 안 됐다.** 글자가 없으면 아래 기본 문구가 나가서 봇이 질문자를
     * 탓했다 — 2026-08-14 22:35 문답이 도구 16회·74.4초·약 $1.440 을 쓰고 그 한 줄로 끝났다.
     *
     * `stop_reason === 'tool_use'` 로 이 루프를 빠져나오는 길은 그 상한 하나뿐이다. 모델이
     * 도구를 부르면 위에서 계속 돌고(generateToolResponse 가 값을 준다), 안 부르면
     * `stop_reason` 이 `tool_use` 가 아니다. 잘림과 겹치면 잘림이 이긴다 — 그쪽은 이미
     * 쓰던 문장이 끊긴 것이라 사람이 볼 위험이 더 크다. 지키는 것은
     * `scripts/check-tool-limit.js`. */
    const toolLimit = !truncated && final?.stopReason === 'tool_use';
    const stopped =
      `⚠️ *여기서 멈췄습니다* — 자료를 찾아 읽기를 ${config.limits.maxToolIterations}번(상한)까지 되풀이하고도 ` +
      '끝나지 않아, 답을 다 쓰지 못한 채 멈췄습니다. **질문이 잘못돼서가 아닙니다.** ' +
      '사업장이나 기간을 나눠 (예: 한 사업장씩) 다시 물어봐 주시면 끝까지 답할 수 있습니다.';
    if (toolLimit) console.warn(`  ! 도구 반복 상한(${config.limits.maxToolIterations}회) 도달 — 답을 못 맺고 멈춤`);

    return {
      text: truncated
        ? [text, cut].filter(Boolean).join('\n\n')
        : toolLimit
          ? [text, stopped].filter(Boolean).join('\n\n')
          : text || '답변을 만들지 못했습니다. 질문을 조금 더 구체적으로 적어 주세요.',
      channels: [...touched],
      refused: false,
      truncated,
      // 상한에 걸려 답을 못 맺은 회차. 로그가 정상 답변과 구별하려면 값이 있어야 한다
      // (index.js 가 아직 이 값을 로그로 넘기지 않는다 — 답변 본문의 ⚠️ 로만 보인다).
      toolLimit,
      toolCalls: attachNarrows(attachSizes(toolCalls, toolSizes), toolNarrows),
      usage,
      model,
      costUsd: ledger.snapshot().costUsd,
          accounting: ledger.snapshot(),
      cacheMiss: summarizeCacheMiss(cacheMiss),
      cacheBaseline: Boolean(baseline),
    };
    } catch (err) {
      throw attachUsage(err, ledger.snapshot());
    }
  }


  return answerQuestion;
}
