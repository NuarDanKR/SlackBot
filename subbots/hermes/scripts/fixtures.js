/**
 * 실제 아카이브에 던지는 **질의 뭉치** — 자료 저장소에서 읽는다.
 *
 * ── 왜 코드 저장소에 못 두나 ──
 *
 * `check-outside-hits.js`·`check-attachment-marks.js` 는 순수 함수 시험이 아니라
 * **실물 아카이브를 상대로** 돈다. 「이 질의를 이 사업장으로 좁히면 확정 히트가 0건이고
 * 부분 일치만 9건」 같은 사실을 근거로 안전망이 켜지는지를 재기 때문에, 질의와 사업장
 * 이름이 **그 팀의 실물과 맞아야** 한다.
 *
 * 그런데 2026-08-31 에 코드 저장소를 팀끼리 나눠 쓰기로 하면서 거기에 사업장·비공개
 * 채널 이름을 둘 수 없게 됐다. 이름을 가짜로 바꾸면 검사가 **에러 없이 헛돈다** —
 * 없는 사업장으로 좁히니 아무것도 안 나오고, 그것을 「샌 것이 없다」로 읽는다.
 *
 * 그래서 질의는 자료 저장소에 두고 코드는 그 파일을 가리킨다.
 *
 * ── 없으면 건너뛴다 ──
 *
 * 새 팀에는 이 파일이 없다. 그때는 **못 잰다고 말하고 건너뛴다** — 남의 팀 질의로는
 * 잴 수가 없고, 그렇다고 영원히 ✗ 를 띄우면 그 팀은 매일 빨간 줄을 보며 무시하는 법을
 * 배운다. 건너뛸 때는 **무엇을 만들면 되는지**를 함께 적는다. 조용히 통과시키지 않는다.
 *
 * ── 한 자리에서 읽는 이유 ──
 *
 * 두 검사가 각자 읽으면 파일 이름·모양이 갈리고, 갈리면 한쪽만 도는데 아무도 모른다.
 * 이 저장소가 여러 번 겪은 모양이라 (`check-shared-rules.js` 가 그 감시다) 처음부터
 * 한 자리에 둔다.
 */
import fs from 'node:fs';
import path from 'node:path';
import { DATA_ROOT } from '../src/config.js';

/** 자료 저장소 안의 자리. 사람이 손으로 만든다. */
export const FIXTURES_FILE = path.join(DATA_ROOT, 'check-fixtures.json');

/* 왜 건너뛰는지를 절 이름별로 적어 둔다 — `skipNote()` 가 이걸 읽어 화면에 낸다.
 * 「파일이 없다」와 「열쇠가 모자라다」는 사람이 할 일이 다르다: 앞은 만들어야 하고
 * 뒤는 이미 만든 파일에 줄을 더해야 한다. 둘을 같은 문구로 내면 새 팀이 멀쩡한 파일을
 * 다시 만들려 든다. */
const skipReason = new Map();

/**
 * 질의 뭉치를 읽는다.
 *
 * @param {string} section `check-fixtures.json` 안의 절 이름 (예: 'outsideHits')
 * @param {string[]} [required] 그 검사가 **꼭 있어야 하는** 열쇠들.
 *   하나라도 없으면 null 을 돌려준다 (2026-09-01).
 *   전에는 없는 열쇠를 그대로 넘겨서 `FX.alias` 가 `undefined` 인 채 검색에 들어갔고,
 *   사람이 보는 첫 화면이 **스택 트레이스**였다. 새 팀의 파일이 우리 코드보다 낡으면
 *   나는 일인데, 그때 필요한 것은 「어느 열쇠를 더하면 되는가」 한 줄이다.
 * @returns {object|null} 그 절. 파일이 없거나 절이 없거나 열쇠가 모자라면 null.
 */
export function loadFixtures(section, required = []) {
  skipReason.delete(section);
  let raw;
  try {
    raw = fs.readFileSync(FIXTURES_FILE, 'utf8');
  } catch {
    skipReason.set(section, { kind: 'nofile' });
    return null;
  }
  let all;
  try {
    all = JSON.parse(raw);
  } catch (err) {
    // 있는데 깨진 것은 없는 것과 다르다 — 사람이 고칠 수 있게 그대로 알린다.
    throw new Error(`${FIXTURES_FILE} 를 읽지 못했습니다: ${err.message}`);
  }
  const got = all[section];
  if (!got || typeof got !== 'object') {
    skipReason.set(section, { kind: 'nosection' });
    return null;
  }
  // 빈 값도 없는 것으로 본다. `""` 나 `[]` 를 그대로 넘기면 검색이 0건을 내고,
  // 그 0건을 「안 샜다」로 읽는 것이 이 파일 머리말이 말하는 바로 그 사고다.
  const missing = required.filter((k) => {
    const v = got[k];
    if (v === undefined || v === null) return true;
    if (typeof v === 'string') return !v.trim();
    if (Array.isArray(v)) return v.length === 0;
    return false;
  });
  if (missing.length) {
    skipReason.set(section, { kind: 'keys', missing });
    return null;
  }
  return got;
}

/**
 * 건너뛸 때 화면에 낼 말. **무엇을 만들면 되는지까지 적는다** —
 * 「건너뜀」만 있으면 다음 사람이 고장으로 읽거나, 고쳐야 할 것으로 안 읽는다.
 */
export function skipNote(section) {
  const why = skipReason.get(section);
  if (why?.kind === 'keys') {
    return [
      `  - 건너뜀: 질의 뭉치에 열쇠가 모자라 **재지 못했습니다** (${section})`,
      `      없거나 비어 있는 열쇠 ${why.missing.length}개: ${why.missing.join(' · ')}`,
      `      고칠 자리: ${FIXTURES_FILE} 의 "${section}" 절`,
      `      모양은 코드 저장소의 check-fixtures.example.json 를 보세요.`,
    ].join('\n');
  }
  return [
    `  - 건너뜀: 질의 뭉치가 없어 **재지 못했습니다** (${section})`,
    `      이 검사는 실물 아카이브를 상대로 돌아서 팀마다 다른 질의가 필요합니다.`,
    `      만들 자리: ${FIXTURES_FILE}`,
    `      모양은 코드 저장소의 check-fixtures.example.json 를 보세요.`,
  ].join('\n');
}
