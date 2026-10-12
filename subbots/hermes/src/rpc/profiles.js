/**
 * 공유 worker 의 **workspace 명부**. 운영자가 손으로 관리하는 정적 매핑이다.
 *
 * ```json
 * {
 *   "tyit": { "domain": "pf-construction", "api_key_file": "/etc/hermes-rpc-shared/keys/tyit" },
 *   "mgmt": { "domain": "enterprise",      "api_key_file": "/etc/hermes-rpc-shared/keys/mgmt",
 *             "model": "claude-opus-5-5", "effort": "high" }
 * }
 * ```
 *
 * ## 허용 목록이 **서버 쪽에 있다**
 *
 * 요청 본문이 domain·모델·키 파일을 고르지 않는다(인계 §6). 요청이 하는 일은 「나는
 * tyit 이다」 라고 말하는 것뿐이고, 그 이름이 이 파일에 있어야 한다. 없으면 거절이다.
 *
 * 뒤집으면 — 클라이언트가 고를 수 있게 하는 순간 **요청 하나로 남의 키를 쓰게** 할 수
 * 있고, 그 요청이 실제로 답을 받아 가므로 아무 경보도 안 울린다.
 *
 * ## 전역 fallback 키를 두지 않는다
 *
 * 「목록에 없으면 공용 키로」 를 한 줄 넣으면 미등록 workspace 가 **그냥 돈다.**
 * 그 상태는 오류가 아니라 「잘 되는데 비용이 엉뚱한 데 찍히는」 모양이고,
 * 비용 귀속이 틀어진 것을 몇 달 뒤에 안다.
 *
 * ## 비밀은 JSON 에 없다
 *
 * 키 **값**이 아니라 키 **파일 경로**를 적는다. JSON 은 운영자가 열어 보고 콘솔에
 * 띄우고 메신저로 보내는 파일이다. 값을 적으면 그 모든 자리로 따라간다.
 * 값이 적힌 것 같으면 **읽기를 거부한다**(아래 `FORBIDDEN_KEYS`).
 */
import fs from 'node:fs';
import path from 'node:path';
import { resolveDomain } from '../domain.js';

/** workspace key 모양. 소켓 key 와 같은 규칙이다. */
export const WORKSPACE_RE = /^[a-z0-9][a-z0-9-]{0,62}$/;

/** 이 이름이 보이면 **비밀을 적은 것**이다. 값을 보지 않고 이름만 보고 막는다. */
const FORBIDDEN_KEYS = ['api_key', 'apikey', 'apiKey', 'key', 'secret', 'token', 'password'];

/** 쓸 수 있는 필드. 모르는 필드는 거절한다 — 오타가 조용히 무시되면 설정이 거짓말을 한다. */
const ALLOWED_KEYS = ['domain', 'api_key_file', 'model', 'effort', 'max_tokens', 'max_iterations'];

export class ProfilesError extends Error {
  constructor(message) {
    super(message);
    this.name = 'ProfilesError';
    this.code = 'hermes_rpc_profiles';
  }
}

/** API 키 한 줄. **값은 어디에도 안 찍는다** — 길이만 본다. */
function readApiKey(file) {
  const raw = fs.readFileSync(file, 'utf8');
  // 사람이 편집기로 만들면 끝 줄바꿈이 늘 붙는다. 그것만 떼고 나머지는 그대로 쓴다.
  const key = raw.replace(/\r?\n$/, '');
  if (key.length < 20) {
    throw new ProfilesError(
      `API 키 파일이 너무 짧습니다(${key.length}자): ${file}\n`
      + '  빈 파일을 놓아 두면 「등록은 됐는데 모든 질문이 실패」 가 되고,'
      + ' 그 증상은 키가 틀린 것과 구별되지 않습니다.',
    );
  }
  if (/\s/.test(key)) {
    throw new ProfilesError(
      `API 키에 공백·줄바꿈이 섞여 있습니다: ${file}\n`
      + '  파일 끝 줄바꿈 하나는 떼지만 그 밖의 공백은 떼지 않습니다 — 떼기 시작하면'
      + ' 진짜 키의 일부를 떼는 날이 옵니다.',
    );
  }
  return key;
}

/**
 * 명부를 읽어 workspace 별 프로필을 만든다.
 *
 * **한 칸이 망가져도 나머지는 산다.** 공유 worker 는 재시작이 전부에 영향을 주므로,
 * `mgmt` 의 키 파일이 지워졌다고 `tyit` 까지 멈추면 그 영향이 사고가 된다. 못 읽은
 * workspace 는 `ready: false` 로 남고 **그 workspace 요청만** 거절된다(health 에 보인다).
 *
 * 다만 **하나도 못 읽으면 던진다** — 그건 돌아도 아무것도 못 하는 상태이고,
 * 조용히 떠 있으면 아무도 안 본다.
 *
 * @param {string} file  profiles.json 경로
 * @returns {{profiles:Map<string,object>, readiness:()=>object, problems:string[]}}
 */
export function loadProfiles(file, { read = fs.readFileSync, readKey = readApiKey } = {}) {
  if (!file) {
    throw new ProfilesError(
      'HERMES_RPC_PROFILES_FILE 이 없습니다.\n'
      + '  공유 worker 는 어떤 workspace 를 받을지 **설정에서** 받습니다 —'
      + ' 요청이 고르게 두면 요청 하나로 남의 키를 쓰게 할 수 있습니다.',
    );
  }

  let text;
  try {
    text = read(file, 'utf8');
  } catch (err) {
    throw new ProfilesError(`명부를 읽지 못했습니다(${err.code || err.message}): ${file}`);
  }

  let raw;
  try {
    raw = JSON.parse(text);
  } catch (err) {
    throw new ProfilesError(`명부가 JSON 이 아닙니다: ${file}\n  ${err.message}`);
  }
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new ProfilesError(`명부의 최상위가 객체가 아닙니다: ${file}`);
  }

  const names = Object.keys(raw);
  if (!names.length) {
    throw new ProfilesError(
      `명부가 비었습니다: ${file}\n`
      + '  빈 명부로 뜨면 모든 요청이 400 이고, 그건 「설정이 없다」 가 아니라'
      + ' 「workspace 를 잘못 보냈다」 로 읽힙니다.',
    );
  }

  const profiles = new Map();
  const problems = [];

  for (const name of names) {
    const entry = raw[name];
    try {
      if (name === '*' || name.includes('*')) {
        throw new ProfilesError('wildcard 는 쓸 수 없습니다 — 미등록 workspace 가 그냥 돕니다.');
      }
      if (!WORKSPACE_RE.test(name)) {
        throw new ProfilesError('workspace key 는 소문자·숫자·하이픈만 쓰고 하이픈으로 시작할 수 없습니다.');
      }
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
        throw new ProfilesError('프로필이 객체가 아닙니다.');
      }

      const leaked = Object.keys(entry).filter((k) => FORBIDDEN_KEYS.includes(k));
      if (leaked.length) {
        throw new ProfilesError(
          `비밀을 명부에 적지 마세요(${leaked.join(', ')}). 키 **파일 경로**를 `
          + '`api_key_file` 에 적습니다 — 이 파일은 사람이 열어 보고 화면에 띄우는 파일입니다.',
        );
      }
      const unknown = Object.keys(entry).filter((k) => !ALLOWED_KEYS.includes(k));
      if (unknown.length) {
        throw new ProfilesError(
          `모르는 필드가 있습니다: ${unknown.join(', ')}\n`
          + `  쓸 수 있는 것: ${ALLOWED_KEYS.join(' · ')}\n`
          + '  모르는 필드를 조용히 무시하면 오타가 설정처럼 보입니다.',
        );
      }

      // **도메인은 기본값이 없다.** 없으면 사내 인스턴스가 사내 사람에게 「사업장」
      // 이라고 말하고, 그건 오류가 아니라 어색한 문장으로만 나타난다.
      const domain = resolveDomain(entry.domain);

      const keyFile = String(entry.api_key_file || '');
      if (!keyFile) throw new ProfilesError('`api_key_file` 이 없습니다.');
      if (!path.isAbsolute(keyFile)) {
        throw new ProfilesError(`\`api_key_file\` 은 절대경로여야 합니다: ${keyFile}`);
      }
      const apiKey = readKey(keyFile);

      const model = String(entry.model || 'claude-opus-5-5').trim();
      const effort = String(entry.effort || 'high').trim();
      const maxTokens = entry.max_tokens === undefined ? 8000 : Number(entry.max_tokens);
      const maxIterations = entry.max_iterations === undefined ? 12 : Number(entry.max_iterations);
      for (const [label, value] of [['max_tokens', maxTokens], ['max_iterations', maxIterations]]) {
        if (!Number.isFinite(value) || value <= 0) {
          throw new ProfilesError(`${label} 이 올바르지 않습니다: ${entry[label]}`);
        }
      }

      profiles.set(name, Object.freeze({
        workspace: name,
        domain,
        apiKey,
        apiKeyFile: keyFile,
        model: Object.freeze({ id: model, maxTokens, effort }),
        maxIterations,
      }));
    } catch (err) {
      // **사유에 workspace 이름은 남기고 키 값은 안 남긴다.**
      problems.push(`${name}: ${err.message.split('\n')[0]}`);
    }
  }

  if (!profiles.size) {
    throw new ProfilesError(
      `쓸 수 있는 workspace 가 하나도 없습니다: ${file}\n  ${problems.join('\n  ')}`,
    );
  }

  /** health 에 실을 모양. **key 와 준비 여부뿐이다.** */
  const readiness = () => {
    const out = {};
    for (const name of names) out[name] = profiles.has(name);
    return out;
  };

  return { profiles, readiness, problems };
}
