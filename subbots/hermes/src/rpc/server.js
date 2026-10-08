/**
 * RPC worker 서버 — Unix socket 위의 HTTP/1.1.
 *
 * 저쪽 클라이언트: `src/tybot/specialist_v3_client.py` · `specialist_http.py`
 *
 * ## 포트를 열지 않는다
 *
 * 인바운드 TCP 포트가 하나도 없다(인계 요구 1). 같은 호스트의 소켓 파일 하나가 전부다 —
 * 그 파일에 닿을 수 있는 것은 파일 권한이 허락한 계정뿐이고, 그 위에 HMAC 이 한 겹 더 있다.
 *
 * ## 소켓은 **방향마다 다르다**
 *
 *   TYBot → Hermes  `/run/tybot-subbots/<specialist>-<workspace>/http.sock`  ← 여기
 *   Hermes → TYBot  `/run/tybot/specialist-tools.sock`                       ← tool-client.js
 *
 * 한 경로에 두 프로세스가 listen 할 수 없다. 「같은 Unix socket」 은 같은 **전송 방식**이지
 * 같은 **파일**이 아니다(계약 §2). 섞어 쓰면 둘 중 하나는 조용히 안 뜬다.
 *
 * ## 요청끼리 섞이지 않게
 *
 * `request_id` 마다 세션 하나다. 도구 토큰은 그 세션 안에 닫혀 있고, 응답 서명도
 * `request_id` 에 묶인다. 같은 `request_id` 가 겹쳐 들어오면 **거절한다** — 받으면
 * 둘 중 어느 답이 어느 질문의 것인지 말할 수 없게 된다(인계 요구 5).
 */
import fs from 'node:fs';
import http from 'node:http';
import net from 'node:net';
import path from 'node:path';
import {
  ANSWER_ENDPOINT, CANCEL_ENDPOINT, HEALTH_ENDPOINT, MAX_REQUEST_BYTES,
  WireViolation, buildAnswerResponse, buildHealthResponse,
  parseAnswerRequest, parseCancelRequest, responseSignature,
} from './wire.js';
import { createAuthenticator } from './replay.js';

const JSON_TYPE = 'application/json; charset=utf-8';

/** 오류 본문. **사유를 밖에 적지 않는다** — 적으면 상대가 무엇을 맞춰야 하는지 알아낸다. */
const ERRORS = Object.freeze({
  400: Buffer.from('{"error":"bad_request"}', 'utf8'),
  401: Buffer.from('{"error":"unauthorized"}', 'utf8'),
  404: Buffer.from('{"error":"not_found"}', 'utf8'),
  409: Buffer.from('{"error":"bad_request"}', 'utf8'),
  413: Buffer.from('{"error":"bad_request"}', 'utf8'),
  500: Buffer.from('{"error":"internal_error"}', 'utf8'),
});

/**
 * @param {object} o
 * @param {Buffer} o.secret        양방향 공유 HMAC 비밀
 * @param {string} o.workspace     이 인스턴스의 workspace. 요청과 **다르면 거절한다**
 * @param {string} o.version       health 가 밝히는 자기 버전
 * @param {{answer:Function}} o.answerer
 */
export function createRpcServer({
  secret, workspace, version, answerer,
  now = () => Date.now(), console: log = globalThis.console,
}) {
  const auth = createAuthenticator({ secret, now: () => now() / 1000 });
  /** 도는 요청. `request_id` → `{controller}`. 끝나면 **반드시** 지운다. */
  const inFlight = new Map();

  function send(res, status, body, headers = {}) {
    res.writeHead(status, {
      'Content-Type': JSON_TYPE,
      'Content-Length': body.length,
      Connection: 'close',
      ...headers,
    });
    res.end(body);
  }

  const fail = (res, status) => send(res, status, ERRORS[status] || ERRORS[500]);

  /** 본문을 상한까지만 읽는다. 상한이 없으면 상대가 우리 메모리를 채운다. */
  function readBody(req) {
    return new Promise((resolve, reject) => {
      const chunks = [];
      let total = 0;
      req.on('data', (chunk) => {
        total += chunk.length;
        if (total > MAX_REQUEST_BYTES) {
          reject(Object.assign(new Error('too large'), { tooLarge: true }));
          req.destroy();
          return;
        }
        chunks.push(chunk);
      });
      req.on('end', () => resolve(Buffer.concat(chunks)));
      req.on('error', reject);
    });
  }

  async function handle(req, res) {
    if (req.method !== 'POST') return fail(res, 404);
    // 질의 문자열을 떼고 **정확히** 맞춘다. 서명 대상도 이 경로이므로 둘이 갈리면 401 이다.
    const route = String(req.url || '').split('?')[0];
    if (![ANSWER_ENDPOINT, CANCEL_ENDPOINT, HEALTH_ENDPOINT].includes(route)) {
      return fail(res, 404);
    }

    let body;
    try {
      body = await readBody(req);
    } catch (err) {
      return fail(res, err?.tooLarge ? 413 : 400);
    }

    if (!auth.accepts({ method: 'POST', path: route, body, headers: req.headers }).ok) {
      return fail(res, 401);
    }

    if (route === HEALTH_ENDPOINT) {
      // **모델을 부르지 않는다.** 저쪽은 2초를 준다 — 여기서 모델을 부르면 health 가
      // 느려지고, 느린 health 는 「죽었다」 로 읽혀 라우팅이 닫힌다.
      return send(res, 200, buildHealthResponse({ version }));
    }

    if (route === CANCEL_ENDPOINT) {
      let requestId;
      try { requestId = parseCancelRequest(body); } catch { return fail(res, 400); }
      inFlight.get(requestId)?.controller.abort();
      return send(res, 200, Buffer.from('{"status":"ok"}', 'utf8'));
    }

    let request;
    try {
      request = parseAnswerRequest(body);
    } catch (err) {
      log?.warn?.(`[rpc] 요청 계약 위반: ${err instanceof WireViolation ? err.message : 'parse'}`);
      return fail(res, 400);
    }

    // **워크스페이스를 대조한다.** 다른 워크스페이스 요청을 받으면 우리 도구 설명·
    // 표시 용어가 그쪽 자료에 붙고, 그건 크로스 워크스페이스 노출이다(절대 원칙 4).
    if (request.workspace !== workspace) {
      log?.warn?.('[rpc] 다른 워크스페이스 요청을 거절했습니다.');
      return fail(res, 400);
    }

    if (inFlight.has(request.requestId)) {
      // 같은 요청이 겹쳐 들어왔다. 받으면 어느 답이 어느 질문의 것인지 말할 수 없다.
      log?.warn?.('[rpc] 같은 request_id 가 이미 돌고 있습니다.');
      return fail(res, 409);
    }

    const controller = new AbortController();
    inFlight.set(request.requestId, { controller });
    let result;
    try {
      result = await answerer.answer(request, { signal: controller.signal });
    } catch (err) {
      // 답변 코어는 예외를 안 내기로 돼 있다. 그래도 여기서 한 번 더 받는다 —
      // 안 받으면 소켓이 응답 없이 닫히고, 저쪽은 `transport` 로 읽어 **원인이 지워진다.**
      log?.error?.(`[rpc] 답변기가 던졌습니다: ${err?.code || err?.name || 'Error'}`);
      result = { status: 'failed', answer: '', usedEvidence: [], uncertain: [], usage: {} };
    } finally {
      inFlight.delete(request.requestId);
    }

    let payload;
    try {
      payload = buildAnswerResponse({
        requestId: request.requestId,
        status: result.status,
        answer: result.answer,
        usedEvidence: result.usedEvidence,
        uncertain: result.uncertain,
        usage: result.usage,
        maxOutputChars: request.maxOutputChars,
      });
    } catch (err) {
      // 우리가 만든 응답이 계약을 어겼다. **보내지 않는다** — 보내면 저쪽이 버리는데,
      // 버린 이유가 저쪽 로그에만 남아 우리는 영영 모른다.
      log?.error?.(`[rpc] 응답을 만들지 못했습니다: ${err.message}`);
      payload = buildAnswerResponse({
        requestId: request.requestId, status: 'failed', answer: '',
        usedEvidence: [], uncertain: [], usage: {},
        maxOutputChars: request.maxOutputChars,
      });
    }

    send(res, 200, payload, {
      'X-TYBot-Signature': responseSignature(secret, { requestId: request.requestId, body: payload }),
    });
  }

  const server = http.createServer((req, res) => {
    handle(req, res).catch((err) => {
      log?.error?.(`[rpc] 처리 중 오류: ${err?.code || err?.name || 'Error'}`);
      if (!res.headersSent) fail(res, 500);
      else res.end();
    });
  });
  // 헤더만 보내고 끊지 않는 상대가 소켓을 묶어 두지 못하게 한다.
  server.headersTimeout = 10_000;
  server.requestTimeout = 0; // 본문 시간은 요청의 deadline 이 정한다

  /**
   * 듣기 시작. **남은 소켓 파일을 치운다** — 지난번이 비정상 종료하면 파일이 남고,
   * 그러면 `EADDRINUSE` 로 안 뜬다. 다만 **살아 있는 서버가 있으면 치우지 않는다**:
   * 연결이 되면 그건 중복 기동이고, 지우면 멀쩡한 쪽을 죽인다.
   */
  async function listen(socketPath, { mode = 0o660, prepareDir = true } = {}) {
    if (prepareDir) {
      await ensureFree(socketPath);
      fs.mkdirSync(path.dirname(socketPath), { recursive: true, mode: 0o750 });
    }
    await new Promise((resolve, reject) => {
      server.once('error', reject);
      server.listen(socketPath, () => { server.off('error', reject); resolve(); });
    });
    // 그룹에게만 연다. 소유 그룹은 unit 이 정한다(RuntimeDirectory + Group).
    // `mode: null` 은 **개발 PC 의 named pipe 전용**이다 — 운영 기본값은 언제나 0660 이고,
    // 그 자리에서 끄는 길을 설정으로 내보내지 않는다(그러면 언젠가 운영에서 꺼진다).
    if (mode != null) fs.chmodSync(socketPath, mode);
    return socketPath;
  }

  function close() {
    return new Promise((resolve) => server.close(() => resolve()));
  }

  return { listen, close, server, get inFlightCount() { return inFlight.size; } };
}

/**
 * 소켓 자리가 비었나. **살아 있으면 던진다.**
 *
 * 그냥 지우고 뜨면 중복 기동이 조용히 성립한다 — 먼저 뜬 쪽은 아무 요청도 못 받는
 * 채로 살아 있고, 아무도 그것을 모른다.
 */
export function ensureFree(socketPath, {
  connect = (p) => net.connect(p), unlink = fs.unlinkSync, exists = fs.existsSync,
} = {}) {
  return new Promise((resolve, reject) => {
    if (!exists(socketPath)) return resolve(false);
    const probe = connect(socketPath);
    const give = (fn, arg) => { probe.destroy(); fn(arg); };
    probe.once('connect', () => give(reject, new Error(
      `소켓이 이미 쓰이고 있습니다: ${socketPath}\n`
      + '  다른 worker 가 이미 떠 있습니다. 중복 기동은 같은 질문을 두 번 처리하고 비용을 두 배로 씁니다.',
    )));
    probe.once('error', () => { unlink(socketPath); give(resolve, true); });
  });
}
