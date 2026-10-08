/**
 * RPC worker 설정. **환경변수와 credential 파일만** 본다.
 *
 * ## `src/config.js` 를 쓰지 않는 이유
 *
 * 그쪽은 아카이브 경로·문서 저장소·Slack 설정을 전부 푼다. worker 가 그것을 들고
 * 있으면 **자료가 도구 말고 다른 문으로도 들어올 수 있는 상태**가 된다 — 인계 요구 6 이
 * 막으려는 것이 정확히 그것이다. 여기서 안 읽으면 「안 읽기로 했다」 가 아니라
 * **읽을 수 없다** 가 된다.
 *
 * 그래서 이 파일이 아는 것은 다섯 가지뿐이다.
 *   소켓 자리 · HMAC 비밀 · 모델 · 표시 용어 · 자기 버전
 *
 * ## 비밀을 환경변수 **값**으로 받지 않는다
 *
 * `systemctl show` 와 `/proc/<pid>/environ` 에 그대로 보인다. systemd 의
 * `LoadCredential=` 이 넣어 주는 파일에서 읽는다 — 그 파일은 그 유닛만 읽는다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { DomainConfigError, resolveDomain } from '../domain.js';

/** 저쪽 `specialist_v3_client._KEY` 와 **같은 규칙**이다. 다르면 경로가 갈린다. */
export const SOCKET_KEY_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

export class RpcConfigError extends Error {
  constructor(message) {
    super(message);
    this.name = 'RpcConfigError';
    this.code = 'hermes_rpc_config';
  }
}

/**
 * 소켓 자리. **경로를 받지 않고 key 로 계산한다.**
 *
 * 받으면 그 값이 곧 「아무 자리에나 listen 하는 권한」 이고, 그 자리를 TYBot 이
 * 부를 리 없으므로 증상은 **조용한 무응답**이다. 저쪽(`specialist_v3_client.socket_path`)
 * 과 같은 식으로 계산해야 두 쪽이 같은 파일을 가리킨다.
 */
export function socketPathFor(specialist, workspace) {
  for (const [name, value] of [['specialist', specialist], ['workspace', workspace]]) {
    if (!SOCKET_KEY_RE.test(String(value ?? ''))) {
      throw new RpcConfigError(
        `소켓 key 가 올바르지 않습니다(${name}): ${JSON.stringify(value)}\n`
        + '  소문자·숫자·하이픈만 쓸 수 있고 하이픈으로 시작할 수 없습니다.\n'
        + '  이 규칙은 TYBot 쪽 계산식과 같아야 합니다 — 다르면 두 쪽이 다른 파일을 가리키고,'
        + ' 증상은 오류가 아니라 **아무 응답이 없는 것**입니다.',
      );
    }
  }
  return `/run/tybot-subbots/${specialist}-${workspace}/http.sock`;
}

/** HMAC 비밀 한 조각. **길이를 본다** — 짧은 키는 서명이 아니라 장식이다. */
export function readSecret({ credentialsDir, file }) {
  const target = file
    || (credentialsDir ? path.join(credentialsDir, 'hmac') : null);
  if (!target) {
    throw new RpcConfigError(
      'HMAC 비밀을 찾지 못했습니다.\n'
      + '  systemd 라면 `LoadCredential=hmac:<경로>` 가 주는 $CREDENTIALS_DIRECTORY 를 씁니다.\n'
      + '  그 밖에는 HERMES_RPC_HMAC_FILE 에 파일 경로를 주세요.\n'
      + '  **환경변수 값으로 주지 마세요** — `systemctl show` 와 /proc 에 그대로 보입니다.',
    );
  }
  let raw;
  try {
    raw = fs.readFileSync(target);
  } catch (err) {
    throw new RpcConfigError(`HMAC 비밀 파일을 읽지 못했습니다: ${err.code || err.message}`);
  }
  // 파일 끝 줄바꿈 하나는 사람이 편집기로 만들면 늘 붙는다. 그것만 떼고 나머지는 그대로 쓴다.
  const secret = raw.length && raw[raw.length - 1] === 0x0a ? raw.subarray(0, raw.length - 1) : raw;
  if (secret.length < 32) {
    throw new RpcConfigError(
      `HMAC 비밀이 너무 짧습니다(${secret.length}바이트). 32바이트 이상을 쓰세요.`,
    );
  }
  return secret;
}

/**
 * 환경 → worker 설정. **기본값으로 물러서는 자리는 없다.**
 *
 * 빠진 값은 전부 기동을 막는다. 돌아가되 틀린 자리를 가리키는 worker 는 「없는 것」
 * 보다 나쁘다 — 없으면 그 자리에서 알지만, 틀린 자리는 아무도 안 부르는 것으로만
 * 나타나고 그 상태가 며칠 간다.
 */
export function loadRpcConfig(env = process.env) {
  const domainRaw = env.HERMES_DOMAIN || '';
  let domain;
  try {
    domain = resolveDomain(domainRaw);
  } catch {
    throw new DomainConfigError(domainRaw);
  }

  const specialist = String(env.HERMES_RPC_SPECIALIST || 'hermes').trim();
  const workspace = String(env.HERMES_RPC_WORKSPACE || '').trim();
  if (!workspace) {
    throw new RpcConfigError(
      'HERMES_RPC_WORKSPACE 가 없습니다.\n'
      + '  이 값이 소켓 자리와 요청 대조의 기준입니다 — 인스턴스마다 다릅니다.',
    );
  }

  const model = String(env.HERMES_RPC_MODEL || 'claude-opus-5-5').trim();
  const maxTokens = Number.parseInt(env.HERMES_RPC_MAX_TOKENS || '8000', 10);
  const effort = String(env.HERMES_RPC_EFFORT || 'high').trim();
  const maxIterations = Number.parseInt(env.HERMES_RPC_MAX_ITERATIONS || '12', 10);
  if (!Number.isFinite(maxTokens) || maxTokens <= 0) {
    throw new RpcConfigError(`HERMES_RPC_MAX_TOKENS 가 숫자가 아닙니다: ${env.HERMES_RPC_MAX_TOKENS}`);
  }
  if (!Number.isFinite(maxIterations) || maxIterations <= 0) {
    throw new RpcConfigError(`HERMES_RPC_MAX_ITERATIONS 가 숫자가 아닙니다: ${env.HERMES_RPC_MAX_ITERATIONS}`);
  }

  return Object.freeze({
    domain,
    specialist,
    workspace,
    socketPath: socketPathFor(specialist, workspace),
    secret: readSecret({
      credentialsDir: env.CREDENTIALS_DIRECTORY,
      file: env.HERMES_RPC_HMAC_FILE,
    }),
    model: Object.freeze({ id: model, maxTokens, effort }),
    maxIterations,
    /** health 가 밝히는 자기 버전. 배포가 무엇을 올렸는지 사람이 대조하는 값이다. */
    version: String(env.HERMES_RPC_VERSION || '').trim() || 'unversioned',
  });
}
