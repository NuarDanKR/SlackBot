/**
 * 일일·주간 요약 생성. Slack 게시와 자료 수집은 호출자의 책임이다.
 * 모델과 대기를 바꿔 끼워 재시도까지 외부 호출 없이 검사할 수 있다.
 */
import { createUsageCollector, attachUsage } from './usage.js';

/* ── 요약 ─────────────────────────────────────────────────────── */

/**
 * 기간 안에 올라온 문서를 요약 재료로 적는다.
 *
 * 대화 원문과 **다른 절에 둔다.** 하나는 사람이 한 말이고 하나는 문서에 적힌 것이라
 * 신뢰도가 달라서, 섞어 놓으면 요약이 "누가 그렇게 말했다" 와 "문서에 그렇게 적혀 있다"를
 * 구분하지 못한다 (documents.js 헤더의 분리 원칙과 같다).
 */
function formatDocuments(documents) {
  if (!documents?.length) return '';
  const blocks = documents.map((d) => {
    const tags = [d.kind, d.date ? `문서일 ${d.date}` : '', d.file].filter(Boolean).join(' · ');
    const lines = [`**${d.project} · ${d.title}**${tags ? ` (${tags})` : ''}`];
    if (d.gist) lines.push(`요지: ${d.gist}`);
    // 스캔본은 조문·숫자가 뭉개져 있다. 이 표시가 없으면 요약이 깨진 값을 그대로 인용한다.
    if (d.ocr) lines.push('⚠️ 스캔본 OCR — 조문·금액을 그대로 인용하지 말 것');
    if (d.text) lines.push('', d.text);
    else lines.push('(본문은 길이 상한으로 싣지 않았습니다 — 올라왔다는 사실과 요지까지만 쓰세요)');
    return lines.join('\n');
  });
  return [
    '',
    '---',
    '',
    '아래는 이 기간에 슬랙에 올라온 첨부 문서입니다. 위 대화와 달리 **문서에 적힌 것**입니다.',
    '',
    blocks.join('\n\n'),
  ].join('\n');
}

/* 요약 호출의 재시도 — **분 단위로 기다린다** (2026-08-06).
 *
 * 그날 17:30 일일 요약이 `overloaded_error` 하나로 통째로 안 나갔다. 재료 수집은 3초 만에
 * 정상으로 끝났고 요약문을 쓰는 호출 하나에서만 걸렸는데, 그 회차는 **재시도를 한 번도
 * 하지 않았다.** SDK 에 기본 재시도가 있지만(client.js 의 shouldRetry, 2회) 그것은 HTTP
 * 응답의 상태코드를 보고 정하는 것이라, 스트림이 열린 **뒤에** SSE error 이벤트로 오는
 * 과부하에는 닿지 않는다 (core/streaming.js 가 status 를 undefined 로 APIError 를 만든다 —
 * 그날 로그의 에러에 상태코드가 없는 것이 그 증거다).
 *
 * 그래서 여기서 직접 기다렸다 다시 건다. 간격이 분 단위인 것은 과부하가 초 단위로 풀리지
 * 않기 때문이다 — 그날도 13분 뒤 손으로 돌린 회차가 첫 시도에 성공했다. 요약은 사람이 앞에서
 * 기다리는 자리가 아니라 몇 분 늦는 것보다 아예 안 나가는 쪽이 나쁘다. Q&A 는 반대라
 * 이 대기를 쓰지 않는다(사람이 슬랙에서 기다리고 있고, 놓쳐도 다시 부르면 된다).
 */
const DIGEST_RETRY_WAITS_MS = [60_000, 180_000];
const defaultSleep = (ms) => new Promise((r) => setTimeout(r, ms));

export function createDigestGenerator({
  config, textModel, promptFile, logUsage, estimateCost, errLabel, sleep = defaultSleep,
}) {
  /**
   * @param {{kind:'daily'|'weekly', transcript:string, documents?:Array, windowLabel:string, stats:string}} opts
   *   documents 는 그 기간에 올라온 문서(일일 요약만 넘긴다). 없으면 대화만으로 만든다.
   */
  return async function generateDigest({ kind, transcript, documents, windowLabel, stats }) {
    const cfg = config.models[kind];
    const ledger = createUsageCollector({ estimate: estimateCost });

    const userText = [
      `[기간] ${windowLabel}`,
      `[수집 현황] ${stats}`,
      '',
      "아래는 이 기간에 오간 슬랙 대화 원문입니다. 스레드 답글은 '└' 로 표시되어 있습니다.",
      '',
      '---',
      '',
      transcript,
      formatDocuments(documents),
    ].join('\n');

    const system = [{ type: 'text', text: promptFile(kind) }];
    const messages = [{ role: 'user', content: userText }];

    /* 시도 순서 — 기본 모델로 (즉시 · 1분 뒤 · 3분 뒤), 그래도 안 되면 대체 모델로 한 번.
     * 대체 모델을 마지막에 두는 것은 과부하가 모델마다 따로 오기 때문이다. 단가와 문장이
     * 달라지므로 기본 모델을 충분히 기다려 본 뒤에만 간다. */
    const attempts = [
      [cfg.id, 0],
      ...DIGEST_RETRY_WAITS_MS.map((wait) => [cfg.id, wait]),
      [config.models.fallback, 0],
    ].filter(([model]) => model);

    let final;
    /* 몇 번 만에 나갔나. 전에는 실패한 시도가 console.warn 으로만 나갔다 systemd journal
     * 링버퍼에서 사라져서, **과부하가 잦아지고 있다는 사실이 어디에도 안 쌓였다.**
     * 통째로 안 나가기 전의 이른 신호라 로그에 남긴다 (2026-08-28).
     * errorType 은 API 어휘 그대로 둔다 — 한글로 옮기면 새 사유가 조용히 묻힌다. */
    const tried = [];
    for (let i = 0; i < attempts.length; i++) {
      const [model, wait] = attempts[i];
      const m = textModel({ ...cfg, id: model });
      if (wait) {
        console.warn(`[${kind}] ${Math.round(wait / 1000)}초 뒤 다시 시도합니다 (${i + 1}/${attempts.length})`);
        await sleep(wait);
      } else if (i) {
        console.warn(`[${kind}] 대체 모델(${model})로 시도합니다 (${i + 1}/${attempts.length})`);
      }
      try {
        final = await m.generate({ system, messages });
        ledger.record(final.raw, model);
        tried.push({ model, waitMs: wait, errorType: null });
        break;
      } catch (err) {
        ledger.failed(model);
        attachUsage(err, ledger.snapshot());
        tried.push({ model, waitMs: wait, errorType: errLabel(err) });
        // 마지막 시도이거나 다시 걸어도 같은 종류면 그대로 던진다 — scheduler 가 DM 으로 알린다.
        if (i === attempts.length - 1 || !m.retryable(err)) {
          // 던지면 반환값이 없다 — 이력을 에러에 실어야 로그가 본다.
          err.hermesAttempts = tried;
          throw err;
        }
        console.warn(`[${kind}] 요약 호출 실패 (${errLabel(err)})`);
      }
    }

    const accounting = ledger.snapshot();
    logUsage(`digest:${kind}`, final.model, final.usage);
    if (final.stopReason === 'refusal') {
      return { text: '', refused: true, attempts: tried, model: final.model,
        usage: final.usage, costUsd: accounting.costUsd, accounting };
    }

    /* **잘린 요약을 완성본처럼 내보내지 않는다.** 요약 대조(compareSummary)는 그전부터 이걸
     * 검사했는데 요약 생성 쪽에는 없었다. 여기 걸리면 문장이 중간에서 끊길 뿐 아니라 뒤쪽
     * 사업장이 통째로 빠지는데, 받는 사람에게는 그냥 짧은 요약으로 보인다 — 팀 전원이 보는
     * 자리라 「빠진 것이 없다」로 읽힌다. 던지면 scheduler 가 DM 으로 사유를 알리고 사람이
     * 다시 보낸다(`npm run digest:daily`), 즉 이미 있는 실패 경로를 그대로 탄다.
     * 이 모델은 생각도 max_tokens 안에서 쓰고(effort high), 일일 요약에는 문서 본문이
     * `limits.digestDocMaxChars` 만큼 실리므로 실제로 닿을 수 있는 한계다. 던지기로 바꾸면서
     * `models.daily.maxTokens` 를 8,000 → 16,000 으로 함께 올렸다 — 같은 날 `projects/` 접두
     * 수정으로 문서 20건이 처음 요약에 들어오기 시작했고, 상한을 그대로 두면 문서가 몰린 날
     * 요약이 통째로 안 나간다. 상한은 천장일 뿐이라 실제 출력만큼만 과금된다. (2026-08-10) */
    if (final.stopReason === 'max_tokens') {
      // 비용은 던지기 **전에** 남긴다 — 상한까지 다 쓴 회차가 가장 비싼데, 뒤에 두면 그 회차만
      // 비용 로그에서 빠져 "실패한 날은 공짜였다"로 읽힌다.
      const err = new Error(
        `${kind} 요약이 max_tokens(${cfg.maxTokens})에 걸려 잘렸습니다 — 잘린 채로는 보내지 않았습니다. ` +
          `config.json 의 \`models.${kind}.maxTokens\` 를 올린 뒤 다시 보내세요.`,
      );
      // API 가 준 type 이 없는 우리 에러다. 집계에서 `(기타)` 로 묻히지 않게 이름을 준다.
      attachUsage(err, accounting);
      err.hermesType = 'max_tokens';
      err.hermesAttempts = tried;
      throw err;
    }

    return {
      text: final.text,
      refused: false,
      // 대화 로그가 정기 발송분의 비용을 남기는 데 쓴다.
      model: final.model,
      usage: final.usage,
      costUsd: accounting.costUsd,
      accounting,
      // 몇 번 만에 나갔나. 두 번 이상일 때만 로그에 줄이 붙는다 (convo-log.js).
      attempts: tried,
    };
  }
}
