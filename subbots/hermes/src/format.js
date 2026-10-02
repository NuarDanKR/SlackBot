/**
 * 슬랙은 일반 마크다운이 아니라 mrkdwn 을 쓴다.
 * **굵게** 는 슬랙에서 그대로 별표 두 개로 보이므로 *굵게* 로 바꿔야 한다.
 * 프롬프트로도 지시하지만, 모델이 가끔 마크다운으로 새기 때문에 출력단에서 한 번 더 정리한다.
 */

function convertSegment(s) {
  return s
    // 헤딩 → 굵게
    .replace(/^#{1,6}\s+(.+)$/gm, '*$1*')
    // **굵게** → *굵게*  (별표 4개짜리 굵은기울임은 먼저 처리)
    .replace(/\*\*\*(.+?)\*\*\*/g, '*_$1_*')
    .replace(/\*\*(.+?)\*\*/g, '*$1*')
    // __기울임__ → _기울임_
    .replace(/__(.+?)__/g, '_$1_')
    // [라벨](url) → <url|라벨>
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<$2|$1>')
    // 목록 기호 통일
    .replace(/^(\s*)[-*]\s+/gm, '$1• ')
    // 수평선은 슬랙에서 의미 없음
    .replace(/^\s*---+\s*$/gm, '');
}

/** 코드블록(```) 안은 건드리지 않는다. */
export function toSlackMrkdwn(text) {
  const parts = String(text ?? '').split(/(```[\s\S]*?```)/g);
  return parts
    .map((p, i) => (i % 2 === 1 ? p : convertSegment(p)))
    .join('')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
}

/** 슬랙 메시지 상한(약 3000자)에 맞춰 문단 경계에서 자른다. */
export function chunkForSlack(text, limit = 2900) {
  const out = [];
  let rest = String(text ?? '');
  while (rest.length > limit) {
    let cut = rest.lastIndexOf('\n\n', limit);
    if (cut < limit * 0.5) cut = rest.lastIndexOf('\n', limit);
    if (cut < limit * 0.5) cut = limit;
    out.push(rest.slice(0, cut).trim());
    rest = rest.slice(cut).trim();
  }
  if (rest) out.push(rest);
  return out;
}

/** 사용량 → 대략적인 비용(USD). 로그용 참고치이며 청구서와 정확히 일치하지 않는다. */
const RATES = {
  // Opus 5.5 는 캐시 읽기가 입력 단가의 0.05배다 — 다른 모델의 0.1배와 다르다 (2026-09-28 공시 확인).
  'claude-opus-5-5': { in: 4, out: 20, read: 0.05 },
  'claude-opus-5': { in: 5, out: 25 },
  'claude-opus-4-8': { in: 5, out: 25 },
  // Sonnet 5 는 $2/$10 이 정가로 확정됐다 — 예고됐던 9/1 $3/$15 인상은 취소 (2026-09-28 공시 확인).
  'claude-sonnet-5': { in: 2, out: 10 },
  'claude-haiku-4-5': { in: 1, out: 5 },
};

/* 캐시 쓰기 단가는 **얼마나 오래 두느냐로 갈린다** — 5분은 정가의 1.25배, 1시간은 2배다.
 * 전부 2배로 계산하던 때는 Q&A 툴 루프에 5분 캐시를 붙인 뒤에도 로그가 비싸 보여서
 * 절감이 안 보였다. 응답의 usage.cache_creation 이 둘을 나눠 주므로 그대로 쓴다.
 * 그 필드가 없는 옛 응답은 예전처럼 전부 1시간으로 친다 (덜 잡는 것보다 낫다). */
const WRITE_5M = 1.25;
const WRITE_1H = 2;

function splitWrites(u) {
  const flat = u.cache_creation_input_tokens || 0;
  const c = u.cache_creation;
  if (!c) return { write5: 0, write1h: flat };
  const write5 = c.ephemeral_5m_input_tokens || 0;
  const write1h = c.ephemeral_1h_input_tokens || 0;
  // 내역의 합이 총량에 못 미치면 남는 것은 1시간으로 돌린다 — 빠뜨리지 않으려는 자리다.
  const rest = Math.max(flat - write5 - write1h, 0);
  return { write5, write1h: write1h + rest };
}

export function estimateCost(model, usage) {
  // Verified 2026-09-28: https://platform.claude.com/docs/en/about-claude/pricing
  const rateKnown = Object.hasOwn(RATES, model);
  const r = RATES[model] || RATES['claude-opus-5'];
  const u = usage || {};
  const fresh = u.input_tokens || 0;
  const { write5, write1h } = splitWrites(u);
  const read = u.cache_read_input_tokens || 0;
  const out = u.output_tokens || 0;
  const usd =
    (fresh * r.in + write5 * r.in * WRITE_5M + write1h * r.in * WRITE_1H + read * r.in * (r.read ?? 0.1) + out * r.out) / 1e6;
  return { usd, fresh, write: write5 + write1h, write5, write1h, read, out,
    rateKnown, rateBasis: rateKnown ? 'standard-global-2026-09-28' : 'legacy-opus-fallback-unverified' };
}

export function logUsage(tag, model, usage) {
  const c = estimateCost(model, usage);
  // 5분·1시간을 나눠 찍는 것은 캐시가 어디서 걸렸는지 보려는 것이다.
  // 시스템 프롬프트는 1시간, 툴 루프의 도구 결과는 5분이라 둘이 갈린다.
  const write = c.write5 && c.write1h ? `${c.write1h}+${c.write5}` : String(c.write);
  console.log(
    `[${new Date().toISOString()}] ${tag} · ${model} · ` +
      `in ${c.fresh} / cache-w ${write} / cache-r ${c.read} / out ${c.out} · ${c.rateKnown ? `약 $${c.usd.toFixed(3)}` : '단가 미상 (기본 단가 추정은 집계에서 제외)'}`,
  );
}
