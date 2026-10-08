/**
 * 요청 인증 — 서명·시계·재생.
 *
 * 저쪽 거울: `src/tybot/specialist_v3_tool_server.py` 의 `RequestAuthenticator`.
 * **같은 규칙으로 막는다** — 한쪽만 느슨하면 느슨한 쪽이 실효 규칙이 된다.
 *
 * ## 소켓 권한이 1차, HMAC 이 2차
 *
 * 소켓 권한만 믿으면 **그 사용자로 도는 아무 프로세스나** 전문 봇을 부를 수 있다.
 * 그 프로세스가 질문을 지어 보내면 우리는 TYBot 의 도구를 그 질문으로 돌린다.
 *
 * ## nonce 를 왜 들고 있나
 *
 * 서명만 보면 **같은 요청을 몇 번이고 다시 보낼 수 있다.** 질문 하나가 열 번
 * 처리되면 비용이 열 배이고, 더 나쁘게는 도구 예산을 태워 **진짜 질문이 못 돈다.**
 *
 * 들고 있는 창은 시계 오차와 같다(30초). 그보다 오래 들고 있을 이유가 없다 —
 * 창 밖의 요청은 시각 검사에서 이미 떨어진다.
 */
import { CLOCK_SKEW_SECONDS, canonical, safeEqualHex, sign } from './wire.js';

export const REASON_OK = 'ok';
export const REASON_BAD_TIMESTAMP = 'bad-timestamp';
export const REASON_SKEW = 'clock-skew';
export const REASON_BAD_NONCE = 'bad-nonce';
export const REASON_REPLAY = 'nonce-reused';
export const REASON_BAD_SIGNATURE = 'bad-signature';

/** nonce 하나의 길이 상한. 저쪽과 같다 — 길이로 메모리를 밀어 넣는 것을 막는다. */
const MAX_NONCE_LENGTH = 128;

/**
 * 인증기 하나. **프로세스에 하나만 둔다** — 요청마다 새로 만들면 nonce 기억이
 * 매번 비어, 재생 방지가 있는 척만 하고 실제로는 없다.
 */
export function createAuthenticator({ secret, now = () => Date.now() / 1000, skewSeconds = CLOCK_SKEW_SECONDS }) {
  if (!secret || !secret.length) {
    throw new Error('HMAC 비밀 없이 인증기를 만들 수 없습니다.');
  }
  /** nonce → 본 시각(초). 창 밖은 매번 버린다. */
  const seen = new Map();

  function sweep(current) {
    const cutoff = current - skewSeconds;
    for (const [nonce, at] of seen) {
      if (at < cutoff) seen.delete(nonce);
    }
  }

  /**
   * @returns {{ok:boolean, reason:string}} 사유를 돌려주되 **밖으로 내보내지 않는다** —
   *   호출부는 전부 `401` 로 접는다. 사유를 응답에 실으면 상대가 그걸 보고
   *   시계·키·nonce 중 무엇을 맞춰야 하는지 알아낸다.
   */
  function accepts({ method = 'POST', path, body, headers }) {
    const raw = headers?.['x-tybot-timestamp'];
    // `Number()` 를 쓰지 않는다 — 빈 문자열과 공백을 0 으로 읽는다.
    if (!/^-?\d+$/.test(String(raw ?? '').trim())) return { ok: false, reason: REASON_BAD_TIMESTAMP };
    const stamp = Number.parseInt(String(raw).trim(), 10);

    const nonce = String(headers?.['x-tybot-nonce'] ?? '');
    const provided = String(headers?.['x-tybot-signature'] ?? '');
    if (!nonce || nonce.length > MAX_NONCE_LENGTH) return { ok: false, reason: REASON_BAD_NONCE };
    if (!provided) return { ok: false, reason: REASON_BAD_SIGNATURE };

    const current = Math.floor(now());
    if (Math.abs(current - stamp) > skewSeconds) return { ok: false, reason: REASON_SKEW };

    // **서명을 nonce 기록보다 먼저 본다.** 뒤집으면 서명도 못 맞추는 상대가
    // nonce 를 아무거나 보내 우리 기억을 채울 수 있다.
    const expected = sign(secret, canonical({ method, path, timestamp: stamp, nonce, body }));
    if (!safeEqualHex(expected, provided)) return { ok: false, reason: REASON_BAD_SIGNATURE };

    sweep(current);
    if (seen.has(nonce)) return { ok: false, reason: REASON_REPLAY };
    seen.set(nonce, current);
    return { ok: true, reason: REASON_OK };
  }

  return { accepts, get size() { return seen.size; } };
}
