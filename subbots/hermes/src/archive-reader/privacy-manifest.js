/**
 * 채널 공개 여부 manifest — **읽고 검증한다. 판정은 여기 하나뿐이다.**
 *
 * 쓰는 쪽이 둘이다: 전환 관문(`scripts/check-archiver-privacy.js`)과 **실행 중 ACL**
 * (`config.js` 의 `isPrivateChannel`·`canSee`). 검증을 두 벌로 두면 관문은 통과시키고
 * 런타임은 닫는(또는 그 반대) 조합이 생기고, 그 상태는 「전환했는데 봇이 아무것도 못
 * 본다」 로만 드러난다.
 *
 * 만드는 쪽은 TYBot 의 `tybot.archive.privacy_manifest` 다. Hermes 는 **파일만 읽는다** —
 * DB 자격증명을 PF 쪽에 두면 운영 DB 로 가는 길이 하나 더 생긴다.
 *
 * ## 쓸 수 없으면 「공개」 가 아니라 「모른다」 다
 *
 * 돌려주는 값은 `{ok}` 로 갈린다. `ok:false` 를 받은 쪽은 **닫아야 한다** — 공개로
 * 후퇴하면 manifest 가 사라진 날 전 채널이 열린다. 개명 지도가 죽었을 때와 같은 자리,
 * 같은 모양이다(`config.js` 의 `map-dead` 분기).
 *
 * ## 무엇을 거부하나
 *
 * | 갈래 | 왜 |
 * |---|---|
 * | 경로 없음·못 읽음·JSON 아님 | 「없음」 을 「비공개가 없음」 으로 읽으면 안 된다 |
 * | 형식·워크스페이스 불일치 | 짐작해 읽으면 공개 여부를 잘못 판정한다 |
 * | `generated_at` 없음·파싱 실패 | **얼마나 낡았는지 모른다.** 「방금」 과 「석 달 전」 이 같아진다 |
 * | `generated_at` 이 미래 | 못 재는 값으로 기간을 판정하면 **영원히 신선한** manifest 가 된다 |
 * | 허용 기간 초과 | 그 사이 공개 채널이 비공개로 바뀌었을 수 있다 |
 * | 빈 `channel_id` | 대조에서 조용히 빠진다. 빠진 줄을 모르면 전수 대조가 아니다 |
 * | 중복 `channel_id` | 뒤엣것이 앞엣것을 덮는다. `true` 뒤에 `false` 면 비공개가 공개가 된다 |
 * | `is_private` 가 불리언이 아님 | `"false"` 를 참으로 읽거나 그 반대로 읽는 쪽이 둘 다 조용히 틀린다 |
 */
import fs from 'node:fs';

/** TYBot 쪽 `privacy_manifest.SCHEMA` 와 같아야 한다. */
export const SCHEMA = 'channel-privacy-manifest/v1';

/** 기본 허용 기간. 하루 한 번 내보내는 운영에서 한 회차를 걸러도 살아남는 길이다. */
export const DEFAULT_MAX_AGE_HOURS = 26;

/**
 * 미래로 봐 주는 폭. 시계는 조금씩 어긋나고, 1초 차이로 빨개지는 관문은 꺼진다.
 * 그 이상은 **손으로 적었거나 시계가 크게 틀린 것**이라 거부한다.
 */
export const FUTURE_SKEW_MS = 2 * 60 * 1000;

function bad(code, message) {
  return { ok: false, code, message };
}

/**
 * manifest 한 장을 읽어 판정한다.
 *
 * @param {object} opts
 * @param {string} opts.path            파일 경로. 비면 거부한다
 * @param {string} opts.workspace       정본의 workspace 키. 다르면 거부한다
 * @param {number} [opts.maxAgeHours]   허용 기간(시간)
 * @param {number} [opts.now]           지금(ms). 시험이 고정한다
 * @returns {{ok: true, privateIds: Set<string>, rows: Map<string, {name: string, isPrivate: boolean}>,
 *            generatedAt: string, ageMs: number} | {ok: false, code: string, message: string}}
 */
export function loadPrivacyManifest({ path: file, workspace, maxAgeHours, now }) {
  if (!file) {
    return bad('no_path',
      '공개 여부 manifest 경로가 없습니다.\n'
      + '  config.json 의 archiver.privacyManifest 또는 HERMES_PRIVACY_MANIFEST 를 적으세요.\n'
      + '  만드는 쪽(사내): python -m tybot.archive.privacy_manifest --workspace <ws> --out <경로>');
  }
  let payload;
  try {
    payload = JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (e) {
    return bad('unreadable', `manifest 를 읽지 못했습니다: ${file}\n  ${e.message}`);
  }
  if (!payload || typeof payload !== 'object') {
    return bad('unreadable', `manifest 가 객체가 아닙니다: ${file}`);
  }
  if (payload.schema !== SCHEMA) {
    return bad('schema',
      `manifest 형식이 다릅니다: ${JSON.stringify(payload.schema)} (기대: ${SCHEMA})\n`
      + '  모르는 형식을 짐작해 읽으면 공개 여부를 잘못 판정합니다.');
  }
  if (payload.workspace !== workspace) {
    return bad('workspace',
      `manifest 의 workspace 가 다릅니다: ${payload.workspace} (정본: ${workspace})\n`
      + '  남의 워크스페이스 manifest 로 대조하면 이름이 겹치는 채널만 우연히 맞습니다.');
  }

  // --- 생성 시각 ---------------------------------------------------------
  const stamp = payload.generated_at;
  if (typeof stamp !== 'string' || !stamp.trim()) {
    return bad('generated_at_missing',
      'manifest 에 generated_at 이 없습니다 — 얼마나 낡았는지 알 수 없습니다.\n'
      + '  「방금 뽑은 것」 과 「석 달 전 것」 이 구별되지 않습니다.');
  }
  const at = Date.parse(stamp);
  if (!Number.isFinite(at)) {
    return bad('generated_at_unparsable',
      `manifest 의 generated_at 을 읽지 못했습니다: ${JSON.stringify(stamp)}`);
  }
  const current = Number.isFinite(now) ? now : Date.now();
  if (at - current > FUTURE_SKEW_MS) {
    return bad('generated_at_future',
      `manifest 의 generated_at 이 미래입니다: ${stamp}\n`
      + '  시계가 어긋났거나 손으로 적은 값입니다. 못 재는 값으로 기간을 판정하면\n'
      + '  영원히 신선한 manifest 가 됩니다.');
  }
  const limitHours = Number.isFinite(maxAgeHours) && maxAgeHours > 0
    ? maxAgeHours : DEFAULT_MAX_AGE_HOURS;
  const ageMs = current - at;
  if (ageMs > limitHours * 3600 * 1000) {
    const hours = (ageMs / 3600 / 1000).toFixed(1);
    return bad('stale',
      `manifest 가 너무 오래됐습니다: ${stamp} (${hours}시간 전 · 허용 ${limitHours}시간)\n`
      + '  그 사이 공개 채널이 비공개로 바뀌었을 수 있습니다. 다시 내보내세요.');
  }

  // --- 채널 행 -----------------------------------------------------------
  if (!Array.isArray(payload.channels)) {
    return bad('rows', 'manifest 에 channels 배열이 없습니다.');
  }
  const rows = new Map();
  const duplicates = new Set();
  for (const row of payload.channels) {
    const id = typeof row?.channel_id === 'string' ? row.channel_id.trim() : '';
    if (!id) {
      return bad('empty_channel_id',
        'manifest 에 channel_id 가 빈 행이 있습니다.\n'
        + '  키가 없는 행은 대조에서 조용히 빠집니다 — 빠진 줄을 모르면 전수 대조가 아닙니다.');
    }
    if (typeof row.is_private !== 'boolean') {
      return bad('is_private',
        `manifest 의 is_private 가 불리언이 아닙니다: ${id} = ${JSON.stringify(row.is_private)}`);
    }
    // **중복은 전부 모은다.** 하나만 적으면 사람이 그것만 고치고 다시 돌린다.
    if (rows.has(id)) duplicates.add(id);
    rows.set(id, { name: String(row.channel_name || ''), isPrivate: row.is_private });
  }
  if (duplicates.size) {
    return bad('duplicate_channel_id',
      `manifest 에 중복된 channel_id 가 ${duplicates.size}개 있습니다: ${[...duplicates].sort().join(', ')}\n`
      + '  뒤엣것이 앞엣것을 덮습니다 — is_private: true 뒤에 false 가 오면 비공개 채널이\n'
      + '  공개로 판정됩니다. 어느 쪽을 고르든 틀릴 수 있어 고르지 않고 거부합니다.');
  }

  const privateIds = new Set();
  for (const [id, row] of rows) if (row.isPrivate) privateIds.add(id);
  return { ok: true, rows, privateIds, generatedAt: stamp, ageMs };
}
