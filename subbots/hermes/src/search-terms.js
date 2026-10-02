/**
 * 검색 낱말 규칙 — 대화(archive.js)와 문서(documents.js)가 함께 쓴다.
 *
 * 쪼개는 규칙을 두 곳에 따로 두면 한쪽만 고쳐져 조용히 갈린다. 여기 한 자리에 둔다.
 * 점검은 scripts/check-search-terms.js.
 */
import { truncMarker } from './config.js';

/** 낱말이 하나뿐이면 "일부만 맞음" 이라는 것이 없다. */
export const PARTIAL_MIN_TERMS = 2;

/** 질의 문자열 → 소문자 낱말 목록. 기존 두 파일의 쪼개기와 같은 동작이어야 한다. */
export function splitTerms(query) {
  return String(query || '')
    .split(/\s+/)
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean);
}

/** 이미 소문자로 만든 본문에 terms 중 몇 개가 들어 있나. */
export function scoreTerms(haystackLower, terms) {
  let n = 0;
  for (const t of terms) if (haystackLower.includes(t)) n += 1;
  return n;
}

/**
 * 이미 소문자로 만든 본문에 terms 가 **몇 번씩** 나오는지 합한 값 — `scoreTerms`(0/1)와
 * 달리 출현 횟수를 센다. `companyWideCards`(documents.js)의 관련성 신호가 이것을 쓴다.
 * 겹치는 위치를 세는 방법은 `clipPartial` 의 occurrences 수집과 같다 (indexOf 반복).
 */
export function termHits(haystackLower, terms) {
  let n = 0;
  for (const t of terms) {
    let idx = haystackLower.indexOf(t);
    while (idx !== -1) {
      n += 1;
      idx = haystackLower.indexOf(t, idx + t.length);
    }
  }
  return n;
}

/** 앞 고정 clip — clipPartial 이 헤더 줄 자체가 상한급일 때 후퇴하는 자리에서 쓴다. */
function clip(text, max) {
  return text.length > max ? `${text.slice(0, max)}\n${truncMarker()}` : text;
}

/**
 * 부분 일치 발췌용 clip — 앞 고정이 아니라 **매칭된 질의 낱말을 가장 많이 담는 창**을 고른다.
 *
 * 왜 있나: 회차 본문이 표 여러 개로 되어 있으면 매칭 낱말이 안 걸린 표가 앞쪽에 오고
 * 정작 걸린 표는 뒤에 온다. 앞 고정 clip(max)은 그 뒤쪽 표를 통째로 잘라내 모델에게
 * 아예 안 보여준다(2026-09 실측 — `입주일일업무일지` PF대출 표, task-2b-report). 확정
 * 히트(clip + DOC_HIT_MAX_CHARS)는 안 건드린다 — 그쪽은 낱말이 전부 있어 4,000자 안에
 * 대개 다 들어오고, 여기서 상한을 올리는 것도 아니다(그러면 헛도는 질의가 비싸진다,
 * 30행 주석 참조). 상한은 그대로 두고 **어디를 자를지만** 바꾼다.
 *
 * 첫 줄(회차 헤더 `**날짜 · 파일명**` 또는 `DOC_PREAMBLE_MARK`)은 항상 보존한다 —
 * `render`(claude.js)가 프로젝트/문서 제목은 별도 줄로 붙이지만 **회차별 날짜·원본
 * 파일명은 이 첫 줄에만 있다.** 창을 본문에서 고르다 그 줄까지 밀어내면 그 정보가
 * 사라진다.
 *
 * 창 고르는 법은 결정적(deterministic)이다 — 매칭된 낱말들의 **모든** 출현 위치를 모으고,
 * 상한 길이의 창을 그 위치들에 앞맞춤해 슬라이드하며 **서로 다른 매칭 낱말을 가장 많이
 * 담는 시작점**을 고른다(동률이면 앞쪽). 창 경계를 어느 한 낱말 위치에 맞추는 것으로
 * 후보를 좁혀도 최적해를 놓치지 않는다 — 창을 그 낱말이 빠지기 직전까지 밀 수 있기
 * 때문이다.
 *
 * 잘려나간 자리 표시는 기존 clip 과 같은 문구를 쓴다(앞이 잘렸으면 창 앞에, 뒤가
 * 잘렸으면 창 뒤에) — 새 형식을 발명하지 않는다.
 */
export function clipPartial(text, max, terms) {
  if (text.length <= max) return text;

  const nl = text.indexOf('\n');
  const header = nl === -1 ? '' : text.slice(0, nl + 1); // 회차 헤더 줄 + 개행. 항상 보존.
  const rest = nl === -1 ? text : text.slice(nl + 1);
  const budget = max - header.length;
  if (budget <= 0) return clip(text, max); // 헤더 줄 자체가 상한급이면 기존 방식으로 후퇴.

  const hay = rest.toLowerCase();
  const occurrences = [];
  for (const t of terms) {
    let idx = hay.indexOf(t);
    while (idx !== -1) {
      occurrences.push({ term: t, pos: idx });
      idx = hay.indexOf(t, idx + 1);
    }
  }

  const windowLen = Math.min(budget, rest.length);
  const maxStart = Math.max(0, rest.length - windowLen);
  let bestStart = 0;
  let bestCount = -1;
  const candidates = new Set([0, ...occurrences.map((o) => Math.min(o.pos, maxStart))]);
  for (const start of candidates) {
    const end = start + windowLen;
    const seen = new Set();
    for (const o of occurrences) if (o.pos >= start && o.pos < end) seen.add(o.term);
    if (seen.size > bestCount || (seen.size === bestCount && start < bestStart)) {
      bestStart = start;
      bestCount = seen.size;
    }
  }

  const end = bestStart + windowLen;
  let windowed = rest.slice(bestStart, end);
  if (bestStart > 0) windowed = `${truncMarker()}\n${windowed}`;
  if (end < rest.length) windowed = `${windowed}\n${truncMarker()}`;
  return header + windowed;
}

/** 자리 이름으로 볼 낱말의 최소 길이. 한 글자는 조사·접두사라 아무 데나 걸린다. */
const PLACE_MIN_TERM = 2;

/**
 * 질의 안에 **자리 이름**(채널·사업장)이 들어 있으면 그 하나를 돌려준다.
 *
 * 왜 있나: 질의에 자리 이름이 있어도 검색은 그것을 낱말 하나로만 다룬다. 그러면 상한을
 * 자리들이 나눠 갖는 `pickSpread` 가 지목한 자리에 한 칸만 주고 나머지를 다른 자리로
 * 채운다 — 실측(2026-09-03, 팀 로그의 실제 질의 78개)으로 돌아온 히트 중 지목한 자리
 * 것이 대화 22%·문서 13%였다. 다른 자리 자료가 답에 실리면 **원문 그대로인데 답이 틀린다.**
 *
 * 규칙 셋:
 *  ① 낱말 하나하나와 **붙어 있는 두 낱말**을 해석기에 넣는다. 사람은 이름을 띄어 쓰거나
 *     줄여 쓰고, 그 풀이는 이미 해석기가 한다 (여기서 다시 만들지 않는다)
 *  ② 서로 다른 자리가 **둘 이상** 나오면 안 좁힌다 — 두 자리를 견주는 질문이라 한쪽으로
 *     좁히면 나머지가 통째로 사라진다
 *  ③ 한 글자 낱말은 안 본다
 *
 * **목록은 부르는 쪽이 넘긴다.** 여기서 전체 목록을 읽으면 볼 수 없는 비공개 채널로도
 * 좁혀져 그 자리가 있다는 것이 드러난다 — 부르는 쪽이 「볼 수 있는 것」만 넘겨야 한다.
 *
 * @param query 검색 질의
 * @param names 후보 이름 목록 (**볼 수 있는 것만**)
 * @param resolve (input, names) => {ok:true,name} | {ok:false} — 후보가 둘 이상이면 스스로 실패한다
 * @returns 자리 이름 하나, 또는 없거나 애매하면 null
 */
export function detectPlace(query, names, resolve) {
  const all = splitTerms(query);
  /* **짝은 거르기 전 목록에서 만든다.** 한 글자를 먼저 빼 버리면 「사업장 가」처럼 한 글자로
   * 끝나는 이름이 짝으로도 안 만들어진다 — 사람이 이름을 띄어 쓰는 바로 그 모양이다.
   * 한 글자를 빼는 것은 **혼자 쓸 때**뿐이다. */
  const tries = all.filter((x) => x.length >= PLACE_MIN_TERM);
  for (let i = 0; i + 1 < all.length; i += 1) tries.push(`${all[i]} ${all[i + 1]}`);
  const found = new Set();
  for (const s of tries) {
    const r = resolve(s, names);
    if (r.ok) found.add(r.name);
  }
  return found.size === 1 ? [...found][0] : null;
}
