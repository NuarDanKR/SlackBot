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
import {
  CAPABILITY_REVIEW, REVIEW_ENDPOINT, buildReviewResponse, parseReviewRequest,
} from './review-wire.js';
import { createAuthenticator } from './replay.js';

const JSON_TYPE = 'application/json; charset=utf-8';

/** 오류 본문. **사유를 밖에 적지 않는다** — 적으면 상대가 무엇을 맞춰야 하는지 알아낸다. */
const ERRORS = Object.freeze({
  400: Buffer.from('{"error":"bad_request"}', 'utf8'),
  401: Buffer.from('{"error":"unauthorized"}', 'utf8'),
  404: Buffer.from('{"error":"not_found"}', 'utf8'),
  409: Buffer.from('{"error":"bad_request"}', 'utf8'),
  413: Buffer.from('{"error":"bad_request"}', 'utf8'),
  // 지금은 자리가 없다. **대기열을 만들지 않는다** — 줄을 세우면 저쪽 deadline 이
  // 먼저 끝나고, 그때 우리는 아무도 안 기다리는 답을 만들고 있다.
  // `overloaded` 는 v2 `ERROR_CODES` 에 있는 값이라 저쪽 `parse_error` 가 그대로 읽는다.
  429: Buffer.from('{"error":"overloaded"}', 'utf8'),
  500: Buffer.from('{"error":"internal_error"}', 'utf8'),
});

/**
 * @param {object} o
 * @param {Buffer} o.secret        양방향 공유 HMAC 비밀
 * @param {string} [o.workspace]   **단일 모드**: 이 인스턴스의 workspace. 요청과 다르면 거절
 * @param {{answer:Function}} [o.answerer]  단일 모드의 답변기
 * @param {(ws:string)=>({answer:Function}|null)} [o.route]
 *   **공유 모드**: workspace → 그 workspace 의 답변기. 모르는 workspace 면 `null`.
 *   `workspace`+`answerer` 를 주면 그 둘로 만든 라우터가 기본값이다 — 즉 **단일 모드는
 *   라우터가 하나뿐인 공유 모드**이고, 코드 경로가 하나로 남는다.
 * @param {{acquire:(ws:string)=>(null|Function)}} [o.limits]
 *   공유 모드의 동시성 게이트. `acquire` 가 `null` 을 주면 **429 로 즉시 돌려보낸다** —
 *   대기열을 만들지 않는다(인계 §동시성).
 * @param {()=>object} [o.readiness]  health 에 실을 workspace 별 준비 상태
 * @param {{review:Function}} [o.reviewer]  단일 모드의 검토 후보 생성기
 * @param {(ws:string)=>({review:Function}|null)} [o.reviewRoute]
 *   **공유 모드**: workspace → 그 workspace 의 검토기. 없으면 검토 endpoint 는 404 다 —
 *   그러면 health 의 `capabilities` 에서도 빠지고, TYBot 은 **설정 오류로 진단한다**
 *   (조용히 Haiku 로 우회하지 않는다, 인계 §health).
 * @param {string} o.version       health 가 밝히는 자기 버전
 */
export function createRpcServer({
  secret, workspace, version, answerer,
  route: routeTo, limits, readiness,
  reviewer, reviewRoute,
  now = () => Date.now(), console: log = globalThis.console,
}) {
  /* 단일 모드를 **라우터 하나짜리 공유 모드**로 접는다. 두 경로를 따로 두면 서명·
   * 재생·격리 같은 규칙이 한쪽에만 고쳐지는 날이 오고, 그 차이는 에러 없이 흐른다. */
  const resolveAnswerer = routeTo || ((ws) => (ws === workspace ? answerer : null));
  const resolveReviewer = reviewRoute
    || (reviewer ? (ws) => (ws === workspace ? reviewer : null) : null);
  /* **검토를 할 수 있나.** 배선이 없으면 endpoint 자체가 없다 — 「있는데 늘 실패」
   * 보다 「없다」 가 낫다. 전자는 TYBot 이 재시도하고, 후자는 바로 설정을 본다. */
  const capabilities = resolveReviewer ? [CAPABILITY_REVIEW] : [];
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
      let over = false;
      req.on('data', (chunk) => {
        if (over) return;
        total += chunk.length;
        if (total > MAX_REQUEST_BYTES) {
          /* 상한을 넘었다. **읽기를 멈추되 소켓은 안 끊는다.**
           *
           * 끊으면 저쪽은 413 을 못 받고 `socket hang up` 만 본다 — 그러면
           * 「우리가 상한을 넘겼다」 가 「연결이 이상하다」 로 둔갑하고, 고칠 곳을
           * 못 찾는다. 상태 코드를 먼저 보내고 호출부가 끊는다. */
          over = true;
          req.pause();
          reject(Object.assign(new Error('too large'), { tooLarge: true }));
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
    const known = [ANSWER_ENDPOINT, CANCEL_ENDPOINT, HEALTH_ENDPOINT];
    if (resolveReviewer) known.push(REVIEW_ENDPOINT);
    if (!known.includes(route)) return fail(res, 404);

    let body;
    try {
      body = await readBody(req);
    } catch (err) {
      fail(res, err?.tooLarge ? 413 : 400);
      // 응답을 **보낸 뒤에** 남은 본문을 버린다. 순서를 뒤집으면 상대가 사유를 못 받는다.
      if (err?.tooLarge) res.once('finish', () => req.destroy());
      return;
    }

    if (!auth.accepts({ method: 'POST', path: route, body, headers: req.headers }).ok) {
      return fail(res, 401);
    }

    if (route === HEALTH_ENDPOINT) {
      // **모델을 부르지 않는다.** 저쪽은 2초를 준다 — 여기서 모델을 부르면 health 가
      // 느려지고, 느린 health 는 「죽었다」 로 읽혀 라우팅이 닫힌다.
      //
      // **전체와 workspace 별을 가른다**(인계 §7). 공유 worker 는 한 workspace 의
      // 키 파일이 망가져도 나머지는 멀쩡히 돈다 — 그걸 「전체 ok」 하나로만 말하면
      // 그 한 칸이 죽은 것을 아무도 모르고, 「전체 실패」 로 말하면 멀쩡한 쪽까지
      // 라우팅이 닫힌다. **비밀도 경로도 싣지 않는다 — key 와 준비 여부뿐이다.**
      return send(res, 200, buildHealthResponse({
        version, workspaces: readiness?.(), capabilities,
      }));
    }

    if (route === CANCEL_ENDPOINT) {
      let requestId;
      try { requestId = parseCancelRequest(body); } catch { return fail(res, 400); }
      inFlight.get(requestId)?.controller.abort();
      return send(res, 200, Buffer.from('{"status":"ok"}', 'utf8'));
    }

    /* 작업 종류는 **경로**가 정한다. 질문 문자열로 추측하지 않는다 — 추측하면 같은
     * 문자열이 어느 날 다른 일을 시키고, 그 변화는 에러를 안 낸다(인계 §요청 계약). */
    const isReview = route === REVIEW_ENDPOINT;
    let request;
    try {
      request = isReview ? parseReviewRequest(body) : parseAnswerRequest(body);
    } catch (err) {
      log?.warn?.(`[rpc] 요청 계약 위반: ${err instanceof WireViolation ? err.message : 'parse'}`);
      return fail(res, 400);
    }

    /* **워크스페이스를 대조한다.** 모르는 workspace 를 받으면 우리 도구 설명·표시
     * 용어·모델 키가 그쪽 자료에 붙고, 그건 크로스 워크스페이스 노출이다(절대 원칙 4).
     *
     * 공유 worker 도 같다 — **허용 목록은 서버 설정에서 온다**(인계 §6). 요청 본문이
     * workspace 를 고르는 것이 아니라, 요청이 고른 workspace 가 설정에 있어야 한다. */
    const forThis = isReview
      ? resolveReviewer(request.workspace)
      : resolveAnswerer(request.workspace);
    if (!forThis) {
      // 로그에 workspace key 는 남긴다 — 운영자가 「목록에 넣는 걸 잊었나」 를
      // 그 자리에서 판단해야 한다. 질문·원문·토큰은 남기지 않는다(인계 §로그).
      log?.warn?.(`[rpc] 등록되지 않은 workspace 요청을 거절했습니다: ${request.workspace}`);
      return fail(res, 400);
    }

    /* **Q&A 와 검토가 같은 `inFlight` 를 쓴다.** 둘을 나누면 같은 `request_id` 가
     * 양쪽에 하나씩 들어올 수 있고, 그때 취소는 둘 중 하나만 끊는다 — 어느 쪽인지는
     * 부르는 쪽이 모른다(인계 §request_id 중복 거절·취소 공유). */
    if (inFlight.has(request.requestId)) {
      log?.warn?.('[rpc] 같은 request_id 가 이미 돌고 있습니다.');
      return fail(res, 409);
    }

    /* 자리를 **먼저** 잡는다. 잡고 나서 `inFlight` 에 넣어야, 429 로 돌려보낸 요청이
     * `request_id` 를 점유한 채 남지 않는다 — 남으면 저쪽이 같은 id 로 재시도할 때
     * 429 가 409 로 바뀌어 원인이 뒤바뀐다.
     *
     * **검토도 같은 게이트를 쓴다.** 검토용 슬롯 풀을 따로 만들면 합산이 상한을
     * 넘는다 — 상한은 이 프로세스가 동시에 부르는 모델 호출 수를 막는 것이고,
     * 그 모델은 Q&A 와 검토를 안 가린다(인계 §동시 상한). */
    const release = limits ? limits.acquire(request.workspace) : () => {};
    if (!release) {
      log?.warn?.(`[rpc] 동시 요청 상한으로 거절했습니다: ${request.workspace}`);
      return fail(res, 429);
    }

    const controller = new AbortController();
    inFlight.set(request.requestId, { controller });
    let result;
    try {
      result = isReview
        ? await forThis.review(request, { signal: controller.signal })
        : await forThis.answer(request, { signal: controller.signal });
    } catch (err) {
      // 코어는 예외를 안 내기로 돼 있다. 그래도 여기서 한 번 더 받는다 —
      // 안 받으면 소켓이 응답 없이 닫히고, 저쪽은 `transport` 로 읽어 **원인이 지워진다.**
      log?.error?.(`[rpc] ${isReview ? '검토기' : '답변기'}가 던졌습니다: ${err?.code || err?.name || 'Error'}`);
      result = isReview
        ? { status: 'failed', candidates: [], errorCode: 'internal_error', metrics: {} }
        : { status: 'failed', answer: '', usedEvidence: [], uncertain: [], usage: {} };
    } finally {
      inFlight.delete(request.requestId);
      /* 자리는 **일이 실제로 끝난 뒤에** 돌려준다.
       *
       * 예산 만료는 답을 먼저 내보내고 실행 루프는 아직 돈다. 그때 바로 돌려주면
       * 살아 있는 모델 호출 위로 새 요청이 올라타 **실제 호출 수가 상한을 넘는다**
       * (Codex 리뷰 P1: 상한 1에서 살아 있는 작업 2개를 실측). 평소 경로에서는
       * `settled` 가 이미 풀려 있어 아래가 같은 tick 에 돈다.
       *
       * 성공·실패·취소 **어느 길로 나가도** 돌려준다. 한 번이라도 안 돌려주면 그 칸은
       * 영영 줄고, 증상은 「어느 날부터 429 가 잦다」 뿐이다. 그래서 `settled` 가
       * 깨져도 돌려준다(`finally`). */
      const settled = result?.settled;
      if (settled && typeof settled.then === 'function') {
        settled.then(release, release);
      } else {
        release();
      }
    }

    if (isReview) {
      /* 검토 완료 줄. **원문·후보 문장·승인 본문·API key 를 안 적는다**(인계 §로그).
       * 적는 것은 workspace · request_id · status · 모델 · 건수 · 시간뿐이다.
       *
       * `proposed/accepted` 를 같이 적는 이유: 「정상 0건」 과 「전부 탈락」 을 journal
       * 한 줄로 가를 수 있어야 한다. 그걸 못 가르면 장애가 정상으로 묻힌다. */
      const m = result.metrics || {};
      const rejected = Object.entries(m.rejected_counts || {})
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([reason, value]) => `${reason}=${value}`)
        .join(',') || '-';
      log?.info?.(
        `[rpc] 검토 완료 request=${request.requestId} workspace=${request.workspace} `
        + `status=${result.status} error=${String(result.errorCode || '-') || '-'} `
        + `elapsed_ms=${Math.max(0, Number(m.elapsed_ms) || 0)} `
        + `model=${String(m.model || '-')} stop_reason=${String(m.stop_reason || '-')} `
        + `in_tok=${Math.max(0, Number(m.input_tokens) || 0)} `
        + `out_tok=${Math.max(0, Number(m.output_tokens) || 0)} `
        + `proposed=${Math.max(0, Number(m.proposed_count) || 0)} `
        + `accepted=${Math.max(0, Number(m.accepted_count) || 0)} rejected=${rejected}`,
      );

      let reviewPayload;
      try {
        reviewPayload = buildReviewResponse({
          requestId: request.requestId,
          workspace: request.workspace,
          // 요청값을 **그대로** 돌려준다. 다시 계산하면 어긋났을 때 어느 쪽이
          // 틀렸는지 못 가린다.
          sourceDigest: request.sourceDigest,
          status: result.status,
          candidates: result.candidates,
          errorCode: result.errorCode,
          metrics: result.metrics,
        });
      } catch (err) {
        // 우리가 만든 응답이 계약을 어겼다. **보내지 않는다** — 보내면 저쪽이 버리고,
        // 버린 이유가 저쪽 로그에만 남아 우리는 영영 모른다.
        log?.error?.(`[rpc] 검토 응답을 만들지 못했습니다: ${err.message}`);
        reviewPayload = buildReviewResponse({
          requestId: request.requestId,
          workspace: request.workspace,
          sourceDigest: request.sourceDigest,
          status: 'failed',
          errorCode: 'internal_error',
          metrics: result.metrics,
        });
      }
      return send(res, 200, reviewPayload, {
        'X-TYBot-Signature': responseSignature(secret, {
          requestId: request.requestId, body: reviewPayload,
        }),
      });
    }

    // 원문·질문·도구 인자는 기록하지 않는다. 운영에서 no_evidence 와 빈 답을
    // 구분하는 데 필요한 비민감 계측값만 한 줄로 남긴다.
    const usage = result.usage || {};
    const trace = Array.isArray(result.trace) ? result.trace : [];
    const byTool = new Map();
    for (const item of trace) {
      const name = String(item?.name || 'unknown').replace(/[^a-z0-9_-]/gi, '_');
      const current = byTool.get(name) || { calls: 0, chars: 0 };
      current.calls += 1;
      current.chars += Math.max(0, Number(item?.chars) || 0);
      byTool.set(name, current);
    }
    const toolSummary = [...byTool.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([name, value]) => `${name}=${value.calls}/${value.chars}`)
      .join(',') || '-';
    /* **workspace 를 적는다.** 공유 worker 에는 여러 workspace 요청이 섞여 들어오므로,
     * 이 줄에 workspace 가 없으면 「어느 쪽이 느린가·어느 쪽이 근거를 못 찾나」 를
     * journal 만 보고는 못 가린다(Codex 리뷰 P2).
     *
     * 적는 값은 **대조를 통과한 `request.workspace`** 다 — 명부에 있는 key 라는 뜻이고,
     * 그래서 임의 문자열이 로그로 들어오지 않는다. 원문·질문·검색어·키·token 은 계속
     * 안 적는다. */
    log?.info?.(
      `[rpc] 완료 request=${request.requestId} workspace=${request.workspace} `
      + `status=${result.status} `
      + `elapsed_ms=${Math.max(0, Number(usage.elapsed_ms) || 0)} `
      + `model=${String(usage.model || '-')} stop_reason=${String(usage.stop_reason || '-')} `
      + `in_tok=${Math.max(0, Number(usage.input_tokens) || 0)} `
      + `out_tok=${Math.max(0, Number(usage.output_tokens) || 0)} `
      + `tool_calls=${Math.max(0, Number(usage.tool_calls) || 0)} tools=${toolSummary}`,
    );

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
    // 그룹에게만 연다. `mode: null` 은 **개발 PC 의 named pipe 전용**이다 — 운영
    // 기본값은 언제나 0660 이고, 그 자리를 끄는 길을 설정으로 내보내지 않는다
    // (내보내면 언젠가 운영에서 꺼진다).
    if (mode != null) {
      fs.chmodSync(socketPath, mode);
      alignSocketGroup(socketPath);
    }
    return socketPath;
  }

  function close() {
    return new Promise((resolve) => server.close(() => resolve()));
  }

  return {
    listen, close, server,
    get inFlightCount() { return inFlight.size; },
    /** 동시성 게이트. **시험이 슬롯 반환을 실측한다** — 세는 값이 없으면 「돌려줬다」 를
     * 증명할 길이 호출 횟수뿐이고, 그건 두 번 돌려준 버그를 못 잡는다. */
    limits,
  };
}

/**
 * 소켓의 그룹이 **디렉터리 그룹과 같은가.** 다르면 맞추고, 못 맞추면 **던진다.**
 *
 * ## 왜 재는가
 *
 * TYBot 은 공유 그룹(`tybot-specialist-rpc`)으로 들어온다. 우리 프로세스의 기본
 * 그룹은 `hermes-rpc-<ws>` 라, 그냥 만들면 소켓이 **그 그룹**을 받는다 — 0660 이어도
 * TYBot 은 못 붙는다.
 *
 * worker 는 먼저 디렉터리를 TYBot 도구 소켓 그룹에 맞춘다. 새 소켓은 프로세스 기본
 * 그룹으로 생길 수 있으므로 여기서 실제로 재고, 디렉터리 그룹으로 고쳐 보고, 못
 * 고치면 **기동을 막는다.**
 *
 * 고칠 수 있는 이유: POSIX 에서 파일 소유자는 **자기가 속한 그룹으로는** 그룹을
 * 바꿀 수 있다. worker 는 소켓 소유자이고 공유 그룹의 보조 멤버다(`SupplementaryGroups`).
 * 저쪽 `ToolServer` 가 `os.chown(path, -1, gid)` 로 하는 것과 같은 일이다.
 *
 * 그룹 **이름**을 여기 적지 않는다 — 디렉터리의 gid 를 기준으로 삼으면 이름이 바뀌어도
 * 맞고, 이름을 적으면 Node 에는 이름→gid 변환이 없어 `/etc/group` 을 파싱하게 된다.
 */
export function alignSocketGroup(socketPath, { stat = fs.statSync, chown = fs.chownSync } = {}) {
  let dir;
  let sock;
  try {
    dir = stat(path.dirname(socketPath));
    sock = stat(socketPath);
  } catch (err) {
    // 그룹을 확인할 수 없다. Windows 등 gid 가 없는 자리에서는 둘 다 0 이라 여기 안 온다.
    throw new Error(
      `소켓 권한을 확인하지 못했습니다: ${err.code || err.message}\n`
      + `  ${socketPath}`,
    );
  }
  if (sock.gid === dir.gid) return dir.gid;

  try {
    chown(socketPath, -1, dir.gid);
  } catch (err) {
    throw new Error(
      `소켓 그룹을 디렉터리 그룹에 맞추지 못했습니다(${err.code || err.message}).\n`
      + `  소켓 ${socketPath} 의 그룹은 ${sock.gid}, 디렉터리는 ${dir.gid} 입니다.\n`
      + '  worker가 공유 그룹의 보조 멤버인지 확인하세요.\n'
      + '  이 상태로 띄우면 TYBot 이 소켓에 못 붙고, 증상은 오류가 아니라'
      + ' **아무 응답이 없는 것**이라 원인을 찾기 어렵습니다.',
    );
  }
  return dir.gid;
}

/**
 * Match the runtime directory to TYBot's tool socket group before listening.
 *
 * systemd 239 may re-apply RuntimeDirectory ownership between ExecStartPre and
 * ExecStart. The worker therefore performs the final alignment itself, using
 * the protected TYBot tool socket as the source of truth.
 */
export function alignSocketDirectoryGroup(
  socketPath,
  referenceSocketPath,
  { stat = fs.statSync, chown = fs.chownSync, chmod = fs.chmodSync } = {},
) {
  const directory = path.dirname(socketPath);
  let current;
  let reference;
  try {
    current = stat(directory);
    reference = stat(referenceSocketPath);
  } catch (err) {
    throw new Error(
      `RPC socket directory group could not be verified: ${err.code || err.message}\n`
      + `  directory: ${directory}\n`
      + `  TYBot tool socket: ${referenceSocketPath}`,
    );
  }

  try {
    if (current.gid !== reference.gid) chown(directory, -1, reference.gid);
    chmod(directory, 0o750);
  } catch (err) {
    throw new Error(
      `RPC socket directory could not be aligned with the TYBot tool socket group: ${err.code || err.message}\n`
      + `  directory: ${directory}\n`
      + `  TYBot tool socket: ${referenceSocketPath}`,
    );
  }
  return reference.gid;
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
