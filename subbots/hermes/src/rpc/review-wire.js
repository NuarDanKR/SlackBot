/**
 * 요약 검토 후보 RPC 의 계약. **Q&A 와 다른 스키마**다.
 *
 * 인계: `docs/verification/2026-10-15-hermes-review-rpc-claude-handoff.md`
 * 후보 필드 정본: `subbots/hermes/contract/summary-review.md`
 *
 * ## 왜 Q&A 스키마를 재사용하지 않나
 *
 * 질문 문자열로 작업을 추측하면 **같은 문자열이 어느 날 다른 일을 시킨다.** 그 변화는
 * 에러를 안 내고, 그때 「누가 이 프롬프트를 바꿨나」 를 되짚을 기준이 없다. 작업이
 * 다르면 스키마가 다르다 — 그래야 상대가 잘못 보낸 것을 전선에서 거절한다.
 *
 * ## 이 요청이 **안 받는** 것
 *
 * 파일 경로 · Slack 토큰 · 임의 프롬프트 · provider key · 도구 socket. 요청이 고를 수
 * 있는 것은 **어느 원문을 볼지**뿐이고, 그 원문도 TYBot 이 이미 권한을 보고 고른
 * 한 채널 사람 발언이다. 도구 토큰도 없다 — 이 작업은 **독립**이고, 아카이브·Slack 을
 * 직접 보지 않는다.
 *
 * ## 상한을 양쪽이 같은 숫자로 둔다
 *
 * 상한이 갈리면 한쪽은 보내고 다른 쪽은 거절하는 구간이 생긴다. 그 구간의 증상은
 * 「가끔 400」 이고, 원인은 숫자 하나다.
 */
import crypto from 'node:crypto';
import { MAX_REQUEST_BYTES, WireViolation } from './wire.js';

export const SCHEMA_REVIEW_REQUEST = 'tybot.hermes.review.request.v1';
export const SCHEMA_REVIEW_RESPONSE = 'tybot.hermes.review.response.v1';

/** Q&A 와 **같은 소켓, 다른 경로**. 서명 대상에 경로가 들어가므로 둘이 안 섞인다. */
export const REVIEW_ENDPOINT = '/v1/review-candidates';

/** health 가 밝히는 능력. 없으면 TYBot 은 **설정 오류로 진단한다** — 조용히 우회하지 않는다. */
export const CAPABILITY_REVIEW = 'review_candidates_v1';

/* ── 상한 (TYBot 과 같은 숫자) ────────────────────────────────────────────── */
export const MAX_REVIEW_REQUEST_BYTES = MAX_REQUEST_BYTES; // 256 KiB
export const MAX_SOURCE_ITEMS = 1_000;
export const MAX_SOURCE_CHARS = 31_500;
export const MAX_APPROVED_CHARS = 8_000;
export const MAX_CANDIDATES = 10;
export const MAX_REVIEW_DEADLINE_MS = 180_000;
export const DEFAULT_REVIEW_DEADLINE_MS = 120_000;

export const STATUS_OK = 'ok';
export const STATUS_EMPTY = 'empty';
export const STATUS_FAILED = 'failed';
export const REVIEW_STATUSES = Object.freeze([STATUS_OK, STATUS_EMPTY, STATUS_FAILED]);

/**
 * 실패 사유. **열린 문자열을 안 돌려준다** — 돌려주면 그 값이 화면·통계로 흘러
 * 들어가고, 그 안에 원문·모델 출력이 섞인다.
 */
export const ERROR_CODES = Object.freeze([
  'model_timeout',
  'model_error',
  'invalid_json',
  'truncated_output',
  'candidate_validation_failed',
  'cancelled',
  'internal_error',
]);

/** 후보가 떨어진 사유. **건수만** 보고한다(본문 금지). */
export const REJECT_REASONS = Object.freeze([
  'unknown_source_id',
  'quote_not_in_source',
  'quote_from_other_source',
  'missing_quote',
  'invented_number',
  'bad_kind',
  'bad_form',
  'quote_mismatch',
  'empty_proposed_text',
  'approved_text_only',
  'duplicate_candidate',
  'over_limit',
]);

const KINDS = Object.freeze(['number_or_schedule', 'new_issue', 'closed_issue']);
const FORMS = Object.freeze(['quote', 'abstract']);
export { KINDS as CANDIDATE_KINDS, FORMS as CANDIDATE_FORMS };

/** 요청 안에서만 유효한 원문 ID. `r1` 부터. **우리 좌표가 아니다.** */
export const SOURCE_ID_RE = /^r[0-9]{1,4}$/;
export const DIGEST_RE = /^[0-9a-f]{64}$/;

const str = (v) => (typeof v === 'string' ? v : '');

/**
 * `/v1/review-candidates` 요청 본문 → 우리가 쓰는 모양.
 *
 * **관대하게 받지 않는다.** 모르는 모양을 적당히 메우면, 보낸 쪽은 보냈다고 믿고
 * 우리는 다른 것을 본다. 그 차이는 후보가 이상해진 뒤에야 드러난다.
 */
export function parseReviewRequest(raw) {
  if (raw.length > MAX_REVIEW_REQUEST_BYTES) {
    throw new WireViolation(`요청 본문이 상한을 넘습니다(${raw.length} > ${MAX_REVIEW_REQUEST_BYTES}).`);
  }
  let payload;
  try {
    payload = JSON.parse(raw.toString('utf8'));
  } catch {
    throw new WireViolation('요청이 JSON 이 아닙니다.');
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new WireViolation('요청의 최상위가 객체가 아닙니다.');
  }
  if (payload.schema !== SCHEMA_REVIEW_REQUEST) {
    throw new WireViolation(`요청 스키마가 다릅니다: ${JSON.stringify(payload.schema)}`);
  }

  const requestId = str(payload.request_id).trim();
  const workspace = str(payload.workspace).trim();
  const sourceDigest = str(payload.source_digest).trim();
  if (!requestId) throw new WireViolation('요청에 request_id 가 없습니다.');
  if (!workspace) throw new WireViolation('요청에 workspace 가 없습니다.');
  if (!DIGEST_RE.test(sourceDigest)) {
    // 소문자 64자리 hex 만 받는다. 모양을 느슨하게 두면 대조가 되는 척만 한다.
    throw new WireViolation('source_digest 가 64자리 소문자 hex 가 아닙니다.');
  }

  const rawSources = Array.isArray(payload.sources) ? payload.sources : null;
  if (!rawSources || !rawSources.length) {
    // **빈 원문은 400 이다.** `empty` 로 접으면 「볼 것이 없었다」 와 「안 보냈다」 가
    // 같은 모양이 되고, 그건 장애를 정상 0건으로 숨기는 길이다(인계 §목적).
    throw new WireViolation('sources 가 비었습니다.');
  }
  if (rawSources.length > MAX_SOURCE_ITEMS) {
    throw new WireViolation(`원문 항목이 상한을 넘습니다(${rawSources.length} > ${MAX_SOURCE_ITEMS}).`);
  }

  const seen = new Set();
  let sourceChars = 0;
  const sources = rawSources.map((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) {
      throw new WireViolation(`sources[${index}] 가 객체가 아닙니다.`);
    }
    const id = str(item.id).trim();
    if (!SOURCE_ID_RE.test(id)) {
      throw new WireViolation(`sources[${index}].id 가 r<숫자> 모양이 아닙니다.`);
    }
    if (seen.has(id)) throw new WireViolation(`원문 ID 가 중복됩니다: ${id}`);
    seen.add(id);
    const text = str(item.text);
    if (!text.trim()) throw new WireViolation(`sources[${index}].text 가 비었습니다.`);
    sourceChars += text.length;
    return Object.freeze({
      id,
      at: str(item.at),
      author: str(item.author),
      text,
    });
  });
  if (sourceChars > MAX_SOURCE_CHARS) {
    throw new WireViolation(`원문 합계가 상한을 넘습니다(${sourceChars} > ${MAX_SOURCE_CHARS}).`);
  }

  const rawApproved = payload.approved === undefined ? [] : payload.approved;
  if (!Array.isArray(rawApproved)) throw new WireViolation('approved 가 목록이 아닙니다.');
  const approved = rawApproved.map((item, index) => {
    if (typeof item !== 'string') throw new WireViolation(`approved[${index}] 가 문자열이 아닙니다.`);
    return item;
  });
  const approvedChars = approved.reduce((sum, item) => sum + item.length, 0);
  if (approvedChars > MAX_APPROVED_CHARS) {
    throw new WireViolation(`승인 문장 합계가 상한을 넘습니다(${approvedChars} > ${MAX_APPROVED_CHARS}).`);
  }

  const limits = payload.limits && typeof payload.limits === 'object' ? payload.limits : {};
  let deadlineMs = DEFAULT_REVIEW_DEADLINE_MS;
  if (limits.deadline_ms !== undefined) {
    const value = Number(limits.deadline_ms);
    if (!Number.isFinite(value) || value <= 0 || value > MAX_REVIEW_DEADLINE_MS) {
      throw new WireViolation(`deadline_ms 가 범위 밖입니다: ${limits.deadline_ms}`);
    }
    deadlineMs = Math.floor(value);
  }
  let maxCandidates = MAX_CANDIDATES;
  if (limits.max_candidates !== undefined) {
    const value = Number(limits.max_candidates);
    if (!Number.isInteger(value) || value < 1 || value > MAX_CANDIDATES) {
      throw new WireViolation(`max_candidates 가 범위 밖입니다: ${limits.max_candidates}`);
    }
    maxCandidates = value;
  }

  return Object.freeze({
    kind: 'review',
    requestId,
    workspace,
    sourceDigest,
    sources: Object.freeze(sources),
    approved: Object.freeze(approved),
    deadlineMs,
    maxCandidates,
  });
}

/**
 * 응답 본문.
 *
 * `source_digest` 를 **요청값 그대로** 되돌려준다 — 저쪽이 「이 답이 내가 보낸 그
 * 원문에 대한 것인가」 를 좌표 하나로 확인한다. 우리가 다시 계산하지 않는다:
 * 계산하면 같은 값이 나오는 것을 증명할 뿐이고, 어긋났을 때 **어느 쪽이 틀렸는지**
 * 를 못 가린다.
 */
export function buildReviewResponse({
  requestId, workspace, sourceDigest, status, candidates = [], errorCode = '', metrics = {},
}) {
  if (!REVIEW_STATUSES.includes(status)) {
    throw new WireViolation(`모르는 상태입니다: ${status}`);
  }
  const code = String(errorCode || '');
  if (status === STATUS_FAILED) {
    if (!ERROR_CODES.includes(code)) {
      throw new WireViolation(`failed 에는 allowlist 오류 코드가 필요합니다: ${code || '(빈 값)'}`);
    }
  } else if (code) {
    throw new WireViolation('failed 가 아닌 응답에 오류 코드를 싣지 않습니다.');
  }

  const list = status === STATUS_OK ? candidates : [];
  if (status === STATUS_OK && (!list.length || list.length > MAX_CANDIDATES)) {
    throw new WireViolation(`ok 는 후보가 1~${MAX_CANDIDATES}건이어야 합니다(${list.length}).`);
  }
  if (status === STATUS_EMPTY && candidates.length) {
    throw new WireViolation('empty 에는 후보를 싣지 않습니다.');
  }
  if (status === STATUS_FAILED && candidates.length) {
    // 실패에 후보를 실으면 「반쯤 성공」 이 생기고, 그 반쯤을 쓸지 말지를 아무도 못 정한다.
    throw new WireViolation('failed 에는 후보를 싣지 않습니다.');
  }

  const payload = {
    schema: SCHEMA_REVIEW_RESPONSE,
    request_id: requestId,
    workspace,
    source_digest: sourceDigest,
    status,
    candidates: list,
    error_code: code,
    metrics: normalizeMetrics(metrics),
  };
  return Buffer.from(JSON.stringify(payload), 'utf8');
}

/** 공통 키를 **고정한다.** 호출마다 다른 키가 오면 집계가 매번 깨진다. */
export function normalizeMetrics(metrics = {}) {
  const count = (value) => Math.max(0, Number(value) || 0);
  const rejected = {};
  const source = metrics.rejected_counts && typeof metrics.rejected_counts === 'object'
    ? metrics.rejected_counts : {};
  for (const [reason, value] of Object.entries(source)) {
    // 사유도 allowlist 다 — 모델이 만든 문자열이 통계 키가 되면 키가 무한히 는다.
    if (REJECT_REASONS.includes(reason) && count(value) > 0) rejected[reason] = count(value);
  }
  return {
    model: String(metrics.model || ''),
    input_tokens: count(metrics.input_tokens),
    output_tokens: count(metrics.output_tokens),
    stop_reason: String(metrics.stop_reason || ''),
    elapsed_ms: count(metrics.elapsed_ms),
    proposed_count: count(metrics.proposed_count),
    accepted_count: count(metrics.accepted_count),
    rejected_counts: rejected,
  };
}

/** 원문 구간 digest. 시험과 진단이 같은 값을 만들 수 있게 여기 둔다. */
export function sourceDigestOf(sources) {
  const hash = crypto.createHash('sha256');
  for (const item of sources) {
    hash.update(`${item.id}\u0000${item.at}\u0000${item.author}\u0000${item.text}\u0000`);
  }
  return hash.digest('hex');
}
