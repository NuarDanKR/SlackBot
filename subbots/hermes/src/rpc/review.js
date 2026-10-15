/**
 * 요약 검토 **후보 생성**. 모델을 한 번 부르고, 나온 것을 원문과 대조한다.
 *
 * 계약: `docs/verification/2026-10-15-hermes-review-rpc-claude-handoff.md`
 * 후보 규칙 정본: `subbots/hermes/contract/summary-review.md` ← **그 파일을 그대로 쓴다**
 *
 * ## Q&A 와 무엇이 다른가
 *
 * 도구가 없다. 자료를 **찾지 않는다** — TYBot 이 이미 권한을 보고 고른 한 채널의
 * 사람 발언을 요청 본문으로 받고, 그 안에서만 고른다. 그래서 도구 토큰도, 도구
 * 소켓도, 왕복도 없다. 한 번 부르고 끝이다.
 *
 * ## 「장애를 정상 0건으로 숨기지 않는다」 가 이 파일의 핵심이다
 *
 * 검토자는 후보가 없어도 DM 을 받는다. 그래서 **「없었다」 와 「못 만들었다」 를
 * 가르는 책임이 여기 있다.** 둘을 합치면 모델이 죽은 날에도 검토자는 「오늘은
 * 요약할 게 없군요」 를 받고, 그 상태가 며칠 간다.
 *
 *   empty   모델을 불렀고 출력 형식도 맞았는데 후보가 0건이다
 *   failed  timeout · JSON 깨짐 · 잘린 출력 · provider 오류 · **전부 탈락**
 *
 * 모델이 후보를 냈는데 전부 검증에서 떨어지면 `failed` 다. 그건 「없었다」 가
 * 아니라 **우리가 쓸 수 없는 것을 받았다** 이고, 사람이 봐야 하는 신호다.
 *
 * ## 검증은 느슨해지지 않는다
 *
 * `contract/summary-review.md` 가 모델에게 거는 규칙을 여기서 **기계로 다시 본다**.
 * 프롬프트는 부탁이고 검증이 계약이다. 다만 여기 검증이 Python 쪽 재검증을
 * 대신하지 않는다 — 좌표·해시·채널 ID 결합은 TYBot 이 보관한 원문에서만 한다.
 */
import {
  CANDIDATE_FORMS, CANDIDATE_KINDS, MAX_CANDIDATES,
  STATUS_EMPTY, STATUS_FAILED, STATUS_OK,
} from './review-wire.js';

/** 저쪽 deadline 보다 **먼저** 접는다. Q&A 와 같은 이유 — 전선 위에서 버려지지 않게. */
export const REVIEW_DEADLINE_GUARD_MS = 1_500;

const TIMED_OUT = Symbol('review-deadline');

/** 숫자처럼 보이는 것. 「원문에 없는 숫자」 를 잡는 데만 쓴다. */
const NUMERIC_RE = /[0-9０-９]+(?:[.,][0-9０-９]+)*/g;

/**
 * 비교용 정규화. **인용 대조에만** 쓴다.
 *
 * 공백만 접는다. 더 접으면(문장부호 제거 등) 「원문에 없는 문장」 이 통과하기
 * 시작하고, 그 순간 인용 검사가 장식이 된다.
 */
const squash = (text) => String(text ?? '').replace(/\s+/g, ' ').trim();

/** 전각 숫자를 반각으로. 같은 숫자가 모양만 달라 「지어냈다」 로 걸리지 않게. */
const toHalf = (text) => String(text ?? '').replace(/[０-９]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0));

/** 그 문자열에 든 숫자들. 쉼표는 자리수 구분이라 뗀다. */
function numbersIn(text) {
  const out = new Set();
  for (const match of toHalf(text).matchAll(NUMERIC_RE)) {
    out.add(match[0].replace(/,/g, ''));
  }
  return out;
}

/**
 * 후보 하나를 본다. 통과면 `{ok:true, value}`, 아니면 `{ok:false, reason}`.
 *
 * **사유는 코드만** 돌려준다. 본문을 사유에 담으면 그 값이 metrics 를 타고 화면과
 * 로그로 나간다(인계 §metrics).
 */
export function validateCandidate(raw, { sources, approvedSet, seen }) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return { ok: false, reason: 'bad_kind' };

  const kind = String(raw.kind || '').trim();
  if (!CANDIDATE_KINDS.includes(kind)) return { ok: false, reason: 'bad_kind' };
  const form = String(raw.form || '').trim();
  if (!CANDIDATE_FORMS.includes(form)) return { ok: false, reason: 'bad_form' };

  const sourceId = String(raw.source_id || '').trim();
  const source = sources.get(sourceId);
  if (!source) return { ok: false, reason: 'unknown_source_id' };

  const quote = String(raw.evidence_quote || '');
  if (!quote.trim()) return { ok: false, reason: 'missing_quote' };

  const proposed = String(raw.proposed_text || '');
  if (!proposed.trim()) return { ok: false, reason: 'empty_proposed_text' };

  /* 인용이 **그 원문 안에** 있나. 다른 원문에서 가져왔으면 `source_id` 가 거짓이고,
   * 그 거짓이 통과하면 TYBot 이 붙이는 좌표가 엉뚱한 메시지를 가리킨다. */
  const flat = squash(quote);
  if (!squash(source.text).includes(flat)) {
    const elsewhere = [...sources.values()].some(
      (item) => item.id !== sourceId && squash(item.text).includes(flat),
    );
    return { ok: false, reason: elsewhere ? 'quote_from_other_source' : 'quote_not_in_source' };
  }

  /* `quote` 는 **그 문장 그대로**여야 한다. 아니면 abstract 로 내야 한다 —
   * 둘을 섞으면 검토자가 「이건 원문 그대로」 라고 믿고 지나간다. */
  if (form === 'quote' && squash(proposed) !== flat) {
    return { ok: false, reason: 'quote_mismatch' };
  }

  /* 숫자는 **인용에 있는 것만.** form 과 무관하다(계약 §규칙 1).
   * 여기가 뚫리면 abstract 가 「요약하면서 계산한 숫자」 를 싣게 된다. */
  const allowed = numbersIn(quote);
  for (const value of numbersIn(proposed)) {
    if (!allowed.has(value)) return { ok: false, reason: 'invented_number' };
  }

  /* 승인 본문만 그대로 베낀 후보. 새 사실이 아니라 **되돌려준 것**이고,
   * 검토자는 같은 문장을 다시 승인하게 된다(인계 §승인 본문만으로 새 후보 금지). */
  const normalizedProposed = squash(proposed);
  if (approvedSet.has(normalizedProposed)) return { ok: false, reason: 'approved_text_only' };

  if (seen.has(normalizedProposed)) return { ok: false, reason: 'duplicate_candidate' };
  seen.add(normalizedProposed);

  return {
    ok: true,
    value: {
      kind,
      form,
      source_id: sourceId,
      current_text: String(raw.current_text || ''),
      proposed_text: proposed,
      evidence_quote: quote,
      // 시각·작성자는 **요청이 준 원문의 값**을 쓴다. 모델이 적은 것을 믿지 않는다 —
      // 거기가 틀리면 검토자가 다른 발언을 보고 승인한다.
      evidence_at: source.at,
      evidence_author: source.author,
    },
  };
}

/** 모델 출력에서 JSON 객체를 꺼낸다. **코드 블록은 벗기되 추측하지 않는다.** */
export function parseModelOutput(text) {
  const body = String(text ?? '').trim();
  if (!body) return { ok: false, reason: 'invalid_json' };
  // ```json … ``` 만 벗긴다. 계약이 「붙이지 마세요」 라고 해도 붙는 날이 있다.
  const fenced = body.match(/^```(?:json)?\s*([\s\S]*?)\s*```$/);
  const source = fenced ? fenced[1] : body;
  let payload;
  try {
    payload = JSON.parse(source);
  } catch {
    return { ok: false, reason: 'invalid_json' };
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { ok: false, reason: 'invalid_json' };
  }
  if (!Array.isArray(payload.candidates)) return { ok: false, reason: 'invalid_json' };
  return { ok: true, candidates: payload.candidates };
}

/**
 * 검토 후보 생성기.
 *
 * @param {object} deps
 * @param {Function} deps.textModel  `(spec) => { generate({system, messages, signal}) }`
 *   — `llm/provider.js` 의 `createTextModel` 과 같은 모양
 * @param {Function} deps.renderPrompt `() => string` — `contract/summary-review.md`
 * @param {object}   deps.model      `{id, maxTokens, effort}`
 */
export function createReviewer({
  textModel, renderPrompt, model, now = () => Date.now(), console: log = globalThis.console,
}) {
  async function review(request, { signal } = {}) {
    const started = now();
    const metrics = {
      model: model.id, input_tokens: 0, output_tokens: 0, stop_reason: '',
      proposed_count: 0, accepted_count: 0, rejected_counts: {},
    };
    const done = (status, errorCode = '', pending = null) => ({
      kind: 'review',
      status,
      candidates: status === STATUS_OK ? accepted : [],
      errorCode,
      metrics: { ...metrics, elapsed_ms: Math.max(0, now() - started) },
      settled: pending || Promise.resolve(),
    });
    let accepted = [];

    const own = new AbortController();
    const onOuterAbort = () => own.abort();
    signal?.addEventListener?.('abort', onOuterAbort, { once: true });

    const budgetMs = Math.max(1_000, request.deadlineMs - REVIEW_DEADLINE_GUARD_MS);
    let timer = null;
    const expired = new Promise((resolve) => {
      timer = setTimeout(() => { own.abort(); resolve(TIMED_OUT); }, budgetMs);
      timer.unref?.();
    });

    const call = (async () => {
      try {
        const turn = await textModel(model).generate({
          system: [{ type: 'text', text: renderPrompt() }],
          messages: [{ role: 'user', content: userMessage(request) }],
          signal: own.signal,
        });
        metrics.input_tokens = Math.max(0, Number(turn?.usage?.input_tokens) || 0);
        metrics.output_tokens = Math.max(0, Number(turn?.usage?.output_tokens) || 0);
        metrics.stop_reason = String(turn?.stopReason || '');
        return { text: String(turn?.text || '') };
      } catch (err) {
        if (own.signal.aborted) return { aborted: true };
        log?.error?.(`[review] 모델 호출 실패: ${err?.code || err?.name || 'Error'}`);
        return { failed: 'model_error' };
      }
    })();

    let outcome;
    try {
      outcome = await Promise.race([call, expired]);
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener?.('abort', onOuterAbort);
    }

    /* 예산이 끝났다. **답은 지금 내보내되 일이 끝난 척하지 않는다** — Q&A 와 같은
     * 규칙이다(2026-10-12 리뷰 P1). 호출부는 `settled` 가 풀린 뒤 슬롯을 돌려준다. */
    if (outcome === TIMED_OUT) {
      log?.error?.(`[review] 예산 ${budgetMs}ms 를 넘겼습니다.`);
      return done(STATUS_FAILED, 'model_timeout', call);
    }
    if (outcome.aborted) return done(STATUS_FAILED, 'cancelled');
    if (outcome.failed) return done(STATUS_FAILED, outcome.failed);

    /* **잘린 출력은 `empty` 가 아니다.** 잘린 JSON 이 우연히 파싱되는 일은 드물지만,
     * `max_tokens` 로 끊긴 출력은 뒤쪽 후보가 통째로 사라진 상태다 — 그걸 「없었다」
     * 로 보고하면 검토자는 있는 것을 못 본다. */
    if (metrics.stop_reason === 'max_tokens') {
      return done(STATUS_FAILED, 'truncated_output');
    }

    const parsed = parseModelOutput(outcome.text);
    if (!parsed.ok) return done(STATUS_FAILED, parsed.reason);

    metrics.proposed_count = parsed.candidates.length;
    if (!metrics.proposed_count) {
      // **여기만이 정상 `empty` 다.** 모델을 불렀고 형식도 맞았는데 낼 것이 없었다.
      return done(STATUS_EMPTY);
    }

    const sources = new Map(request.sources.map((item) => [item.id, item]));
    const approvedSet = new Set(request.approved.map(squash));
    const seen = new Set();
    const rejected = {};
    const out = [];
    for (const item of parsed.candidates) {
      if (out.length >= request.maxCandidates) {
        rejected.over_limit = (rejected.over_limit || 0) + 1;
        continue;
      }
      const checked = validateCandidate(item, { sources, approvedSet, seen });
      if (checked.ok) out.push(checked.value);
      else rejected[checked.reason] = (rejected[checked.reason] || 0) + 1;
    }
    metrics.rejected_counts = rejected;
    metrics.accepted_count = out.length;
    accepted = out;

    if (!out.length) {
      /* 모델이 냈는데 **전부 떨어졌다.** 「없었다」 가 아니라 「쓸 수 없는 것을
       * 받았다」 이고, 사람이 봐야 하는 신호다(인계 §검증 탈락). */
      log?.error?.(
        `[review] 후보 ${metrics.proposed_count}건이 전부 탈락했습니다: `
        + Object.entries(rejected).map(([k, v]) => `${k}=${v}`).join(','),
      );
      return done(STATUS_FAILED, 'candidate_validation_failed');
    }
    return done(STATUS_OK);
  }

  return { review };
}

/**
 * 모델에게 주는 사용자 메시지.
 *
 * 원문에 **요청 안에서만 유효한 ID** 를 붙여 준다. 모델은 그 ID 로 「어느 발언에
 * 기댔나」 를 말하고, 우리는 그 ID 로 인용을 대조한다. 채널 ID·메시지 ts·파일 경로는
 * 주지 않는다 — 줄 이유가 없고, 주면 모델 출력에 섞여 나온다.
 */
export function userMessage(request) {
  const lines = ['[새 원문]'];
  for (const item of request.sources) {
    lines.push(`[${item.id}] ${item.at} ${item.author}: ${item.text}`);
  }
  lines.push('');
  if (request.approved.length) {
    lines.push('[기존 승인 요약]');
    lines.push('아래 문장은 **대조용**입니다. 여기서 새 후보를 만들지 마세요.');
    for (const text of request.approved) lines.push(`- ${text}`);
    lines.push('');
  }
  lines.push(
    `후보는 최대 ${Math.min(request.maxCandidates, MAX_CANDIDATES)}건입니다.`,
    '각 후보에 `source_id` 를 넣어 어느 원문에 기댔는지 밝히세요. 위 대괄호 안의 ID 그대로입니다.',
    'JSON 객체 하나만 출력하세요.',
  );
  return lines.join('\n');
}
