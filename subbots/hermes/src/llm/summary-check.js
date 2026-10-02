import { createUsageCollector, attachUsage } from './usage.js';
/**
 * 상단 요약 대조. config·모델·프롬프트·사용량 기록은 호출자가 연결한다.
 * import만으로 실자료나 API 키를 읽지 않는다.
 */
/* ── 상단 요약 대조 (ingest) ───────────────────────────────────── */

/* 요약 대조 응답의 모양을 **API 수준에서 못박는다**(structured outputs).
 *
 * 전에는 "JSON 만 출력합니다" 라고 프롬프트로 부탁하고 코드펜스를 벗겨 파싱했다. 부탁은
 * 지켜지지 않을 때가 있고, 그러면 그 채널 회차가 통째로 실패했다 (2026-08-10 #사업장거:
 * 모델이 JSON 대신 `-` 로 시작하는 목록을 돌려줬다). 이 스키마를 주면 API 가 형식을
 * 보장하므로 그 실패가 사라진다.
 *
 * `type` 을 세 가지로 못박는 것도 같은 이유다 — 프롬프트가 「잡을 것은 세 가지뿐」이라고
 * 적어 두었으니, 지키기를 바라는 대신 지켜지게 한다. */
const SUMMARY_CHECK_SCHEMA = {
  type: 'object',
  properties: {
    findings: {
      type: 'array',
      items: {
        type: 'object',
        properties: {
          type: { type: 'string', enum: ['숫자·일정', '새 쟁점', '끝난 쟁점'] },
          where: { type: 'string' },
          was: { type: 'string' },
          now: { type: 'string' },
          evidence: { type: 'string' },
        },
        required: ['type', 'where', 'was', 'now', 'evidence'],
        additionalProperties: false,
      },
    },
  },
  required: ['findings'],
  additionalProperties: false,
};


export function createSummaryChecker({ config, jsonModel, promptFile, logUsage, estimateCost }) {
  /**
   * 새 메시지가 채널 상단 요약과 어긋나는지 찾는다. **고치지 않고 후보만** 돌려준다.
   *
   * 자동 반영에서 모델을 쓰는 유일한 자리다. 나머지(수집·삽입·검사)는 전부 규칙이라
   * 틀릴 여지가 없지만, 요약 대조만은 판단이다. 그래서 결과를 바로 반영하지 않고
   * 사람에게 DM 으로 보낸다.
   *
   * @param {{channel:string, summary:string, transcript:string}} opts
   * @returns {Promise<Array<{type:string, where:string, was:string, now:string, evidence:string}>>}
   */
  return async function compareSummary({ channel, summary, transcript, onUsage }) {
    const cfg = config.models.summaryCheck || config.models.qa;

    const userText = [
      `[채널] #${channel}`,
      '',
      '## 현재 상단 요약',
      '',
      summary || '(요약 없음)',
      '',
      '## 이번에 새로 들어온 메시지',
      '',
      transcript,
    ].join('\n');

    const system = [{ type: 'text', text: promptFile('summary-check') }];
    const messages = [{ role: 'user', content: userText }];

    const ledger = createUsageCollector({ estimate: estimateCost });
    // 창구 만들기는 try 밖이다 (digest.js 와 같은 자리). config 의 provider 오타는 여기서
    // 던지므로 ledger.failed·onUsage 를 안 거치고 나간다 — 옛 코드에 없던 유일한 갈래이고,
    // 시도마다 같은 cfg 라 첫 호출 전에만 날 수 있는 설정 오류다.
    const m = jsonModel(cfg);
    let final;
    try {
      final = await m.generate({ system, messages, schema: SUMMARY_CHECK_SCHEMA });
    } catch (err) {
      ledger.failed(cfg.id);
      const accounting = ledger.snapshot();
      attachUsage(err, accounting);
      onUsage?.(accounting);
      throw err;
    }
    ledger.record(final.raw, cfg.id);
    const accounting = ledger.snapshot();
    onUsage?.(accounting);
    logUsage('summary-check', final.model, final.usage);
    if (final.stopReason === 'refusal') return [];
    try {

    /* 형식은 스키마가 보장하지만 **길이는 보장하지 않는다.** max_tokens 에 걸려 잘리면 JSON 도
     * 잘린 채로 온다. 그때 파싱 에러만 올리면 사유가 「JSON 이 이상하다」로 보여 엉뚱한 곳을
     * 뒤지게 되므로, 잘렸다는 사실을 그대로 말한다. 이 모델은 생각도 max_tokens 안에서
     * 쓰므로(Opus 5 는 기본으로 생각한다) 대조할 원문이 길면 여기 걸릴 수 있다. */
    if (final.stopReason === 'max_tokens') {
      throw new Error(
        `요약 대조 응답이 max_tokens(${cfg.maxTokens})에 걸려 잘렸습니다 — ` +
          'config.json 의 `models.summaryCheck.maxTokens` 를 올려야 합니다.',
      );
    }

    const text = final.text;

    /* 파싱 실패는 "어긋난 것 없음"이 아니라 **조용한 누락**이므로, 빈 배열 대신 예외를 던져
     * 호출부가 DM 에 남기게 한다. 스키마를 준 뒤로는 여기 오면 안 되지만, 오면 무엇이
     * 왔는지 보여야 다음 사람이 원인을 찾는다. */
    let parsed;
    try {
      parsed = JSON.parse(text);
    } catch (e) {
      throw new Error(`요약 대조 응답이 JSON 이 아닙니다 (${e.message}): ${text.slice(0, 200)}`);
    }
    return Array.isArray(parsed.findings) ? parsed.findings : [];
    } catch (err) {
      throw attachUsage(err, accounting);
    }
  }
}
