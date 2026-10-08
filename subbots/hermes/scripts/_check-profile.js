/** Runtime-specific check planning. This module must not import application config. */

export const STANDALONE_PROFILE = 'standalone';
export const PF_ARCHIVER_PROFILE = 'pf-archiver';

const PF_ARCHIVER_OPERATIONAL = new Set([
  'scripts/check-shared-rules.js',
  'scripts/check-board-docs.js',
  'scripts/check-attachment-marks.js',
  'scripts/check-archiver-privacy.js',
]);

export function runtimeCheckProfile(env = process.env) {
  return String(env.HERMES_MODE || '').trim().toLowerCase() === PF_ARCHIVER_PROFILE
    ? PF_ARCHIVER_PROFILE
    : STANDALONE_PROFILE;
}

function exclusionReason(check) {
  if (check.file === 'scripts/check-archiver-reader.js') {
    return '실제 수집기로 만드는 격리 fixture가 필요해 저장소 회귀시험에서 실행';
  }
  if (check.runtime === 'python') {
    return 'legacy writer·skill 개발 시험이며 pf-archiver에서는 원문 쓰기가 의도적으로 차단됨';
  }
  if (check.runtime === 'bash') {
    return 'standalone 설치 저장소를 바꾸는 배포 개발 시험이며 실행 중 readiness 대상이 아님';
  }
  return 'standalone Git/slack-export 또는 합성 fixture 계약이며 pf-archiver 실자료에 실행하지 않음';
}

/**
 * Keep the old exhaustive archive suite for standalone development. On a running
 * pf-archiver instance, execute only checks whose inputs are the active Archiver
 * source. Every exclusion is returned with a reason so it cannot look like a pass.
 */
export function planChecks(checks, mode, profile) {
  const candidates = checks.filter((check) => mode === 'all' || check.mode === mode);
  if (profile !== PF_ARCHIVER_PROFILE || !['all', 'archive'].includes(mode)) {
    return { selected: candidates, excluded: [] };
  }

  const selected = [];
  const excluded = [];
  for (const check of candidates) {
    if (check.mode !== 'archive' || PF_ARCHIVER_OPERATIONAL.has(check.file)) {
      selected.push(check);
    } else {
      excluded.push({ check, reason: exclusionReason(check) });
    }
  }
  return { selected, excluded };
}

