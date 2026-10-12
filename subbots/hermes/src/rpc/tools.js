/**
 * RPC 답변이 모델에게 주는 도구. **전부 저쪽(TYBot ToolBox)으로 나간다.**
 *
 * ## Hermes 로컬 도구를 쓰지 않는 이유
 *
 * `src/llm/tools.js` 는 자기 아카이브를 읽는다. 그걸 여기 쓰면 **권한 판정이 두
 * 곳**이 되고, 둘은 반드시 갈라진다 — 「내부에서는 안 보이는데 외부에서는 보인다」
 * 가 그렇게 생긴다(계약 §2). RPC 에서 자료는 도구 호출로만 들어온다(인계 요구 6).
 *
 * ## 인자 이름은 **저쪽 것**이다
 *
 * `ToolBox._search/_read_channel/_read_document/_fetch_recent` 가 읽는 키를 그대로
 * 쓴다. Hermes 로컬 도구의 이름(`document`·`project`·`month`)과 다르다 — 섞어 적으면
 * 저쪽은 빈 인자로 읽고 **「못 찾았습니다」 가 정상 응답으로** 돌아온다. 에러가 아니다.
 *
 * ## 도구 실패는 글로 돌아온다
 *
 * 예산 소진·도구 오류는 `ok:true` 의 본문으로 온다(계약 §5). 모델이 도구 실패를
 * **모른 채** 답을 만드는 것이 가장 나쁘기 때문이다. 우리는 그 문장을 그대로 넘긴다.
 *
 * 반대로 `ok:false`(토큰 만료·요청 불일치)는 **계약이 깨진 것**이라 더 부르면 안 된다.
 * 그때는 「그만 찾고 지금까지 읽은 것으로 답하라」 를 돌려주고, 끝난 뒤 호출부가
 * `halted` 를 보고 상태를 정한다.
 */
import { betaTool } from '@anthropic-ai/sdk/helpers/beta/json-schema';

/** 계약이 깨졌을 때 모델에게 주는 문장. **자료가 없다고 단정하게 두지 않는다.** */
const HALT_NOTE = [
  '(자료를 더 가져올 수 없게 됐습니다. 더 찾지 말고 지금까지 읽은 것으로 답하세요.',
  ' 자료가 없다고 단정하지 말고, 어디까지 찾아봤는지 한 줄 적으세요.)',
].join('');

/**
 * @param {object} o
 * @param {{call:Function}} o.client  `createToolClient` 가 만든 창구
 * @param {string[]} o.allow          이 요청이 허용받은 도구
 * @param {object} o.terms            표시 용어(도메인 프로필). 설명문에만 쓴다
 * @param {Array} [o.trace]           호출 기록. 비용·관측용이고 **원문은 안 담는다**
 * @param {AbortSignal} [o.signal]    예산·취소. **호출 직전에 본다** — 끝난 뒤에 보면
 *   이미 저쪽 예산을 한 번 더 태운 뒤다
 */
export function buildRemoteTools({ client, allow, terms: T, trace, signal }) {
  async function run(tool, args) {
    if (signal?.aborted) return HALT_NOTE;
    const out = await client.call(tool, args);
    if (out.halted) return HALT_NOTE;
    // evidence 는 전선 검증용 메타데이터인 동시에 모델이 최종 답에 붙여야 할
    // opaque 인용 ID다. 본문만 넘기면 모델은 ID를 한 번도 보지 못해 모든 정상
    // 답변이 no_evidence 로 폐기된다. 좌표·경로는 보이지 않고 발급 ID만 덧붙인다.
    const citations = out.evidence
      .map((item) => String(item?.id || '').trim())
      .filter(Boolean)
      .map((id) => `[${id}]`);
    if (!citations.length) return out.text;
    return `${out.text}\n\n이 결과에서 인용 가능한 근거: ${citations.join(' ')}`;
  }

  // 호출 기록은 **이름과 글자 수만** 남긴다. 인자에는 질문자가 쓴 낱말이 들어가고,
  // 본문에는 사내 원문이 들어간다 — 둘 다 이 프로세스 로그에 남을 자리가 아니다.
  const wrap = (name, fn) => async (input) => {
    const text = await fn(input);
    trace?.push({ name, chars: typeof text === 'string' ? text.length : 0 });
    return text;
  };

  const all = {
    search: () => betaTool({
      name: 'search',
      description:
        '권한 안의 원문을 낱말로 찾는다. 먼저 이것부터 부른다. '
        + `한 번에 안 나오면 낱말을 바꿔 다시 부른다. ${T.place}을 알면 where 로 좁힌다.`,
      inputSchema: {
        type: 'object',
        properties: {
          query: { type: 'string', description: '검색 낱말들. 예: "대출 만기 연장 협의"' },
          where: {
            type: 'string',
            description: `채널로 좁힐 때만. 검색 결과에 적힌 **정확한 채널 이름**. 예: "${T.examples.EX_B}"`,
          },
        },
        required: ['query'],
      },
      run: wrap('search', (input) => run('search', {
        query: String(input?.query ?? ''),
        ...(input?.where ? { where: String(input.where) } : {}),
      })),
    }),

    read_channel: () => betaTool({
      name: 'read_channel',
      description: '채널 원문을 최근 것부터 읽는다. 검색으로 자리를 찾은 뒤 앞뒤 맥락이 필요할 때 쓴다.',
      inputSchema: {
        type: 'object',
        properties: {
          channel: { type: 'string', description: `채널 이름. 예: "${T.examples.EX_B}"` },
          limit: { type: 'integer', description: '몇 줄까지. 1~400, 생략하면 80.' },
        },
        required: ['channel'],
      },
      run: wrap('read_channel', (input) => run('read_channel', {
        channel: String(input?.channel ?? ''),
        ...(Number.isFinite(input?.limit) ? { limit: input.limit } : {}),
      })),
    }),

    read_document: () => betaTool({
      name: 'read_document',
      description:
        '문서·첨부 본문을 읽는다. 표와 숫자는 여기 있다 — 검색 결과에 첨부 이름이 보이면 이걸로 연다.',
      inputSchema: {
        type: 'object',
        properties: {
          name: { type: 'string', description: '문서·첨부 이름. 일부만 적어도 된다. 예: "잔금수금"' },
        },
        required: ['name'],
      },
      run: wrap('read_document', (input) => run('read_document', {
        name: String(input?.name ?? ''),
      })),
    }),

    // **기본으로 주지 않는다**(계약 §10.2). 수집 주인이 둘이 되는 것을 막는 결정이라
    // `allow` 에 명시로 들어온 요청에서만 모양을 갖춘다.
    fetch_recent_slack: () => betaTool({
      name: 'fetch_recent_slack',
      description: '아카이브에 아직 없는 최근 대화. 방금 오간 이야기를 물을 때만 쓴다.',
      inputSchema: {
        type: 'object',
        properties: {
          channel: { type: 'string', description: '채널 이름. 생략하면 질문이 온 곳.' },
          limit: { type: 'integer', description: '몇 건까지. 생략하면 20.' },
        },
      },
      run: wrap('fetch_recent_slack', (input) => run('fetch_recent_slack', {
        ...(input?.channel ? { channel: String(input.channel) } : {}),
        ...(Number.isFinite(input?.limit) ? { limit: input.limit } : {}),
      })),
    }),
  };

  // **허용된 것만 만든다.** 안 준 도구를 스키마에만 남겨 두면 모델이 그 길로 가고,
  // 저쪽은 `tool-not-allowed` 로 끊는다 — 예산 한 번과 회차 하나를 그냥 버린다.
  return allow.filter((name) => name in all).map((name) => all[name]());
}

export { HALT_NOTE };
