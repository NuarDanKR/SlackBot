#!/usr/bin/env node
/**
 * 전문봇 RPC worker — TYBot 이 부르는 `/v3/answer` 를 받는 **별도 프로세스**.
 *
 *   node scripts/run-rpc-worker.js
 *
 * 설계: `docs/design/specialist-v3-tool-contract.md`
 * 인계: `docs/verification/2026-10-08-hermes-external-router-claude-handoff.md`
 *
 * ## 왜 Slack 프로세스와 같이 안 띄우나
 *
 * Socket Mode 는 **한 프로세스의 이벤트 루프**로 ping/pong 을 주고받는다. RPC 답변은
 * 모델을 여러 번 부르며 수십 초를 쓰므로, 같은 프로세스에 얹으면 그 동안 ping 이
 * 늦고 Slack 이 연결을 끊는다. 끊기면 **질문이 아예 안 들어온다** — 느려지는 게
 * 아니라 조용히 멈춘다(인계 요구 10).
 *
 * ## 이 프로세스가 **못 하는** 것
 *
 * Slack 을 가져오지 않는다. 아카이브도 안 읽는다. 자료는 TYBot 도구 소켓으로만
 * 들어오고, 그 문은 요청 하나짜리 토큰으로 열린다. 「안 한다」 가 아니라 **할 수
 * 없다** 로 둔 자리이고, `scripts/check-rpc-isolation.js` 가 그걸 잰다.
 */
import { pathToFileURL } from 'node:url';
import Anthropic from '@anthropic-ai/sdk';
import { TYBOT, mode } from '../src/mode.js';
import { createToolSession } from '../src/llm/provider.js';
import { lazyTerms } from '../src/domain.js';
import { createRpcAnswerer } from '../src/rpc/answer.js';
import { createRpcServer } from '../src/rpc/server.js';
import { createToolClient } from '../src/rpc/tool-client.js';
import { buildRemoteTools } from '../src/rpc/tools.js';
import { loadRpcConfig } from '../src/rpc/config.js';
import { renderRpcPrompt } from '../src/rpc/prompt.js';
import { TOOL_SOCKET_PATH } from '../src/rpc/wire.js';

/**
 * worker 한 벌을 **배선만** 한다. 소켓은 열지 않는다 — 시험이 같은 배선을 임시
 * 자리에서 띄울 수 있어야 하고, 배선과 기동을 한 함수에 묶으면 그게 불가능해진다.
 */
export function wireWorker(cfg, { toolSocketPath = TOOL_SOCKET_PATH, console: log = globalThis.console } = {}) {
  let client;
  const anthropic = () => (client ??= new Anthropic());
  const terms = lazyTerms(cfg.domain);

  const answerer = createRpcAnswerer({
    toolSession: (spec) => createToolSession(spec, { client: anthropic }),
    buildTools: buildRemoteTools,
    createClient: ({ requestId, token, allow }) => createToolClient({
      socketPath: toolSocketPath, secret: cfg.secret, requestId, token, allow,
    }),
    renderPrompt: ({ maxOutputChars }) => renderRpcPrompt({ domain: cfg.domain, maxOutputChars }),
    model: cfg.model,
    maxIterations: cfg.maxIterations,
    terms,
    console: log,
  });

  return createRpcServer({
    secret: cfg.secret, workspace: cfg.workspace, version: cfg.version, answerer, console: log,
  });
}

async function main() {
  // 모드를 **고정한다.** RPC 는 TYBot 밑에서 도는 운영이고, 그 모드에서 원문 쓰기와
  // 독립 Slack 역할이 전부 막혀 있다. 다른 모드로 띄우면 그 두 가지가 열린 채로
  // RPC 까지 도는데, 열린 것은 원문이 늘어난 뒤에야 알게 된다.
  const current = mode();
  if (current !== TYBOT) {
    throw Object.assign(new Error(
      `RPC worker 는 HERMES_MODE=${TYBOT} 에서만 띄웁니다(지금: ${current}).\n`
      + '  이 프로세스는 TYBot 이 권한 도구와 함께 부르는 쪽이고, 자기 권한으로 자료를 읽지 않습니다.\n'
      + `  PF 직접 실행이라면 이 스크립트가 아니라 npm start 입니다.`,
    ), { code: 'hermes_rpc_wrong_mode' });
  }

  const cfg = loadRpcConfig();
  const worker = wireWorker(cfg);
  const at = await worker.listen(cfg.socketPath);
  console.log(
    `[rpc] ${cfg.specialist}-${cfg.workspace} 준비됨 · ${at}\n`
    + `      도메인 ${cfg.domain} · 모델 ${cfg.model.id} · 버전 ${cfg.version}`,
  );

  // 종료 신호에 소켓을 **반드시** 치운다. 남기면 다음 기동이 `EADDRINUSE` 로 안 뜨고,
  // 그 실패는 systemd 재시작 상한을 태운 뒤 영구 failed 로 굳는다.
  for (const signal of ['SIGTERM', 'SIGINT']) {
    process.once(signal, () => {
      worker.close().finally(() => process.exit(0));
    });
  }
}

// 직접 실행일 때만 뜬다. 시험은 `wireWorker` 를 가져다 쓴다.
// `pathToFileURL` 을 쓴다 — 손으로 `file://` 를 붙이면 Windows 경로와 공백에서 어긋난다.
if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  main().catch((err) => {
    console.error(err?.message || String(err));
    process.exitCode = err?.code === 'hermes_rpc_wrong_mode' ? 2 : 1;
  });
}
