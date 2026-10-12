#!/usr/bin/env node
/**
 * **공유** 전문봇 RPC worker — 여러 workspace 를 한 프로세스가 받는다.
 *
 *   node scripts/run-rpc-shared.js
 *
 * 인계: `docs/verification/2026-10-12-hermes-shared-rpc-handoff.md`
 * 회신: `docs/verification/2026-10-12-hermes-shared-rpc-handback.md`
 * 계약: `docs/design/specialist-v3-tool-contract.md` (스키마는 v3 그대로다)
 *
 * ## 개별 worker 와 무엇이 다른가
 *
 * **전선 위는 하나도 안 다르다.** 스키마·서명·nonce·health·취소·요청 격리 규칙이
 * 같고, 요청의 `workspace` 는 실제 workspace key 다 — `shared` 로 바꾸지 않는다.
 *
 * 다른 것은 **누가 어떤 설정으로 답하는가**뿐이다. 개별 worker 는 env 하나로
 * domain·모델·키가 정해지고, 공유 worker 는 **요청의 workspace 로 명부를 찾아** 정한다.
 *
 * ## 신뢰 경계가 넓어진다 — 숨기지 않는다
 *
 * 한 프로세스가 여러 workspace 의 API 키를 메모리에 들고 있다. 그 프로세스가 새면
 * **전부** 샌다. 장애도 같다 — 재시작 한 번이 공유 대상 전체를 끊는다.
 * OS 수준 격리가 필요한 workspace 는 **개별 worker 를 그대로 쓴다**
 * (`hermes-rpc@<ws>.service` 는 이 작업으로 바뀌지 않았다).
 *
 * ## 요청끼리 무엇도 공유하지 않는다
 *
 * 요청마다 새로 만드는 것: 모델 client · 도구 client · 도구 객체 · 발급 근거 집합 ·
 * trace · AbortController · 대화. **프로세스에 하나뿐인 것**은 HMAC 인증기(재생 방지
 * 기억)와 동시성 게이트뿐이고, 둘 다 workspace 를 가로지르는 값을 안 들고 있다.
 *
 * API 키를 요청마다 client 에 넣는다. `process.env` 는 **건드리지 않는다** —
 * 한 요청이 전역을 바꾸면 그 사이에 도는 다른 workspace 요청이 남의 키로 나간다.
 */
import { pathToFileURL } from 'node:url';
import Anthropic from '@anthropic-ai/sdk';
import { TYBOT, mode } from '../src/mode.js';
import { createToolSession } from '../src/llm/provider.js';
import { lazyTerms } from '../src/domain.js';
import { createRpcAnswerer } from '../src/rpc/answer.js';
import { alignSocketDirectoryGroup, createRpcServer } from '../src/rpc/server.js';
import { createToolClient } from '../src/rpc/tool-client.js';
import { buildRemoteTools } from '../src/rpc/tools.js';
import { createConcurrencyGate } from '../src/rpc/limits.js';
import { loadProfiles } from '../src/rpc/profiles.js';
import { loadSharedRpcConfig } from '../src/rpc/config.js';
import { renderRpcPrompt } from '../src/rpc/prompt.js';
import { TOOL_SOCKET_PATH } from '../src/rpc/wire.js';

/**
 * 프로필 하나짜리 답변기. **요청마다 부른다.**
 *
 * 모델 client 를 workspace 별로 캐시하지 않는다. 캐시해도 기능은 같지만, 캐시가
 * 있으면 「저 client 에 어느 키가 들어 있나」 를 사람이 추적해야 하고 — 그 추적이
 * 틀리는 날 남의 키로 나간다. 요청마다 만들면 **추적할 것이 없다.**
 * 비용은 객체 하나이고, 동시 요청 상한이 한 자릿수라 문제가 되지 않는다.
 */
export function answererFor(profile, { toolSocketPath, secret, console: log }) {
  const anthropic = () => new Anthropic({ apiKey: profile.apiKey });
  return createRpcAnswerer({
    toolSession: (spec) => createToolSession(spec, { client: anthropic }),
    buildTools: buildRemoteTools,
    createClient: ({ requestId, token, allow }) => createToolClient({
      socketPath: toolSocketPath, secret, requestId, token, allow,
    }),
    // 말투는 **그 workspace 의 것**이다. 공유 프로세스가 한 말투로 통일하면
    // 사내 사람에게 「사업장」 이라고 말하게 된다.
    renderPrompt: ({ maxOutputChars }) => renderRpcPrompt({ domain: profile.domain, maxOutputChars }),
    model: profile.model,
    maxIterations: profile.maxIterations,
    terms: lazyTerms(profile.domain),
    console: log,
  });
}

/**
 * 공유 worker 한 벌을 **배선만** 한다. 소켓은 열지 않는다 — 시험이 같은 배선을
 * 임시 자리에서 띄울 수 있어야 한다.
 */
export function wireSharedWorker(cfg, {
  profiles, readiness, toolSocketPath = TOOL_SOCKET_PATH, console: log = globalThis.console,
}) {
  const gate = createConcurrencyGate(cfg.limits);

  /* **workspace → 답변기.** 모르는 이름이면 `null` 이고, 서버는 그걸 400 으로 접는다.
   * 허용 목록이 여기 한 곳뿐이라 「어디선가 또 허용되고 있나」 를 묻지 않아도 된다. */
  function route(workspace) {
    const profile = profiles.get(workspace);
    if (!profile) return null;
    return answererFor(profile, { toolSocketPath, secret: cfg.secret, console: log });
  }

  return createRpcServer({
    secret: cfg.secret,
    version: cfg.version,
    route,
    limits: gate,
    readiness,
    console: log,
  });
}

async function main() {
  // 모드를 **고정한다.** 공유든 개별이든 RPC 는 TYBot 밑에서 도는 운영이고,
  // 그 모드에서 원문 쓰기와 독립 Slack 역할이 전부 막혀 있다.
  const current = mode();
  if (current !== TYBOT) {
    throw Object.assign(new Error(
      `공유 RPC worker 는 HERMES_MODE=${TYBOT} 에서만 띄웁니다(지금: ${current}).`,
    ), { code: 'hermes_rpc_wrong_mode' });
  }

  const cfg = loadSharedRpcConfig();
  const { profiles, readiness, problems } = loadProfiles(cfg.profilesFile);

  const worker = wireSharedWorker(cfg, { profiles, readiness });

  // 개별 worker 와 **같은 보정**이다. systemd 239 는 ExecStartPre 뒤에
  // RuntimeDirectory 소유권을 다시 적용하므로, 마지막 정렬을 worker 가 직접 한다.
  // 기준은 TYBot 도구 소켓의 그룹 — 그게 「TYBot 이 들어올 수 있는 그룹」 의 정의다.
  alignSocketDirectoryGroup(cfg.socketPath, TOOL_SOCKET_PATH);
  const at = await worker.listen(cfg.socketPath);

  // **workspace key 와 수만 찍는다.** 질문·원문·키·토큰은 journal 에 안 남긴다(인계 §로그).
  console.log(
    `[rpc-shared] 준비됨 · ${at}\n`
    + `             workspace ${[...profiles.keys()].join(', ')}\n`
    + `             동시 상한 전체 ${cfg.limits.max} · workspace 당 ${cfg.limits.maxPerWorkspace}`
    + ` · 버전 ${cfg.version}`,
  );
  // 못 읽은 workspace 는 **살아서 돌되 그 workspace 만 거절된다.** 한 칸이 망가졌다고
  // 전부 멈추면 그 영향이 사고가 된다 — 대신 여기와 health 양쪽에 남긴다.
  for (const problem of problems) console.error(`[rpc-shared] 준비 안 됨 — ${problem}`);

  for (const signal of ['SIGTERM', 'SIGINT']) {
    process.once(signal, () => { worker.close().finally(() => process.exit(0)); });
  }
}

// 직접 실행일 때만 뜬다. 시험은 `wireSharedWorker` 를 가져다 쓴다.
if (process.argv[1] && pathToFileURL(process.argv[1]).href === import.meta.url) {
  main().catch((err) => {
    console.error(err?.message || String(err));
    process.exitCode = err?.code === 'hermes_rpc_wrong_mode' ? 2 : 1;
  });
}
