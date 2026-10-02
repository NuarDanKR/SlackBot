/**
 * 파생값 재계산 — 판단이 0인 값은 감시하지 말고 다시 계산한다.
 *
 * 채널 md 헤더의 `**실제 메시지**: N건` 과 `slack-export/index.md` 의 건수 표는 md 파일에서
 * 100% 계산되는 값이다. 사람이 정할 것이 없는데도 예전에는 「낡으면 사람이 알아채고 고치는」
 * 자리였고, 그래서 조용히 벌어졌다 — 2026-08-10 에 index.md 표가 16곳 밀려 있었고 그 파일은
 * 봇 시스템 프롬프트에 통째로 실린다.
 *
 * 계산은 여기 두지 않는다. `.claude/skills/slack-sync/scripts/sync_index.py` 한 곳에 있고
 * 이 파일은 그것을 부르기만 한다. 사람이 스킬로 돌릴 때와 VM 이 자동으로 돌 때가 **같은
 * 코드**를 써야 하기 때문이다 — 계산이 두 곳에 있으면 갈리고, 실제로 갈렸다(사람은 봇이 보는
 * 블록 수를, 코드는 사람 발언 수를 셌다).
 *
 * **관문(runGate)에 올리지 않는다.** 관문 실패는 `git reset --hard` 로 그날 대화·문서·로그
 * 반영을 통째로 되돌린다. 파생값 하나 때문에 치를 값이 아니다. 대신 스크립트가 쓰기 직전에
 * 불변식(줄 수 · 행 이름과 순서 · 🔒 행)을 확인하고 하나라도 깨지면 아무것도 쓰지 않는다.
 * 그래서 이 단계는 「고치거나, 손대지 않거나」 둘 중 하나만 한다.
 */
import { SKILL_SCRIPTS, runScript } from './util.js';

/**
 * @param {{dry?: boolean}} opts
 * @returns {Promise<{changed:Array, unresolved:Array, failed:string|null}>}
 */
export async function runDerive({ dry = false } = {}) {
  const args = ['--all', '--json', ...(dry ? ['--dry-run'] : [])];
  const r = await runScript(SKILL_SCRIPTS.syncIndex, args);

  // 스크립트는 사람이 읽는 줄들을 먼저 찍고 **마지막 줄에 JSON 한 줄**을 낸다.
  // stdout 을 정규식으로 긁지 않으려고 정한 계약이다.
  const last = (r.stdout || '').trim().split('\n').pop() || '';
  let parsed = null;
  try {
    parsed = JSON.parse(last);
  } catch {
    /* 아래에서 실패로 다룬다 */
  }

  if (!parsed) {
    return {
      changed: [],
      unresolved: [],
      failed: (r.stderr || r.stdout || '결과를 읽지 못했습니다').trim().slice(0, 300),
    };
  }
  if (parsed.ok === false) {
    return {
      changed: parsed.changed || [],
      unresolved: parsed.unresolved || [],
      failed: `쓰기 불변식 위반 — ${parsed.invariant}`,
    };
  }
  return {
    changed: parsed.changed || [],
    unresolved: parsed.unresolved || [],
    failed: r.ok ? null : (r.stderr || '').trim().slice(0, 300) || null,
  };
}
