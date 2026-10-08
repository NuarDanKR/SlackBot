/**
 * RPC 답변 코어 — 도구를 돌려 문장을 만들고 **상태 넷**으로 접는다.
 *
 * 계약: `docs/design/specialist-v3-tool-contract.md` §6
 *
 * ## Slack 이 여기 없다
 *
 * 이 파일도, 이 파일이 가져오는 것들도 Slack 을 모른다. RPC 요청에 대해 Hermes 가
 * Slack 에 글을 올리면 **같은 답이 두 번 나간다** — 하나는 마스터가 출처를 붙여서,
 * 하나는 우리가 출처 없이. 둘의 내용이 다르면 사람은 어느 쪽이 맞는지 알 수 없다.
 * 「안 부른다」 가 아니라 **부를 것이 없다** 로 만든다(`scripts/check-rpc-isolation.js`).
 *
 * ## 상태를 넷으로 가르는 이유
 *
 * 「모른다」 와 「못 찾았다」 와 「고장났다」 를 하나로 묶으면 화면에서 원인을 못 가리고,
 * 그러면 고칠 곳도 못 정한다. 마스터는 `failed` 에서만 폴백한다 — `refused` 에서
 * 폴백하면 권한 때문에 안 답한 판단이 무의미해진다.
 *
 * ## 지어낸 인용은 **답 전체를 버린다**
 *
 * 받지 않은 `[eN]` 이 섞이면 저쪽 `validate_response` 가 어차피 통째로 거부한다.
 * 그 전에 우리가 `failed` 로 접는 이유는 둘이다 — 저쪽에 보내 봐야 왕복만 늘고,
 * 무엇보다 **그 문장들이 무엇에 기댔는지 우리가 말할 수 없다.** 조용히 표식만 떼면
 * 출처 없는 문장이 출처 있는 문장과 똑같은 모양으로 사람에게 간다(절대 원칙 2).
 */
import {
  STATUS_ANSWERED, STATUS_FAILED, STATUS_NO_EVIDENCE, STATUS_REFUSED,
  collectCitations, forbiddenReason,
} from './wire.js';

/** 못 찾았을 때 사람에게 가는 문장. 비워 두면 마스터가 붙일 말이 없다. */
const NOTHING_FOUND = '찾아봤지만 근거가 될 기록을 찾지 못했습니다.';

/** 저쪽 deadline 보다 **먼저** 접기 위한 여유. 응답이 전선 위에서 버려지지 않게. */
export const DEADLINE_GUARD_MS = 1_500;

/** 예산 만료를 가리키는 표. 값으로 쓰지 않으므로 다른 결과와 안 섞인다. */
const TIMED_OUT = Symbol('rpc-deadline');

/**
 * 답변 한 번.
 *
 * @param {object} deps
 * @param {Function} deps.toolSession   `(spec) => { open(req) }` — `llm/provider.js` 와 같은 모양
 * @param {Function} deps.buildTools    `({client, allow, terms, trace}) => tool[]`
 * @param {Function} deps.createClient  `({requestId, token, allow}) => toolClient`
 * @param {Function} deps.renderPrompt  `({maxOutputChars}) => string`
 * @param {object}   deps.model         `{id, maxTokens, effort}`
 * @param {object}   deps.terms         표시 용어
 * @param {number}   deps.maxIterations
 */
export function createRpcAnswerer({
  toolSession, buildTools, createClient, renderPrompt, model, terms,
  maxIterations = 12, now = () => Date.now(), console: log = globalThis.console,
}) {
  /**
   * @param {object} request `wire.parseAnswerRequest` 가 돌려준 것
   * @param {{signal?:AbortSignal}} [opts]
   * @returns {Promise<{status:string, answer:string, usedEvidence:string[], uncertain:string[], usage:object}>}
   */
  async function answer(request, { signal } = {}) {
    const started = now();
    const trace = [];
    const client = createClient({
      requestId: request.requestId,
      token: request.token,
      allow: request.allow,
    });
    // 예산 시계는 `own` 이 쥔다(아래). 도구는 **호출 직전에** 그 신호를 보고 멈춘다 —
    // 안 보면 예산이 끝난 뒤에도 저쪽 도구 예산을 한 번 더 태운다.
    const own = new AbortController();
    const tools = buildTools({ client, allow: request.allow, terms, trace, signal: own.signal });

    // 요청이 준 씨앗 근거. v3 에서는 **선택**이고, 있으면 그대로 싣는다 — 그 ID 도
    // 저쪽이 발급한 것이라 인용할 수 있다.
    const seeded = new Set(request.evidence.map((e) => e.id));

    const userText = [
      '[질문]',
      request.question,
      '',
      ...(request.evidence.length
        ? [
          '[먼저 주어진 근거]',
          ...request.evidence.map((e) => `[${e.id}] ${e.text}`),
          '',
        ]
        : []),
      ...(request.followUp.length
        ? [
          '[이어 가는 자리]',
          '앞선 답변의 **문장이 아니라** 그 답변이 읽은 원문 자리입니다. 필요하면 도구로 다시 엽니다.',
          ...request.followUp.map((f) => `- ${f.id}`),
          '',
        ]
        : []),
      `답변은 ${request.maxOutputChars}자 안에서, 기댄 문장마다 [eN] 을 붙여 적으세요.`,
    ].join('\n');

    const usage = { tool_calls: 0, input_tokens: 0, output_tokens: 0, model: model.id };
    let text = '';
    let refused = false;

    /* 우리 예산. **저쪽 deadline 보다 먼저 끝난다.**
     *
     * 저쪽은 `deadline_ms` 에 소켓을 끊고 `transport` 로 적는다. 그 시각에 맞춰 끝내면
     * 우리가 만든 답이 전선 위에서 버려지고, **버린 이유가 저쪽 로그에만** 남는다 —
     * 「고장」 과 「시간 초과」 를 우리 쪽에서 가릴 수 없게 된다. 한 숨 먼저 접어
     * 우리가 쓴 사유로 돌려준다. */
    const budgetMs = Math.max(1_000, request.deadlineMs - DEADLINE_GUARD_MS);
    const onOuterAbort = () => own.abort();
    signal?.addEventListener?.('abort', onOuterAbort, { once: true });
    const aborted = () => Boolean(signal?.aborted) || own.signal.aborted;

    let timer = null;
    const expired = new Promise((resolve) => {
      timer = setTimeout(() => { own.abort(); resolve(TIMED_OUT); }, budgetMs);
      timer.unref?.();
    });

    /** 정상 완주면 `null`, 일찍 끝났으면 **그대로 돌려줄 결과**. */
    const loop = (async () => {
      try {
        const session = toolSession(model).open({
          system: [{ type: 'text', text: renderPrompt({ maxOutputChars: request.maxOutputChars }) }],
          messages: [{ role: 'user', content: userText }],
          tools,
          maxIterations,
          previousMessageId: null,
        });

        // 계약이 깨진 뒤(토큰 만료·요청 불일치) **한 회차만 더** 돈다. 도구는 이미
        // 「그만 찾고 지금까지 읽은 것으로 답하라」 를 돌려줬으니, 그 회차에서 문장을
        // 맺게 한다. 그 자리에서 끊으면 읽은 것이 있는데도 빈 답이 나간다.
        let halted = false;

        for await (const turn of session) {
          if (aborted()) {
            // 취소는 **도구 폐기가 본체**다(계약 §8). 여기서 멈추는 것은 비용 절약이지
            // 안전장치가 아니다 — 안전장치는 저쪽이 토큰을 버리는 것이다.
            return done(STATUS_FAILED, '', ['취소되었습니다.']);
          }
          // 도구만 부른 회차는 본문이 비어 있다. 덮어쓰면 **마지막 문장이 지워진다**.
          if (turn.text) text = turn.text;
          usage.input_tokens += turn.usage?.input_tokens || 0;
          usage.output_tokens += turn.usage?.output_tokens || 0;

          if (turn.stopReason === 'refusal') { refused = true; break; }
          if (halted) break;

          const toolMessage = await session.generateToolResponse();
          if (!toolMessage) break; // 도구를 안 불렀다 = 이 답이 최종이다
          session.continueWith(toolMessage, turn);
          if (client.haltedReason) halted = true;
        }
        return null;
      } catch (err) {
        // **예외를 밖으로 내지 않는다.** 전문 봇 장애로 마스터 답변 전체가 실패해서는 안 된다.
        log?.error?.(`[rpc] 답변 실패: ${err?.code || err?.name || 'Error'}`);
        return done(STATUS_FAILED, '', [`도중에 끊겼습니다(${err?.code || err?.name || 'Error'}).`]);
      }
    })();

    let early;
    try {
      early = await Promise.race([loop, expired]);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener?.('abort', onOuterAbort);
    }

    if (early === TIMED_OUT) {
      /* 예산이 끝났다. **돌던 회차는 여기서 못 죽인다** — 모델 스트림과 도구 호출은
       * 각자의 상한으로 끝난다(도구는 `timeoutMs`, 모델은 `maxIterations`). 우리가
       * 하는 것은 `own.abort()` 로 다음 회차·다음 도구 호출을 막는 것까지다. */
      log?.error?.(`[rpc] 예산 ${budgetMs}ms 를 넘겼습니다.`);
      return done(STATUS_FAILED, '', ['시간 안에 끝내지 못했습니다.']);
    }
    if (early) return early;

    usage.tool_calls = client.callCount;

    if (refused) return done(STATUS_REFUSED, '', ['모델이 답변을 거절했습니다.']);

    // 상한을 **먼저** 자른다. 자른 뒤에 인용을 거둬야, 잘려 나간 문장의 근거를
    // 「썼다」 고 말하지 않는다.
    const clipped = String(text || '').slice(0, request.maxOutputChars).trim();

    const bad = forbiddenReason(clipped);
    if (bad) {
      log?.error?.(`[rpc] 응답 계약 위반: ${bad}`);
      return done(STATUS_FAILED, '', [bad]);
    }

    const issued = client.issued;
    for (const id of seeded) issued.add(id);
    const { known, unknown } = collectCitations(clipped, issued);

    if (unknown.length) {
      log?.error?.(`[rpc] 발급하지 않은 근거를 인용했습니다: ${unknown.join(', ')}`);
      return done(STATUS_FAILED, '', ['발급하지 않은 근거를 인용했습니다.']);
    }

    const uncertain = [];
    if (client.haltedReason) uncertain.push('자료를 끝까지 다 보지 못했습니다.');
    if (!clipped) {
      return done(STATUS_NO_EVIDENCE, NOTHING_FOUND, uncertain);
    }
    if (!known.length) {
      // 문장은 있는데 근거 표식이 없다. **근거 없는 답이므로 보내지 않는다** — 다만
      // 「고장」 은 아니라서 마스터가 폴백하지 않게 `no_evidence` 로 돌린다.
      return done(STATUS_NO_EVIDENCE, clipped, uncertain);
    }
    return done(STATUS_ANSWERED, clipped, uncertain, known);

    function done(status, answerText, notes = [], usedEvidence = []) {
      return {
        status,
        answer: answerText,
        usedEvidence,
        uncertain: notes,
        usage: { ...usage, tool_calls: client.callCount, elapsed_ms: Math.max(0, now() - started) },
        trace,
      };
    }
  }

  return { answer };
}

export { NOTHING_FOUND };
