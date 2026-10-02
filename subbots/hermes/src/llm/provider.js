/**
 * 모델 제공사 어댑터 — 호출 자리는 이 창구만 보고, 회사별 SDK 어휘는 이 안에서 끝낸다.
 *
 * 계약 (네 창구 — 글 반환 · JSON 반환 · 도구 왕복 · 수동 루프):
 *   createTextModel(spec, { client }) → { provider, id, generate, retryable }
 *   generate({ system, messages }) → { text, stopReason, model, usage, raw }
 *   createJsonModel(spec, { client }) → 같은 모양. generate 가 schema 를 더 받는다.
 *   generate({ system, messages, schema }) → 같은 반환
 *     - **파싱하지 않는다.** text 는 글 창구와 같은 규칙으로 뽑은 원문이고, 파싱과 그
 *       실패 문구는 호출 자리의 도메인이다 (요약 대조는 잘림을 파싱보다 먼저 판정한다).
 *     - stopReason 정규화: 'ok' | 'max_tokens' | 'refusal'. 모르는 사유는 원문 그대로 —
 *       'ok' 로 뭉개면 새 사유가 조용히 완성본으로 통과한다.
 *     - usage 는 회사 원문 그대로다. format.js 의 5분/1시간 캐시 단가 구분이 원문 필드를
 *       보므로, 여기서 정규화하면 회계가 후퇴한다 (2026-09-18 설계 검증 3번 발견).
 *     - raw 는 회사 원문 응답 — 비용 원장(usage.js record)이 message.id 중복 제거에 쓴다.
 *   retryable(err) → 다시 걸어 볼 만한 실패인가. 에러 어휘가 회사마다 달라 판정을 여기 둔다.
 *
 *   createToolSession(spec, { client }) → { provider, id, retryable, open }
 *     open(req) → 세션. 세션은 회차를 흘리고(for await), 도구 실행을 대신하고,
 *     다음 회차 대화를 잇는다. 캐시 표시와 대조 기준(previous_message_id)은
 *     **세션 안에서 끝난다** — 둘 다 회사별 어휘이고 상한도 회사별이다.
 *     id 모양 판정은 **보내는 쪽 한 곳뿐**이다: 세션이 실제로 보낸 기준은 `session.previousMessageId`
 *     로 되읽고, 회차의 `messageId` 는 회사가 준 값 그대로다(거르지 않는다).
 *
 *   createToolLoop(spec, { client }) → { provider, id, retryable, open }
 *     open(req) → 루프. next() 로 한 회차씩 부르고, continueWith(회차, 도구결과들) 로
 *     대화를 잇는다. **도구는 호출 자리가 실행한다** — 종결 도구로 끝나거나 도구 실행에
 *     자기 권한 판정이 끼어드는 자리(Clio)가 쓴다. 캐시 표시 옮기기만 창구가 맡는다.
 *     회차에 messageId·cacheMiss 는 **없다** — 이 창구를 쓰는 자리가 안 쓰는 것을
 *     지어내면 그것이 계약인 줄 읽힌다.
 *
 *     **주의 — 도구 왕복 창구의 continueWith 와 이름은 같고 인자는 반대다.**
 *       세션: continueWith(toolMessage, assistantTurn)   ← 회사가 만든 메시지가 먼저
 *       루프: continueWith(assistantTurn, toolResults)   ← 회차가 먼저
 *     세션은 회사 실행기가 만들어 준 것을 받고, 루프는 호출 자리가 만든 것을 받는다.
 *     타사 구현을 쓸 때 이 둘을 바꿔 넣으면 **에러 없이 대화가 뒤집힌다.**
 *
 * effort 는 **Anthropic 어휘다.** 타사에 옮길 때 손실 변환이 된다 — 저쪽은 등급이 없거나
 * 개수가 다르거나(예: 추론 토큰 예산 같은 연속값) 아예 다른 축이다. 그래서 네 창구 모두
 * spec.effort 를 **그대로 얹기만 하고 해석하지 않는다.** 타사 구현을 더하는 날 그 회사
 * 어휘로 옮기는 표를 그 provider 안에 두고, 옮기면서 무엇이 뭉개지는지를 거기 적는다 —
 * 이 자리에서 공통 등급으로 미리 뭉개면 Anthropic 쪽 정보까지 함께 잃는다.
 *
 * client 는 지연 생성 함수로 주입받는다 — 검사(check-llm-provider.js 등)가 가짜를 꽂는 자리다.
 *
 * **이 파일은 Clio 저장소와 바이트가 같아야 한다** (clio/src/llm/provider.js). 고쳤으면
 * 양쪽에 같이 옮기고 `node scripts/verify-shared-with-clio.js` 로 확인한다 — 상시 검사에는
 * 안 들어간다(Clio 가 없는 설치가 대부분이라). 자동으로 지키는 자리는 Clio 의 npm test 다.
 */
import { APIConnectionError } from '@anthropic-ai/sdk';

const STOP_MAP = { end_turn: 'ok', max_tokens: 'max_tokens', refusal: 'refusal' };

/** 다시 걸어 볼 만한 에러인가 — 과부하·레이트리밋·5xx·연결 끊김. 잘못된 요청(4xx)은 다시 걸어도 같다.
 * (digest.js 의 isRetryableApiError 를 그대로 옮겼다 — 판정은 이제 여기 한 곳뿐이다) */
function anthropicRetryable(err) {
  if (err instanceof APIConnectionError) return true;
  // APIError.type 은 응답 본문의 error.type 이다. 스트림 안에서 온 에러는 status 가 없어 이쪽으로만 잡힌다.
  if (['overloaded_error', 'api_error', 'rate_limit_error'].includes(err?.type)) return true;
  const status = err?.status;
  return typeof status === 'number' && (status === 408 || status === 409 || status === 429 || status >= 500);
}

/** 회사 원문 응답 → 창구 공통 반환. 두 창구가 같은 규칙을 쓰게 한 자리다 —
 * 갈라 두면 한쪽만 고쳐지고 그 차이는 에러 없이 흐른다. */
function normalize(raw) {
  return {
    text: (raw.content || []).filter((b) => b.type === 'text').map((b) => b.text).join('\n').trim(),
    stopReason: STOP_MAP[raw.stop_reason] ?? raw.stop_reason,
    model: raw.model,
    usage: raw.usage,
    raw,
  };
}

function anthropicTextModel(spec, client) {
  return {
    provider: 'anthropic',
    id: spec.id,
    retryable: anthropicRetryable,
    async generate({ system, messages }) {
      // 출력이 길어질 수 있어 스트리밍으로 받는다 (비스트리밍은 HTTP 타임아웃 위험).
      const raw = await client().messages.stream({
        model: spec.id,
        max_tokens: spec.maxTokens,
        output_config: { effort: spec.effort },
        system,
        messages,
      }).finalMessage();
      return normalize(raw);
    },
  };
}

function anthropicJsonModel(spec, client) {
  return {
    provider: 'anthropic',
    id: spec.id,
    retryable: anthropicRetryable,
    async generate({ system, messages, schema }) {
      // 스트리밍을 쓰지 않는다 — 응답이 스키마로 묶인 짧은 JSON 이고, 옛 호출도 create 였다.
      // format 으로 형식을 API 가 보장한다. 프롬프트로 부탁하던 시절에는 그 부탁이 깨지면
      // 그 채널 회차가 통째로 실패했다 (summary-check.js 주석 참조).
      const raw = await client().messages.create({
        model: spec.id,
        max_tokens: spec.maxTokens,
        output_config: { effort: spec.effort, format: { type: 'json_schema', schema } },
        system,
        messages,
      });
      return normalize(raw);
    },
  };
}

/* ── 도구 왕복의 캐시 표시 ──────────────────────────────────────────
 *
 * 툴 루프는 회차마다 대화 전체를 다시 보낸다. 표시를 대화 맨 끝으로 옮기면 이미 보낸
 * 부분이 다음 회차에 읽기(1/10 단가)가 되고 새로 늘어난 것만 정가로 나간다. 답변 내용은
 * 바뀌지 않는다 — 같은 것을 같은 순서로 보내고 어디까지 캐시할지만 알려주는 것이다.
 *
 * TTL 을 5분으로 두는 이유: 툴 루프는 몇 초~1분 안에 끝난다. 1시간짜리는 쓰기가 2배라
 * 여기서는 손해다. 시스템 블록의 1시간은 호출 자리가 건다 — 그쪽은 질문과 질문 사이에
 * 재사용돼야 해서 계산이 반대다. */
// 이 객체 하나를 두 창구(withCacheTail·continueWith)가 모든 표시 자리에 그대로 참조로
// 꽂는다. freeze 로 막아 두지 않으면 언젠가 `block.cache_control.ttl = '1h'` 같은 제자리
// 수정이 양쪽 저장소의 모든 표시로 조용히 번진다 — 직렬화된 바이트는 지금과 같지만
// 사고는 나중에 난다. 호스트 블록에서 `delete marked.cache_control` 로 이 값을 떼는 것은
// 이 상수가 아니라 호스트 쪽 속성을 지우는 것이라 freeze 와 무관하게 그대로 된다.
const TOOL_CACHE_CONTROL = Object.freeze({ type: 'ephemeral' }); // ttl 생략 = 5분

function withCacheTail(message) {
  const content = message.content;
  if (!Array.isArray(content) || !content.length) return message;
  return {
    ...message,
    content: content.map((b, i) =>
      i === content.length - 1 ? { ...b, cache_control: TOOL_CACHE_CONTROL } : b,
    ),
  };
}

function hasCacheMark(m) {
  return Array.isArray(m?.content) && m.content.some((b) => b?.cache_control);
}

function withoutCacheMarks(message) {
  return { ...message, content: message.content.map(({ cache_control, ...b }) => b) };
}

/** 앞선 표시는 **가장 최근 것 하나만** 남긴다. 표시는 한 요청에 4개가 상한이라 계속 쌓으면
 * API 가 400 을 낸다. 시스템 1개 + 대화 2개 = 3개로 여유를 둔다. 표시를 떼어도 글자는
 * 그대로라 이미 저장된 앞부분은 계속 맞는다 (캐시는 가장 긴 일치 지점을 알아서 찾는다). */
function rollCacheMarks(messages) {
  const out = [...messages];
  let seen = 0;
  for (let i = out.length - 1; i >= 0; i--) {
    if (!hasCacheMark(out[i])) continue;
    seen += 1;
    if (seen > 1) out[i] = withoutCacheMarks(out[i]);
  }
  return out;
}

/** 다음 요청의 대조 기준으로 쓸 수 있는 값인가. 모양이 틀린 값(빈 문자열 포함)은 400 이고,
 * 모양이 맞는데 없는 id 는 정상 응답에 previous_message_not_found 로 돌아온다(실측).
 * 그래서 낡아서 못 찾는 것은 안전하지만 지어낸 값은 안전하지 않다. */
function anthropicMessageId(id) {
  return typeof id === 'string' && id.startsWith('msg_') ? id : null;
}

function anthropicToolSession(spec, client) {
  return {
    provider: 'anthropic',
    id: spec.id,
    retryable: anthropicRetryable,
    open({ system, messages, tools, maxIterations, previousMessageId }) {
      /* 모양 판정은 **보내는 쪽에만** 둔다. 회차가 내는 messageId 는 회사가 준 id 그대로다 —
       * 호출 자리는 그 값을 「다음 질문의 기준 후보」로 들고 있는데, 받을 때 걸러 버리면
       * 모양이 틀린 id 가 앞서 들고 있던 멀쩡한 값을 **안 덮어** 다음 질문이 낡은 기준을
       * 보내게 된다 (2026-09-18 Fix round 1: 질문 셋짜리 대조에서 실제로 갈렸다). */
      const baseline = anthropicMessageId(previousMessageId);
      const runner = client().beta.messages.toolRunner({
        model: spec.id,
        max_tokens: spec.maxTokens,
        output_config: { effort: spec.effort },
        system,
        messages,
        tools,
        max_iterations: maxIterations,
        // 캐시가 **왜** 안 맞았는지를 응답에 적어 달라는 요청이다 (diagnostics.cache_miss_reason).
        // betas 를 빼면 400 "Extra inputs are not permitted" 이 난다 — 베타 기능이라 플래그가 필요하다.
        betas: ['cache-diagnosis-2026-04-07'],
        diagnostics: { previous_message_id: baseline },
      });
      return {
        // 이 세션이 **실제로 보낸** 대조 기준. 호출 자리는 「기준이 있었나」를 답에 실을 때
        // 이 값을 본다 — 같은 모양 규칙을 호출 자리에 한 벌 더 두지 않기 위해서다.
        previousMessageId: baseline,
        [Symbol.asyncIterator]() {
          const inner = runner[Symbol.asyncIterator]();
          return {
            async next() {
              const step = await inner.next();
              if (step.done) return step;
              const raw = step.value;
              const miss = raw.diagnostics?.cache_miss_reason;
              return { done: false, value: {
                ...normalize(raw),
                messageId: raw.id ?? null,
                toolUses: (raw.content || [])
                  .filter((b) => b.type === 'tool_use')
                  .map((b) => ({ id: b.id, name: b.name, input: b.input })),
                cacheMiss: miss ? { type: miss.type, tokens: miss.cache_missed_input_tokens || 0 } : null,
              } };
            },
            return: (v) => inner.return?.(v) ?? Promise.resolve({ done: true, value: v }),
          };
        },
        generateToolResponse: () => runner.generateToolResponse(),
        continueWith(toolMessage, assistantTurn) {
          const raw = assistantTurn.raw;
          runner.setMessagesParams((p) => ({
            ...p,
            messages: [
              ...rollCacheMarks(p.messages),
              { role: raw.role, content: raw.content },
              withCacheTail(toolMessage),
            ],
            // 다음 회차가 이 응답과 대조되게 한다.
            diagnostics: { previous_message_id: raw.id ?? null },
          }));
        },
      };
    },
  };
}

/* ── 수동 루프의 캐시 표시 ──────────────────────────────────────────
 *
 * 도구 왕복 창구(rollCacheMarks)는 표시를 「가장 최근 하나만」 남기려고 대화를 훑는다.
 * 수동 루프는 그럴 필요가 없다 — 표시를 다는 자리가 이 함수 하나뿐이라, 직전에 단 블록을
 * 손에 들고 있다가 지우면 된다. 훑지 않으므로 호출 자리가 직접 넣은 표시(시스템 블록 등)를
 * 건드리지 않는다. 상한 4개 중 시스템 1개 + 이력 1개만 쓴다. */
function anthropicToolLoop(spec, client) {
  return {
    provider: 'anthropic',
    id: spec.id,
    retryable: anthropicRetryable,
    open({ system, messages, tools }) {
      const convo = [...messages];
      let marked = null; // 직전 회차에 표시를 단 블록
      return {
        async next() {
          // 스트리밍으로 받는다 — 출력 상한이 크면 비스트리밍은 HTTP 타임아웃이 난다.
          const raw = await client().messages.stream({
            model: spec.id,
            max_tokens: spec.maxTokens,
            output_config: { effort: spec.effort },
            system,
            tools,
            messages: convo,
          }).finalMessage();
          return {
            ...normalize(raw),
            toolUses: (raw.content || [])
              .filter((b) => b.type === 'tool_use')
              .map((b) => ({ id: b.id, name: b.name, input: b.input })),
          };
        },
        /* **빈 toolResults 를 막지 않는다 — 일부러 그렇다.** 막고 싶은 손이 여기서 가장
         * 많이 움직인다: `if (!toolResults.length) return;` 한 줄이면 되니까. 그런데 옛
         * Clio 코드(agent.js:554-556)는 그 경우 `undefined.cache_control` 에서 TypeError 로
         * **죽는다.** 가드를 넣으면 죽던 것이 조용한 진행으로 바뀌고, 도구 결과 없이
         * assistant 로 끝난 기형 대화가 다음 요청으로 나간다 — 크래시가 400 으로 바뀔 뿐
         * 나아지는 것이 없고, 무엇보다 **이 계획이 증명하겠다는 무변화가 깨진다.**
         * 고칠 값어치가 있는 자리지만 그것은 이전이 끝난 뒤 눈 뜨고 하는 별건이다. */
        continueWith(assistantTurn, toolResults) {
          const raw = assistantTurn.raw;
          convo.push({ role: raw.role, content: raw.content });
          if (marked) delete marked.cache_control;
          marked = toolResults[toolResults.length - 1];
          marked.cache_control = TOOL_CACHE_CONTROL;
          convo.push({ role: 'user', content: toolResults });
        },
      };
    },
  };
}

// 설정 점검(check-setup.js [2/6]·clio-config.test.js)이 「오타 낸 provider 값을 배포 전에
// 잡는다」에 쓰려고 내보낸다 — 값을 여기 또 옮겨 적으면 새 provider 를 더하는 날 한쪽만
// 고쳐지고 점검은 낡은 목록으로 계속 돈다.
export const PROVIDERS = { anthropic: { text: anthropicTextModel, json: anthropicJsonModel, tool: anthropicToolSession, loop: anthropicToolLoop } };

function createModel(kind, spec, client) {
  /* 칸이 **아예 없는 것**만 생략으로 친다. `||` 로 두면 빈 문자열·null 이 생략과 한 갈래가
   * 되어 조용히 기본값으로 떨어진다 — 설정에 `"provider": ""` 를 자리표시자로 남기는 흔한
   * 실수가 약속된 「던지기」 대신 통과하고, 이 칸이 있는 이유(오타를 막는 것)와 어긋난다. */
  const provider = spec.provider === undefined ? 'anthropic' : spec.provider;
  const make = PROVIDERS[provider]?.[kind];
  // 값을 따옴표째 보인다 — 빈 문자열·공백이 「없음」처럼 읽히면 원인을 못 찾는다.
  if (!make) throw new Error(`모르는 모델 제공사입니다: ${JSON.stringify(provider)} — provider 설정값을 확인하세요 (쓸 수 있는 값: ${Object.keys(PROVIDERS).join(', ')}).`);
  return make(spec, client);
}

export function createTextModel(spec, { client }) {
  return createModel('text', spec, client);
}

export function createJsonModel(spec, { client }) {
  return createModel('json', spec, client);
}

export function createToolSession(spec, { client }) {
  return createModel('tool', spec, client);
}

export function createToolLoop(spec, { client }) {
  return createModel('loop', spec, client);
}
