/**
 * 전사 종합 카드가 **조용히 반쯤 죽는** 설정 상태를 화면에 내는 문구.
 *
 * `check-setup.js` 가 부르고, `check-doc-card-kill-switch.js` [3/3] 이 **동작으로** 잰다.
 * 함수로 뺀 이유가 그것이다 — 문구가 `check-setup.js` 본문 안에 박혀 있으면 자동으로
 * 부를 방법이 없어 **수동 확인만** 남고(2026-09-11 이월 Minor), 누가 지워도 아무 소리가
 * 안 난다.
 */

/**
 * 안전망(`outsideWhenNarrowedMaxHits`)을 0 이하로 두면 — 그 자체는 정당한 되돌리기
 * 스위치다 — `claude.js` 의 autoNarrow 폴백(봇이 자리를 안 짚어 검색이 스스로 추론한
 * 경로)이 전사 종합 카드를 버린다. 봇이 사업장을 직접 짚은 검색에는 영향이 없다
 * (실측 70건 중 자동 추론 22건, 그중 카드가 계산된 것 9건 — 2026-09-11 성능·비용 검토 ③).
 *
 * **실패로 만들지 않는다**(끄는 것은 정당한 선택이다) — `[못잼]` 표시로 화면에만 낸다.
 * 목록이 비어 카드 기능 자체가 꺼져 있으면 이 값은 무의미하므로 아무 말도 안 한다.
 *
 * @param {{companyWideCount:number, outsideMax:number}} state
 * @returns {string|null} 낼 줄, 없으면 null
 */
export function safetyNetOffNote({ companyWideCount, outsideMax }) {
  // 조건을 `outsideMax > 0` 의 부정으로 적는다 — 옛 인라인 판(`outsideMax <= 0`)과
  // 정상 숫자에서는 완전히 같고, `NaN`·글자 같은 **망가진 설정값**에서만 갈린다.
  // 그때 옛 판은 **조용**했고 이 판은 **말한다.** 망가진 값은 `?? 0` 을 안 거치므로
  // 안전망이 꺼진 것과 같은 결과가 되는데, 그것을 잠자코 넘기는 쪽이 이 저장소의
  // 방침(fail-loud)과 반대다 (2026-09-11 회의적 검증이 이 차이를 짚었다 — 일부러 둔다).
  if (!companyWideCount || outsideMax > 0) return null;
  return `[못잼]  - 안전망(outsideWhenNarrowedMaxHits = ${outsideMax})이 꺼져 있어, `
    + '봇이 사업장을 안 짚고 검색이 스스로 자리를 추론한 경로(autoNarrow 폴백)에서 '
    + '전사 종합 카드가 버려집니다 — 봇이 사업장을 직접 짚은 검색에는 영향이 없습니다.';
}

/**
 * 위 문구를 **화면에 낸다.** 부르는 쪽은 결과를 안 받는다.
 *
 * 왜 찍는 일까지 여기 있나(2026-09-11 회의적 검증): 문구 만들기만 함수로 빼고 찍는 일을
 * 부르는 쪽에 두면, **부르기는 하고 찍지 않는** 모양(`const w = safetyNetOffNote(…);`
 * 만 남기기 · `if (false && w) console.log(w)` · 아무 데도 안 가는 곳으로 보내기)이
 * 전부 검사를 통과했다 — 화면엔 아무것도 안 뜨는데 초록이었다. 찍는 일을 안으로 들이면
 * 그 갈래가 아예 없어지고, 검사는 **가짜 기록계를 넣어 무엇이 찍혔는지**를 직접 잰다.
 *
 * @param {{companyWideCount:number, outsideMax:number}} state
 * @param {(line:string)=>void} [log] 시험이 갈아 끼우는 자리. 기본은 화면.
 * @returns {boolean} 낸 것이 있으면 true
 */
export function reportCardWarnings(state, log = console.log) {
  const note = safetyNetOffNote(state);
  if (!note) return false;
  log(note);
  return true;
}
