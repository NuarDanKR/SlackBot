/**
 * v3 전문봇 계약의 **Node 쪽 거울**. 순수 모듈 — 소켓도 모델도 여기 없다.
 *
 * 기준은 우리 구현이 아니라 저쪽 코드다:
 *   `src/tybot/specialist_wire_v3.py` · `src/tybot/specialist_wire.py`
 *   설계: `docs/design/specialist-v3-tool-contract.md`
 *
 * ## 왜 베껴 적는가
 *
 * 한 벌로 두면 좋겠지만 런타임이 다르다(Python ↔ Node). 그래서 **값을 베끼되
 * 베낀 자리를 한 곳에 모은다.** 흩어 두면 한쪽만 고쳐지는 날이 오고, 그때는
 * 서명이 맞는데 내용이 어긋나는 — 가장 찾기 어려운 — 상태가 된다.
 *
 * 시험(`scripts/check-rpc-wire.js`)이 **Python 원본을 읽어** 상수를 대조한다.
 * 저쪽이 바뀌면 여기서 빨개진다.
 *
 * ## 서명을 바이트로 맞춘다
 *
 * `canonical()` 이 만드는 문자열은 저쪽 `specialist_wire.canonical` 과 **한 바이트도
 * 달라선 안 된다.** 다르면 증상이 「401」 하나뿐이라, 시계가 틀렸는지 키가 틀렸는지
 * 줄바꿈이 틀렸는지 구별할 수 없다.
 */
import crypto from 'node:crypto';

/* ── 스키마 이름 ────────────────────────────────────────────────────────── */
export const SCHEMA_REQUEST = 'tybot.specialist.request.v3';
export const SCHEMA_TOOL_CALL = 'tybot.specialist.tool.v3';
export const SCHEMA_TOOL_RESULT = 'tybot.specialist.tool_result.v3';
export const SCHEMA_RESPONSE = 'tybot.specialist.response.v3';

/* ── 경로 ──────────────────────────────────────────────────────────────── */
export const ANSWER_ENDPOINT = '/v3/answer';
export const CANCEL_ENDPOINT = '/v3/cancel';
export const TOOL_ENDPOINT = '/v3/tools/call';
export const TOOL_SOCKET_PATH = '/run/tybot/specialist-tools.sock';
/** v2 가 이미 정한 자리다. 새로 만들지 않는다 — 만들면 저쪽 `health()` 가 못 찾는다. */
export const HEALTH_ENDPOINT = '/v1/health';
export const CONTRACT_VERSION = 'v2'; // health 응답의 `contract_version` (v2 wire 소유)

/* ── 상한 ──────────────────────────────────────────────────────────────── */
export const MAX_REQUEST_BYTES = 256 * 1024;
export const MAX_RESPONSE_BYTES = 64 * 1024;
export const MAX_TOOL_RESULT_BYTES = 32 * 1024;
export const MAX_OUTPUT_CHARS = 3_000;
export const DEADLINE_MS = 90_000;
export const HEALTH_DEADLINE_MS = 2_000;
export const CLOCK_SKEW_SECONDS = 30;

/* ── 도구·상태 ─────────────────────────────────────────────────────────── */
export const TOOLS = Object.freeze(['search', 'read_channel', 'read_document', 'fetch_recent_slack']);
export const DEFAULT_TOOLS = Object.freeze(['search', 'read_channel', 'read_document']);

export const STATUS_ANSWERED = 'answered';
export const STATUS_NO_EVIDENCE = 'no_evidence';
export const STATUS_REFUSED = 'refused';
export const STATUS_FAILED = 'failed';
export const STATUSES = Object.freeze([
  STATUS_ANSWERED, STATUS_NO_EVIDENCE, STATUS_REFUSED, STATUS_FAILED,
]);

/** 근거 ID 모양. 저쪽 `_EVIDENCE_ID_RE` 와 같다 — broker 가 `e1` 부터 센다. */
export const EVIDENCE_ID_RE = /^e[0-9]{1,4}$/;

/**
 * 응답 본문에 있으면 **우리가 먼저 버린다.**
 *
 * v2 는 이 목록을 `validate_response` 에서 본다. v3 `validate_response` 에는 아직
 * 없다 — 그래서 우리가 `출처:` 를 붙여 보내면 저쪽은 그대로 받고, 사람은 **우리가
 * 지어낸 출처**를 사실로 본다(절대 원칙 2: 출처는 마스터가 붙인다).
 *
 * 저쪽 계약에 이 검사를 넣자는 제안은 인계 문서에 남긴다. 그 전까지는 여기가
 * 유일한 관문이므로 **지나가게 두지 않는다.**
 */
export const FORBIDDEN = Object.freeze([
  ['출처:', '출처는 마스터 봇만 부착할 수 있습니다.'],
  ['slack.com/archives/', '응답에 Slack 링크가 있습니다.'],
  ['file://', '응답에 파일 경로가 있습니다.'],
  ['<script', '응답에 스크립트가 있습니다.'],
  ['<iframe', '응답에 프레임이 있습니다.'],
]);

/** 계약 위반. 서버는 이것을 **상태 코드**로 접어서 답한다 — 문구를 내보내지 않는다. */
export class WireViolation extends Error {
  constructor(message) {
    super(message);
    this.name = 'WireViolation';
    this.code = 'hermes_wire_violation';
  }
}

/* ── 서명 ──────────────────────────────────────────────────────────────── */

export function sha256Hex(buf) {
  return crypto.createHash('sha256').update(buf).digest('hex');
}

/**
 * 서명 대상. **본문 해시까지 넣는다** — 넣지 않으면 같은 서명으로 다른 본문을
 * 보낼 수 있고, 그건 서명이 아니라 통행증이다.
 *
 * 저쪽: `specialist_wire.canonical`.
 */
export function canonical({ method, path, timestamp, nonce, body }) {
  return Buffer.from(
    [String(method).toUpperCase(), path, String(timestamp), nonce, sha256Hex(body)].join('\n'),
    'utf8',
  );
}

export function sign(secret, message) {
  return crypto.createHmac('sha256', secret).update(message).digest('hex');
}

/**
 * **길이가 달라도 새지 않게** 비교한다.
 *
 * `crypto.timingSafeEqual` 은 길이가 다르면 던지므로, 그 전에 길이를 재면 길이가
 * 새어 나간다. 양쪽을 다시 해시해 **항상 32바이트**로 만든 뒤 비교한다.
 */
export function safeEqualHex(a, b) {
  const left = crypto.createHash('sha256').update(String(a ?? ''), 'utf8').digest();
  const right = crypto.createHash('sha256').update(String(b ?? ''), 'utf8').digest();
  return crypto.timingSafeEqual(left, right);
}

/** 요청 헤더. 저쪽 `signature_headers` 와 **헤더 이름까지** 같다. */
export function signatureHeaders(secret, { method = 'POST', path, body, now, nonce } = {}) {
  const stamp = now ?? Math.floor(Date.now() / 1000);
  const token = nonce ?? crypto.randomBytes(16).toString('hex');
  return {
    'X-TYBot-Timestamp': String(stamp),
    'X-TYBot-Nonce': token,
    'X-TYBot-Signature': sign(secret, canonical({ method, path, timestamp: stamp, nonce: token, body })),
    'Content-Type': 'application/json; charset=utf-8',
  };
}

/**
 * 우리 응답에 붙이는 서명. 저쪽 `verify_response_signature` 가 이것을 본다.
 *
 * `request_id` 를 묶는다 — 묶지 않으면 **다른 요청의 답**을 그대로 중계해도
 * 서명이 맞는다.
 */
export function responseSignature(secret, { requestId, body }) {
  return sign(secret, Buffer.from(`${requestId}\n${sha256Hex(body)}`, 'utf8'));
}

/* ── 요청 읽기 ─────────────────────────────────────────────────────────── */

const str = (v) => (typeof v === 'string' ? v : '');

/**
 * `/v3/answer` 요청 본문 → 우리가 쓰는 모양.
 *
 * **관대하게 받지 않는다.** 모르는 모양을 적당히 메우면 계약이 무엇인지가 두 코드에서
 * 서서히 갈라지고, 갈라진 뒤에는 어느 쪽이 맞는지 판정할 근거가 없다.
 */
export function parseAnswerRequest(raw) {
  if (raw.length > MAX_REQUEST_BYTES) {
    throw new WireViolation(`요청 본문이 상한을 넘습니다(${raw.length} > ${MAX_REQUEST_BYTES}).`);
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
  if (payload.schema !== SCHEMA_REQUEST) {
    throw new WireViolation(`요청 스키마가 다릅니다: ${JSON.stringify(payload.schema)}`);
  }

  const requestId = str(payload.request_id).trim();
  const workspace = str(payload.workspace).trim();
  const authorizationId = str(payload.authorization_id).trim();
  const question = str(payload.question).trim();
  for (const [name, value] of [
    ['request_id', requestId], ['workspace', workspace],
    ['authorization_id', authorizationId], ['question', question],
  ]) {
    if (!value) throw new WireViolation(`요청에 ${name} 이 없습니다.`);
  }

  const tools = payload.tools;
  if (!tools || typeof tools !== 'object') throw new WireViolation('요청에 tools 가 없습니다.');
  const token = str(tools.token).trim();
  // 토큰이 없으면 도구를 못 쓰고, v3 는 씨앗 근거를 요구하지 않는다 — 즉 **근거 없이
  // 답하라는 요청**이 된다. 그건 가장 나쁜 조합이라 받지 않는다(저쪽 `Request.__post_init__`).
  if (!token) throw new WireViolation('도구 토큰이 없는 v3 요청은 받지 않습니다.');

  const allow = Array.isArray(tools.allow) ? tools.allow.map(str) : [];
  if (!allow.length) throw new WireViolation('허용 도구가 비어 있습니다.');
  const unknown = allow.filter((name) => !TOOLS.includes(name));
  if (unknown.length) throw new WireViolation(`모르는 도구를 허용했습니다: ${unknown.join(', ')}`);

  const endpoint = str(tools.endpoint) || TOOL_ENDPOINT;
  if (endpoint !== TOOL_ENDPOINT) {
    throw new WireViolation(`도구 엔드포인트가 계약과 다릅니다: ${endpoint}`);
  }
  // 소켓 경로는 **요청에서 받지 않는다.** 받으면 그 값이 곧 「아무 소켓이나 부르게
  // 하는 권한」 이 된다. 보내 오면 계약값과 같은지만 보고, 실제로 쓰는 것은 우리 설정이다.
  const socket = str(tools.socket);
  if (socket && socket !== TOOL_SOCKET_PATH) {
    throw new WireViolation(`도구 소켓 경로가 계약과 다릅니다: ${socket}`);
  }

  const limits = payload.limits && typeof payload.limits === 'object' ? payload.limits : {};
  const deadlineMs = Number.isFinite(limits.deadline_ms) && limits.deadline_ms > 0
    ? Math.floor(limits.deadline_ms) : DEADLINE_MS;
  const maxOutputChars = Number.isFinite(limits.max_output_chars) && limits.max_output_chars > 0
    ? Math.floor(limits.max_output_chars) : MAX_OUTPUT_CHARS;

  const evidence = Array.isArray(payload.evidence)
    ? payload.evidence
      .filter((e) => e && typeof e === 'object')
      .map((e) => ({ id: str(e.id), text: str(e.text) }))
      .filter((e) => e.id)
    : [];
  const ids = evidence.map((e) => e.id);
  if (new Set(ids).size !== ids.length) throw new WireViolation('근거 식별자가 중복됩니다.');

  const followUp = Array.isArray(payload.follow_up)
    ? payload.follow_up
      .filter((f) => f && typeof f === 'object')
      .map((f) => ({ id: str(f.id), locator: str(f.locator) }))
      .filter((f) => f.id && f.locator)
    : [];

  const budget = tools.budget && typeof tools.budget === 'object' ? tools.budget : {};

  return Object.freeze({
    requestId, workspace, authorizationId, question, token,
    allow: Object.freeze(allow),
    evidence: Object.freeze(evidence),
    followUp: Object.freeze(followUp),
    deadlineMs,
    maxOutputChars,
    budget: Object.freeze({
      maxCalls: Number.isFinite(budget.max_calls) ? budget.max_calls : 12,
      maxPerTool: Number.isFinite(budget.max_per_tool) ? budget.max_per_tool : 4,
      maxChars: Number.isFinite(budget.max_chars) ? budget.max_chars : 60_000,
      maxSeconds: Number.isFinite(budget.max_seconds) ? budget.max_seconds : 45,
    }),
  });
}

/** `/v3/cancel` 본문 → `request_id`. */
export function parseCancelRequest(raw) {
  let payload;
  try {
    payload = JSON.parse(raw.toString('utf8'));
  } catch {
    throw new WireViolation('취소 요청이 JSON 이 아닙니다.');
  }
  const requestId = str(payload?.request_id).trim();
  if (!requestId) throw new WireViolation('취소 요청에 request_id 가 없습니다.');
  return requestId;
}

/* ── 응답 만들기 ───────────────────────────────────────────────────────── */

/**
 * `/v3/answer` 응답 본문.
 *
 * **여기서 한 번 더 거른다.** 보내기 직전이 우리가 잡을 수 있는 마지막 자리이고,
 * 저쪽은 `used_evidence` 의 **소속**만 보지 문구는 안 본다.
 */
export function buildAnswerResponse({
  requestId, status, answer = '', usedEvidence = [], uncertain = [], usage = {},
  maxOutputChars = MAX_OUTPUT_CHARS,
}) {
  if (!STATUSES.includes(status)) throw new WireViolation(`모르는 상태입니다: ${status}`);
  const text = String(answer ?? '');
  if (text.length > maxOutputChars) {
    throw new WireViolation(`답변이 상한을 넘습니다(${text.length} > ${maxOutputChars}).`);
  }
  const ids = [...new Set(usedEvidence.map(String))];
  const bad = ids.filter((id) => !EVIDENCE_ID_RE.test(id));
  if (bad.length) throw new WireViolation(`근거 식별자 형식이 다릅니다: ${bad.join(', ')}`);
  if (status === STATUS_ANSWERED) {
    if (!text.trim()) throw new WireViolation('answered 인데 답변이 비었습니다.');
    if (!ids.length) throw new WireViolation('근거 없이 answered 로 답할 수 없습니다.');
  }
  const payload = {
    schema: SCHEMA_RESPONSE,
    request_id: requestId,
    status,
    answer: text,
    used_evidence: ids,
    uncertain: uncertain.map(String),
    usage: usage && typeof usage === 'object' ? usage : {},
  };
  const raw = Buffer.from(JSON.stringify(payload), 'utf8');
  if (raw.length > MAX_RESPONSE_BYTES) {
    throw new WireViolation(`응답이 상한을 넘습니다(${raw.length} > ${MAX_RESPONSE_BYTES}).`);
  }
  return raw;
}

/**
 * `/v1/health` 응답. **모델을 부르지 않는다** — 저쪽은 2초를 준다.
 *
 * `workspaces` 는 공유 worker 전용이고 **선택**이다. 저쪽 `validate_health` 는
 * `status`·`contract_version`·`version` 셋만 보므로 모르는 키는 그냥 지나간다 —
 * 그래서 계약을 안 흔들고 「전체 ok 인데 한 칸이 죽음」 을 말할 수 있다.
 *
 * **담는 것은 workspace key 와 준비 여부뿐이다.** 경로·키 파일 이름·사유 문구를
 * 실으면 그 값이 저쪽 로그와 콘솔로 흘러 들어간다.
 */
export function buildHealthResponse({ version, workspaces }) {
  const payload = {
    status: 'ok',
    contract_version: CONTRACT_VERSION,
    version: String(version ?? ''),
  };
  if (workspaces && typeof workspaces === 'object') {
    payload.workspaces = Object.fromEntries(
      Object.entries(workspaces).map(([ws, ready]) => [String(ws), ready === true]),
    );
  }
  return Buffer.from(JSON.stringify(payload), 'utf8');
}

/* ── 도구 호출 (Hermes → TYBot) ────────────────────────────────────────── */

export function toolCallBody({ requestId, token, callId, tool, args = {} }) {
  if (!TOOLS.includes(tool)) throw new WireViolation(`모르는 도구입니다: ${tool}`);
  const raw = Buffer.from(JSON.stringify({
    schema: SCHEMA_TOOL_CALL,
    request_id: requestId,
    token,
    call_id: callId,
    tool,
    args: args && typeof args === 'object' ? args : {},
  }), 'utf8');
  if (raw.length > MAX_TOOL_RESULT_BYTES) {
    throw new WireViolation(`도구 호출이 상한을 넘습니다(${raw.length} > ${MAX_TOOL_RESULT_BYTES}).`);
  }
  return raw;
}

/**
 * 도구 결과 읽기.
 *
 * **`ok:false` 는 계약 위반이지 도구 실패가 아니다**(토큰 만료·모르는 도구·요청
 * 불일치). 그때 재시도하면 같은 거절이 반복되며 예산만 태운다 — 호출부는 멈추고
 * 지금까지 읽은 것으로 답해야 한다.
 */
export function parseToolResult(raw) {
  if (raw.length > MAX_TOOL_RESULT_BYTES + 8192) {
    throw new WireViolation('도구 결과가 상한을 넘습니다.');
  }
  let payload;
  try {
    payload = JSON.parse(raw.toString('utf8'));
  } catch {
    throw new WireViolation('도구 결과가 JSON 이 아닙니다.');
  }
  if (!payload || typeof payload !== 'object') throw new WireViolation('도구 결과가 객체가 아닙니다.');
  if (payload.schema !== SCHEMA_TOOL_RESULT) {
    throw new WireViolation(`도구 결과 스키마가 다릅니다: ${JSON.stringify(payload.schema)}`);
  }
  const evidence = Array.isArray(payload.evidence)
    ? payload.evidence
      .filter((e) => e && typeof e === 'object' && EVIDENCE_ID_RE.test(str(e.id)))
      .map((e) => ({ id: str(e.id), kind: str(e.kind) }))
    : [];
  return Object.freeze({
    callId: str(payload.call_id),
    ok: payload.ok === true,
    text: str(payload.text),
    reason: str(payload.reason),
    evidence: Object.freeze(evidence),
    budget: payload.budget && typeof payload.budget === 'object' ? payload.budget : {},
  });
}

/* ── 답변에서 근거 ID 를 거둔다 ────────────────────────────────────────── */

/** 본문에 박힌 `[e7]` 표식. 모델이 **문장 옆에** 적게 해서 어느 문장이 무엇에 기댔는지 남긴다. */
export const CITATION_RE = /\[(e[0-9]{1,4})\]/g;

/**
 * 답변에서 인용 ID 를 거둔다.
 *
 * **발급한 것과 아닌 것을 갈라서 돌려준다.** 합쳐서 거르면 「지어낸 인용이 있었다」 는
 * 사실이 사라지고, 사라지면 그 답을 보낼지 말지를 호출부가 정할 수 없다.
 */
export function collectCitations(text, issued) {
  const known = [];
  const unknown = [];
  for (const [, id] of String(text ?? '').matchAll(CITATION_RE)) {
    const bucket = issued.has(id) ? known : unknown;
    if (!bucket.includes(id)) bucket.push(id);
  }
  return { known, unknown };
}

/** 금지 문구 검사. 걸리면 사유를 돌려준다(없으면 `null`). */
export function forbiddenReason(text) {
  const body = String(text ?? '');
  const lowered = body.toLowerCase();
  for (const [needle, why] of FORBIDDEN) {
    if (body.includes(needle) || lowered.includes(needle)) return why;
  }
  return null;
}
