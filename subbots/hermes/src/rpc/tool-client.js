/**
 * Hermes → TYBot 도구 호출. **자료가 들어오는 유일한 문이다.**
 *
 * 저쪽: `src/tybot/specialist_v3_tool_server.py` · `specialist_v3_broker.py`
 *
 * ## 이 모듈이 **모르는** 것
 *
 * Archiver 경로·Slack 토큰·DB 자격·채널 ID·파일 경로를 하나도 안 가진다. 가진 것은
 * 소켓 경로 하나와 HMAC 비밀, 그리고 **그 요청 하나짜리 도구 토큰**뿐이다.
 * 그래서 이 프로세스가 통째로 샌다 해도 **권한 밖 자료는 나올 수 없다** — 판정은
 * 저쪽 `RequestContext` 한 곳에서만 일어난다(TYBot 원칙 3).
 *
 * ## 토큰은 세션에 갇힌다
 *
 * 토큰을 인자로 받지 않고 **팩토리가 닫아 둔다.** 인자로 받으면 다른 요청의 토큰을
 * 넘기는 코드가 언젠가 생기고, 그 순간 요청 사이 격리가 사라진다(계약 §5).
 *
 * ## 재시도하지 않는다
 *
 * 전송 실패·서명 불일치·`ok:false` 는 전부 **그 자리에서 끝**이다. 다시 부르면 같은
 * 거절이 반복되며 예산만 태우고, 느려서 실패하는 상황이라면 정확히 그 상황을
 * 악화시킨다(저쪽 `specialist_http` 와 같은 판단).
 */
import http from 'node:http';
import {
  MAX_TOOL_RESULT_BYTES, TOOL_ENDPOINT, WireViolation,
  parseToolResult, responseSignature, safeEqualHex, signatureHeaders, toolCallBody,
} from './wire.js';

/** 전송 실패. 계약 위반(`WireViolation`)과 **가른다** — 고칠 자리가 다르다. */
export class ToolTransportError extends Error {
  constructor(message, cause) {
    super(message);
    this.name = 'ToolTransportError';
    this.code = 'hermes_tool_transport';
    if (cause) this.cause = cause;
  }
}

/** 응답 몸체 상한. 헤더 몫을 넉넉히 더한다 — 본문 상한은 계약이 다시 본다. */
const READ_CAP = MAX_TOOL_RESULT_BYTES + 8192;

/**
 * 한 번 POST 한다. **상한을 두고 읽는다** — 두지 않으면 상대가 무한히 흘려보내
 * 우리 메모리를 채운다.
 */
function post({ socketPath, path, body, headers, timeoutMs, agent, signal, request = http.request }) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new ToolTransportError('이미 중단된 요청입니다.'));
      return;
    }
    const req = request({
      socketPath,
      path,
      method: 'POST',
      agent,
      // **진행 중인 연결까지 끊는다.** 안 주면 예산이 끝난 뒤에도 이 왕복은 끝까지
      // 가고, 저쪽 ToolBox 예산을 한 번 더 태운다(Codex 리뷰 P1).
      signal,
      headers: {
        Host: 'tybot',
        Connection: 'close',
        'Content-Length': Buffer.byteLength(body),
        ...headers,
      },
    }, (res) => {
      const chunks = [];
      let total = 0;
      res.on('data', (chunk) => {
        total += chunk.length;
        if (total > READ_CAP) {
          res.destroy();
          reject(new ToolTransportError('도구 응답이 상한을 넘습니다.'));
          return;
        }
        chunks.push(chunk);
      });
      res.on('end', () => resolve({
        status: res.statusCode,
        headers: res.headers,
        body: Buffer.concat(chunks),
      }));
      res.on('error', (err) => reject(new ToolTransportError('도구 응답을 읽지 못했습니다.', err)));
    });
    req.setTimeout(timeoutMs, () => {
      req.destroy();
      reject(new ToolTransportError('도구 호출이 시간 안에 끝나지 않았습니다.'));
    });
    // 오류 문자열에 소켓 경로를 넣지 않는다 — 로그로 우리 배치가 새어 나간다.
    req.on('error', (err) => reject(new ToolTransportError(`소켓 오류: ${err?.code || err?.name || 'unknown'}`, err)));
    req.end(body);
  });
}

/**
 * 이 요청 하나짜리 도구 창구.
 *
 * @param {object} o
 * @param {string} o.socketPath   `/run/tybot/specialist-tools.sock`. **설정에서 온다** —
 *   요청 본문에서 받지 않는다(받으면 그 값이 곧 임의 소켓 호출 권한이다).
 * @param {Buffer} o.secret       양방향 공유 HMAC 비밀
 * @param {string} o.requestId    이 요청
 * @param {string} o.token        이 요청 하나짜리 도구 토큰. **닫아 둔다**
 * @param {string[]} o.allow      허용 도구
 * @param {AbortSignal} [o.signal] 예산·취소. **진행 중인 왕복까지 끊는다** — 안 주면
 *   예산이 끝난 뒤에도 이 호출은 끝까지 가고, 저쪽 ToolBox 예산을 한 번 더 태운다.
 */
export function createToolClient({
  socketPath, secret, requestId, token, allow, signal,
  timeoutMs = 30_000, now = () => Date.now(), post: postFn = post,
}) {
  let calls = 0;
  /** 저쪽이 발급한 근거 ID. **우리가 지어내지 않는다** — 여기 담긴 것만 인용할 수 있다. */
  const issued = new Set();
  /** 계약이 깨졌다. 한 번 서면 더 부르지 않는다 — 재시도는 예산만 태운다. */
  let halted = null;

  async function call(tool, args) {
    if (halted) return { ok: false, text: '', reason: halted, halted: true };
    if (!allow.includes(tool)) {
      halted = 'tool-not-allowed';
      return { ok: false, text: '', reason: halted, halted: true };
    }
    calls += 1;
    const callId = `t${calls}`;
    const body = toolCallBody({ requestId, token, callId, tool, args });
    const headers = signatureHeaders(secret, {
      method: 'POST', path: TOOL_ENDPOINT, body, now: Math.floor(now() / 1000),
    });

    const reply = await postFn({
      socketPath, path: TOOL_ENDPOINT, body, headers, timeoutMs, signal,
    });

    if (reply.status !== 200) {
      // 4xx 는 우리 계약이 틀렸다는 뜻이다. 다시 보내도 같다.
      halted = `http-${reply.status}`;
      return { ok: false, text: '', reason: halted, halted: true };
    }

    // **본문을 파싱하기 전에 서명을 본다.** 뒤집으면 위조된 본문이 우리 파서를 먼저 지난다.
    const provided = reply.headers['x-tybot-signature'] || '';
    if (!safeEqualHex(responseSignature(secret, { requestId, body: reply.body }), provided)) {
      halted = 'bad-signature';
      return { ok: false, text: '', reason: halted, halted: true };
    }

    let result;
    try {
      result = parseToolResult(reply.body);
    } catch (err) {
      halted = 'invalid-tool-result';
      throw new WireViolation(`도구 결과를 읽지 못했습니다: ${err.message}`);
    }
    if (result.callId !== callId) {
      // 다른 호출의 결과다. 소켓을 공유하거나 재시도가 엇갈렸을 때 난다.
      halted = 'call-mismatch';
      return { ok: false, text: '', reason: halted, halted: true };
    }

    if (!result.ok) {
      // 계약 위반이다 — 토큰 만료·모르는 도구·요청 불일치. 더 부르지 않는다.
      halted = result.reason || 'refused';
      return { ok: false, text: '', reason: halted, halted: true };
    }

    for (const e of result.evidence) issued.add(e.id);
    return {
      ok: true,
      text: result.text,
      evidence: result.evidence,
      budget: result.budget,
      halted: false,
    };
  }

  return {
    call,
    /** 저쪽이 발급한 ID 모음. 답변의 인용을 **이것과만** 맞춘다. */
    get issued() { return new Set(issued); },
    get callCount() { return calls; },
    get haltedReason() { return halted; },
  };
}
