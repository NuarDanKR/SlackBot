/**
 * 동시 요청 자리. **줄을 세우지 않는다.**
 *
 * ## 왜 대기열이 아닌가
 *
 * 공유 worker 는 여러 워크스페이스가 같이 쓴다. 자리가 없을 때 줄을 세우면, 줄 끝의
 * 요청은 **저쪽 deadline 이 먼저 끝난 뒤에** 모델을 부르기 시작한다 — 아무도 안
 * 기다리는 답을 비용을 내며 만드는 상태다. 더 나쁜 것은 그게 **보이지 않는다**는
 * 점이다: 저쪽 로그에는 `transport`, 우리 로그에는 정상 완료가 찍힌다.
 *
 * 그래서 자리가 없으면 **그 자리에서 429** 다. 저쪽은 그걸 실패로 닫고 임의 폴백을
 * 하지 않는다(인계 §동시성). 사람은 「지금 밀린다」 를 바로 안다.
 *
 * ## 두 겹인 이유
 *
 * 전체 상한만 두면 한 워크스페이스가 전부를 먹고, 다른 워크스페이스는 **자기 잘못이
 * 아닌 이유로** 전부 막힌다. 워크스페이스 상한만 두면 워크스페이스 수가 늘 때
 * 전체 부하가 같이 는다. 둘 다 둔다.
 */

/** 상한 넘김 사유. 로그에만 쓰고 **응답에는 안 싣는다** — 밖에서 부하를 추측하게 둘 이유가 없다. */
export const OVER_GLOBAL = 'global';
export const OVER_WORKSPACE = 'workspace';

/**
 * @param {object} o
 * @param {number} o.max         전체 동시 요청 상한
 * @param {number} o.maxPerWorkspace  워크스페이스 하나의 상한
 * @returns {{acquire:(ws:string)=>(null|Function), inFlight:number, forWorkspace:Function, lastReason:string}}
 *   `acquire` 는 **자리를 돌려주는 함수**를 주거나, 자리가 없으면 `null` 을 준다.
 */
export function createConcurrencyGate({ max, maxPerWorkspace }) {
  if (!Number.isFinite(max) || max < 1) {
    throw new Error(`전체 동시 상한이 올바르지 않습니다: ${max}`);
  }
  if (!Number.isFinite(maxPerWorkspace) || maxPerWorkspace < 1) {
    throw new Error(`워크스페이스 동시 상한이 올바르지 않습니다: ${maxPerWorkspace}`);
  }

  let total = 0;
  /** workspace → 지금 도는 수. 0 이 되면 **지운다** — 안 지우면 key 가 영영 쌓인다. */
  const perWorkspace = new Map();
  let lastReason = '';

  function acquire(workspace) {
    const ws = String(workspace);
    const mine = perWorkspace.get(ws) || 0;
    if (total >= max) { lastReason = OVER_GLOBAL; return null; }
    if (mine >= maxPerWorkspace) { lastReason = OVER_WORKSPACE; return null; }

    total += 1;
    perWorkspace.set(ws, mine + 1);
    lastReason = '';

    /* **두 번 불려도 한 번만 돌려준다.** 호출부의 `finally` 와 다른 경로가 겹쳐
     * 두 번 부르면 자리가 늘어나 상한이 조용히 무너진다 — 그건 상한이 없는 것보다
     * 나쁘다(있다고 믿으므로 아무도 안 본다). */
    let done = false;
    return function release() {
      if (done) return;
      done = true;
      total -= 1;
      const left = (perWorkspace.get(ws) || 1) - 1;
      if (left <= 0) perWorkspace.delete(ws);
      else perWorkspace.set(ws, left);
    };
  }

  return {
    acquire,
    get inFlight() { return total; },
    forWorkspace: (ws) => perWorkspace.get(String(ws)) || 0,
    /** 마지막 거절 사유. 시험과 로그용이고 **응답에는 안 나간다.** */
    get lastReason() { return lastReason; },
    get trackedWorkspaces() { return perWorkspace.size; },
  };
}
